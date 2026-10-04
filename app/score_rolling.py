"""
score_rolling.py -- rolling returns and risk, per fund.
--------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/score_rolling.py
    /opt/mfapi/venv/bin/python3 /opt/mfapi/score_rolling.py --check
    /opt/mfapi/venv/bin/python3 /opt/mfapi/score_rolling.py --explain "Helios Mid Cap Fund"
    /opt/mfapi/venv/bin/python3 /opt/mfapi/score_rolling.py --limit 50

WHY THIS EXISTS
    score_returns.py says so itself, at the end of its own docstring:

        "It computes point-to-point returns, which swing on their
         endpoints. A fund measured to a market peak looks better than
         the same fund measured a month later. Rolling returns would be
         steadier; that is a later job, not this one."

    This is that job. A single 3-year number is one draw from a
    distribution. Rolling the same window forward month by month gives
    the whole distribution: what it returned on average, how bad the
    worst stretch was, and how often it beat what a goal needs.

WHAT IT COMPUTES, AND WHAT EACH IS FOR

  ROLLING RETURNS -- the same window, started on every month-end the
    fund has history for. Reported as average, median, worst, best, and
    the share of windows that cleared a hurdle. "This fund averaged 18%
    over 3-year stretches and its worst was -4%" is a different, more
    useful claim than "it returned 18% over the last 3 years".

  VOLATILITY -- the standard deviation of MONTHLY returns, annualised.
    How much the ride moves, not how far it went.

  MAX DRAWDOWN -- the deepest peak-to-trough fall in the window. The
    number that actually decides whether somebody stays invested; a
    standard deviation does not tell you that you were down 38% for
    fourteen months.

  SHARPE and SORTINO -- return per unit of risk. Sharpe divides by all
    volatility, Sortino by downside volatility only, which is the one
    investors actually mind. Both need a risk-free rate, and there is no
    honest default: RISK_FREE is stated below, stored on every row, and
    printed on every run, so a number computed against 6.5% can never be
    compared with one computed against 7% by accident.

WHY MONTH-ENDS AND NOT EVERY DAY
    Daily starts give ~250 windows a year instead of 12, at 250x the
    cost, and the extra windows overlap by all but one day -- they are
    not twenty times more evidence, they are the same evidence counted
    twenty times. Month-end starts are what the industry quotes and
    what the arithmetic can defend.

CONVENTIONS BORROWED FROM score_returns.py, NOT REINVENTED
    Nearest prior NAV with a MAX_GAP_DAYS limit, the same canonical fund
    universe, and the same refusal to compute a window the fund is too
    young for. Two files disagreeing about what a "3-year return" means
    would be worse than either being wrong on its own.
"""

import argparse
import math
import os
import sys
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Is /opt/mfapi/.env present?")

FUND_ALGO = os.getenv("MF_FUND_ALGO", "fund-v2")

# Same limit score_returns.py uses. A period boundary landing on a long
# holiday must not silently become a window measured from a fortnight out.
MAX_GAP_DAYS = 15

# The rolling windows, in years.
WINDOWS = [1, 3, 5]

# How far back to start windows from. Ten years of month-ends gives 85
# three-year windows, which is a distribution; three years gives one.
LOOKBACK_YEARS = 10

# A window needs enough observations to describe anything. Twelve
# month-end starts is a year of them; below that the "average rolling
# return" is a handful of overlapping numbers wearing a statistic's name.
MIN_WINDOWS = 12

# STATED, NOT ASSUMED. Roughly the 10-year government bond. Change it
# here, re-run, and every row carries the rate it was computed against.
RISK_FREE = float(os.getenv("MF_RISK_FREE", "6.5"))

# The hurdle the "how often did it clear this" figure counts against.
# Not advice and not a target -- a reference an equity goal commonly
# assumes, so that "cleared 12% in 78% of 3-year stretches" has a
# meaning the reader can check.
HURDLE = float(os.getenv("MF_ROLLING_HURDLE", "12.0"))

