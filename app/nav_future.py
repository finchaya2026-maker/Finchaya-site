"""
nav_future.py -- which schemes are posting NAV for days that have not
happened, and what shape the damage is.
---------------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/nav_future.py

READ-ONLY. Writes nothing.

WHY THIS MATTERS MORE THAN THE ROW COUNT SUGGESTS
    662 rows out of several million is nothing. But score_returns.py
    took MAX(nav_date) as its as-of date, so those 662 rows stamped
    ALL 12,415 rows of mf_returns with 14 September -- a day that had
    not happened. Every trailing return on the site was labelled with a
    future date, and the freshness check could not see it because every
    age test only asked "is this too old".

    The clamp in score_returns.py stops the damage spreading. It does
    not fix the rows, and it does not answer the question this script
    exists for: WHY does the loader accept them at all.

THE THREE THINGS TO LOOK FOR, in order of what each would mean
    1. A HANDFUL OF SCHEMES, ALL FROM ONE AMC. That is the AMC
       publishing a forward-dated NAV in the AMFI file -- real, and
       nothing to do with us. The fix is to refuse them at load.

    2. EVERY SCHEME OF ONE TYPE (say all FoFs, or all segregated
       portfolios). Some scheme categories legitimately carry a "next
       valuation date". Same fix, but worth knowing it is a category
       rather than an accident.

    3. THE SAME SCHEMES THAT ALSO HAVE A CORRECT ROW, with the SAME
       NAV value. That is a date-parsing bug on our side -- one row
       written twice, once under a mangled date. This is the one worth
       finding, because it means the loader is corrupting data rather
       than faithfully loading something odd.

    The last section tests exactly that, so the answer is a fact rather
    than a theory.
"""

import os
import sys

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def connect():
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")
    return psycopg.connect(dsn, row_factory=dict_row)


