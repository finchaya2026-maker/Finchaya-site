"""
load_cas.py -- read a detailed CAS PDF and store its transactions.
---------------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/load_cas.py STATEMENT.pdf \
        --password SECRET --user 7 [--client 12] [--dry-run]

    --dry-run    parse, resolve and check. Writes NOTHING. Prints exactly
                 what a real run would store. Use this first, every time.
    --user N     the FinChaya user who owns this import. Required.
    --client N   the client_id this statement belongs to. Leave it off and
                 the statement is treated as the user's own money.
    --password   the CAS password. Also read from CAS_PASSWORD if you would
                 rather not have it in your shell history.
    --json PATH  write the parsed statement to a file instead of loading it.
                 For working out what a statement contains without touching
                 the database.

BEFORE THIS WILL WORK
    pip install casparser        (in the venv, once)
    python3 create_cas_tables.py (once)

THE STATEMENT HAS TO BE THE RIGHT ONE
    There are two CAS formats and only one of them is any use here.

      DETAILED   every transaction with its date. This is the one.
      SUMMARY    closing balances only. No dates, no cashflows, and
                 therefore no XIRR -- it tells us nothing we do not
                 already have from the amount typed into the portfolio.

    The loader refuses a SUMMARY statement rather than importing a
    holdings list that looks like success and silently buys nothing.

    It must also be the ORIGINAL PDF as CAMS or KFintech emailed it. A
    statement opened and re-saved as PDF, or printed to PDF, loses the
    selectable text the parser reads and will fail. MF Central's own
    reformatted statement is a different layout again and is not
    supported.

WHAT IS DELIBERATELY NOT STORED
    A CAS carries the investor's PAN, email address, postal address and
    mobile number. None of it is written to the database. The name is
    printed here so you can confirm the statement belongs to the client
    you are importing for, and stored on the import row alone as an audit
    trail. Everything else from investor_info is discarded in this file
    and never reaches a table.

    This is a choice, not an omission. Holding a client's PAN creates an
    obligation that the feature does not need in order to work.

HOW A FUND IS IDENTIFIED
    By ISIN first. The CAS gives a plan-level ISIN and mf_scheme already
    carries scheme_isin, so the existing resolver places the fund with no
    name matching at all. The AMFI code is tried second, for the rare row
    where the ISIN is missing.

    A fund that resolves to nothing is stored with scheme_code NULL and
    listed at the end of the run. It is NOT guessed at by name: a wrong
    fund silently attached to real money is worse than a gap you can see.

WHAT IS CHECKED, AND WHY THE CHECKS ARE PRINTED
    For every scheme the run compares three closing unit balances:

      stated      what the RTA printed on the statement
      calculated  what casparser made of the statement's own rows
      stored      what is actually in cas_txn after loading

    All three agreeing means the import is sound. stated vs calculated
    disagreeing is a parsing problem. calculated vs stored disagreeing
    means the unique index collapsed two genuinely identical
    transactions on the same day -- two SIPs of the same amount, which
    does happen -- and the run says so by name rather than leaving a
    quietly short holding to be discovered in a return figure later.
"""

import argparse
import os
import sys
from collections import Counter
from decimal import Decimal

import psycopg
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

# The same resolver the portfolio API uses, so a fund placed here is the
# same fund the rest of the site would place. Copied rather than
# imported: this script must run from cron without FastAPI loaded.
RESOLVE_BY_ISIN = """
SELECT c.canonical_scheme_code AS code, c.scheme_name
FROM mf_scheme s
JOIN v_fund_canonical c ON c.scheme_name = s.scheme_name
                       AND c.amc_name IS NOT DISTINCT FROM s.amc_name
WHERE s.scheme_isin = %(key)s LIMIT 1
"""

RESOLVE_BY_CODE = """
SELECT c.canonical_scheme_code AS code, c.scheme_name
FROM mf_scheme s
JOIN v_fund_canonical c ON c.scheme_name = s.scheme_name
                       AND c.amc_name IS NOT DISTINCT FROM s.amc_name
WHERE s.scheme_code = %(key)s LIMIT 1
"""

