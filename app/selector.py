"""
selector.py -- choosing a set of funds that lands on a target exposure.
------------------------------------------------------------------------
Mounted in api.py:

    from selector import router as selector_router
    app.include_router(selector_router)

WHY THIS IS A SET PROBLEM AND NOT A LOOKUP
    The allocation says where money should END UP, measured through to the
    shares. So the question is never "which fund is best for the mid cap
    row" -- it is "which small set of funds, taken together, lands near the
    target without colliding with itself".

    One fund per bucket would be the wrong shape and would produce worse
    portfolios: a flexi cap and a small cap fund can cover large, mid and
    small between them, more cheaply and with fewer moving parts than three
    dedicated funds.

FOUR STAGES

    1. ELIGIBILITY   a hard gate, not a score. A fund is in or out, and the
                     reason is a sentence. This is also where funds nobody
                     can buy are removed -- closed-ended series and the like.

    2. SHORTLIST     rank WITHIN a role, never across the universe. Funds
                     are only comparable against funds doing the same job:
                     a small cap fund and a liquid fund cannot be ranked
                     against each other, and any score that claims to is
                     measuring category, not quality.

    3. SEARCH        every combination of the allowed size, scored as a SET
                     on how close its combined exposure comes to the target.
                     Overlap and concentration are constraints here, not
                     footnotes: two funds holding the same forty companies
                     produce a portfolio far less diversified than it looks.

    4. PRESENT       three sets, with their gaps stated. Not one.
                     A single answer invites acceptance; three invite the
                     judgement the distributor is being paid for.

WHAT THIS DOES NOT DO
    It does not recommend. It produces a shortlist for a distributor to
    review, reject or override, and it shows its working at every step so
    that reviewing is possible. Nothing here is shown to a client.
"""

from itertools import combinations
from typing import List, Optional

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from portfolio_api import (DB, _q, TREND_BATCH, _trend_summary,
                           _scores_on, apply_fund_filters)
from saved_portfolio_api import _user_id

router = APIRouter(prefix="/api/selector", tags=["selector"])

# The count comes from the MONEY, not from the number of buckets. Splitting
# 3,000 rupees across five funds is 600 each -- below several AMC minimums
# and operationally daft. Above five, the extra fund adds paperwork and
# almost no diversification, because by then the holdings overlap anyway.
def fund_count_for(sip_amount: float) -> int:
    if sip_amount < 5000:
        return 2
    if sip_amount <= 15000:
        return 3
    return 4 if sip_amount <= 40000 else 5


# A shortlist wider than this makes the search slow without making it
# better: the funds ranked 8th in a role are rarely in a winning set.
SHORTLIST_PER_ROLE = 7
# Above this, two funds are substantially the same holding.
MAX_PAIR_OVERLAP = 35.0
def max_one_fund(n: int) -> int:
    """The most any single fund may carry, given how many there are.

    A flat cap does not work. Two funds summing to 100 ALWAYS put at least
    50 in one, so a flat 45 made every two-fund weighting invalid and a
    sub-5,000 SIP would have returned nothing -- silently, since the search
    would simply find no valid set.

    So it scales: tight where there is room to spread, loose where the
    arithmetic forbids it.
    """
    return {2: 60, 3: 50}.get(n, 45)
# A rolling-window record shorter than this is a sample rather than a
# record. Overridable per request, because the right answer is a judgement:
# 36 windows excluded ITI Mid Cap at 31 windows with a 96.8% beat rate,
# which is a real fund lost to a threshold I picked. Set it to 0 to allow
# funds with no rolling history at all -- which is a different decision
# again, and one worth making deliberately.
MIN_WINDOWS = 36

# How a fund is ranked within its role. Three different questions, and
# reasonable people weight them differently:
#
#   consistency  how reliably it has beaten its peers over rolling windows.
#                Slow to move, hard to fake, and the best guide to a manager
#                rather than a market.
#   returns      what it actually delivered over the goal's horizon. A fund
#                that is consistently mediocre is not the goal.
#   rising       what share of the shares it holds TODAY are trending up.
#                Measurable now, before any return series reflects it -- a
#                reading of what the manager currently owns rather than of
#                what the market did to them.
#
# Defaults lean on consistency because it is the one least likely to be
# noise. Recent returns select for whatever style has just run, and the
# trend split is a few months of price behaviour. Both belong; neither
# should decide alone.
DEFAULT_WEIGHTS = {"consistency": 0.30, "benchmark": 0.25,
                   "category": 0.25, "rising": 0.20}

# Consistency is read over BOTH windows, not one.
#
# A fund strong over three years and weak over one is losing its way; one
# strong over one year and weak over three has had a good spell. Neither is
# visible from a single window, and averaging the two is the cheapest way
# to see both at once.
WINDOWS_JUDGED = (1, 3)

# Categories that do not belong in a core allocation, however well their
# cap mix happens to fit.
#
# A sector or thematic fund is a bet on one industry. Its mid cap share
# tells you where those companies sit, not what the fund is. Selecting a
# pharma fund to fill a mid cap row would be matching the arithmetic and
# missing the point entirely -- and it is the same reason a placing is not
# shown for these categories anywhere else in this product.
#
# ELSS carries a three-year lock-in, which is a decision about liquidity
# rather than allocation, and belongs only in a goal whose purpose is tax.
EXCLUDED_CATEGORIES = (
    "Sectoral/Thematic", "ELSS", "ETFs", "Index Funds",
    "FoF Domestic", "FoF Overseas", "Gold ETF",
)
# Combined top-ten weight above this is a concentrated portfolio whatever
# the fund count says.
MAX_TOP10 = 45.0

EQUITY_BUCKETS = ("large", "mid", "small", "international")

# Eligibility.
#
# Every condition is a fact about whether we can MEASURE the fund, not a
# judgement about whether it is good:
#   - a portfolio we hold, or its exposure is unknown
#   - a consistency record, or its reliability is unknown
#   - an open-ended scheme, or nobody can buy it
#
# The "series" pattern catches closed-ended tranches -- fixed maturity
# plans and fixed horizon funds, 1,031 of them in the canonical view. No
# open-ended equity fund is called "Series 6".
ELIGIBLE = """
SELECT c.canonical_scheme_code AS scheme_code,
       c.scheme_name, c.amc_name, vc.category,
       p.equity_pct, p.large_pct, p.mid_pct, p.small_pct,
       p.international_pct, p.unclassified_pct, p.top10_pct,
       p.holding_count, p.drifted, p.mandate_bucket,
       p.mandate_floor, p.mandate_actual,
       f.beat_pct, f.cat_beat_pct, f.worst_excess, f.median_cagr,
       f.windows_total, f.cat_windows, f.bench_source,
       -- Returns and risk, fetched here so the screen filters can be
       -- applied to the WHOLE universe before the combination search
       -- runs -- which is the point of narrowing rather than screening.
       -- Pulled as columns rather than as WHERE conditions on purpose:
       -- filtering in Python lets each filter report how many funds it
       -- removed, and "no fund survived" is then a sentence naming the
       -- filter rather than an empty list.
       ret.excess_cagr, ret.fund_cagr AS period_cagr,
       ret.category_rank, ret.category_count,
       risk.sharpe, risk.sortino, risk.volatility, risk.max_drawdown
FROM v_fund_canonical c
JOIN fund_profile p ON p.scheme_code = c.canonical_scheme_code
LEFT JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
LEFT JOIN fund_consistency f ON f.scheme_code = c.canonical_scheme_code
                            AND f.window_years = 3
-- LATERAL rather than a join to a MAX() subquery: one indexed lookup per
-- fund, taking that fund's newest row, which is what the index on
-- (scheme_code, ..., as_of_date DESC) is built for.
LEFT JOIN LATERAL (
    SELECT r.excess_cagr, r.fund_cagr, r.category_rank, r.category_count
    FROM mf_returns r
    WHERE r.scheme_code = c.canonical_scheme_code
      AND r.period = %(rperiod)s
    ORDER BY r.as_of_date DESC
    LIMIT 1
) ret ON true
LEFT JOIN LATERAL (
    SELECT g.sharpe, g.sortino, g.volatility, g.max_drawdown
    FROM mf_rolling g
    WHERE g.scheme_code = c.canonical_scheme_code
      AND g.window_years = %(window)s
    ORDER BY g.as_of_date DESC
    LIMIT 1
) risk ON true
WHERE c.scheme_name !~* '(series|fixed maturity|fixed horizon|fixed term|interval)'
  AND p.holding_count >= 15
  AND p.equity_pct > 20
"""


