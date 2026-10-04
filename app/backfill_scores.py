"""
backfill_scores.py -- score past month ends under the live algo.
-----------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/backfill_scores.py
    --months 24     how far back (default 24)
    --dry-run       list what it would do, run nothing
    --force         rescore months that already have a reading

WHY THIS IS NEEDED, AND WHY IT IS CHEAP
    check_history_depth.py measured the chain and found exactly one short
    link:

        raw candles      2014-09-15 -> today   2,969 days
        indicators       2014-09-05 -> today   2,736 days
        v3 scores        2026-08-28 -> today       3 days   <-- here
        fund portfolios  2023-02-28 -> today      43 months

    Everything the scorer reads goes back years. Only the scorer's own
    OUTPUT is new, because stock-v3 was written recently and has only run
    forward from the day it shipped.

    So this needs no downloads and no Kite calls. score_stocks.py --date
    reads stock_technical at or before a given day and writes a v3 row for
    it; running that once per past month end fills the gap from data
    already on this machine.

WHY MONTH ENDS AND NOT EVERY DAY
    The consumer is the fund rising score, and the portfolio underneath it
    is disclosed monthly. A daily series would re-score the same holdings
    against moving prices and call the wobble a trend. Month on month
    compares one disclosure against the next, which is the real unit of
    change. 24 runs, not 700.

WHY IT IS RESUMABLE
    A month that already carries a v3 reading is skipped, so an
    interrupted run can simply be started again. Each month is scored and
    committed by score_stocks.py itself before the next begins, so
    stopping half way leaves the months already done intact rather than
    rolling everything back.

A CAUTION WORTH STATING
    This writes stock-v3 readings for dates that stock-v3 did not exist
    on. That is the point -- a consistent series needs one algorithm
    throughout, and comparing a v3 reading against a v2 one would show a
    step where the CODE changed and report it as the market moving.

    It does not touch the v2, v2l or v2m series, which stay exactly as
    they are for anybody comparing the scorers against each other.
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

PYTHON = "/opt/mfapi/venv/bin/python3"
SCORER = "/opt/mfapi/score_stocks.py"
ALGO = os.getenv("MF_STOCK_ALGO", "stock-v3")


def connect():
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")
    return psycopg.connect(dsn, row_factory=dict_row)


def month_ends(n):
    """Last calendar day of each of the last n months, oldest first."""
    out, d = [], date.today().replace(day=1)
    for _ in range(n):
        out.append(d - timedelta(days=1))
        d = (d - timedelta(days=1)).replace(day=1)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT MIN(as_of_date) AS first, MAX(as_of_date) AS last
            FROM stock_technical
        """)
        tech = cur.fetchone()
        cur.execute("""
            SELECT MIN(as_of_date) AS first FROM mf_holding
        """)
        hold = cur.fetchone()
        # Which months already carry a reading, so a rerun is cheap.
        cur.execute("""
            SELECT DISTINCT date_trunc('month', as_of_date)::date AS m
            FROM stock_score WHERE algo_version = %(a)s
        """, {"a": ALGO})
        done = {r["m"] for r in cur.fetchall()}

    if not tech["first"]:
        sys.exit("stock_technical is empty -- nothing to score from.")

    wanted = month_ends(args.months)

    todo, skip_early, skip_done, skip_nohold = [], [], [], []
    for d in wanted:
        if d < tech["first"]:
            skip_early.append(d)
        elif hold["first"] and d < hold["first"]:
            # A score with no portfolio to read it through produces no fund
            # reading, so the run would be work for nothing.
            skip_nohold.append(d)
        elif not args.force and d.replace(day=1) in done:
            skip_done.append(d)
        else:
            todo.append(d)

    print("algo                : %s" % ALGO)
    print("indicators available: %s to %s" % (tech["first"], tech["last"]))
    print("portfolios from     : %s" % hold["first"])
    print("months requested    : %d" % len(wanted))
    if skip_early:
        print("  before indicators : %d (skipped)" % len(skip_early))
    if skip_nohold:
        print("  before portfolios : %d (skipped -- nothing to read a score"
              " through)" % len(skip_nohold))
    if skip_done:
        print("  already scored    : %d (skipped; --force to redo)"
              % len(skip_done))
    print("months to score     : %d" % len(todo))
    print()

    if not todo:
        print("Nothing to do.")
        return

    if args.dry_run:
        for d in todo:
            print("  would run: %s %s --date %s" % (PYTHON, SCORER, d))
        print("\n--dry-run: nothing run.")
        return

    started = time.time()
    failed = []
    for i, d in enumerate(todo, 1):
        t0 = time.time()
        print("[%d/%d] %s ... " % (i, len(todo), d), end="", flush=True)
        # Output captured rather than streamed: the scorer prints a page
        # per run, and 24 pages would bury the one line that matters. It
        # is printed in full only when a run fails.
        r = subprocess.run([PYTHON, SCORER, "--date", d.isoformat()],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("FAILED (%.0fs)" % (time.time() - t0))
            print(r.stdout[-2000:])
            print(r.stderr[-2000:])
            failed.append(d)
            # Keep going. One bad month should not cost the other 23 --
            # and the summary names it.
            continue
        # The scorer's own count, so this reports what was written rather
        # than that the process exited zero.
        wrote = ""
        for ln in r.stdout.splitlines():
            if "wrote" in ln.lower() or "scored" in ln.lower():
                wrote = ln.strip()
        print("done (%.0fs)  %s" % (time.time() - t0, wrote[:70]))

    print()
    print("elapsed: %.1f min" % ((time.time() - started) / 60))
    if failed:
        print("FAILED months: %s" % ", ".join(str(d) for d in failed))
        print("Re-run this script to retry only those.")

    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT count(DISTINCT as_of_date) AS dates,
                   MIN(as_of_date) AS first, MAX(as_of_date) AS last
            FROM stock_score WHERE algo_version = %(a)s
        """, {"a": ALGO})
        h = cur.fetchone()
    print("%s now holds %d date(s), %s to %s"
          % (ALGO, h["dates"], h["first"], h["last"]))
    print()
    print("Next: /opt/mfapi/venv/bin/python3 /opt/mfapi/backfill_fund_trend.py"
          " --months %d" % args.months)


if __name__ == "__main__":
    main()
