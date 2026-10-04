"""
create_cas_tables.py -- storage for real transaction history.
---------------------------------------------------------------------------
Run once:
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_cas_tables.py

    --check   report only. Writes nothing. Shows what exists already and
              how many rows are in each table.
    --drop    remove all four tables and their data. Asks for confirmation
              by name. There is no other way to undo this script.

WHY THIS EXISTS
    portfolio_holding stores (portfolio_id, scheme_code, amount). That is
    enough to say what a portfolio IS, and every look-through figure on
    the report -- overlap, size bands, sectors, largest positions -- is a
    function of current weights, so those are honest today.

    It is not enough to say what the investor EARNED. Without the dates
    the money went in, there is no XIRR, and a five-year CAGR applied to
    today's balance flatters a SIP that has been running for eighteen
    months. The portfolio report says so in as many words. These tables
    are what let it stop saying so.

TWO SOURCES, ONE SHAPE
    A consolidated account statement can arrive two ways: the investor
    downloads the detailed CAS PDF from CAMS or KFintech and uploads it,
    or MF Central delivers it over an API after the investor consents
    with an OTP. They differ entirely in how they are obtained and not at
    all in what they contain -- folios, schemes, and a dated list of
    transactions.

    So the schema is written against the CONTENT, and cas_import.source
    records which door it came through. Adding the MF Central route later
    is a second loader writing the same rows, not a migration.

WHY FOUR TABLES AND NOT ONE

    cas_import      one row per statement ingested. An audit log: who
                    uploaded what, when, covering which period, and what
                    the parser complained about. Without it a wrong
                    import cannot be identified, let alone undone.

    cas_folio       one row per folio-and-scheme, PER CLIENT -- not per
                    import. This is the distinction that matters. A CAS
                    covers a period, so a second statement six months
                    later is not a correction of the first, it is a
                    continuation. Keying the folio to the client rather
                    than to the upload means the second statement adds
                    its new transactions and leaves the old ones alone.
                    Keying it to the import would give you the same folio
                    twice and a portfolio worth double.

    cas_txn         the cashflows. Unique on the folio row plus date,
                    type, amount and units, so re-uploading a statement
                    you already loaded -- which will happen, because the
                    periods overlap -- inserts nothing and reports
                    nothing wrong. first_import_id says which upload
                    brought each row in.

    cas_valuation   what the statement said the holding was worth on its
                    closing date. Kept apart from cas_folio because it is
                    a point-in-time observation, and a folio accumulates
                    several of them as statements arrive. It is also the
                    only honest way to check our own arithmetic: units
                    and NAV from the RTA against units and NAV we derived
                    from the transactions.

ON HOLDING OTHER PEOPLE'S FINANCIAL DATA
    A detailed CAS contains the investor's name, email address, postal
    address, mobile number and PAN. None of that is stored here, and the
    omission is deliberate rather than an oversight to be corrected later:

      - PAN, email, address and mobile are never written. The loader
        reads investor_info only to show you the name on screen so you
        can confirm the statement belongs to the client you are importing
        for, and then discards it.

      - investor_name is stored on cas_import ALONE, not on the folio or
        transaction rows, because its only job is that confirmation and
        an audit trail of what was uploaded.

      - client_id is how a statement is attached to a person. The client
        table already holds who they are, with a consent_on column. This
        schema does not duplicate identity; it points at it.

    A folio number is stored, because reconciling two statements without
    it is guesswork. It is the minimum that makes the feature work.

ON THE ARN
    cas_folio.advisor is the ARN the folio sits under, exactly as the
    statement prints it, and is_own_arn marks whether it matches the
    distributor's own. That one boolean is the whole "external portfolio"
    idea: folios under your ARN are your book, everything else is money
    the client holds elsewhere. It is computed at load time against
    distributor.arn rather than hard-coded.

WHAT THIS SCRIPT DOES NOT DO
    It does not touch portfolio_holding and it does not change any page.
    Loading a statement leaves the existing report exactly as it is. The
    XIRR work sits on top of these tables and is a separate job.
"""

import os
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

TABLES = ("cas_import", "cas_folio", "cas_txn", "cas_valuation")