# How much two funds are actually holding the same companies.
#
# Overlap is measured stock by stock as the SMALLER of the two weights --
# if fund A holds 8% HDFC Bank and fund B holds 5%, they share 5%, not 8%
# and not 13%. Summed across every ISIN both funds hold, that is the
# share of the money that is buying the same thing twice.
#
# WHY THREE QUERIES AND A PYTHON LOOP RATHER THAN ONE SQL STATEMENT
#     Because the date to compare on is a property of the PAIR, not of the
#     basket. Fund A may have published August and fund B only July; the
#     only date on which both funds are known is July, and it differs for
#     every pair in the basket. A single query would have to work that out
#     per pair inside itself, and the result would be unreadable.
#
#     A basket holds a handful of funds, so this is a few dozen pairs at
#     most. Clear beats clever at that size.
PAIR_LATEST = """
SELECT scheme_code, MAX(as_of_date) AS d
FROM mf_holding
WHERE scheme_code = ANY(%(codes)s)
GROUP BY scheme_code
"""

# Each fund's newest disclosure AT OR BEFORE each date any pair needs.
PAIR_ASOF = """
SELECT h.scheme_code, t.target, MAX(h.as_of_date) AS d
FROM mf_holding h
CROSS JOIN (SELECT unnest(%(targets)s::date[]) AS target) t
WHERE h.scheme_code = ANY(%(codes)s)
  AND h.as_of_date <= t.target
GROUP BY 1, 2
"""

PAIR_HOLDINGS = """
SELECT scheme_code, as_of_date, isin, pct_of_nav
FROM mf_holding
WHERE scheme_code = ANY(%(codes)s)
  AND as_of_date = ANY(%(dates)s)
  AND isin IS NOT NULL AND pct_of_nav IS NOT NULL
"""


def pairwise_overlap(cur, codes):
    """Overlap for every pair, each measured on a date BOTH funds published.

    WHY NOT EACH FUND'S OWN NEWEST PORTFOLIO
        Because that compares across time and calls the result a fact
        about two funds. If A has filed August and B has only filed July,
        then A's August against B's July counts a month of ordinary
        trading as a difference between the two funds -- and it asserts
        something about B's August that B has not disclosed.

        This is not hypothetical here. Measured on this database: 115 of
        the 1,109 comparable equity funds sit a month behind, whole fund
        houses at a time -- Bandhan, DSP, UTI, Quantum, Bank of India all
        file late together. Across 2,971 real pairs the two methods
        disagreed on 30.7% of them, by up to 13.35 points. Tata Balanced
        Advantage against Bandhan Large Cap read 23.25% one way and 9.90%
        the other.

        Only one of those is a statement anybody can check. 9.90% is what
        the two funds actually held on the same day. 23.25% is a guess
        about a portfolio Bandhan has not published yet.

        It also makes this agree with the fund compare tab, which has
        always aligned. The same pair showing two different numbers on two
        pages was the worse bug of the two.

    Returns (overlap, as_of): {(a, b): pct} and {(a, b): date}, both keyed
    with the lower code first, and the date being what the pair was
    measured on so the page can say so when it is not the newest.
    """
    codes = [str(c) for c in codes]
    if len(codes) < 2:
        return {}, {}

    latest = {str(r["scheme_code"]): r["d"]
              for r in _q(cur, PAIR_LATEST, {"codes": codes}) if r["d"]}
    if len(latest) < 2:
        return {}, {}

    pairs = [(a, b) for i, a in enumerate(sorted(latest))
             for b in sorted(latest)[i + 1:]]
    targets = sorted({min(latest[a], latest[b]) for a, b in pairs})

    # (fund, target) -> the disclosure to read for that fund at that date
    asof = {(str(r["scheme_code"]), r["target"]): r["d"]
            for r in _q(cur, PAIR_ASOF,
                        {"codes": codes, "targets": targets}) if r["d"]}

    dates = sorted({d for d in asof.values()})
    if not dates:
        return {}, {}

    held = {}
    for r in _q(cur, PAIR_HOLDINGS, {"codes": codes, "dates": dates}):
        held.setdefault((str(r["scheme_code"]), r["as_of_date"]), {})[
            r["isin"]] = float(r["pct_of_nav"])

    overlap, used = {}, {}
    for a, b in pairs:
        target = min(latest[a], latest[b])
        da, db = asof.get((a, target)), asof.get((b, target))
        if not da or not db:
            continue
        ha, hb = held.get((a, da)), held.get((b, db))
        if not ha or not hb:
            continue
        small, big = (ha, hb) if len(ha) <= len(hb) else (hb, ha)
        overlap[(a, b)] = round(
            sum(min(w, big[i]) for i, w in small.items() if i in big), 2)
        # The later of the two, which is the date the comparison is really
        # "as of" -- both funds are read at or before it.
        used[(a, b)] = max(da, db)
    return overlap, used


# The fund's own return over the window being judged, and its placing
# among funds doing the same job.
CANDIDATE_RETURNS = """
WITH latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d
    FROM mf_returns WHERE scheme_code = ANY(%(codes)s)
    GROUP BY scheme_code
)
SELECT r.scheme_code, r.period, ROUND(r.fund_cagr, 2) AS fund_cagr
FROM mf_returns r
JOIN latest l ON l.scheme_code = r.scheme_code AND l.d = r.as_of_date
WHERE r.period = %(period)s AND r.fund_cagr IS NOT NULL
"""


# Beat rates over every window we judge, per fund.
CONSISTENCY = """
SELECT scheme_code, window_years, beat_pct, cat_beat_pct,
       windows_total, cat_windows
FROM fund_consistency
WHERE scheme_code = ANY(%(codes)s) AND window_years = ANY(%(windows)s)
"""

# What the average fund in a category currently has rising. The yardstick
# for "above average", so the test is against the fund's own peers rather
# than an absolute number that means different things in different markets.
CATEGORY_RISING = """
SELECT category, up_pct, fund_count
FROM mf_category_trend t
WHERE as_of_date = (SELECT MAX(as_of_date) FROM mf_category_trend)
"""


def _percentile(values):
    """Rank a set of numbers 0-100 within itself.

    Components are only comparable after this. A beat rate is already a
    percentage; a CAGR of 18% and a rising share of 62% are not on any
    shared scale, and adding them raw would weight whichever happens to
    have the larger numbers. Ranking within the ROLE also means a fund is
    only ever measured against funds doing the same job.
    """
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ordered)
    if n <= 1:
        return {k: 50.0 for k in values}
    return {k: 100.0 * i / (n - 1) for i, (k, _v) in enumerate(ordered)}


def _role_of(f) -> Optional[str]:
    """Which bucket a fund mostly serves.

    Decided by WHAT IT HOLDS, not by what it is called. A flexi cap fund
    holding 90% large caps serves the large role whatever the label says,
    and a mid cap fund that has drifted into large caps is honestly a
    large-tilted fund. This is the whole reason the profile is measured.
    """
    mix = {"large": float(f["large_pct"] or 0), "mid": float(f["mid_pct"] or 0),
           "small": float(f["small_pct"] or 0),
           "international": float(f["international_pct"] or 0)}
    top = max(mix, key=mix.get)
    return top if mix[top] >= 40 else None