# Rows that move units but are not the investor putting money in or
# taking it out. Kept in the table -- they are needed to reconcile the
# unit balance -- but never counted as a cashflow.
NOT_CASHFLOW = {"STAMP_DUTY_TAX", "STT_TAX", "TDS_TAX",
                "DIVIDEND_REINVEST", "SEGREGATION", "MISC", "UNKNOWN"}


def d(x):
    """Decimal or None, whatever casparser handed over."""
    if x is None or x == "":
        return None
    return Decimal(str(x))


def as_date(x):
    return str(x)[:10] if x else None


def connect():
    # FINCHAYA_DB first -- it is what every other script here reads. See
    # the same note in create_cas_tables.py: reading DATABASE_URL first
    # silently connected to a different database.
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")
    return psycopg.connect(dsn)


def parse(path, password):
    try:
        from casparser import read_cas_pdf
    except ImportError:
        sys.exit("casparser is not installed.\n"
                 "  /opt/mfapi/venv/bin/pip install casparser")
    try:
        return read_cas_pdf(path, password, output="dict")
    except Exception as exc:                      # noqa: BLE001
        sys.exit(
            f"Could not read that statement: {exc}\n\n"
            "The usual causes, in order of how often they are the cause:\n"
            "  1. Wrong password.\n"
            "  2. It is not the original file -- a re-saved or\n"
            "     printed-to-PDF copy loses the text the parser reads.\n"
            "  3. It is an MF Central statement, which is a different\n"
            "     layout and is not supported."
        )


def resolve(cur, isin, amfi):
    if isin:
        cur.execute(RESOLVE_BY_ISIN, {"key": isin})
        row = cur.fetchone()
        if row:
            return row[0], "isin"
    if amfi:
        cur.execute(RESOLVE_BY_CODE, {"key": str(amfi)})
        row = cur.fetchone()
        if row:
            return row[0], "amfi"
    return None, None