# Below this, a category median is not a peer group.
MIN_CATEGORY_FUNDS = 5


DDL = """
CREATE TABLE IF NOT EXISTS mf_rolling (
    scheme_code     text    NOT NULL,
    as_of_date      date    NOT NULL,
    window_years    int     NOT NULL,

    observations    int     NOT NULL,
    first_start     date,
    last_start      date,

    avg_cagr        numeric,
    median_cagr     numeric,
    worst_cagr      numeric,
    best_cagr       numeric,
    -- THE SHAPE, not just the ends. Worst -6, average 22, best 61
    -- describes a fund that lands near 22 almost every time and a fund
    -- that swings between -6 and 61 equally well, and they are not the
    -- same fund. p25 to p75 is where the middle half landed; p10 to p90
    -- is where nine in ten did. Four numbers instead of the 85 windows
    -- they summarise.
    p10_cagr        numeric,
    p25_cagr        numeric,
    p75_cagr        numeric,
    p90_cagr        numeric,
    -- Share of windows that ended above water, and above the hurdle.
    pct_positive    numeric,
    pct_above_hurdle numeric,
    hurdle_pct      numeric NOT NULL,

    -- Risk, over the same stretch the windows were drawn from.
    volatility      numeric,
    downside_vol    numeric,
    max_drawdown    numeric,
    sharpe          numeric,
    sortino         numeric,
    risk_free_pct   numeric NOT NULL,

    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code, as_of_date, window_years)
);

CREATE INDEX IF NOT EXISTS ix_mf_rolling_scheme
    ON mf_rolling (scheme_code, window_years, as_of_date DESC);

-- The peer number. Every figure above is meaningless on its own: a
-- Sharpe of 0.81 is neither good nor bad until you know the middle fund
-- in the same category is 0.62. Same reason mf_category_return exists
-- beside mf_returns.
CREATE TABLE IF NOT EXISTS mf_rolling_category (
    category        text NOT NULL,
    as_of_date      date NOT NULL,
    window_years    int  NOT NULL,
    funds           int  NOT NULL,
    avg_cagr        numeric,
    worst_cagr      numeric,
    -- The MEDIAN FUND'S percentiles, not the percentiles of every
    -- category window pooled. "The middle fund's bad stretch" is a fund
    -- that exists; a pooled tenth percentile is mostly a statement about
    -- how many funds the category happens to contain.
    p10_cagr        numeric,
    p25_cagr        numeric,
    p75_cagr        numeric,
    p90_cagr        numeric,
    pct_above_hurdle numeric,
    volatility      numeric,
    downside_vol    numeric,
    max_drawdown    numeric,
    sharpe          numeric,
    sortino         numeric,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (category, as_of_date, window_years)
);
"""

# MEDIANS, NOT MEANS, on every column.
#
# One fund that returned 60% pulls a category mean somewhere no fund
# actually sits. The median is a fund -- the middle one -- which is what
# "typical for this kind" has to mean if a distributor is going to say it
# out loud.
#
# MIN_CATEGORY_FUNDS guards the other end: the median of three funds is
# not a peer group, it is three funds, and printing it beside a fourth
# would dress an accident as a standard.
CATEGORY_ROLLUP = """
INSERT INTO mf_rolling_category
    (category, as_of_date, window_years, funds, avg_cagr, worst_cagr,
     p10_cagr, p25_cagr, p75_cagr, p90_cagr,
     pct_above_hurdle, volatility, downside_vol, max_drawdown,
     sharpe, sortino, updated_at)
SELECT vc.category, r.as_of_date, r.window_years, COUNT(*) AS funds,
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.avg_cagr)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.worst_cagr)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.p10_cagr)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.p25_cagr)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.p75_cagr)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.p90_cagr)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.pct_above_hurdle)::numeric, 1),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.volatility)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.downside_vol)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.max_drawdown)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.sharpe)::numeric, 2),
       ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY r.sortino)::numeric, 2),
       now()
FROM mf_rolling r
JOIN v_scheme_category vc ON vc.scheme_code = r.scheme_code
WHERE r.as_of_date = %(as_of)s
  AND vc.category IS NOT NULL
GROUP BY vc.category, r.as_of_date, r.window_years
HAVING COUNT(*) >= %(minf)s
ON CONFLICT (category, as_of_date, window_years) DO UPDATE SET
    funds = EXCLUDED.funds,
    avg_cagr = EXCLUDED.avg_cagr, worst_cagr = EXCLUDED.worst_cagr,
    p10_cagr = EXCLUDED.p10_cagr, p25_cagr = EXCLUDED.p25_cagr,
    p75_cagr = EXCLUDED.p75_cagr, p90_cagr = EXCLUDED.p90_cagr,
    pct_above_hurdle = EXCLUDED.pct_above_hurdle,
    volatility = EXCLUDED.volatility, downside_vol = EXCLUDED.downside_vol,
    max_drawdown = EXCLUDED.max_drawdown,
    sharpe = EXCLUDED.sharpe, sortino = EXCLUDED.sortino, updated_at = now()
"""

