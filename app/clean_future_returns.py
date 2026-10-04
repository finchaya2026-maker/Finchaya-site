"""
clean_future_returns.py -- remove the mf_returns rows stamped with a
date that had not happened.
---------------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/clean_future_returns.py
    /opt/mfapi/venv/bin/python3 /opt/mfapi/clean_future_returns.py --apply

REPORTS BY DEFAULT. Deletes only with --apply.

WHAT WENT WRONG
    score_returns.py picked its as-of date with a bare
    MAX(nav_date) FROM mf_nav. Liquid and overnight funds are valued on
    calendar days, so AMFI's feed legitimately carries NAVs dated ahead
    of the last trading day -- 662 of them on 13 September 2026. That
    dragged the as-of date forward, and all 12,415 rows written in that
    run were stamped 14 September.

    The clamp in score_returns.py stops it happening again. This
    removes what the unclamped runs already wrote.

WHY THE NAV ROWS ARE NOT TOUCHED
    They are correct. A liquid fund accrues interest every calendar day
    and its NAV is published accordingly -- 452.9246 to 453.1299 over a
    weekend is about 7% a year, exactly what it should be. Deleting
    those would be destroying real history to tidy up a symptom.

    mf_returns is different in kind: nothing observes it, it is computed
    from NAV, and its as_of_date is now clamped to today. A future date
    in there cannot be right, so it can only be a leftover.

WHAT TO RUN AFTERWARDS
    score_returns.py, so the windows are recomputed and stamped with a
    date that exists. Until then mf_returns simply has no current row,
    which check_freshness.py will report as staleness -- correctly.
"""

import os
import sys
from datetime import date

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


def main():
    apply = "--apply" in sys.argv[1:]

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT current_database() AS db, current_user AS me")
        r = cur.fetchone()
        print(f"\nconnected to '{r['db']}' as '{r['me']}'   today is {date.today()}")

        cur.execute("""
            SELECT as_of_date, count(*) AS n
            FROM mf_returns WHERE as_of_date > CURRENT_DATE
            GROUP BY 1 ORDER BY 1
        """)
        bad = cur.fetchall()

        cur.execute("""
            SELECT count(*) AS n,
                   count(*) FILTER (WHERE as_of_date > CURRENT_DATE) AS future,
                   MAX(as_of_date) FILTER (WHERE as_of_date <= CURRENT_DATE) AS good
            FROM mf_returns
        """)
        t = cur.fetchone()

        print(f"\n  mf_returns total rows          {t['n']:,}")
        print(f"  dated after today              {t['future']:,}")
        print(f"  newest date that has happened  {t['good']}")

        if not bad:
            print("\nNothing dated in the future. Nothing to do.\n")
            return

        print("\n--- rows to remove ---")
        for x in bad:
            print(f"  {x['as_of_date']}   {x['n']:,} rows")

        # REFUSE TO EMPTY THE TABLE.
        #
        # If every row is future-dated, deleting them leaves the site
        # with no returns at all. That is a bigger outage than a wrong
        # date, and it should be a deliberate decision rather than a
        # side effect of a cleanup script. Recompute first, then clean.
        if t["future"] == t["n"]:
            print("\n  STOP: every row in mf_returns is future-dated.")
            print("  Deleting them would leave the table empty and the site")
            print("  with no trailing returns at all.")
            print("\n  Run score_returns.py FIRST -- it is clamped now, so it")
            print("  will write a correct set alongside these -- then run")
            print("  this again to remove the leftovers.\n")
            return

        if not apply:
            print("\nReport only. Nothing was changed.")
            print("Re-run with --apply to delete these rows.\n")
            return

        cur.execute("DELETE FROM mf_returns WHERE as_of_date > CURRENT_DATE")
        removed = cur.rowcount
        conn.commit()
        print(f"\n  deleted {removed:,} rows")

        cur.execute("""
            SELECT count(*) AS n, MAX(as_of_date) AS newest
            FROM mf_returns
        """)
        a = cur.fetchone()
        print(f"  mf_returns now {a['n']:,} rows, newest {a['newest']}")
        print("\nNow run score_returns.py so the newest date is today's.\n")


if __name__ == "__main__":
    main()
