"""
create_ipo_tables.py -- the tables behind the IPO section: the issues, and the offer documents read for each. Run once, by hand.
---------------------------------------------------------------------------
    python create_ipo_tables.py --check            # what is there and what is missing; changes nothing
    python create_ipo_tables.py                    # create them with the site's own login
    python create_ipo_tables.py --admin doadmin    # ... or with the cluster admin login (asks for the password at the prompt; never written down)

    ipo_issue      one row per issue: the company, SME or mainboard, where it stands (filed with SEBI / upcoming / open / closed / listed), its dates, price
                   band, issue price, lot, the exchange's detail (lead managers, registrar), and the SEBI filing it came from
    ipo_document   one row per offer document read (the draft red herring prospectus from SEBI, the red herring prospectus from the NSE): where it came
                   from, whether it was read, and what was read (jsonb, exactly as ipo_extract.extract returns it, with page numbers)

THE PDFs THEMSELVES ARE NOT KEPT. The job downloads one, reads it, stores what it found and the link, and deletes the file.
NOTHING HERE IS A VERDICT. The tables hold what the issuers and the exchanges published.

WHEN THE SITE'S LOGIN CANNOT CREATE TABLES
    The first run says "permission denied for schema public" -- use --admin. A table made by the admin login is owned by it and the site's login gets
    nothing by default, so this also GRANTS the site's login SELECT / INSERT / UPDATE / DELETE and the use of the id sequences.

SAFE TO RE-RUN. Every statement is IF NOT EXISTS.
"""

import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv("/opt/mfapi/.env")

TABLES = ("ipo_issue", "ipo_document")
SEQUENCES = ("ipo_issue_issue_id_seq", "ipo_document_doc_id_seq")

DDL = """
CREATE TABLE IF NOT EXISTS ipo_issue (
    issue_id           bigserial     PRIMARY KEY,
    company_key        text          NOT NULL UNIQUE,         -- the company however each source spells it (ipo_sources.company_key)
    slug               text          NOT NULL UNIQUE,         -- the page address: /ipo/<slug>
    name               text          NOT NULL,
    symbol             text,                                  -- the exchange symbol, once the exchange lists the issue
    board              text          NOT NULL DEFAULT 'mainboard' CHECK (board IN ('mainboard', 'sme')),
    stage              text          NOT NULL DEFAULT 'filed' CHECK (stage IN ('filed', 'upcoming', 'open', 'closed', 'listed')),
    open_date          date,
    close_date         date,
    listing_date       date,
    price_low          numeric(12,2),
    price_high         numeric(12,2),
    issue_price        numeric(12,2),                         -- the final price, once fixed
    lot_size           integer,
    issue_size_shares  bigint,
    sebi_filed_on      date,                                  -- the date of the draft on SEBI's list
    sebi_url           text,
    exchange_detail    jsonb,                                 -- what the exchange publishes about the issue (ipo_sources.norm_detail)
    card               jsonb,                                 -- the short summary the lists show and the scanner filters on (ipo_store.build_card)
    first_seen        timestamptz   NOT NULL DEFAULT now(),
    updated_at         timestamptz   NOT NULL DEFAULT now()
);
ALTER TABLE ipo_issue ADD COLUMN IF NOT EXISTS card jsonb;
CREATE INDEX IF NOT EXISTS ix_ipo_issue_stage  ON ipo_issue (stage, open_date DESC);
CREATE INDEX IF NOT EXISTS ix_ipo_issue_symbol ON ipo_issue (symbol);

CREATE TABLE IF NOT EXISTS ipo_document (
    doc_id           bigserial    PRIMARY KEY,
    issue_id         bigint       NOT NULL REFERENCES ipo_issue (issue_id) ON DELETE CASCADE,
    kind             text         NOT NULL CHECK (kind IN ('DRHP', 'RHP', 'PROSPECTUS')),
    source           text         NOT NULL CHECK (source IN ('SEBI', 'NSE')),
    source_ref       text         NOT NULL,                   -- the SEBI page or the NSE zip this came from: what makes it the same document next day
    doc_date         date,                                    -- the date SEBI lists it, or the day it was first seen
    pdf_url          text,
    status           text         NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'done', 'failed')),
    attempts         integer      NOT NULL DEFAULT 0,
    error            text,
    pages            integer,
    bytes            bigint,
    extract_version  integer,
    highlights       jsonb,                                   -- ipo_extract.extract() output
    fetched_at       timestamptz,
    extracted_at     timestamptz,
    UNIQUE (issue_id, kind, source_ref)
);
CREATE INDEX IF NOT EXISTS ix_ipo_document_status ON ipo_document (status, doc_date DESC);
"""