def nav_value_column(cur):
    """Which column holds the NAV figure.

    Introspected rather than assumed. A script that guesses 'nav' and
    dies on 'nav_value' is a script you debug instead of read.
    """
    cur.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'mf_nav'
        ORDER BY ordinal_position
    """)
    cols = cur.fetchall()
    print("mf_nav columns: " + ", ".join(
        f"{c['column_name']}" for c in cols))
    for c in cols:
        if c["column_name"] in ("nav", "nav_value", "net_asset_value"):
            return c["column_name"]
    for c in cols:
        if c["data_type"] in ("numeric", "double precision", "real"):
            return c["column_name"]
    return None


def main():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT current_database() AS db, CURRENT_DATE AS today")
        r = cur.fetchone()
        print(f"\nconnected to '{r['db']}'   today is {r['today']}\n")

        navcol = nav_value_column(cur)
        print(f"NAV value column: {navcol}\n")

        # ---- 1. HOW MANY, AND FOR WHICH DATES ------------------------
        cur.execute("""
            SELECT nav_date, count(*) AS n,
                   count(DISTINCT scheme_code) AS schemes
            FROM mf_nav WHERE nav_date > CURRENT_DATE
            GROUP BY 1 ORDER BY 1
        """)
        rows = cur.fetchall()
        if not rows:
            print("No future-dated NAV rows. Nothing to explain.\n")
            return
        print("--- future-dated rows, by date ---")
        for x in rows:
            print(f"  {x['nav_date']}   {x['n']:>6} rows   "
                  f"{x['schemes']:>6} schemes")

        # ---- 2. WHOSE SCHEMES ARE THEY -------------------------------
        #
        # One AMC means their file; many AMCs means our loader.
        cur.execute("""
            SELECT COALESCE(s.amc_name, '(unknown AMC)') AS amc,
                   count(DISTINCT n.scheme_code) AS schemes
            FROM mf_nav n
            LEFT JOIN mf_scheme s ON s.scheme_code = n.scheme_code
            WHERE n.nav_date > CURRENT_DATE
            GROUP BY 1 ORDER BY 2 DESC LIMIT 20
        """)
        print("\n--- which AMCs (top 20) ---")
        for x in cur.fetchall():
            print(f"  {x['schemes']:>6}   {x['amc'][:60]}")

        # ---- 3. A LOOK AT THE ACTUAL ROWS ----------------------------
        cur.execute(f"""
            SELECT n.scheme_code, n.nav_date, n.{navcol} AS nav,
                   COALESCE(s.scheme_name, '(not in mf_scheme)') AS name
            FROM mf_nav n
            LEFT JOIN mf_scheme s ON s.scheme_code = n.scheme_code
            WHERE n.nav_date > CURRENT_DATE
            ORDER BY n.scheme_code LIMIT 15
        """)
        print("\n--- fifteen of them ---")
        for x in cur.fetchall():
            print(f"  {x['scheme_code']:<9}{str(x['nav_date']):<12}"
                  f"{str(x['nav']):>12}   {x['name'][:52]}")

        # ---- 4. THE TEST THAT DECIDES IT -----------------------------
        #
        # For each scheme with a future row, is there also a normal row
        # at the latest REAL date, and does it carry the same NAV?
        #
        #   same value   -> one row written twice under a bad date.
        #                   Our parser. Fixable at load, and the future
        #                   row is a duplicate that can simply go.
        #   different    -> the feed genuinely carried a forward date
        #                   with its own figure. Theirs. We refuse it.
        #   no real row  -> the ONLY row for that scheme is the future
        #                   one, so the scheme has no usable NAV at all
        #                   and anything holding it is being valued off
        #                   a date that does not exist.
        cur.execute(f"""
            WITH fut AS (
                SELECT scheme_code, nav_date, {navcol} AS nav
                FROM mf_nav WHERE nav_date > CURRENT_DATE
            ),
            real_latest AS (
                SELECT DISTINCT ON (n.scheme_code)
                       n.scheme_code, n.nav_date, n.{navcol} AS nav
                FROM mf_nav n
                JOIN fut f ON f.scheme_code = n.scheme_code
                WHERE n.nav_date <= CURRENT_DATE
                ORDER BY n.scheme_code, n.nav_date DESC
            )
            SELECT
              count(*) FILTER (WHERE r.scheme_code IS NULL)        AS only_future,
              count(*) FILTER (WHERE r.nav IS NOT NULL
                                 AND r.nav = f.nav)                AS same_value,
              count(*) FILTER (WHERE r.nav IS NOT NULL
                                 AND r.nav <> f.nav)               AS diff_value
            FROM fut f LEFT JOIN real_latest r USING (scheme_code)
        """)
        v = cur.fetchone()
        print("\n--- is the future row a duplicate of a real one? ---")
        print(f"  same NAV as the latest real row   {v['same_value']:>6}"
              "   <- our date parsing")
        print(f"  different NAV                     {v['diff_value']:>6}"
              "   <- the feed's own forward date")
        print(f"  no real row at all                {v['only_future']:>6}"
              "   <- scheme has NO usable NAV")

        # Side by side, so the verdict above can be checked by eye
        # rather than taken on trust.
        cur.execute(f"""
            WITH fut AS (
                SELECT scheme_code, nav_date, {navcol} AS nav
                FROM mf_nav WHERE nav_date > CURRENT_DATE
            ),
            real_latest AS (
                SELECT DISTINCT ON (n.scheme_code)
                       n.scheme_code, n.nav_date, n.{navcol} AS nav
                FROM mf_nav n
                JOIN fut f ON f.scheme_code = n.scheme_code
                WHERE n.nav_date <= CURRENT_DATE
                ORDER BY n.scheme_code, n.nav_date DESC
            )
            SELECT f.scheme_code, f.nav_date AS fut_date, f.nav AS fut_nav,
                   r.nav_date AS real_date, r.nav AS real_nav
            FROM fut f LEFT JOIN real_latest r USING (scheme_code)
            ORDER BY f.scheme_code LIMIT 12
        """)
        print("\n  code      future date   future NAV      real date     real NAV")
        for x in cur.fetchall():
            print(f"  {x['scheme_code']:<10}{str(x['fut_date']):<14}"
                  f"{str(x['fut_nav']):>10}      "
                  f"{str(x['real_date']):<14}{str(x['real_nav']):>10}")

        print("""
HOW TO READ IT
  If "same NAV" carries nearly all of them, the loader is writing one
  row twice under a mangled date and the future copies can be deleted.
  If "different NAV" dominates, AMFI really is publishing a forward
  date and the loader should refuse anything after today at the point
  of load. Either way score_returns.py is now clamped, so this can no
  longer move the whole returns table into next week.
""")


if __name__ == "__main__":
    main()
