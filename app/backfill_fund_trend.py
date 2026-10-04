"""
backfill_fund_trend.py -- the rising score, month by month, backwards.
----------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/backfill_fund_trend.py
    --months 24      how far back to go (default 24)
    --dry-run        compute and print, write nothing
    --codes 118989,120503   only these funds

WHY THIS EXISTS -- AND WHY IT IS NOT "WAITING FOR HISTORY"
    An alert like "this fund's rising score has fallen since the day I
    invested" reads as though it needs a diary kept from that day onward.
    I said exactly that, and it was wrong.

    Every input is already a time series that nothing prunes:

        stock_score      keyed (isin, as_of_date, algo_version), never
                         deleted from -- a full daily record
        stock_technical  every day, every timeframe
        mf_holding       every monthly disclosure, kept

    So a fund's rising split on any past date is not lost. It is a query
    nobody had written. This writes it, month by month, into the same
    mf_fund_trend table the nightly job now appends to -- which means the
    three-month and since-you-invested rules can work the day they are
    switched on rather than three months later.

MONTH ENDS, NOT DAYS
    The reading is sampled at each month end, because the holdings under
    it only change monthly: AMFI portfolios are disclosed once a month, so
    a daily series would show the same portfolio re-scored against moving
    prices and call small wobbles a trend. Month on month compares one
    disclosure against the next, which is the real unit of change here.

THE ALGO VERSION IS PINNED
    stock-v2 and stock-v3 give different numbers for the same stock on the
    same day. A series that took whichever algo was newest at each date
    would show a step where the CODE changed and report it as the market
    moving -- every alert built on it firing on our own release history.

    Pinned, a month with no rows for that version gets no row at all, and
    this prints which months those were. A visible gap beats an invented
    number.
"""

import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

sys.path.insert(0, "/opt/mfapi")
from portfolio_api import TREND_BATCH_AT, _q, _trend_summary   # noqa: E402

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")

DRY = "--dry-run" in sys.argv
MONTHS = 24
if "--months" in sys.argv:
    MONTHS = int(sys.argv[sys.argv.index("--months") + 1])
ONLY = None
if "--codes" in sys.argv:
    ONLY = [c.strip() for c in
            sys.argv[sys.argv.index("--codes") + 1].split(",") if c.strip()]

ALGO = os.getenv("MF_STOCK_ALGO", "stock-v3")
CHUNK = 40

FUNDS = """
SELECT DISTINCT h.scheme_code, vc.category
FROM mf_holding h
JOIN v_scheme_category vc ON vc.scheme_code = h.scheme_code
WHERE vc.category IS NOT NULL
"""

DDL = """
CREATE TABLE IF NOT EXISTS mf_fund_trend (
    scheme_code   text NOT NULL,
    as_of_date    date NOT NULL,
    category      text,
    up_pct        numeric NOT NULL,
    sideways_pct  numeric NOT NULL,
    down_pct      numeric NOT NULL,
    unscored_pct  numeric NOT NULL,
    readable_pct  numeric NOT NULL,
    built_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code, as_of_date)
);
CREATE INDEX IF NOT EXISTS mf_fund_trend_code_date
    ON mf_fund_trend (scheme_code, as_of_date DESC);
"""


def month_ends(n):
    """The last calendar day of each of the last n months, oldest first.

    Calendar month ends rather than "30 days ago", because that is what
    the holdings disclosures are aligned to. A 30-day step would drift
    across disclosure boundaries and compare a portfolio against itself in
    some months and against the next one in others.
    """
    out, d = [], date.today().replace(day=1)
    for _ in range(n):
        out.append(d - timedelta(days=1))          # last day of previous month
        d = (d - timedelta(days=1)).replace(day=1)
    return sorted(out)


