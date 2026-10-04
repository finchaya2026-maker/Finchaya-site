"""
check_stale.py -- what is current, what is not, and what nothing feeds.
---------------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/check_stale.py

READ-ONLY. Writes nothing.

WHY, WHEN check_freshness.py ALREADY RUNS NIGHTLY
    Because it runs inside nightly.sh, and nightly.sh only knows about
    the mutual fund side: schemes, NAV, splits, fund scores, returns.
    The stock tables are not in that script, so nothing checks them and
    nothing updates them -- and a check that only looks where the jobs
    ran cannot report the tables where no job was ever scheduled.

    That is the failure this exists to catch: not a job that broke, but
    a job that was never there. Those are invisible to every monitor
    that watches jobs rather than data.

WHAT IT REPORTS
    For every table that carries a date, the newest date in it and how
    many business days behind today that is -- with weekends taken out,
    because a Saturday with no NAV is correct and should never be
    reported as two days of staleness.

    Then batch_run_log, which records what actually ran.

WHY THE STOCK SIDE MATTERS MORE THAN IT LOOKS
    stock_score drives the rising / sideways / falling reading on every
    fund card and the whole "Trading now" section of the portfolio
    report. If stock_technical stops, those readings freeze at whatever
    the last run said, and the page keeps presenting them as current.
    A stale price is worse than a missing one, because nothing on the
    screen says it is stale.
"""

import os
import sys
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

# (table, date column, what feeds it, expected cadence, days before it
#  counts as late)
#
# The tolerance is per table and not a global number, because these move
# on completely different clocks. mf_holding is monthly -- AMCs disclose
# once a month -- so nine days behind is healthy, and a checker that
# flags it every month is one nobody reads by the third month. A monitor
# that cries wolf is worse than no monitor: it trains you to ignore it,
# and then it is silent in exactly the way that matters.
WATCH = [
    ("mf_nav",          "nav_date",   "load_mf_schemes.py",  "every business day", 1),
    ("mf_score",        "as_of_date", "score_funds.py",      "every business day", 1),
    ("mf_returns",      "as_of_date", "score_returns.py",    "every business day", 1),
    ("mf_rolling",      "as_of_date", "score_rolling.py",    "weekly is enough",   7),
    ("mf_holding",      "as_of_date", "promote_holdings.py", "monthly",           32),
    ("stock_technical", "as_of_date", "(nothing scheduled)", "every business day", 1),
    ("stock_score",     "as_of_date", "score_stocks.py",     "every business day", 1),
]


def business_days_between(a, b):
    """Business days from a to b, weekends excluded.

    Without this a Saturday reads as two days stale on Monday and every
    report cries wolf twice a week. Public holidays are not handled --
    they would need the exchange calendar, and one day of false alarm a
    few times a year is a fair price for not carrying one.
    """
    if not a or a >= b:
        return 0
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.isoweekday() <= 5:
            n += 1
    return n


def connect():
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")
    return psycopg.connect(dsn, row_factory=dict_row)


def main():
    today = date.today()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT current_database() AS db")
        print(f"\nconnected to '{cur.fetchone()['db']}'   today is {today}")

        print(f"\n{'table':<18}{'newest':<13}{'behind':<9}{'fed by':<24}expected")
        print("-" * 84)
        for table, col, fed_by, cadence, tolerance in WATCH:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,))
            if not cur.fetchone()["ok"]:
                print(f"{table:<18}{'NO SUCH TABLE':<13}{'':<9}{fed_by:<24}{cadence}")
                continue
            cur.execute(f"SELECT max({col}) AS d, count(*) AS n FROM {table}")
            r = cur.fetchone()
            if not r["d"]:
                print(f"{table:<18}{'EMPTY':<13}{'':<9}{fed_by:<24}{cadence}")
                continue
            behind = business_days_between(r["d"], today)
            over = behind - tolerance
            flag = ("" if over <= 0 else
                    "  <-- STALE" if over <= 4 else "  <-- BADLY STALE")
            print(f"{table:<18}{str(r['d']):<13}{behind:<9}{fed_by:<24}"
                  f"{cadence}{flag}")

        # Dates in the FUTURE are their own category of wrong, and a max()
        # hides them inside what looks like freshness.
        print("\n--- anything dated in the future? ---")
        found = False
        for table, col, _, _, _ in WATCH:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,))
            if not cur.fetchone()["ok"]:
                continue
            cur.execute(f"SELECT {col} AS d, count(*) AS n FROM {table} "
                        f"WHERE {col} > CURRENT_DATE GROUP BY 1 ORDER BY 1")
            for r in cur.fetchall():
                found = True
                print(f"  {table:<18}{str(r['d']):<13}{r['n']:>8} rows")
        if not found:
            print("  none")
        else:
            print("\n  A handful of rows is a bad feed row. Thousands is a")
            print("  date-parsing bug. Either way max() reads them as today,")
            print("  so anything keyed to max(nav_date) is now keyed to it.")

        # ---- what actually ran -------------------------------------
        cur.execute("SELECT to_regclass('batch_run_log') IS NOT NULL AS ok")
        if cur.fetchone()["ok"]:
            print("\n--- the last twelve recorded runs ---")
            cur.execute("""
                SELECT run_type, business_date, status, started_at, finished_at
                FROM batch_run_log ORDER BY started_at DESC LIMIT 12
            """)
            rows = cur.fetchall()
            if not rows:
                print("  batch_run_log is empty.")
            for r in rows:
                started = str(r["started_at"])[:16] if r["started_at"] else "-"
                print(f"  {str(r['run_type']):<22}{str(r['business_date']):<13}"
                      f"{str(r['status']):<10}{started}")
        else:
            print("\n  no batch_run_log table.")

        print("""
READ IT LIKE THIS
  Each table is judged against its OWN cadence, so a monthly one is
  not called stale for being three weeks old.
  A table behind AND fed by "(nothing scheduled)" is not a broken job --
  it is a job that does not exist, and no amount of re-running will fix
  it. That needs a line in nightly.sh.""")


if __name__ == "__main__":
    main()