UPSERT = """
INSERT INTO mf_rolling
    (scheme_code, as_of_date, window_years, observations, first_start,
     last_start, avg_cagr, median_cagr, worst_cagr, best_cagr,
     p10_cagr, p25_cagr, p75_cagr, p90_cagr,
     pct_positive, pct_above_hurdle, hurdle_pct, volatility, downside_vol,
     max_drawdown, sharpe, sortino, risk_free_pct, updated_at)
VALUES (%(code)s, %(as_of)s, %(win)s, %(n)s, %(first)s, %(last)s,
        %(avg)s, %(med)s, %(worst)s, %(best)s,
        %(p10)s, %(p25)s, %(p75)s, %(p90)s, %(pos)s, %(hurd)s,
        %(hurdle_pct)s, %(vol)s, %(dvol)s, %(mdd)s, %(sharpe)s,
        %(sortino)s, %(rf)s, now())
ON CONFLICT (scheme_code, as_of_date, window_years) DO UPDATE SET
    observations = EXCLUDED.observations,
    first_start = EXCLUDED.first_start, last_start = EXCLUDED.last_start,
    avg_cagr = EXCLUDED.avg_cagr, median_cagr = EXCLUDED.median_cagr,
    worst_cagr = EXCLUDED.worst_cagr, best_cagr = EXCLUDED.best_cagr,
    p10_cagr = EXCLUDED.p10_cagr, p25_cagr = EXCLUDED.p25_cagr,
    p75_cagr = EXCLUDED.p75_cagr, p90_cagr = EXCLUDED.p90_cagr,
    pct_positive = EXCLUDED.pct_positive,
    pct_above_hurdle = EXCLUDED.pct_above_hurdle,
    hurdle_pct = EXCLUDED.hurdle_pct,
    volatility = EXCLUDED.volatility, downside_vol = EXCLUDED.downside_vol,
    max_drawdown = EXCLUDED.max_drawdown,
    sharpe = EXCLUDED.sharpe, sortino = EXCLUDED.sortino,
    risk_free_pct = EXCLUDED.risk_free_pct, updated_at = now()
"""

# v_fund_canonical ALREADY carries both things needed here: the canonical
# scheme code and the fund's name. The first version joined it back to
# mf_scheme on name and AMC and then required the code to match -- three
# joins to recover what the view had already decided, and it returned
# nothing at all.
#
# v_returns_source is still consulted, because a Growth sibling may hold
# the NAV history for an IDCW series and reading the wrong one would
# report a fund's payouts as losses.
UNIVERSE = """
SELECT c.canonical_scheme_code AS scheme_code,
       COALESCE(v.nav_scheme_code, c.canonical_scheme_code) AS nav_scheme_code,
       c.scheme_name
FROM v_fund_canonical c
LEFT JOIN v_returns_source v ON v.scheme_code = c.canonical_scheme_code
WHERE (%(like)s::text IS NULL OR c.scheme_name ILIKE %(like)s::text)
ORDER BY c.scheme_name
"""