DDL = """
-- ------------------------------------------------------------------
-- One row per statement ingested.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cas_import (
    import_id      bigserial PRIMARY KEY,
    owner_user_id  bigint NOT NULL,

    -- Whose statement this is. NULL means the uploader's own money,
    -- which is the distributor looking at their own portfolio.
    client_id      bigint REFERENCES client(client_id) ON DELETE CASCADE,

    -- 'cas_pdf' today. 'mfcentral' when that route opens. The column
    -- exists now so the second loader needs no migration.
    source         text NOT NULL CHECK (source IN ('cas_pdf', 'mfcentral')),

    -- As reported by the parser, not as typed by anyone: CAMS,
    -- KFINTECH, CDSL, NSDL; DETAILED or SUMMARY.
    rta            text,
    cas_type       text,

    -- The window the statement covers. A SUMMARY CAS has no
    -- transactions and therefore no useful period; both stay NULL.
    period_from    date,
    period_to      date,

    -- The name printed on the statement, kept so you can see later what
    -- was uploaded and for whom. No PAN, no email, no address, no
    -- mobile -- see the note at the top of this file.
    investor_name  text,

    -- What the parser said it could not read. Stored rather than
    -- printed and forgotten: a fund missing from a report six months
    -- from now is answered by looking here.
    parse_warnings text[],

    folio_count    int NOT NULL DEFAULT 0,
    scheme_count   int NOT NULL DEFAULT 0,
    txn_count      int NOT NULL DEFAULT 0,
    txn_inserted   int NOT NULL DEFAULT 0,

    file_name      text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cas_import_owner_idx
    ON cas_import (owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS cas_import_client_idx
    ON cas_import (client_id) WHERE client_id IS NOT NULL;

-- ------------------------------------------------------------------
-- One row per folio-and-scheme for a given client. Durable across
-- imports -- see the note at the top about why this is not keyed to
-- the upload.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cas_folio (
    folio_row_id   bigserial PRIMARY KEY,
    owner_user_id  bigint NOT NULL,
    client_id      bigint REFERENCES client(client_id) ON DELETE CASCADE,

    folio          text NOT NULL,
    amc            text,

    -- The scheme as the RTA names it, and every identifier it gave us.
    -- isin is the one that matters: mf_scheme.scheme_isin already maps
    -- plan ISINs onto canonical codes, so the fund resolves without any
    -- name matching.
    scheme_name    text NOT NULL,
    isin           text,
    amfi_code      text,
    rta_code       text,

    -- The canonical scheme code, resolved at load time. NULL means we
    -- could not place the fund -- a fund we do not carry, or an ISIN
    -- not in mf_scheme. Left NULL rather than guessed, and reported.
    scheme_code    text,

    -- The ARN printed against the folio, and whether it is the
    -- distributor's own. 'DIRECT' appears for direct plans.
    advisor        text,
    is_own_arn     boolean,

    first_import_id bigint REFERENCES cas_import(import_id) ON DELETE SET NULL,
    last_import_id  bigint REFERENCES cas_import(import_id) ON DELETE SET NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- A folio can hold more than one scheme, and the same scheme can sit in
-- two folios, so neither alone identifies the row. client_id is in the
-- key because two clients may legitimately be in the same family folio.
-- COALESCE, because NULL client_id (the uploader's own money) must
-- still collide with itself rather than insert a new row every time.
CREATE UNIQUE INDEX IF NOT EXISTS cas_folio_uniq
    ON cas_folio (owner_user_id, COALESCE(client_id, -1), folio,
                  COALESCE(isin, scheme_name));

CREATE INDEX IF NOT EXISTS cas_folio_scheme_idx
    ON cas_folio (scheme_code) WHERE scheme_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS cas_folio_client_idx
    ON cas_folio (owner_user_id, client_id);

-- ------------------------------------------------------------------
-- The cashflows. This is the table the whole exercise is for.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cas_txn (
    txn_id         bigserial PRIMARY KEY,
    folio_row_id   bigint NOT NULL
                   REFERENCES cas_folio(folio_row_id) ON DELETE CASCADE,

    txn_date       date NOT NULL,

    -- casparser's own vocabulary, stored verbatim: PURCHASE,
    -- PURCHASE_SIP, REDEMPTION, SWITCH_IN, SWITCH_OUT, DIVIDEND_PAYOUT,
    -- DIVIDEND_REINVEST, STAMP_DUTY_TAX, STT_TAX, TDS_TAX, SEGREGATION,
    -- GIFT_IN, GIFT_OUT, REVERSAL, MISC, UNKNOWN.
    --
    -- NOT collapsed to 'in' and 'out' on the way in. A switch is not a
    -- purchase, stamp duty is not an investment, and a reinvested
    -- dividend is not new money -- an XIRR that treats them alike is
    -- wrong, and the distinction cannot be recovered once discarded.
    txn_type       text NOT NULL,

    -- All nullable on purpose. Tax rows carry an amount and no units;
    -- a segregation carries units and no amount.
    amount         numeric(18,4),
    units          numeric(20,6),
    nav            numeric(18,6),
    balance        numeric(20,6),

    description    text,
    first_import_id bigint REFERENCES cas_import(import_id) ON DELETE SET NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Re-uploading an overlapping statement must be a no-op, not a
-- duplicate. Two genuinely identical transactions on the same day in
-- the same folio -- two SIPs of the same amount, which does happen --
-- would collapse to one here. That is the accepted cost: silently
-- doubling someone's holdings on a re-upload is far worse than losing
-- a rare duplicate, and the units balance in the statement is checked
-- against ours on load so the loss is detected rather than hidden.
CREATE UNIQUE INDEX IF NOT EXISTS cas_txn_uniq
    ON cas_txn (folio_row_id, txn_date, txn_type,
                COALESCE(amount, 0), COALESCE(units, 0));

CREATE INDEX IF NOT EXISTS cas_txn_folio_date_idx
    ON cas_txn (folio_row_id, txn_date);

-- ------------------------------------------------------------------
-- What the statement said the holding was worth at its close. One per
-- folio row per statement date.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cas_valuation (
    folio_row_id   bigint NOT NULL
                   REFERENCES cas_folio(folio_row_id) ON DELETE CASCADE,
    as_of          date NOT NULL,

    units          numeric(20,6),
    nav            numeric(18,6),
    value          numeric(18,4),
    cost           numeric(18,4),

    -- The closing balance we compute from the transactions we stored.
    -- Kept beside the RTA's own figure precisely so the two can be
    -- compared: a mismatch means we dropped or double-counted a row,
    -- and that must be visible rather than inferred from a wrong return
    -- six months later.
    units_derived  numeric(20,6),

    import_id      bigint REFERENCES cas_import(import_id) ON DELETE SET NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (folio_row_id, as_of)
);
"""