GRANTS = """
GRANT SELECT, INSERT, UPDATE, DELETE ON ipo_issue, ipo_document TO "{u}";
GRANT USAGE, SELECT ON SEQUENCE ipo_issue_issue_id_seq, ipo_document_doc_id_seq TO "{u}";
"""


def site_dsn():
    dsn = os.environ.get("FINCHAYA_DB") or os.environ.get("DATABASE_URL") or os.environ.get("MF_DSN")
    if not dsn:
        sys.exit("No FINCHAYA_DB in the environment or .env.")
    return dsn


def admin_dsn(user):
    d = conninfo_to_dict(site_dsn())
    print("\nConnecting to %s as '%s'." % (d.get("host", "(unknown host)"), user))
    print("The password is in your database provider's control panel, not on this machine. It is not shown as you type.")
    pw = getpass.getpass("%s password: " % user)
    if not pw:
        sys.exit("No password given. Nothing was changed.")
    d["user"], d["password"] = user, pw
    return make_conninfo(**d)


def app_user():
    return conninfo_to_dict(site_dsn()).get("user")


def report(cur):
    cur.execute("SELECT current_database(), current_user")
    db, who = cur.fetchone()
    print("\nconnected to '%s' as '%s'\n" % (db, who))
    ok = True
    for t in TABLES:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (t,))
        there = cur.fetchone()[0]
        print("  %-14s %s" % (t, "present" if there else "MISSING"))
        if not there:
            ok = False
            continue
        cur.execute("SELECT count(*) FROM " + t)
        print("  %-14s %d rows" % ("", cur.fetchone()[0]))
    me = app_user()
    if me and ok:
        for t in TABLES:
            cur.execute("""SELECT has_table_privilege(%s, %s, 'SELECT'), has_table_privilege(%s, %s, 'INSERT'),
                                  has_table_privilege(%s, %s, 'UPDATE'), has_table_privilege(%s, %s, 'DELETE')""", (me, t) * 4)
            good = all(cur.fetchone())
            print("  %s usable by %s: %s" % (t, me, "yes" if good else "NO -- run with --admin"))
            ok = ok and good
        for seq in SEQUENCES:
            cur.execute("SELECT has_sequence_privilege(%s, %s, 'USAGE')", (me, seq))
            good = cur.fetchone()[0]
            print("  sequence %s usable by %s: %s" % (seq, me, "yes" if good else "NO -- run with --admin"))
            ok = ok and good
    return ok


def main():
    args = sys.argv[1:]
    if "--check" in args:
        with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
            report(cur)
        print("\nRead-only check. Nothing was changed.\n")
        return

    admin = None
    for i, a in enumerate(args):
        if a == "--admin":
            admin = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else "doadmin"

    dsn = admin_dsn(admin) if admin else site_dsn()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user")
        me = cur.fetchone()[0]
        print("\nrunning as '%s'" % me)
        cur.execute(DDL)
        print("  " + ", ".join(TABLES) + " ready")
        site = app_user()
        if site and me != site:
            cur.execute(GRANTS.format(u=site))
            print("  granted table and sequence access to %s" % site)
        conn.commit()

    print("\n--- verifying as the site's own login ---")
    with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
        ok = report(cur)
    print("\nDone." if ok else "\nSomething above says NO or MISSING. Re-run with --admin doadmin.")
    print()


if __name__ == "__main__":
    main()
