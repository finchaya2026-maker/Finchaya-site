"""
build_fund_capture.py -- up capture, down capture, information ratio
----------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/build_fund_capture.py
    --dry-run       compute and report, write nothing
    --years 3 5     which window lengths (default both)
    --explain NAME  one fund, printed in full, nothing written

WHAT THESE THREE ANSWER THAT A RETURN DOES NOT
    A fund that returned 14% while its index returned 12% has beaten it.
    That sentence hides the only question a client actually asks, which
    is: what happens to my money when the market falls?

    Two funds can both be 2% ahead over five years and have got there in
    opposite ways. One rides the good months harder than the index and
    falls just as hard in the bad ones. The other gives up some of the
    rise and loses much less on the way down. They have the same return
    and they are not the same product, and only the second one is
    sellable to somebody who will panic in a drawdown.

        up capture      of every 100 the index gained, the fund caught
                        this much
        down capture    of every 100 the index lost, the fund lost this
                        much
        information     how much excess return the manager produced per
          ratio         unit of the extra wobble they introduced doing it

    Down capture under 100 is the one most clients care about and the
    one most funds fail. Up capture over 100 with down capture over 100
    is leverage, not skill -- it is what a higher-beta portfolio does by
    construction, and it will be found out in the first bad year.

THE INFORMATION RATIO IS THE HONEST VERSION OF "IT BEAT THE INDEX"
    Excess return alone rewards a manager who took a wild swing and
    happened to land it. The information ratio divides that excess by
    the tracking error -- how much the fund's month-to-month path
    diverged from the index's -- so consistently-a-little-ahead scores
    above wildly-ahead-sometimes. It is the same instinct as Sortino
    over raw return, applied to the benchmark instead of the risk-free
    rate.

    A negative information ratio is not a rounding problem. It means
    the fund trailed its index over the window, and the magnitude says
    how firmly.

DEFINITIONS, STATED, BECAUSE VENDORS DISAGREE
    Monthly, on month-end NAV against the month-end index, paired by
    calendar month so the two sides always cover the same stretch.

    up capture   = annualised fund return over the months the index rose
                   / annualised index return over those same months,
                   x100. Geometric: the months are compounded and then
                   annualised by how many of them there were, which is
                   the Morningstar convention. An arithmetic mean of the
                   monthly ratios is a different and more flattering
                   number, and is not what a factsheet means.
    down capture = the same over the months the index fell. Both sides
                   are negative, so the ratio is positive, and a value
                   under 100 means the fund fell less.
    information  = (annualised fund return - annualised index return)
      ratio        over the whole window, divided by tracking error.
    tracking     = standard deviation of the monthly difference between
      error        fund and index, annualised by sqrt(12).

A RATIO NEEDS ENOUGH MONTHS OF ITS OWN KIND
    Down capture computed from three falling months is not a measure of
    anything. MIN_SIDE below is the floor: a window with fewer up months
    than that gets a NULL up capture, and likewise down, and the row is
    still written for whichever side does qualify. Absent rather than
    zero, for the same reason the rest of this codebase prefers it --
    "we could not measure it" and "it captured nothing" are different
    facts and a client reads them very differently.

WHY THERE IS NO ONE-YEAR ROW
    Twelve monthly returns split into ups and downs leaves perhaps four
    of one kind. Every vendor quotes capture over three and five years
    and none quotes it over one, for this reason. Adding a one-year
    column would put a number on the page that the page cannot stand
    behind.
"""

import argparse
import math
import os
import sys
from collections import defaultdict

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Is /opt/mfapi/.env present?")

# Three and five. See the docstring: one year cannot support a capture
# ratio and printing one would be a lie told in a small font.
DEFAULT_WINDOWS = [3, 5]

# The fewest up months (or down months) a capture ratio may be built on.
MIN_SIDE = 6

# How many of the window's months must actually be present on both
# sides. A 3-year window is 36 monthly returns; a fund that stopped
# reporting for two months still has a usable window, one missing half
# its months does not.
MIN_COVERAGE = 0.85

# Funds are processed in chunks -- seventeen million NAV rows will not
# fit in memory, and one query per fund would be thousands of round
# trips.
CHUNK = 200