def main():
    started = time.time()
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(DDL)

        funds = _q(cur, FUNDS)
        cat_of = {str(f["scheme_code"]): f["category"] for f in funds}
        codes = [c for c in sorted(cat_of) if not ONLY or c in ONLY]
        if not codes:
            sys.exit("No funds matched.")

        # How far back the SCORES actually go for this algo version. Asking
        # for 24 months when the scorer has 7 months of v3 rows would
        # otherwise mean 17 silent misses.
        span = _q(cur, """
            SELECT MIN(as_of_date) AS first, MAX(as_of_date) AS last,
                   count(DISTINCT as_of_date) AS days
            FROM stock_score WHERE algo_version = %(a)s
        """, {"a": ALGO}, one=True)

        print("algo pinned to      : %s" % ALGO)
        print("scores available    : %s to %s (%d days)"
              % (span["first"], span["last"], span["days"] or 0))
        print("funds               : %d" % len(codes))
        if not span["first"]:
            sys.exit("No stock_score rows for %s -- nothing to read." % ALGO)

        wanted = [d for d in month_ends(MONTHS)]
        usable = [d for d in wanted if d >= span["first"]]
        skipped = [d for d in wanted if d < span["first"]]
        if skipped:
            print("months before the scores start (skipped): %s .. %s"
                  % (skipped[0], skipped[-1]))
        print("months to build     : %d" % len(usable))
        print()

        written = 0
        for when in usable:
            rows_out = []
            score_date = None
            for i in range(0, len(codes), CHUNK):
                chunk = codes[i:i + CHUNK]
                grouped = defaultdict(list)
                for r in _q(cur, TREND_BATCH_AT,
                            {"codes": chunk, "as_of": when, "algo": ALGO}):
                    grouped[str(r["scheme_code"])].append(r)
                for code in chunk:
                    rs = grouped.get(code)
                    if not rs:
                        continue
                    s = _trend_summary(rs)
                    score_date = score_date or s.get("score_date")
                    rows_out.append((code, s))

            if not rows_out:
                print("  %s  nothing readable" % when)
                continue

            avg = sum(s["up_pct"] for _, s in rows_out) / len(rows_out)
            print("  %s  scored on %s  %4d funds  mean rising %5.1f%%"
                  % (when, score_date, len(rows_out), avg))

            if DRY:
                continue

            # Stored under the MONTH END asked for, not the score date it
            # happened to land on. The series has to be evenly spaced for
            # "three months running" to mean anything; the score date is
            # printed above so a large gap is still visible.
            for code, s in rows_out:
                readable = round(s["up_pct"] + s["sideways_pct"]
                                 + s["down_pct"], 1)
                cur.execute("""
                    INSERT INTO mf_fund_trend
                        (scheme_code, as_of_date, category,
                         up_pct, sideways_pct, down_pct, unscored_pct,
                         readable_pct)
                    VALUES (%(c)s, %(d)s, %(cat)s, %(u)s, %(s)s, %(dn)s,
                            %(x)s, %(r)s)
                    ON CONFLICT (scheme_code, as_of_date) DO UPDATE
                       SET category     = EXCLUDED.category,
                           up_pct       = EXCLUDED.up_pct,
                           sideways_pct = EXCLUDED.sideways_pct,
                           down_pct     = EXCLUDED.down_pct,
                           unscored_pct = EXCLUDED.unscored_pct,
                           readable_pct = EXCLUDED.readable_pct,
                           built_at     = now()
                """, {"c": code, "d": when, "cat": cat_of.get(code),
                      "u": s["up_pct"], "s": s["sideways_pct"],
                      "dn": s["down_pct"], "x": s["unscored_pct"],
                      "r": readable})
                written += 1
            conn.commit()

        print()
        if DRY:
            print("--dry-run: nothing written.")
            return
        print("rows written        : %d" % written)
        print("elapsed             : %.1fs" % (time.time() - started))

        h = _q(cur, """
            SELECT count(DISTINCT as_of_date) AS dates,
                   MIN(as_of_date) AS first, MAX(as_of_date) AS last
            FROM mf_fund_trend
        """, {}, one=True)
        print("history now holds   : %d month(s), %s to %s"
              % (h["dates"], h["first"], h["last"]))


if __name__ == "__main__":
    main()