def _quality(f) -> float:
    """A score used ONLY to rank funds within one role.

    Deliberately not exposed and never compared across roles. Consistency
    against the category leads, because "did this beat the alternatives I
    could have chosen" is the question a distributor faces; the benchmark
    figure supports it; the worst window is a penalty, because a fund a
    client sells during a bad stretch has failed regardless of its average.
    """
    cat = float(f["cat_beat_pct"] or 50.0)
    bench = float(f["beat_pct"]) if f["beat_pct"] is not None else None
    worst = float(f["worst_excess"]) if f["worst_excess"] is not None else None

    score = cat
    if bench is not None:
        score = 0.65 * cat + 0.35 * bench
    if worst is not None and worst < 0:
        # A fund that was once ten points behind its index over a full
        # three years is a different proposition from one never worse than
        # two, even at the same beat rate.
        score += max(-15.0, worst / 2.0)
    if f["drifted"]:
        # Not disqualifying -- it may still be the right fund -- but it is
        # not what its name says.
        #
        # Five points was far too gentle. A fund holding 63% mid against a
        # 65% floor was chosen BECAUSE its extra large cap helped hit a
        # large target, while being the worst fund in the set on every
        # quality measure. The search was treating a breach of mandate as a
        # feature. It has to cost more than the fit it buys.
        score -= 20.0
    return score


def _set_exposure(funds, weights):
    """Combined exposure of a weighted set, as shares of the whole pot.

    Each fund contributes its own cap mix scaled by its equity share, so a
    fund holding 30% cash contributes 30% less equity than its label
    implies. What is left over lands in debt -- which is what cash in an
    equity fund actually is.
    """
    out = {b: 0.0 for b in EQUITY_BUCKETS}
    out["debt"] = 0.0
    for f, w in zip(funds, weights):
        eq = float(f["equity_pct"] or 0) / 100.0
        for b in EQUITY_BUCKETS:
            out[b] += w * eq * float(f[b + "_pct"] or 0)
        # Unclassified equity is still equity; it is counted where it can
        # be seen and not smuggled into debt.
        out["debt"] += w * (1 - eq) * 100.0
    return out


def _fit_gap(exposure, target):
    """How far OUTSIDE the allowed band a set sits, in percentage points.

    Distance from the target is the obvious measure and it is the wrong
    one. Set a large cap target of 0 and every mid cap fund is penalised
    for the 6% large it inevitably holds, so the search drifts toward the
    most extreme funds to avoid exposure nobody objected to.

    The rules already say what is acceptable: a floor and a ceiling per
    bucket. "Target 0, ceiling 10" means do not allocate any, but up to 10%
    arriving incidentally is fine. Inside the band costs nothing.

    Distance from target survives as a small secondary term, so among sets
    that all sit inside their bands the one closest to what was actually
    asked for still wins.
    """
    outside, drift = 0.0, 0.0
    for b, spec in target.items():
        got = exposure.get(b, 0.0)
        lo = spec["min"] if spec["min"] is not None else spec["target"]
        hi = spec["max"] if spec["max"] is not None else spec["target"]
        if got < lo:
            outside += lo - got
        elif got > hi:
            outside += got - hi
        drift += abs(got - spec["target"])
    return outside + 0.15 * drift


def _weights_for(funds, target):
    """How much to put in each fund.

    A coarse grid in steps of 5%, which is what an adviser would actually
    write on a form -- nobody sets a SIP at 37.4%. Searching a grid also
    avoids pretending to an optimisation precision the inputs do not
    support: the exposure itself is a month-old disclosure.
    """
    n = len(funds)
    best, best_gap = None, float("inf")
    step = 5

    def walk(i, left, acc):
        nonlocal best, best_gap
        if i == n - 1:
            combo = acc + [left]
            if min(combo) < 10:      # a 5% sleeve is not worth its paperwork
                return
            if max(combo) > max_one_fund(n):
                return
            w = [x / 100.0 for x in combo]
            gap = _fit_gap(_set_exposure(funds, w), target)
            if gap < best_gap:
                best, best_gap = combo, gap
            return
        for x in range(10, left - 10 * (n - i - 1) + 1, step):
            walk(i + 1, left - x, acc + [x])

    walk(0, 100, [])
    return best, best_gap


class SelectRequest(BaseModel):
    purpose: str
    years: float
    risk: str = "moderate"
    sip_amount: float = 10000
    window_years: int = 3
    exclude: List[str] = []
    # Off by default, and deliberately awkward to turn on: a thematic fund
    # in a core allocation should be a decision someone made, not something
    # the search slipped in because the numbers worked.
    allow_thematic: bool = False
    # Your judgement, not mine. Raise consistency to favour long records,
    # raise rising to favour what the holdings are doing now.
    weights: dict = {}
    # 0 admits funds with no rolling record at all -- new funds, which may
    # be excellent and cannot yet be shown to be.
    min_windows: int = MIN_WINDOWS

    # Which return window a fund is JUDGED on, separately from which
    # rolling-window table its consistency comes from.
    #
    # These were the same field, and that was wrong: consistency exists
    # only for 3Y and 5Y windows, so asking to be judged on one-year
    # returns silently destroyed the consistency lookup. A fund ranking
    # 82nd percentile on 3Y while being last over 1Y then looked strong,
    # because the window being scored was not the window in mind.
    return_period: str = "3Y"          # 1Y, 3Y, 5Y, 10Y

    # HARD FLOORS. Weights shift the ranking; a floor removes a fund.
    #
    # A weight of 0.2 on consistency cannot stop a fund with a 30% beat
    # rate arriving on the strength of recent returns -- and sometimes the
    # answer is not "rank it lower" but "do not show me that fund".
    min_rising_pct: float = 0.0        # share of holdings trending up
    min_cat_beat_pct: float = 0.0      # beat rate against category median

    # SCREEN FILTERS -- narrow the universe before the search runs.
    #
    # All optional and all off by default. A planner that quietly applied
    # a Sharpe floor nobody asked for would produce different funds for
    # the same goal depending on a default, which is the sort of thing
    # that is impossible to notice and impossible to explain afterwards.
    #
    # They constrain the INPUT to the combination search, never its
    # output -- see apply_fund_filters in portfolio_api. The set scoring,
    # overlap constraint and three-candidate presentation are untouched.
    require_beat_benchmark: bool = False
    min_excess_cagr: Optional[float] = None      # points a year over benchmark
    max_category_rank_pct: Optional[float] = None  # e.g. 25 = top quarter
    min_sharpe: Optional[float] = None
    max_volatility: Optional[float] = None       # annualised %, from mf_rolling
    max_drawdown_pct: Optional[float] = None     # positive depth, e.g. 35
    # A fund whose holdings are trending up less than the average fund in
    # its own category is, by that reading, behind its peers today. The
    # test is relative on purpose: an absolute floor means different things
    # in a rising market and a falling one.
    require_rising_above_category: bool = True