def connect():
    # FINCHAYA_DB is what every other script on this box reads. The two
    # names after it are fallbacks and nothing more -- an earlier version
    # of this file looked for DATABASE_URL FIRST, found the stray one the
    # system sets, connected happily to the wrong database and reported
    # that the client table did not exist. It was right; it was looking
    # in the wrong place. Hence the house variable first, and hence the
    # database name printed below on every run.
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env -- nothing to connect to.")
    return psycopg.connect(dsn)


def report(cur):
    # Which database, said out loud. Every confusing result this script
    # can produce starts with being connected to the wrong one.
    cur.execute("SELECT current_database(), current_user")
    db, who = cur.fetchone()
    print(f"\nconnected to '{db}' as '{who}'\n")
    print("table            exists   rows")
    print("-" * 34)
    for t in TABLES:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (t,))
        exists = cur.fetchone()[0]
        n = ""
        if exists:
            cur.execute(f"SELECT count(*) FROM {t}")
            n = cur.fetchone()[0]
        print(f"{t:<16} {'yes' if exists else 'no':<8} {n}")


def main():
    args = set(sys.argv[1:])

    with connect() as conn:
        with conn.cursor() as cur:
            if "--check" in args:
                report(cur)
                return

            if "--drop" in args:
                print("This deletes every imported statement and every")
                print("transaction stored from one. It cannot be undone.")
                if input("Type DROP to confirm: ").strip() != "DROP":
                    print("Nothing dropped.")
                    return
                cur.execute("DROP TABLE IF EXISTS cas_valuation, cas_txn, "
                            "cas_folio, cas_import CASCADE")
                conn.commit()
                print("Dropped.")
                return

            # client must exist first -- cas_import and cas_folio point at
            # it. Said plainly rather than as a foreign-key error.
            cur.execute("SELECT to_regclass('client') IS NOT NULL")
            if not cur.fetchone()[0]:
                cur.execute("SELECT current_database()")
                sys.exit(
                    f"Table 'client' does not exist in database "
                    f"'{cur.fetchone()[0]}'.\n\n"
                    "Two different causes, and they need different fixes:\n"
                    "  - Connected to the wrong database. Check FINCHAYA_DB\n"
                    "    in /opt/mfapi/.env is the one the site uses.\n"
                    "  - Right database, but the distributor tier was never\n"
                    "    set up. Run create_distributor_tables.py, which is\n"
                    "    safe to run again if it has been run before.")

            cur.execute(DDL)
            conn.commit()
            print("Created (or already present):\n")
            report(cur)
            print("\nNothing else changed. No page reads these tables yet.")


if __name__ == "__main__":
    main()