DDL = """
CREATE TABLE IF NOT EXISTS fund_capture (
    scheme_code    text NOT NULL,
    window_years   int  NOT NULL,
    as_of_date     date NOT NULL,

    benchmark_id   text,
    bench_source   text,          -- 'scheme' or 'category'
    months         int  NOT NULL, -- paired monthly returns in the window
    up_months      int  NOT NULL,
    down_months    int  NOT NULL,

    up_capture     numeric,       -- NULL when up_months < the floor
    down_capture   numeric,
    information_ratio numeric,
    tracking_error numeric,
    -- How much of the fund's month-to-month movement the index
    -- accounts for, 0 to 100. The closet-index number: a fund at 97
    -- with an active fee is selling you an index at four times the
    -- price, and nothing else on a factsheet says so.
    r_squared      numeric,
    excess_cagr    numeric,       -- fund annualised less index annualised
    fund_cagr      numeric,
    bench_cagr     numeric,

    first_month    date,
    last_month     date,
    built_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code, window_years)
);
CREATE INDEX IF NOT EXISTS ix_capture_window
    ON fund_capture (window_years, down_capture);

-- For a database where fund_capture was built by an earlier version of
-- this file. CREATE TABLE IF NOT EXISTS does not add a column to a
-- table that already exists, and without this the first run after an
-- upgrade reads every NAV and then dies on the INSERT. The table is
-- created by this script and therefore owned by the site login, so
-- unlike mf_rolling it can ALTER its own table and needs no separate
-- migration run as the admin.
ALTER TABLE fund_capture ADD COLUMN IF NOT EXISTS r_squared numeric;
ALTER TABLE fund_capture_category ADD COLUMN IF NOT EXISTS r_squared numeric;

-- The peer number. A down capture of 92 is neither good nor bad until
-- you know the middle fund of the same category is 97. Same reason
-- mf_rolling_category exists beside mf_rolling.
CREATE TABLE IF NOT EXISTS fund_capture_category (
    category       text NOT NULL,
    window_years   int  NOT NULL,
    as_of_date     date NOT NULL,
    funds          int  NOT NULL,
    up_capture     numeric,
    down_capture   numeric,
    information_ratio numeric,
    tracking_error numeric,
    r_squared      numeric,
    built_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (category, window_years)
);
"""

# MEDIANS, NOT MEANS. One fund that captured 300% of a rise pulls a mean
# somewhere no fund actually sits; the median is a fund that exists.
CATEGORY_ROLLUP = """
INSERT INTO fund_capture_category
    (category, window_years, as_of_date, funds, up_capture, down_capture,
     information_ratio, tracking_error, r_squared, built_at)
SELECT vc.category, f.window_years, MAX(f.as_of_date), COUNT(*),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY f.up_capture)::numeric, 1),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY f.down_capture)::numeric, 1),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY f.information_ratio)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY f.tracking_error)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY f.r_squared)::numeric, 1),
       now()
FROM fund_capture f
JOIN v_scheme_category vc ON vc.scheme_code = f.scheme_code
WHERE vc.category IS NOT NULL
GROUP BY vc.category, f.window_years
HAVING COUNT(*) >= 5
ON CONFLICT (category, window_years) DO UPDATE SET
    as_of_date = EXCLUDED.as_of_date, funds = EXCLUDED.funds,
    up_capture = EXCLUDED.up_capture, down_capture = EXCLUDED.down_capture,
    information_ratio = EXCLUDED.information_ratio,
    tracking_error = EXCLUDED.tracking_error,
    r_squared = EXCLUDED.r_squared, built_at = now()
"""

UPSERT = """
INSERT INTO fund_capture
    (scheme_code, window_years, as_of_date, benchmark_id, bench_source,
     months, up_months, down_months, up_capture, down_capture,
     information_ratio, tracking_error, r_squared, excess_cagr,
     fund_cagr, bench_cagr, first_month, last_month, built_at)
VALUES (%(code)s, %(years)s, %(as_of)s, %(bid)s, %(src)s, %(months)s,
        %(up_n)s, %(dn_n)s, %(up)s, %(dn)s, %(ir)s, %(te)s, %(r2)s,
        %(excess)s, %(fund)s, %(bench)s, %(first)s, %(last)s, now())
ON CONFLICT (scheme_code, window_years) DO UPDATE SET
    as_of_date = EXCLUDED.as_of_date,
    benchmark_id = EXCLUDED.benchmark_id,
    bench_source = EXCLUDED.bench_source,
    months = EXCLUDED.months, up_months = EXCLUDED.up_months,
    down_months = EXCLUDED.down_months,
    up_capture = EXCLUDED.up_capture, down_capture = EXCLUDED.down_capture,
    information_ratio = EXCLUDED.information_ratio,
    tracking_error = EXCLUDED.tracking_error,
    r_squared = EXCLUDED.r_squared,
    excess_cagr = EXCLUDED.excess_cagr, fund_cagr = EXCLUDED.fund_cagr,
    bench_cagr = EXCLUDED.bench_cagr,
    first_month = EXCLUDED.first_month, last_month = EXCLUDED.last_month,
    built_at = now()
"""