@router.post("/suggest")
def suggest(body: SelectRequest, request: Request):
    """Candidate fund sets for a goal's target allocation."""
    _user_id(request)

    from distributor_api import allocation as _allocation
    alloc = _allocation(purpose=body.purpose, years=body.years,
                        request=request, risk=body.risk)
    # Every bucket, including those targeted at zero -- a zero target with
    # a ceiling is a statement about what is TOLERATED, and dropping it
    # would silently allow unlimited exposure to something set to nothing
    # on purpose.
    target = {b["bucket"]: {"target": b["final_pct"],
                            "min": b["min_pct"], "max": b["max_pct"]}
              for b in alloc["buckets"]}
    if not any(v["target"] > 0 for v in target.values()):
        raise HTTPException(400, "That profile has no allocation set.")

    want = fund_count_for(body.sip_amount)

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        period = body.return_period if body.return_period in (
            "1Y", "3Y", "5Y", "10Y") else "3Y"
        pool = _q(cur, ELIGIBLE, {"window": body.window_years,
                                  "rperiod": period})
        universe_size = len(pool)
        pool = [f for f in pool if str(f["scheme_code"]) not in set(body.exclude)]

        # Stage 1b: NARROW, before anything is ranked or combined.
        pool, screen = apply_fund_filters(pool, body)
        if not pool:
            # Which filter emptied it, not just that it is empty. A page
            # saying "no funds match" sends someone to loosen the wrong
            # one; naming the step that took the last fund does not.
            culprit = next((s["filter"] for s in screen if s["left"] == 0),
                           "the filters")
            raise HTTPException(
                400, f"No fund passes every filter -- '{culprit}' left none. "
                     f"Loosen it and try again.")

        # Stage 2: rank within each role, take the top few.
        by_role = {}
        for f in pool:
            if not body.allow_thematic and f["category"] in EXCLUDED_CATEGORIES:
                continue
            # Too short a record is not a bad record -- it is no record. A
            # fund is excluded rather than ranked low, because ranking it
            # would compare a sample against a history.
            windows = f["cat_windows"] or f["windows_total"] or 0
            if windows < body.min_windows:
                continue
            role = _role_of(f)
            # A role is worth filling only if something is actually
            # targeted at it. A bucket set to zero needs no fund.
            if not role or target.get(role, {}).get("target", 0) <= 0:
                continue
            f["role"] = role
            f["quality"] = _quality(f)
            by_role.setdefault(role, []).append(f)
        # Returns and the trend split, for everything still standing.
        pool_codes = [str(f["scheme_code"]) for fs in by_role.values() for f in fs]
        # `period` is settled once, above, where the eligibility query
        # needs it. Recomputing it here was harmless only for as long as
        # the two copies agreed.
        rets = {str(r["scheme_code"]): float(r["fund_cagr"])
                for r in _q(cur, CANDIDATE_RETURNS,
                            {"codes": pool_codes, "period": period})}

        # The "rising" reading is built from the stock scores, so with
        # scores switched off it is not read at all: no rising shares, no
        # category yardstick, no rising floor and a zero weight below. The
        # other three components rank the funds exactly as before.
        scores_on = _scores_on()
        grouped = {}
        if scores_on:
            for r in _q(cur, TREND_BATCH, {"codes": pool_codes}):
                grouped.setdefault(str(r["scheme_code"]), []).append(r)
        rising = {c: _trend_summary(rs)["up_pct"] for c, rs in grouped.items()}

        # Beat rates over BOTH windows, averaged. Judging on one window
        # cannot tell a fund losing its way from one having a good spell.
        con = {}
        for r in _q(cur, CONSISTENCY, {"codes": pool_codes,
                                       "windows": list(WINDOWS_JUDGED)}):
            con.setdefault(str(r["scheme_code"]), {})[r["window_years"]] = r

        def _avg(code, field):
            vals = [float(v[field]) for v in con.get(code, {}).values()
                    if v[field] is not None]
            return sum(vals) / len(vals) if vals else None

        cat_rising = ({r["category"]: float(r["up_pct"])
                       for r in _q(cur, CATEGORY_RISING, {})}
                      if scores_on else {})

        # Floors, applied before ranking. A fund removed here is gone --
        # no weighting can bring it back, which is the point of a floor.
        dropped = {"rising": 0, "consistency": 0, "below_category_rising": 0}

        if body.require_rising_above_category and scores_on:
            for role, fs in list(by_role.items()):
                keep = []
                for f in fs:
                    c = str(f["scheme_code"])
                    mine = rising.get(c)
                    theirs = cat_rising.get(f["category"])
                    # No category average means no comparison, and a fund
                    # is not removed for a yardstick we do not have.
                    if mine is not None and theirs is not None and mine < theirs:
                        dropped["below_category_rising"] += 1
                        continue
                    keep.append(f)
                by_role[role] = keep
            by_role = {k: v for k, v in by_role.items() if v}
            if not by_role:
                raise HTTPException(
                    404, "Every fund is currently below its category average "
                         "on rising holdings (%d removed). That is a statement "
                         "about the market, not the funds -- set "
                         "require_rising_above_category to false to see them "
                         "anyway." % dropped["below_category_rising"])
        if body.min_rising_pct > 0 or body.min_cat_beat_pct > 0:
            for role, fs in list(by_role.items()):
                keep = []
                for f in fs:
                    c = str(f["scheme_code"])
                    r = rising.get(c)
                    if scores_on and body.min_rising_pct > 0 and (
                            r is None or r < body.min_rising_pct):
                        dropped["rising"] += 1
                        continue
                    beat = f["cat_beat_pct"]
                    if body.min_cat_beat_pct > 0 and (
                            beat is None or float(beat) < body.min_cat_beat_pct):
                        dropped["consistency"] += 1
                        continue
                    keep.append(f)
                by_role[role] = keep
            by_role = {k: v for k, v in by_role.items() if v}
            if not by_role:
                raise HTTPException(
                    404, "Every fund was removed by the floors you set "
                         "(%d on rising, %d on consistency). Lower them."
                         % (dropped["rising"], dropped["consistency"]))

        # Each component ranked WITHIN its role, then blended. Ranking
        # within the role is what makes the three comparable at all, and
        # what stops a small cap fund being judged against a mid cap one.
        w = dict(DEFAULT_WEIGHTS)
        w.update({k: float(v) for k, v in (body.weights or {}).items()
                  if k in DEFAULT_WEIGHTS})
        if not scores_on:
            w["rising"] = 0.0          # whatever the caller asked for
        total_w = sum(w.values()) or 1.0

        for role, fs in by_role.items():
            codes_r = [str(f["scheme_code"]) for f in fs]
            # Four separate questions, each ranked within the role:
            #   consistency -- the return delivered over the window
            #   benchmark   -- how often it beat its own index
            #   category    -- how often it beat the median fund like it
            #   rising      -- how much of what it holds is trending up
            # Kept apart because a fund can be strong on one and weak on
            # another, and that difference is information.
            p_con = _percentile({c: rets.get(c, 0.0) for c in codes_r})
            p_ben = _percentile({c: (_avg(c, "beat_pct") or 50.0)
                                 for c in codes_r})
            p_cat = _percentile({c: (_avg(c, "cat_beat_pct") or 50.0)
                                 for c in codes_r})
            p_ris = _percentile({c: rising.get(c, 0.0) for c in codes_r})

            for f in fs:
                c = str(f["scheme_code"])
                f["cagr"] = rets.get(c)
                f["rising_pct"] = rising.get(c)
                f["cat_rising_pct"] = cat_rising.get(f["category"])
                f["beat_1y3y"] = (round(_avg(c, "beat_pct"), 1)
                                  if _avg(c, "beat_pct") is not None else None)
                f["cat_beat_1y3y"] = (round(_avg(c, "cat_beat_pct"), 1)
                                      if _avg(c, "cat_beat_pct") is not None else None)
                f["parts"] = {"consistency": round(p_con[c], 1),
                              "benchmark": round(p_ben[c], 1),
                              "category": round(p_cat[c], 1)}
                if scores_on:
                    f["parts"]["rising"] = round(p_ris[c], 1)
                blended = (w["consistency"] * p_con[c]
                           + w["benchmark"] * p_ben[c]
                           + w["category"] * p_cat[c]
                           + w["rising"] * p_ris[c]) / total_w
                # Drift is applied AFTER the blend, so no amount of recent
                # performance buys a fund out of not being what its name
                # says.
                f["quality"] = blended - (20.0 if f["drifted"] else 0.0)

        for role in by_role:
            by_role[role].sort(key=lambda x: -x["quality"])
            by_role[role] = by_role[role][:SHORTLIST_PER_ROLE]

        candidates = [f for fs in by_role.values() for f in fs]
        if len(candidates) < want:
            raise HTTPException(
                404, "Only %d eligible funds cover this allocation; %d are "
                     "needed. Widen the window or check that holdings are "
                     "loaded." % (len(candidates), want))

        codes = [str(f["scheme_code"]) for f in candidates]
        ov, _ovdate = pairwise_overlap(cur, codes)

    def pair_overlap(a, b):
        return ov.get((a, b), ov.get((b, a), 0.0))

    # Stage 3: score every combination as a set.
    scored = []
    for combo in combinations(candidates, want):
        cs = [str(f["scheme_code"]) for f in combo]

        worst_ov = max((pair_overlap(cs[i], cs[j])
                        for i in range(len(cs)) for j in range(i + 1, len(cs))),
                       default=0.0)
        if worst_ov > MAX_PAIR_OVERLAP:
            continue          # substantially the same fund twice

        weights, gap = _weights_for(list(combo), target)
        if weights is None:
            continue

        amcs = {f["amc_name"] for f in combo}
        top10 = sum(w / 100.0 * float(f["top10_pct"] or 0)
                    for f, w in zip(combo, weights))
        if top10 > MAX_TOP10:
            continue

        # The set score. Fit dominates; the rest are tie-breakers, and the
        # trend split is deliberately last -- it is a reading of the past
        # few months, which cannot decide a twenty-year holding but can
        # separate two sets that are otherwise equal.
        quality = sum(f["quality"] for f in combo) / len(combo)
        drifted = sum(1 for f in combo if f["drifted"])
        penalty = (worst_ov * 0.35
                   + max(0, len(combo) - len(amcs)) * 6.0
                   + max(0, top10 - 35) * 0.4
                   + drifted * 8.0)
        scored.append({
            "funds": combo, "weights": weights, "gap": round(gap, 1),
            "worst_overlap": round(worst_ov, 1),
            "amc_count": len(amcs), "top10": round(top10, 1),
            "quality": round(quality, 1),
            # Quality at 0.6, up from 0.25. At 0.25 a fund thirty points
            # worse on every measure lost about seven points, while a 0.2
            # difference in gap -- inside the noise of a month-old
            # disclosure -- was nearly free. The fit decides which sets are
            # admissible; quality should decide between them.
            "score": round(gap + penalty - quality * 0.6, 2),
        })

    if not scored:
        raise HTTPException(
            404, "No combination passed the overlap and concentration "
                 "limits. That usually means too few eligible funds.")

    # Gaps are rounded to the nearest point before ranking.
    #
    # The exposure behind them is a month-old disclosure, so a 0.6-point
    # difference between two sets is inside the noise of the input. Letting
    # it decide meant a set with a slightly better gap and a materially
    # worse fund ranked first. Round the gap, and quality settles the tie.
    scored.sort(key=lambda s: (round(s["gap"]), s["score"]))

    # Three sets, and they must be genuinely different -- three variations
    # on the same four funds is one answer wearing three hats.
    chosen, seen = [], []
    for s in scored:
        cs = {str(f["scheme_code"]) for f in s["funds"]}
        if any(len(cs & prev) >= len(cs) - 1 for prev in seen):
            continue
        chosen.append(s)
        seen.append(cs)
        if len(chosen) == 3:
            break

    # Shown best-fit first. The blended score decides WHICH three sets are
    # worth showing; the gap decides the order they are read in, because
    # the gap is the number on the page and a set with a worse gap sitting
    # above a better one cannot be explained to anyone.
    chosen.sort(key=lambda s: s["gap"])

    def shape(s):
        exposure = _set_exposure(list(s["funds"]),
                                 [w / 100.0 for w in s["weights"]])
        return {
            "gap": s["gap"], "worst_overlap": s["worst_overlap"],
            "amc_count": s["amc_count"], "top10_pct": s["top10"],
            "exposure": {k: round(v, 1) for k, v in exposure.items()},
            "funds": [{
                "scheme_code": str(f["scheme_code"]),
                "scheme_name": f["scheme_name"],
                "amc_name": f["amc_name"],
                "category": f["category"],
                "role": f["role"],
                "weight_pct": w,
                "monthly": round(body.sip_amount * w / 100.0),
                "cap_mix": {"large": float(f["large_pct"] or 0),
                            "mid": float(f["mid_pct"] or 0),
                            "small": float(f["small_pct"] or 0),
                            "international": float(f["international_pct"] or 0)},
                "equity_pct": float(f["equity_pct"] or 0),
                "cat_beat_pct": float(f["cat_beat_pct"]) if f["cat_beat_pct"] is not None else None,
                "beat_pct": float(f["beat_pct"]) if f["beat_pct"] is not None else None,
                "worst_excess": float(f["worst_excess"]) if f["worst_excess"] is not None else None,
                "windows": f["cat_windows"] or f["windows_total"],
                # What earned this fund its place, each 0-100 within its
                # role. Without these the ranking is an assertion.
                "score_parts": f.get("parts"),
                "beat_bench_1y3y": f.get("beat_1y3y"),
                "beat_category_1y3y": f.get("cat_beat_1y3y"),
                "category_rising_pct": f.get("cat_rising_pct"),
                "cagr": f.get("cagr"),
                "rising_pct": round(f["rising_pct"], 1) if f.get("rising_pct") is not None else None,
                "drifted": f["drifted"],
                "mandate_note": (
                    "%s cap %.0f%% against a %d%% floor"
                    % (f["mandate_bucket"], f["mandate_actual"] or 0,
                       f["mandate_floor"] or 0)) if f["drifted"] else None,
            } for f, w in zip(s["funds"], s["weights"])],
        }

    return {
        "target": target,
        "weights": w,
        "min_windows": body.min_windows,
        "period_judged": period,
        "floors": {"rising": body.min_rising_pct if scores_on else 0,
                   "consistency": body.min_cat_beat_pct},
        "scores_enabled": scores_on,
        "dropped_by_floors": dropped,
        # What the screen removed, step by step, before anything was
        # ranked. Returned whether or not any filter was set, so the page
        # can always show the working rather than only when it is
        # flattering -- and so "considered" below is never a number
        # somebody has to take on trust.
        "screen": screen,
        "universe": universe_size,
        "view": alloc.get("view"),
        "fund_count": want,
        "sip_amount": body.sip_amount,
        "considered": len(pool),
        "shortlisted": len(candidates),
        "sets_scored": len(scored),
        "sets": [shape(s) for s in chosen],
    }