def nav_series(cur, nav_code, since):
    """Every NAV on file for one fund, oldest first."""
    cur.execute("""
        SELECT nav_date, nav FROM mf_nav
        WHERE scheme_code = %(c)s AND nav_date >= %(s)s AND nav > 0
        ORDER BY nav_date
    """, {"c": nav_code, "s": since})
    return cur.fetchall()


def month_ends(rows):
    """One NAV per calendar month: the last one in it.

    Returned as a list of (date, nav). Built from the series rather than
    asked of the database per month, which would be one round trip per
    month per fund -- 120 queries a fund, 400,000 in a run."""
    out, seen = [], None
    for i, r in enumerate(rows):
        key = (r["nav_date"].year, r["nav_date"].month)
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if nxt is None or (nxt["nav_date"].year, nxt["nav_date"].month) != key:
            out.append((r["nav_date"], float(r["nav"])))
        seen = key
    return out


# How far the realised span may sit from the nominal window before the
# window is thrown away. Month-ends are 28-31 days apart, so a clean
# 3-year window lands within a few days of three years; 45 days catches
# a fund that stopped reporting for a month without discarding February.
MAX_SPAN_SLIP_DAYS = 45


def cagr(start_nav, end_nav, years):
    if start_nav <= 0 or end_nav <= 0 or years <= 0:
        return None
    return (math.pow(end_nav / start_nav, 1.0 / years) - 1.0) * 100.0