# One NAV per month per fund -- the last trading day of each month.
MONTHLY_NAV = """
SELECT DISTINCT ON (scheme_code, date_trunc('month', nav_date))
       scheme_code, nav_date, nav
FROM mf_nav
WHERE scheme_code = ANY(%(codes)s) AND nav > 0
ORDER BY scheme_code, date_trunc('month', nav_date), nav_date DESC
"""

MONTHLY_INDEX = """
SELECT DISTINCT ON (benchmark_id, date_trunc('month', index_date))
       benchmark_id, index_date, index_value
FROM benchmark_nav
WHERE index_value > 0
ORDER BY benchmark_id, date_trunc('month', index_date), index_date DESC
"""

BENCH_MAP = """
SELECT DISTINCT ON (scheme_code) scheme_code, benchmark_id
FROM mf_benchmark_map
WHERE benchmark_id IS NOT NULL
ORDER BY scheme_code, benchmark_id
"""

# Only a minority of schemes carry their own mapping, so a per-scheme
# lookup alone leaves most of the universe unmeasurable for reasons that
# have nothing to do with the funds. Same fallback api.py and
# build_fund_consistency.py already use: the benchmark most of a
# category's mapped schemes point at, applied to the rest of it. Stored
# in bench_source so the page can say which kind it was.
BENCH_BY_CATEGORY = """
SELECT c.category, m.benchmark_id, COUNT(*) AS n
FROM mf_benchmark_map m
JOIN v_scheme_category c ON c.scheme_code = m.scheme_code
WHERE m.benchmark_id IS NOT NULL
GROUP BY 1, 2
ORDER BY c.category, n DESC
"""

SCHEME_CATEGORY = """
SELECT c.canonical_scheme_code AS scheme_code, vc.category
FROM v_fund_canonical c
JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
WHERE vc.category IS NOT NULL
"""

# v_returns_source is consulted because a Growth sibling may hold the NAV
# history for an IDCW series, and reading the wrong one reports a fund's
# payouts as losses.
FUNDS = """
SELECT c.canonical_scheme_code AS scheme_code,
       COALESCE(v.nav_scheme_code, c.canonical_scheme_code) AS nav_scheme_code,
       c.scheme_name
FROM v_fund_canonical c
LEFT JOIN v_returns_source v ON v.scheme_code = c.canonical_scheme_code
WHERE (%(like)s::text IS NULL OR c.scheme_name ILIKE %(like)s::text)
ORDER BY c.scheme_name
"""


def stdev(values):
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def r_squared(fund, bench):
    """How much of the fund's monthly movement the index accounts for.

    The square of the correlation between the two monthly series, as a
    percentage. 0 means the index explains nothing about where this
    fund went; 100 means every month's move was the index's move,
    scaled.

    WHY THIS AND NOT TRACKING ERROR
        They answer different questions and are routinely confused. A
        leveraged index fund has a large tracking error -- it moves
        much further than the index -- and an R-squared near 100,
        because it moves further in the SAME DIRECTION, every month.
        Tracking error measures how far apart they are; this measures
        whether they are doing the same thing.

        Which is why this is the number that exposes a closet index
        fund. A fund at 97 is selling index performance at an active
        management fee, and no figure on its factsheet says so.

    CORRELATION, NOT REGRESSION. For a single explanatory variable the
    two give the same R-squared, and the correlation form cannot
    silently mis-handle a zero-variance series -- it returns None
    instead, which is what a flat month-end series deserves.
    """
    n = len(fund)
    if n < 12:
        return None
    mf = sum(fund) / n
    mb = sum(bench) / n
    cov = sum((f - mf) * (b - mb) for f, b in zip(fund, bench))
    vf = sum((f - mf) ** 2 for f in fund)
    vb = sum((b - mb) ** 2 for b in bench)
    if vf <= 0 or vb <= 0:
        return None
    r = cov / math.sqrt(vf * vb)
    # Floating point can push a perfect correlation a hair past 1.
    r = max(-1.0, min(1.0, r))
    return round(r * r * 100, 1)