# ---------------------------------------------------------------------
# Review -- the portfolio someone chose, checked against the rules
# ---------------------------------------------------------------------
#
# This does not pick funds. Somebody else picked them; this says what is
# worth noticing about the choice.
#
# That is a different product from a recommendation and a safer one: every
# finding is a comparison between what was chosen and a target that was
# set in advance, with both numbers on the page. A reader can disagree with
# the target, disagree with the reading, and still see exactly where the
# statement came from.
#
# Findings carry a level rather than a score:
#   gap    -- a real distance from the stated target
#   watch  -- worth a look, not necessarily wrong
#   good   -- said out loud, because a review that only lists problems
#             reads as a sales pitch for changing something


class ReviewFund(BaseModel):
    scheme_code: str
    amount: Optional[float] = None      # monthly or lump, same units throughout


class ReviewRequest(BaseModel):
    funds: List[ReviewFund]
    purpose: Optional[str] = None
    years: Optional[float] = None
    risk: str = "moderate"
    return_period: str = "3Y"
    # Distributors see their own rules; a retail reader sees FinChaya's.
    # The wording differs even though the arithmetic does not.
    audience: str = "distributor"       # or "retail"


# Every company these funds hold, so diversification is measured on the
# COMBINED portfolio. Four funds holding forty names each are not a hundred
# and sixty companies.
# Point to point, per period -- THE SAME NUMBERS THE FUND PAGE SHOWS.
#
# This replaces the rolling-window beat rate here, and the reason is worth
# recording. Rolling windows count every overlapping period since 2016, so
# a fund that did well for years and has lagged recently still scores a
# high beat rate: the past drowns the present. That is a defensible
# statistic and the wrong one for this page, because the fund page reports
# point-to-point returns and the two disagreed about the same fund.
#
# A product that contradicts itself is worse than one using a blunter
# measure. Rolling windows still have their place -- they say whether a
# record is luck -- but not next to a number a client can check.
REVIEW_PERIODS = """
WITH latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d
    FROM mf_returns WHERE scheme_code = ANY(%(codes)s)
    GROUP BY scheme_code
)
SELECT r.scheme_code, r.period, r.years,
       ROUND(r.fund_cagr, 2)  AS fund_cagr,
       ROUND(r.bench_cagr, 2) AS bench_cagr,
       ROUND(r.fund_cagr - cr.median_cagr, 2) AS vs_category,
       cr.median_cagr,
       b.display_name AS benchmark_name
FROM mf_returns r
JOIN latest l ON l.scheme_code = r.scheme_code AND l.d = r.as_of_date
LEFT JOIN benchmark_master b USING (benchmark_id)
LEFT JOIN mf_category_return cr
       ON cr.category = r.category AND cr.as_of_date = r.as_of_date
      AND cr.period   = r.period
WHERE r.period IN ('1Y','3Y','5Y')
ORDER BY r.scheme_code, r.years
"""