def pct(values, p):
    """Percentile of a sorted-able list, linear interpolation."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def stdev(values):
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def risk_figures(points):
    """Volatility, downside volatility and max drawdown, from monthly moves.

    Annualised by sqrt(12), the usual convention for monthly data.
    Drawdown is computed on the month-end series, so it understates an
    intra-month low -- stated here because a drawdown quoted without its
    sampling frequency is not a comparable number.
    """
    if len(points) < 13:
        return None, None, None

    rets = []
    for i in range(1, len(points)):
        prev, cur_ = points[i - 1][1], points[i][1]
        if prev > 0:
            rets.append(cur_ / prev - 1.0)
    if len(rets) < 12:
        return None, None, None

    sd = stdev(rets)
    vol = sd * math.sqrt(12) * 100 if sd is not None else None

    # Downside deviation: only the months that lost money count. A fund
    # that jumps 8% in a good month is not riskier for it, and Sharpe
    # says otherwise.
    downs = [r for r in rets if r < 0]
    dsd = None
    if len(downs) >= 2:
        dsd = math.sqrt(sum(r ** 2 for r in downs) / len(rets))
    dvol = dsd * math.sqrt(12) * 100 if dsd else None

    peak, mdd = points[0][1], 0.0
    for _d, v in points:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return vol, dvol, abs(mdd) * 100


def rolling_for(points, years, earliest_start=None):
    """Every window of `years` that fits, started on a month-end.

    MATCHED BY CALENDAR MONTH, NOT BY DATE ARITHMETIC.

    The first version built a target date -- start plus N years, day
    clamped to 28 -- and looked for the nearest NAV on or before it. For
    a start of 31 March that target was 28 March, which falls BEFORE the
    month-end it was meant to find, so the search fell back to the
    previous month-end 28 days earlier and the 15-day gap rule threw the
    window away. Only February starts, whose month-end is the 28th,
    survived: a decade of history collapsed to fourteen windows and the
    3- and 5-year figures vanished entirely.

    A month-end series wants a month index, not day arithmetic. The
    window from (2017, 3) ends at (2020, 3), whatever day either
    month-end happens to fall on, and the CAGR uses the real elapsed
    days between them.
    """
    idx = {(d.year, d.month): (d, v) for d, v in points}
    out = []
    for d, v in points:
        if earliest_start and d < earliest_start:
            continue
        end = idx.get((d.year + years, d.month))
        if not end or end[0] <= d:
            continue
        days = (end[0] - d).days
        # A gap in the NAV history can put a month-end far from where it
        # should be. Measured, not assumed.
        if abs(days - years * 365.25) > MAX_SPAN_SLIP_DAYS:
            continue
        g = cagr(v, end[1], days / 365.25)
        if g is not None:
            out.append((d, end[0], g))
    return out


def summarise(code, as_of, points, win):
    # Starts are capped to the last LOOKBACK_YEARS so every fund is
    # described over the same stretch. A 25-year-old fund averaged over
    # 25 years of windows is not comparable with a 12-year-old one
    # averaged over 12, and the page would put them in the same column.
    earliest = date(as_of.year - LOOKBACK_YEARS, as_of.month,
                    min(as_of.day, 28))
    rows = rolling_for(points, win, earliest)
    if len(rows) < MIN_WINDOWS:
        return None
    gs = [g for _s, _e, g in rows]
    vol, dvol, mdd = risk_figures(points)

    avg = sum(gs) / len(gs)
    # Sharpe and Sortino are both built on the AVERAGE ROLLING return,
    # not the latest one, so the ratio describes the same stretch the
    # distribution above it does.
    sharpe = (avg - RISK_FREE) / vol if vol else None
    sortino = (avg - RISK_FREE) / dvol if dvol else None

    return {
        "code": code, "as_of": as_of, "win": win,
        "n": len(rows),
        "first": rows[0][0], "last": rows[-1][0],
        "avg": round(avg, 2),
        "med": round(pct(gs, 0.5), 2),
        "worst": round(min(gs), 2),
        "best": round(max(gs), 2),
        # The shape between the ends. Computed from the same `gs` the
        # average and the worst come from, so the box and its whiskers
        # can never describe a different set of windows than the numbers
        # printed beside them.
        "p10": round(pct(gs, 0.10), 2),
        "p25": round(pct(gs, 0.25), 2),
        "p75": round(pct(gs, 0.75), 2),
        "p90": round(pct(gs, 0.90), 2),
        "pos": round(100.0 * sum(1 for g in gs if g > 0) / len(gs), 1),
        "hurd": round(100.0 * sum(1 for g in gs if g >= HURDLE) / len(gs), 1),
        "hurdle_pct": HURDLE,
        "vol": round(vol, 2) if vol is not None else None,
        "dvol": round(dvol, 2) if dvol is not None else None,
        "mdd": round(mdd, 2) if mdd is not None else None,
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "rf": RISK_FREE,
    }


def rollup(cur, conn, as_of):
    """The middle fund of each category, for every figure above."""
    cur.execute(CATEGORY_ROLLUP, {"as_of": as_of, "minf": MIN_CATEGORY_FUNDS})
    conn.commit()
    cur.execute("""
        SELECT count(*) AS n, count(DISTINCT category) AS cats
        FROM mf_rolling_category WHERE as_of_date = %(d)s
    """, {"d": as_of})
    r = cur.fetchone()
    print("\nPeer medians: %d rows across %d categories "
          "(categories with fewer than %d funds are left out)."
          % (r["n"], r["cats"], MIN_CATEGORY_FUNDS))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="compute and report, write nothing")
    ap.add_argument("--explain", help="one fund, printed in full")
    ap.add_argument("--limit", type=int, help="only this many funds")
    ap.add_argument("--date", help="as at this date (default: latest NAV)")
    ap.add_argument("--category-only", action="store_true",
                    help="recompute the peer medians from what is already "
                         "stored, without re-reading a single NAV")
    args = ap.parse_args()

    print("Rolling windows: %s years, over %d years of month-ends."
          % (", ".join(str(w) for w in WINDOWS), LOOKBACK_YEARS))
    print("Risk-free rate: %.2f%%   Hurdle: %.2f%%" % (RISK_FREE, HURDLE))
    print("Both are stored on every row, so numbers computed against "
          "different rates are never compared by accident.\n")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        if not args.check:
            cur.execute(DDL)
            conn.commit()

        # CREATE TABLE IF NOT EXISTS does not add a column to a table
        # that already exists, so on an established database the
        # percentile columns arrive only via add_rolling_percentiles.py.
        # Without this check the run reads every NAV for twenty minutes
        # and then dies on the first INSERT with UndefinedColumn, having
        # written nothing -- a traceback at the end of a long wait,
        # naming a column rather than the script that adds it.
        cur.execute("""
            SELECT count(*) AS n FROM information_schema.columns
            WHERE table_name = 'mf_rolling'
              AND column_name IN ('p10_cagr','p25_cagr','p75_cagr','p90_cagr')
        """)
        if cur.fetchone()["n"] < 4 and not args.check:
            sys.exit(
                "mf_rolling has no percentile columns yet, and this version\n"
                "writes them. Add them first:\n\n"
                "    /opt/mfapi/venv/bin/python3 "
                "/opt/mfapi/add_rolling_percentiles.py --admin doadmin\n\n"
                "Then run this again. Nothing has been read or written.")

        if args.category_only:
            cur.execute("SELECT MAX(as_of_date) AS d FROM mf_rolling")
            as_of = date.fromisoformat(args.date) if args.date \
                else cur.fetchone()["d"]
            if not as_of:
                sys.exit("mf_rolling is empty -- run without --category-only first.")
            rollup(cur, conn, as_of)
            return

        cur.execute("SELECT MAX(nav_date) AS d FROM mf_nav")
        latest = cur.fetchone()["d"]
        as_of = date.fromisoformat(args.date) if args.date else latest
        if not as_of:
            sys.exit("mf_nav is empty.")
        since = as_of - timedelta(days=int(365.25 * (LOOKBACK_YEARS + max(WINDOWS))) + 40)
        print("As at %s, reading NAVs from %s.\n" % (as_of, since))

        # Filtered in SQL, not in Python. --explain used to pull the whole
        # universe and then throw all but one row away, which is the slow
        # part of the run done in full to look at a single fund.
        print("Finding funds\u2026")
        cur.execute(UNIVERSE,
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

        if not funds:
            sys.exit("The universe query returned no funds. Check "
                     "v_fund_canonical has rows.")
        print("%d fund(s) to read.\n" % len(funds))

        written = skipped = 0
        for i, f in enumerate(funds, 1):
            rows = nav_series(cur, f["nav_scheme_code"], since)
            pts = [p for p in month_ends(rows) if p[0] <= as_of]
            if len(pts) < 13:
                skipped += 1
                continue

            for win in WINDOWS:
                r = summarise(str(f["scheme_code"]), as_of, pts, win)
                if not r:
                    continue
                if args.explain:
                    print("%s -- %dY rolling" % (f["scheme_name"], win))
                    print("  %d windows, %s to %s" % (r["n"], r["first"], r["last"]))
                    print("  average %s%%   median %s%%   worst %s%%   best %s%%"
                          % (r["avg"], r["med"], r["worst"], r["best"]))
                    print("  middle half %s%% to %s%%, nine in ten %s%% to %s%%"
                          % (r["p25"], r["p75"], r["p10"], r["p90"]))
                    print("  positive in %s%% of them, above %.0f%% in %s%%"
                          % (r["pos"], HURDLE, r["hurd"]))
                    print("  volatility %s%%   downside %s%%   worst fall %s%%"
                          % (r["vol"], r["dvol"], r["mdd"]))
                    print("  sharpe %s   sortino %s\n" % (r["sharpe"], r["sortino"]))
                elif not args.check:
                    cur.execute(UPSERT, r)
                written += 1

            if not args.check and i % 200 == 0:
                conn.commit()
                print("  %d/%d funds" % (i, len(funds)))

        if not args.check and not args.explain:
            conn.commit()

        if not args.check and not args.explain:
            rollup(cur, conn, as_of)

        print("\n%d rows %s, %d funds skipped for too little history."
              % (written, "computed" if (args.check or args.explain) else "written",
                 skipped))
        if args.check:
            print("--check: nothing was written.")


main()