def annualise(returns):
    """Compound a list of monthly returns and annualise by their count.

    GEOMETRIC, NOT ARITHMETIC. Compounding +50% then -50% is -25%, and
    the mean of the two is 0%. On the down-capture months especially,
    the arithmetic version flatters every fund in the universe.

    Returns None if compounding drives the value to or below zero, which
    a monthly series cannot legitimately do -- that is corrupt data, not
    a fund that lost everything.
    """
    if not returns:
        return None
    acc = 1.0
    for r in returns:
        acc *= (1.0 + r)
    if acc <= 0:
        return None
    return acc ** (12.0 / len(returns)) - 1.0


def monthly_returns(series, index_series):
    """Paired (month, fund return, index return), oldest first.

    PAIRED BY CALENDAR MONTH, not by position. A fund missing March and
    an index that is not would otherwise be compared April-to-fund-March
    from that point on, and every figure after the gap would be built on
    a one-month offset that nothing on the page would reveal.
    """
    out = []
    for i in range(1, len(series)):
        d_prev, n_prev = series[i - 1]
        d_cur, n_cur = series[i]
        # Consecutive calendar months only. A gap means the return
        # across it is a two- or three-month move, and pairing it with
        # one month of index is a category error.
        months_apart = ((d_cur.year - d_prev.year) * 12
                        + d_cur.month - d_prev.month)
        if months_apart != 1 or n_prev <= 0:
            continue
        i_prev = index_series.get(d_prev.replace(day=1))
        i_cur = index_series.get(d_cur.replace(day=1))
        if not i_prev or not i_cur or i_prev <= 0:
            continue
        out.append((d_cur, n_cur / n_prev - 1.0, i_cur / i_prev - 1.0))
    return out