REVIEW_HOLDINGS = """
WITH latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d
    FROM mf_holding WHERE scheme_code = ANY(%(codes)s)
    GROUP BY scheme_code
)
SELECT h.scheme_code, h.isin, h.pct_of_nav
FROM mf_holding h
JOIN latest l ON l.scheme_code = h.scheme_code AND l.d = h.as_of_date
WHERE h.isin IS NOT NULL AND h.pct_of_nav IS NOT NULL
"""

REVIEW_FUNDS = """
SELECT c.canonical_scheme_code AS scheme_code, c.scheme_name, c.amc_name,
       vc.category,
       p.equity_pct, p.large_pct, p.mid_pct, p.small_pct,
       p.international_pct, p.top10_pct, p.holding_count,
       p.drifted, p.mandate_bucket, p.mandate_floor, p.mandate_actual
FROM v_fund_canonical c
LEFT JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
LEFT JOIN fund_profile p ON p.scheme_code = c.canonical_scheme_code
WHERE c.canonical_scheme_code = ANY(%(codes)s)
"""


class OverlapRequest(BaseModel):
    funds: List[ReviewFund]


@router.post("/overlap")
def overlap(body: OverlapRequest, request: Request):
    """How much of the SAME thing a basket holds -- and nothing else.

    WHY THIS EXISTS ALONGSIDE /review
        /review answers about fifteen questions and costs a dozen queries
        to do it. The one a reader has while still choosing funds is much
        narrower: "am I buying the same thing twice?" Asking it used to
        mean running the whole review, which needs a complete plan and
        lands the reader three tabs further on than they meant to go.

        So this is the overlap half on its own: the same
        pairwise_overlap() the review calls, the same combined-portfolio
        arithmetic, the same numbers. Not an approximation of the review
        -- a subset of it, computed by the same code, so the two pages can
        never disagree about a pair.

    Same paywall as /review. It reads through to what every fund holds,
    which is the product.
    """
    _user_id(request)
    from portfolio_api import _require_subscription
    _require_subscription(request)
    if len(body.funds) < 2:
        raise HTTPException(400, "Overlap needs at least two funds.")
    if len(body.funds) > 20:
        raise HTTPException(400, "That is more than 20 funds.")

    codes = [str(f.scheme_code) for f in body.funds]
    amounts = {str(f.scheme_code): float(f.amount or 0) for f in body.funds}
    named = sum(amounts.values())
    if named > 0:
        weights = {c: amounts[c] / named for c in codes}
        weighted_by = "the shares you entered"
    else:
        weights = {c: 1.0 / len(codes) for c in codes}
        weighted_by = "an equal split, because no shares were given"

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = {str(r["scheme_code"]): r
                for r in _q(cur, REVIEW_FUNDS, {"codes": codes})}
        ov, ov_asof = pairwise_overlap(cur, codes)

        held = {}
        for r in _q(cur, REVIEW_HOLDINGS, {"codes": codes}):
            held.setdefault(str(r["scheme_code"]), []).append(
                (r["isin"], float(r["pct_of_nav"])))

    usable = [c for c in codes if c in held]
    if len(usable) < 2:
        # Said, not silently returned as zero overlap. A fund whose
        # holdings we do not have is not a fund that overlaps with
        # nothing, and the two look identical in a number.
        return {"pairs": [], "companies": None, "effective_holdings": None,
                "top10_pct": None, "held_by_more_than_one": None,
                "weighted_by": weighted_by,
                "no_holdings": [rows.get(c, {}).get("scheme_name") or c
                                for c in codes if c not in held]}

    combined, per_fund = {}, {}
    for c in usable:
        per_fund[c] = {i for i, _ in held[c]}
        for isin, pct in held[c]:
            combined[isin] = combined.get(isin, 0.0) + weights[c] * pct
    total_eq = sum(combined.values())
    shares = sorted((v / total_eq for v in combined.values()), reverse=True) \
        if total_eq > 0 else []
    hhi = sum(s * s for s in shares)

    name = lambda c: (rows.get(c, {}) or {}).get("scheme_name") or c
    pairs = [{"a": name(a), "b": name(b), "overlap_pct": round(o, 1),
              "as_of": ov_asof[(a, b)].isoformat() if ov_asof.get((a, b)) else None}
             for (a, b), o in sorted(ov.items(), key=lambda x: -x[1])]

    return {
        "pairs": pairs,
        "companies": len(combined),
        # How many equally sized positions would be as concentrated as
        # this actually is. Four funds of forty names look like a hundred
        # and sixty companies; weighted by what sits in each, the honest
        # number is far smaller, and it is the one that describes risk.
        "effective_holdings": int(round(1 / hhi)) if hhi > 0 else None,
        "top10_pct": round(100 * sum(shares[:10]), 1) if shares else None,
        "held_by_more_than_one": sum(
            1 for isin in combined
            if sum(1 for c in usable if isin in per_fund[c]) > 1),
        "weighted_by": weighted_by,
        "no_holdings": [name(c) for c in codes if c not in held],
    }