def own_arn(cur, user_id):
    cur.execute("SELECT arn FROM distributor WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    return (row[0] or "").strip().upper() if row else ""


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("pdf")
    ap.add_argument("--password", default=os.environ.get("CAS_PASSWORD"))
    ap.add_argument("--user", type=int, required=True)
    ap.add_argument("--client", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()

    if not a.password:
        sys.exit("Need --password (or CAS_PASSWORD in the environment).")
    if not os.path.exists(a.pdf):
        sys.exit(f"No such file: {a.pdf}")

    data = parse(a.pdf, a.password)

    if a.json:
        import json
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        print(f"Wrote {a.json}. Nothing loaded.")
        return

    cas_type = str(data.get("cas_type", "")).upper()
    file_type = str(data.get("file_type", "")).upper()
    period = data.get("statement_period") or {}
    folios = data.get("folios") or []
    warnings = list(data.get("parse_warnings") or [])

    # The name only. Read here, printed here, and the rest of
    # investor_info goes no further than this line.
    investor = (data.get("investor_info") or {}).get("name")

    print(f"\n  Statement   {file_type} {cas_type}")
    print(f"  Period      {period.get('from') or '?'} to {period.get('to') or '?'}")
    print(f"  Investor    {investor or '(not stated)'}")
    print(f"  Folios      {len(folios)}")
    if warnings:
        print(f"  Warnings    {len(warnings)}")
        for w in warnings[:5]:
            print(f"              {w}")

    if "DETAILED" not in cas_type:
        sys.exit(
            "\nThis is a SUMMARY statement -- closing balances only, no\n"
            "transactions and no dates, so it cannot answer what anyone\n"
            "earned. Request the DETAILED statement instead:\n"
            "  CAMS/KFintech CAS request page -> Statement Type:\n"
            "  'Detailed' (not 'Summary'), period 'From inception',\n"
            "  and include zero-balance folios."
        )

    total_txn = sum(len(s.get("transactions") or [])
                    for f in folios for s in (f.get("schemes") or []))
    if not total_txn:
        sys.exit("\nNo transactions in this statement. Nothing to load.")

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        print(f"  Database    {cur.fetchone()[0]}")

        if a.client is not None:
            cur.execute("SELECT name FROM client WHERE client_id = %s "
                        "AND owner_user_id = %s", (a.client, a.user))
            row = cur.fetchone()
            if not row:
                sys.exit(f"No client {a.client} belonging to user {a.user}.")
            print(f"  Importing for client {a.client}: {row[0]}")
            if investor and row[0].strip().lower() not in investor.strip().lower():
                print(f"  ! The statement is in the name of '{investor}',")
                print(f"    which does not look like '{row[0]}'. Check this")
                print("    is the right client before loading for real.")
        else:
            print(f"  Importing as user {a.user}'s own money (no client)")

        arn = own_arn(cur, a.user)
        print(f"  Your ARN    {arn or '(none on file)'}\n")

        import_id = None
        if not a.dry_run:
            cur.execute("""
                INSERT INTO cas_import (owner_user_id, client_id, source, rta,
                    cas_type, period_from, period_to, investor_name,
                    parse_warnings, file_name)
                VALUES (%(u)s, %(c)s, 'cas_pdf', %(rta)s, %(t)s,
                        %(pf)s, %(pt)s, %(inv)s, %(w)s, %(fn)s)
                RETURNING import_id
            """, {"u": a.user, "c": a.client, "rta": file_type, "t": cas_type,
                  "pf": period.get("from") or None,
                  "pt": period.get("to") or None,
                  "inv": investor, "w": warnings,
                  "fn": os.path.basename(a.pdf)})
            import_id = cur.fetchone()[0]

        rows, unresolved, mismatches = [], [], []
        by_source = Counter()
        n_schemes = n_txn = n_new = 0

        for folio in folios:
            fno = folio.get("folio")
            amc = folio.get("amc")
            for s in (folio.get("schemes") or []):
                txns = s.get("transactions") or []
                if not txns:
                    continue
                n_schemes += 1
                n_txn += len(txns)

                isin = s.get("isin")
                amfi = s.get("amfi")
                code, how = resolve(cur, isin, amfi)
                by_source[how or "unresolved"] += 1
                if not code:
                    unresolved.append((s.get("scheme"), isin, amfi))

                advisor = (s.get("advisor") or "").strip()
                is_own = bool(arn) and advisor.upper() == arn

                folio_row_id = None
                if not a.dry_run:
                    cur.execute("""
                        INSERT INTO cas_folio (owner_user_id, client_id, folio,
                            amc, scheme_name, isin, amfi_code, rta_code,
                            scheme_code, advisor, is_own_arn,
                            first_import_id, last_import_id)
                        VALUES (%(u)s,%(c)s,%(f)s,%(amc)s,%(sn)s,%(i)s,%(a)s,
                                %(r)s,%(sc)s,%(adv)s,%(own)s,%(imp)s,%(imp)s)
                        ON CONFLICT (owner_user_id, COALESCE(client_id, -1),
                                     folio, COALESCE(isin, scheme_name))
                        DO UPDATE SET
                            -- A later statement may place a fund the
                            -- earlier one could not, so let a real code
                            -- replace a NULL. Never the other way round.
                            scheme_code = COALESCE(EXCLUDED.scheme_code,
                                                   cas_folio.scheme_code),
                            advisor     = COALESCE(EXCLUDED.advisor,
                                                   cas_folio.advisor),
                            is_own_arn  = EXCLUDED.is_own_arn,
                            amc         = COALESCE(EXCLUDED.amc, cas_folio.amc),
                            last_import_id = EXCLUDED.last_import_id,
                            updated_at  = now()
                        RETURNING folio_row_id
                    """, {"u": a.user, "c": a.client, "f": fno, "amc": amc,
                          "sn": s.get("scheme"), "i": isin,
                          "a": str(amfi) if amfi else None,
                          "r": s.get("rta_code"), "sc": code,
                          "adv": advisor or None, "own": is_own,
                          "imp": import_id})
                    folio_row_id = cur.fetchone()[0]

                    for t in txns:
                        cur.execute("""
                            INSERT INTO cas_txn (folio_row_id, txn_date,
                                txn_type, amount, units, nav, balance,
                                description, first_import_id)
                            VALUES (%(fr)s,%(dt)s,%(ty)s,%(am)s,%(un)s,
                                    %(nv)s,%(bal)s,%(ds)s,%(imp)s)
                            ON CONFLICT DO NOTHING
                        """, {"fr": folio_row_id, "dt": as_date(t.get("date")),
                              "ty": str(t.get("type") or "UNKNOWN"),
                              "am": d(t.get("amount")), "un": d(t.get("units")),
                              "nv": d(t.get("nav")), "bal": d(t.get("balance")),
                              "ds": t.get("description"), "imp": import_id})
                        n_new += cur.rowcount

                # The three-way check described at the top of this file.
                stated = d(s.get("close"))
                calc = d(s.get("close_calculated"))
                stored = None
                if not a.dry_run:
                    cur.execute("SELECT COALESCE(sum(units), 0) FROM cas_txn "
                                "WHERE folio_row_id = %s", (folio_row_id,))
                    stored = cur.fetchone()[0]

                    val = s.get("valuation") or {}
                    cur.execute("""
                        INSERT INTO cas_valuation (folio_row_id, as_of, units,
                            nav, value, cost, units_derived, import_id)
                        VALUES (%(fr)s,%(as)s,%(u)s,%(n)s,%(v)s,%(c)s,%(ud)s,%(imp)s)
                        ON CONFLICT (folio_row_id, as_of) DO UPDATE SET
                            units = EXCLUDED.units, nav = EXCLUDED.nav,
                            value = EXCLUDED.value, cost = EXCLUDED.cost,
                            units_derived = EXCLUDED.units_derived,
                            import_id = EXCLUDED.import_id
                    """, {"fr": folio_row_id, "as": as_date(val.get("date")),
                          "u": stated, "n": d(val.get("nav")),
                          "v": d(val.get("value")), "c": d(val.get("cost")),
                          "ud": stored, "imp": import_id})

                ref = stored if stored is not None else calc
                if stated is not None and ref is not None \
                        and abs(stated - ref) > Decimal("0.01"):
                    mismatches.append((s.get("scheme"), fno, stated, calc, stored))

                rows.append((fno, s.get("scheme"), len(txns), code, advisor,
                             is_own))

        if a.dry_run:
            conn.rollback()
        else:
            cur.execute("""UPDATE cas_import SET folio_count = %s,
                           scheme_count = %s, txn_count = %s, txn_inserted = %s
                           WHERE import_id = %s""",
                        (len(folios), n_schemes, n_txn, n_new, import_id))
            conn.commit()

    # ---- what happened -------------------------------------------
    print(f"  {'WOULD LOAD' if a.dry_run else 'LOADED'}")
    print(f"    schemes with transactions   {n_schemes}")
    print(f"    transactions in statement   {n_txn}")
    if not a.dry_run:
        print(f"    newly stored                {n_new}")
        print(f"    already had                 {n_txn - n_new}")
    print(f"    matched by ISIN             {by_source['isin']}")
    print(f"    matched by AMFI code        {by_source['amfi']}")
    print(f"    not matched to a fund       {by_source['unresolved']}")

    own = sum(1 for r in rows if r[5])
    print(f"\n    under your ARN              {own}")
    print(f"    held elsewhere (external)   {len(rows) - own}")

    if unresolved:
        print("\n  NOT MATCHED to any fund we carry. Stored, but they will")
        print("  not appear in look-through or returns until they resolve:")
        for name, isin, amfi in unresolved:
            print(f"    {name}")
            print(f"      isin {isin or '-'}   amfi {amfi or '-'}")

    if mismatches:
        print("\n  ! UNIT BALANCE DOES NOT RECONCILE")
        print("    The statement's closing units and ours disagree. Do not")
        print("    trust a return computed from these until it is resolved.")
        for name, fno, stated, calc, stored in mismatches:
            print(f"    {name}  (folio {fno})")
            print(f"      stated {stated}  parsed {calc}  stored {stored}")
    elif not a.dry_run:
        print("\n  Unit balances reconcile against the statement.")

    if a.dry_run:
        print("\n  Dry run. Nothing was written. Re-run without --dry-run")
        print("  once the fund matching and the balances look right.")


if __name__ == "__main__":
    main()