def capture(pairs, years):
    """Every figure for one fund over one window, or None.

    `pairs` is the whole paired history; the window is the most recent
    years*12 months of it.
    """
    want = years * 12
    win = pairs[-want:]
    if len(win) < want * MIN_COVERAGE:
        return None

    fr = [f for _d, f, _b in win]
    br = [b for _d, _f, b in win]

    fund = annualise(fr)
    bench = annualise(br)
    if fund is None or bench is None:
        return None

    # TRACKING ERROR on the monthly DIFFERENCE, not the difference of the
    # two volatilities. A fund and an index can each swing 18% a year and
    # track each other perfectly or not at all; subtracting the two
    # numbers cannot tell those apart, and taking the deviation of the
    # difference can.
    diff = [f - b for f, b in zip(fr, br)]
    sd = stdev(diff)
    te = sd * math.sqrt(12) * 100 if sd is not None else None

    excess = (fund - bench) * 100
    ir = round(excess / te, 2) if te and te > 0 else None

    ups = [(f, b) for _d, f, b in win if b > 0]
    dns = [(f, b) for _d, f, b in win if b < 0]

    def side(rows):
        if len(rows) < MIN_SIDE:
            return None
        f = annualise([r[0] for r in rows])
        b = annualise([r[1] for r in rows])
        if f is None or b is None or b == 0:
            return None
        return round(100.0 * f / b, 1)

    return {
        "years": years, "months": len(win),
        "up_n": len(ups), "dn_n": len(dns),
        "up": side(ups), "dn": side(dns),
        "ir": ir,
        "te": round(te, 2) if te is not None else None,
        "r2": r_squared(fr, br),
        "excess": round(excess, 2),
        "fund": round(fund * 100, 2), "bench": round(bench * 100, 2),
        "first": win[0][0], "last": win[-1][0],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and report, write nothing")
    ap.add_argument("--years", nargs="*", type=int,
                    help="window lengths (default 3 5)")
    ap.add_argument("--explain", help="one fund, printed in full")
    ap.add_argument("--limit", type=int, help="only this many funds")
    args = ap.parse_args()

    windows = args.years or DEFAULT_WINDOWS
    dry = args.dry_run or bool(args.explain)

    print("Capture and information ratio over %s years."
          % ", ".join(str(w) for w in windows))
    print("Monthly, month-end NAV against month-end index, paired by "
          "calendar month.")
    print("A side with fewer than %d months of its own is left NULL.\n"
          % MIN_SIDE)

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        if not dry:
            cur.execute(DDL)
            conn.commit()

        cur.execute("SELECT MAX(nav_date) AS d FROM mf_nav")
        as_of = cur.fetchone()["d"]
        if not as_of:
            sys.exit("mf_nav is empty.")
        print("As at %s." % as_of)

        cur.execute(BENCH_MAP)
        own_bench = {str(r["scheme_code"]): r["benchmark_id"]
                     for r in cur.fetchall()}
        cur.execute(BENCH_BY_CATEGORY)
        cat_bench = {}
        for r in cur.fetchall():
            cat_bench.setdefault(r["category"], r["benchmark_id"])
        cur.execute(SCHEME_CATEGORY)
        category = {str(r["scheme_code"]): r["category"] for r in cur.fetchall()}

        print("schemes with their own benchmark: %d" % len(own_bench))
        print("categories with a modal benchmark: %d" % len(cat_bench))

        cur.execute(MONTHLY_INDEX)
        idx = defaultdict(dict)
        for r in cur.fetchall():
            idx[r["benchmark_id"]][r["index_date"].replace(day=1)] = \
                float(r["index_value"])
        print("benchmark series: %d\n" % len(idx))

        cur.execute(FUNDS,
                    {"like": ("%" + args.explain + "%") if args.explain else None})
        funds = cur.fetchall()
        if args.explain:
            if not funds:
                sys.exit("No fund whose name contains %r." % args.explain)
            if len(funds) > 1:
                print("%d funds match; taking the first:" % len(funds))
                for f in funds[:6]:
                    print("   %s" % f["scheme_name"])
            funds = funds[:1]
        elif args.limit:
            funds = funds[:args.limit]
        print("%d fund(s) to read.\n" % len(funds))

        written = 0
        no_bench = short = thin = 0
        for start_i in range(0, len(funds), CHUNK):
            chunk = funds[start_i:start_i + CHUNK]
            cur.execute(MONTHLY_NAV,
                        {"codes": [f["nav_scheme_code"] for f in chunk]})
            series = defaultdict(list)
            for r in cur.fetchall():
                series[str(r["scheme_code"])].append(
                    (r["nav_date"], float(r["nav"])))

            for f in chunk:
                code = str(f["scheme_code"])
                s = series.get(str(f["nav_scheme_code"]))
                if not s or len(s) < 13:
                    short += 1
                    continue
                bid = own_bench.get(code) or cat_bench.get(category.get(code))
                if not bid or bid not in idx:
                    no_bench += 1
                    continue
                src = "scheme" if own_bench.get(code) else "category"
                pairs = monthly_returns(s, idx[bid])

                for years in windows:
                    r = capture(pairs, years)
                    if not r:
                        thin += 1
                        continue
                    r.update(code=code, as_of=as_of, bid=bid, src=src)

                    if args.explain:
                        print("%s -- %dY" % (f["scheme_name"], years))
                        print("  index %s (%s mapping), %d paired months "
                              "%s to %s" % (bid, src, r["months"],
                                            r["first"], r["last"]))
                        print("  fund %.2f%%  index %.2f%%  excess %.2f%%"
                              % (r["fund"], r["bench"], r["excess"]))
                        print("  up capture %s over %d rising months"
                              % (r["up"] if r["up"] is not None
                                 else "-- (too few)", r["up_n"]))
                        print("  down capture %s over %d falling months"
                              % (r["dn"] if r["dn"] is not None
                                 else "-- (too few)", r["dn_n"]))
                        print("  tracking error %s%%   information ratio %s"
                              % (r["te"], r["ir"]))
                        print("  the index explains %s%% of its movement\n"
                              % (r["r2"] if r["r2"] is not None else "--"))
                    elif not dry:
                        cur.execute(UPSERT, r)
                    written += 1

            if not dry and start_i % (CHUNK * 5) == 0:
                conn.commit()
            print("  reading %d/%d" % (min(start_i + CHUNK, len(funds)),
                                       len(funds)), end="\r")
        print(" " * 44, end="\r")

        if not dry:
            conn.commit()
            cur.execute(CATEGORY_ROLLUP)
            conn.commit()
            cur.execute("SELECT count(*) AS n, count(DISTINCT category) AS c "
                        "FROM fund_capture_category")
            r = cur.fetchone()
            print("Peer medians: %d rows across %d categories."
                  % (r["n"], r["c"]))

        print("\n%d row(s) %s." % (written, "computed" if dry else "written"))
        print("  %d fund(s) with no benchmark to measure against" % no_bench)
        print("  %d fund(s) with too little NAV history" % short)
        print("  %d window(s) skipped for too few paired months" % thin)
        if dry:
            print("\nNothing was written.")


# Guarded, unlike its neighbours, so that test_fund_capture.py can import
# annualise() and capture() and check the arithmetic against cases worked
# out by hand. A bare main() call would make importing this file try to
# read every NAV in the database.
if __name__ == "__main__":
    main()