@router.post("/review")
def review(body: ReviewRequest, request: Request):
    """What is worth noticing about a portfolio somebody chose."""
    # Signed in AND subscribed. It used to be signed-in alone, which made
    # the whole look-through free to anyone with an email address.
    #
    # _user_id first so the two refusals stay distinct: 401 means "sign
    # in", 402 means "subscribe". A page that cannot tell them apart asks
    # a signed-in subscriber to sign in again.
    _user_id(request)
    from portfolio_api import _require_subscription
    _require_subscription(request)
    if not body.funds:
        raise HTTPException(400, "No funds to review.")
    if len(body.funds) > 20:
        raise HTTPException(400, "That is more than 20 funds.")

    codes = [str(f.scheme_code) for f in body.funds]
    # Equal weights when amounts are missing, and the page says so rather
    # than implying the split is known.
    amounts = {str(f.scheme_code): float(f.amount or 0) for f in body.funds}
    named = sum(amounts.values())
    if named > 0:
        weights = {c: amounts[c] / named for c in codes}
        weighted_by = "the amounts you entered"
    else:
        weights = {c: 1.0 / len(codes) for c in codes}
        weighted_by = "an equal split, because no amounts were given"

    target, alloc = None, None
    if body.purpose and body.years is not None:
        from distributor_api import allocation as _allocation
        alloc = _allocation(purpose=body.purpose, years=body.years,
                            request=request, risk=body.risk)
        target = {b["bucket"]: b for b in alloc["buckets"]}

    period = body.return_period if body.return_period in (
        "1Y", "3Y", "5Y", "10Y") else "3Y"

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = {str(r["scheme_code"]): r
                for r in _q(cur, REVIEW_FUNDS, {"codes": codes})}
        missing = [c for c in codes if c not in rows or rows[c]["equity_pct"] is None]

        rets = {str(r["scheme_code"]): float(r["fund_cagr"])
                for r in _q(cur, CANDIDATE_RETURNS,
                            {"codes": codes, "period": period})}

        periods = {}
        for r in _q(cur, REVIEW_PERIODS, {"codes": codes}):
            periods.setdefault(str(r["scheme_code"]), {})[r["period"]] = r

        # The "rising holdings" note is built from the stock scores. With
        # scores switched off both lookups are skipped, so `rising` and
        # `cat_rising` are empty and the per-fund note below never fires.
        # Everything else in a verdict is returns, overlap or holdings.
        grouped = {}
        cat_rising = {}
        if _scores_on():
            for r in _q(cur, TREND_BATCH, {"codes": codes}):
                grouped.setdefault(str(r["scheme_code"]), []).append(r)
            cat_rising = {r["category"]: float(r["up_pct"])
                          for r in _q(cur, CATEGORY_RISING, {})}
        rising = {c: _trend_summary(rs)["up_pct"] for c, rs in grouped.items()}

        ov, ov_asof = pairwise_overlap(cur, codes)

        held = {}
        for r in _q(cur, REVIEW_HOLDINGS, {"codes": codes}):
            held.setdefault(str(r["scheme_code"]), []).append(
                (r["isin"], float(r["pct_of_nav"])))

    def period_marks(code):
        """Beat or not, period by period, against the index and the peers.

        Three separate answers, not one average. A fund ahead over five
        years and behind over one is telling you something an average
        would hide -- and hiding it is exactly what went wrong before.
        """
        out = []
        for p in ("1Y", "3Y", "5Y"):
            r = periods.get(code, {}).get(p)
            if not r or r["fund_cagr"] is None:
                out.append({"period": p, "fund": None, "bench": None,
                            "beat_bench": None, "vs_category": None,
                            "beat_category": None})
                continue
            fund = float(r["fund_cagr"])
            bench = float(r["bench_cagr"]) if r["bench_cagr"] is not None else None
            vs_cat = float(r["vs_category"]) if r["vs_category"] is not None else None
            out.append({
                "period": p, "fund": fund, "bench": bench,
                "beat_bench": None if bench is None else fund > bench,
                "category_median": float(r["median_cagr"]) if r["median_cagr"] is not None else None,
                "vs_category": vs_cat,
                "beat_category": None if vs_cat is None else vs_cat > 0,
                "benchmark_name": r["benchmark_name"],
            })
        return out

    usable = [c for c in codes if c in rows and rows[c]["equity_pct"] is not None]
    exposure = _set_exposure([rows[c] for c in usable],
                             [weights[c] for c in usable])

    # Diversification on the combined portfolio.
    #
    # effective_holdings is how many equally sized positions would be as
    # concentrated as this actually is. Four funds of forty names look like
    # a hundred and sixty companies; weighted by how much sits in each, the
    # honest number is far smaller, and it is the one that describes risk.
    combined, per_fund_isins = {}, {}
    for c in usable:
        per_fund_isins[c] = {i for i, _ in held.get(c, [])}
        for isin, pct in held.get(c, []):
            combined[isin] = combined.get(isin, 0.0) + weights[c] * pct
    total_eq = sum(combined.values())
    diversification = None
    if total_eq > 0:
        shares = sorted((v / total_eq for v in combined.values()), reverse=True)
        hhi = sum(s * s for s in shares)
        shared = sum(1 for isin in combined
                     if sum(1 for c in usable if isin in per_fund_isins[c]) > 1)
        diversification = {
            "companies": len(combined),
            "effective_holdings": int(round(1 / hhi)) if hhi > 0 else None,
            "top10_pct": round(100 * sum(shares[:10]), 1),
            "held_by_more_than_one": shared,
            "worst_pair_overlap": None,
            "worst_pair_as_of": None,
            "holdings_as_of": None,
        }

    findings = []
    def add(level, title, detail, **extra):
        findings.append(dict(level=level, title=title, detail=detail, **extra))

    # ---- allocation -------------------------------------------------
    if target:
        for bucket, spec in target.items():
            got = exposure.get(bucket, 0.0)
            want, lo, hi = spec["final_pct"], spec["min_pct"], spec["max_pct"]
            lo = lo if lo is not None else want
            hi = hi if hi is not None else want
            short = round(want - got, 1)
            if got < lo:
                add("gap", "%s is light" % bucket.capitalize(),
                    "These funds put %.0f%% in %s companies. The plan for a "
                    "%s goal %s years out asks for %.0f%%, and treats %.0f%% "
                    "as the least it should be."
                    % (got, bucket, body.purpose, body.years, want, lo),
                    bucket=bucket, actual=round(got, 1), target=want,
                    shortfall=short)
            elif got > hi:
                add("gap", "%s is heavy" % bucket.capitalize(),
                    "These funds put %.0f%% in %s companies against a plan of "
                    "%.0f%%, and a ceiling of %.0f%%."
                    % (got, bucket, want, hi),
                    bucket=bucket, actual=round(got, 1), target=want)
            elif want > 0 and abs(short) <= 5:
                add("good", "%s is on plan" % bucket.capitalize(),
                    "%.0f%% against a target of %.0f%%." % (got, want),
                    bucket=bucket, actual=round(got, 1), target=want)

    # ---- overlap ----------------------------------------------------
    worst = None
    all_pairs = []
    for i, a in enumerate(usable):
        for b in usable[i + 1:]:
            key = (a, b) if (a, b) in ov else (b, a)
            o = ov.get(key, 0.0)
            all_pairs.append((a, b, o, ov_asof.get(key)))
            if worst is None or o > worst[2]:
                worst = (a, b, o, ov_asof.get(key))

    # The date the closest pair was measured on. Stated whenever it is not
    # the same month as the newest portfolio in the basket, because a
    # reader comparing this against the fund pages deserves to know that
    # one of these two funds has not filed yet -- rather than wondering
    # why the numbers moved.
    newest_asof = max([d for d in ov_asof.values() if d], default=None)
    stale_pair = (worst and worst[3] and newest_asof
                  and worst[3] < newest_asof)

    if diversification and worst:
        diversification["worst_pair_overlap"] = round(worst[2], 1)
        diversification["worst_pair_as_of"] = (worst[3].isoformat()
                                               if worst[3] else None)
        diversification["holdings_as_of"] = (newest_asof.isoformat()
                                             if newest_asof else None)

        # EVERY PAIR, not only the worst one.
        #
        # The page was showing a single Venn for the closest pair, which
        # answers "is any pair too close" and silently drops the rest. With
        # four funds there are six pairs, and a reader looking at one
        # picture cannot tell whether the other five are 40% or 4% -- the
        # difference between a portfolio that is diversified apart from one
        # collision and one that is uniformly the same bet.
        #
        # Sorted highest first, because the question being asked of this
        # table is always "what is the closest", and reading order should
        # answer it before the eye has to search.
        diversification["pairs"] = [
            {"a": rows[a]["scheme_name"], "b": rows[b]["scheme_name"],
             "overlap_pct": round(o, 1),
             "as_of": d.isoformat() if d else None}
            for a, b, o, d in sorted(all_pairs, key=lambda x: -x[2])
        ]

    # Both funds are read on a date they have BOTH published, so the figure
    # is a fact about one day rather than a comparison across two.
    dated = (" Measured on %s, the newest portfolio both of them have filed."
             % worst[3].strftime("%d %B %Y")) if stale_pair else ""

    if worst and worst[2] >= 25:
        add("gap", "Two funds are largely the same holding",
            "%s and %s hold %.0f%% of the same companies by weight. Two funds "
            "that overlap this much do less for spreading risk than two names "
            "suggest.%s"
            % (rows[worst[0]]["scheme_name"], rows[worst[1]]["scheme_name"],
               worst[2], dated),
            overlap_pct=round(worst[2], 1),
            as_of=worst[3].isoformat() if worst[3] else None)
    elif worst and worst[2] < 15:
        add("good", "The funds are genuinely different",
            "The closest pair shares only %.0f%% of its companies by weight.%s"
            % (worst[2], dated),
            as_of=worst[3].isoformat() if worst[3] else None)

    # ---- concentration and fund houses -------------------------------
    if diversification and diversification["top10_pct"] > 30:
        # The COMBINED portfolio's ten largest companies, not the average of
        # each fund's own top ten.
        #
        # Those two are different numbers and I had them both on the page
        # with nearly the same wording -- 27.8% and 49% side by side. The
        # average of each fund's internal concentration says nothing about
        # the portfolio, because the funds' top tens are different
        # companies. Only the combined figure answers the question.
        add("watch", "A lot rides on ten companies",
            "%.0f%% of the equity here sits in ten companies once the funds "
            "are added together." % diversification["top10_pct"],
            top10_pct=diversification["top10_pct"])
        amcs = {rows[c]["amc_name"] for c in usable}
        if len(usable) >= 3 and len(amcs) < len(usable) - 1:
            add("watch", "Most of this is with one fund house",
                "%d funds from %d fund %s. That is one process and one "
                "operational point of failure carrying most of the money."
                % (len(usable), len(amcs), "house" if len(amcs) == 1 else "houses"))

    # ---- per fund ----------------------------------------------------
    ret_vals = [v for v in rets.values() if v is not None]
    median_ret = sorted(ret_vals)[len(ret_vals) // 2] if ret_vals else None

    for c in usable:
        f = rows[c]
        name = f["scheme_name"]
        notes = []

        r = rising.get(c)
        peer = cat_rising.get(f["category"])
        if r is not None and peer is not None:
            if r < peer - 3:
                notes.append(("watch",
                    "Of what it holds, %.0f%% is in shares whose price is "
                    "rising, against %.0f%% for the average fund in its "
                    "category. That describes what has already happened to "
                    "those shares, not what they will do." % (r, peer)))
            elif r > peer + 3:
                notes.append(("good",
                    "%.0f%% of its holdings are rising against %.0f%% for its "
                    "category." % (r, peer)))

        marks = period_marks(c)
        bench_won = [m for m in marks if m["beat_bench"] is True]
        bench_lost = [m for m in marks if m["beat_bench"] is False]
        cat_won = [m for m in marks if m["beat_category"] is True]
        cat_lost = [m for m in marks if m["beat_category"] is False]

        if bench_lost and not bench_won:
            notes.append(("gap",
                "It trailed its benchmark over every period we hold -- %s."
                % ", ".join("%s by %.1f points"
                            % (m["period"], m["bench"] - m["fund"])
                            for m in bench_lost)))
        elif bench_lost and bench_won:
            notes.append(("watch",
                "It beat its benchmark over %s but trailed over %s."
                % (" and ".join(m["period"] for m in bench_won),
                   " and ".join(m["period"] for m in bench_lost))))
        elif bench_won and not bench_lost:
            notes.append(("good",
                "It beat its benchmark over every period we hold."))

        if cat_lost and not cat_won:
            worst_p = min(cat_lost, key=lambda m: m["vs_category"])
            notes.append(("gap",
                "It was behind the median fund in its category over every "
                "period, by as much as %.1f points over %s."
                % (abs(worst_p["vs_category"]), worst_p["period"])))
        elif cat_won and not cat_lost:
            notes.append(("good",
                "It was ahead of the median fund in its category over every "
                "period we hold."))

        cagr = rets.get(c)
        if cagr is not None and median_ret is not None and len(ret_vals) >= 3:
            if cagr < median_ret - 3:
                notes.append(("watch",
                    "Its %s return of %.1f%% is the weakest here; the middle "
                    "of this set is %.1f%%." % (period, cagr, median_ret)))

        if f["drifted"]:
            notes.append(("gap",
                "It holds %.0f%% %s cap against the %d%% its category name "
                "requires, so it is not quite the fund it is sold as."
                % (f["mandate_actual"] or 0, f["mandate_bucket"],
                   f["mandate_floor"] or 0)))

        level = ("gap" if any(n[0] == "gap" for n in notes)
                 else "watch" if any(n[0] == "watch" for n in notes)
                 else "good")
        if True:
            add(level, name,
                " ".join(n[1] for n in notes) or
                "Nothing stands out either way on the measures we hold.",
                scheme_code=c, amc_name=f["amc_name"], category=f["category"],
                weight_pct=round(100 * weights[c], 1),
                rising_pct=round(r, 1) if r is not None else None,
                category_rising_pct=round(peer, 1) if peer is not None else None,
                cagr=cagr,
                # Period by period, exactly as the fund page reports it, so
                # the two pages cannot disagree about the same fund.
                periods=marks,
                rising_above_category=(None if (r is None or peer is None)
                                       else r >= peer),
                cap_mix={"large": float(f["large_pct"] or 0),
                         "mid": float(f["mid_pct"] or 0),
                         "small": float(f["small_pct"] or 0),
                         "international": float(f["international_pct"] or 0)},
                per_fund=True)

    # ---- how many funds ----------------------------------------------
    if named > 0:
        want_n = fund_count_for(named)
        if len(codes) > want_n + 1:
            add("watch", "That is a lot of funds for the money",
                "%d funds for %s means about %s each. Below roughly a "
                "thousand rupees a fund the paperwork outweighs the "
                "diversification, and the holdings overlap anyway."
                % (len(codes), _rupee(named), _rupee(named / len(codes))))

    if missing:
        add("watch", "Some funds could not be read",
            "%d of these have no portfolio loaded, so they are missing from "
            "every number above." % len(missing), missing=missing)

    order = {"gap": 0, "watch": 1, "good": 2}
    findings.sort(key=lambda f: (order[f["level"]], f.get("per_fund", False)))

    allocation = []
    for b in ("large", "mid", "small", "international", "gold", "debt"):
        got = exposure.get(b)
        spec = (target or {}).get(b)
        if not got and not spec:
            continue
        allocation.append({
            "bucket": b, "actual": round(got or 0, 1),
            "target": spec["final_pct"] if spec else None,
            "min": spec["min_pct"] if spec else None,
            "max": spec["max_pct"] if spec else None,
        })

    # What this portfolio returned over each period, weighted by how much
    # is in each fund.
    #
    # Returned so the page can carry it forward. It is a WEIGHTED PAST
    # RETURN and nothing more -- projecting it is arithmetic on a number
    # that has already happened, not a forecast, and the page has to say so
    # every time it shows one.
    blended = {}
    for p in ("1Y", "3Y", "5Y"):
        num, wt = 0.0, 0.0
        for c in usable:
            r = periods.get(c, {}).get(p)
            if r and r["fund_cagr"] is not None:
                num += weights[c] * float(r["fund_cagr"])
                wt += weights[c]
        # Only where most of the money has a figure. A blend over half the
        # portfolio would describe a portfolio nobody holds.
        blended[p] = round(num / wt, 2) if wt >= 0.75 else None

    return {
        "target": {k: v["final_pct"] for k, v in (target or {}).items()},
        "allocation": allocation,
        "blended_returns": blended,
        "total_amount": round(named, 2) if named > 0 else None,
        "diversification": diversification,
        "exposure": {k: round(v, 1) for k, v in exposure.items()},
        "weighted_by": weighted_by,
        "period_judged": period,
        "audience": body.audience,
        "view": (alloc or {}).get("view"),
        "counts": {"gap": sum(1 for f in findings if f["level"] == "gap"),
                   "watch": sum(1 for f in findings if f["level"] == "watch"),
                   "good": sum(1 for f in findings if f["level"] == "good")},
        "findings": findings,
    }


def _rupee(n):
    return "\u20b9" + format(int(round(n)), ",d")
