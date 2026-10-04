"""
portfolio_api.py -- the look-through, as an endpoint.
-----------------------------------------------------
Same computation as portfolio_lookthrough.py, returned as JSON. Kept in
its own module so api.py stays readable and this can be removed in one
line if the feature does not earn its place.

MOUNTING IT
    In api.py, after `app = FastAPI(...)`:

        from portfolio_api import router as portfolio_router
        app.include_router(portfolio_router)

    And to serve the page, next to the other static pages:

        @app.get("/portfolio", include_in_schema=False)
        def portfolio_page():
            return _static_page("portfolio.html")

WHAT IT WILL NOT DO
    No advice, no verdicts, no thresholds. Every number here is a fact
    about what is held: exposure, overlap, rank, return against a
    benchmark. The score research found no forward power in the fund
    score, so nothing is presented as predictive -- and the score LEVEL
    is not returned at all, only its category rank, because a level is
    only comparable against other funds on the same date.
"""

import os
import re
from collections import defaultdict
from datetime import date
from typing import List, Optional

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Query, Request

from invest_value import value_holdings, plan_siblings
from pydantic import BaseModel

load_dotenv()
DB = os.getenv("FINCHAYA_DB")

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")

# A portfolio bigger than this is almost certainly a mistake, and each fund
# costs several queries. Twenty is well past the 3-6 funds most people hold.
MAX_FUNDS = 20


def _q(cur, sql, params=None, one=False):
    cur.execute(sql, params or {})
    return cur.fetchone() if one else cur.fetchall()


class Holding(BaseModel):
    key: str          # name, scheme code, or plan ISIN

    # OPTIONAL NOW. What the holding is worth today used to be typed in
    # and was the foundation of every rupee on the report -- the one
    # number nobody could check, and the easiest to get wrong, since a
    # SIP row also carries a monthly amount. When the three invest
    # fields below are present the value is PRICED FROM NAV instead and
    # this is ignored. It is still accepted for holdings with no dates.
    amount: Optional[float] = None

    # Optional, all three or none. `amount` is what the holding is worth
    # TODAY; these say what went IN and when, which is what turns a
    # hypothetical return into a real one. Passed straight back out to
    # the page, which does the XIRR -- nothing here is stored, because
    # a look-through is not saved anywhere.
    invested_on: Optional[str] = None       # ISO date
    invest_mode: Optional[str] = None       # 'lumpsum' | 'sip'
    invest_amount: Optional[float] = None   # the lump, or one instalment

    # 'REGULAR' or 'DIRECT'. A distributor's client holds Regular, whose
    # NAV carries the commission -- about a point a year behind Direct.
    # The page sends whatever the saved portfolio resolved; absent means
    # Direct, which is what every unattributed look-through has always
    # been.
    plan: Optional[str] = None


def _invest_date(s):
    """An ISO date string if it is one, and in a range that can mean
    something, else None. A bad date must not travel on as text and
    become NaN in the browser's arithmetic."""
    if not s:
        return None
    try:
        d = date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        return None
    return str(d) if date(1993, 1, 1) <= d <= date.today() else None


class LookthroughRequest(BaseModel):
    holdings: List[Holding]
    as_of: Optional[str] = None
    top: int = 30


# ---------------------------------------------------------------------
# Typeahead. Canonical funds only -- the picker must not offer four plan
# variants of the same fund, because holdings live against one code.
#
# WORD-BY-WORD AND PUNCTUATION-BLIND, matching /api/funds.
#
# A plain ILIKE '%hsbc midcap%' fails against "HSBC Mid Cap Fund" over one
# space, so both sides are stripped of everything that is not a letter or
# digit before comparing. The query is then split into words and every word
# must appear somewhere in the fund or AMC name -- which buys order
# independence for free, so "midcap hsbc" finds the same fund as
# "hsbc midcap". People type the distinctive word first.
# ---------------------------------------------------------------------
_NORM = "regexp_replace(lower(%s), '[^a-z0-9]', '', 'g')"

SEARCH_HEAD = """
SELECT c.canonical_scheme_code AS scheme_code,
       c.scheme_name,
       c.amc_name,
       vc.category
FROM v_fund_canonical c
LEFT JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
WHERE EXISTS (SELECT 1 FROM mf_holding h
               WHERE h.scheme_code = c.canonical_scheme_code)
"""

# Shorter names first: typing "hdfc mid cap" should surface
# "HDFC Mid Cap Fund" above "HDFC Mid Cap Opportunities Direct Plan".
SEARCH_TAIL = """
ORDER BY length(c.scheme_name), c.scheme_name
LIMIT 20
"""

# Last resort, for a name half-remembered or mistyped. Compares the whole
# query against the whole punctuation-stripped fund name and ranks by
# closeness, so "invsco midcap" still finds Invesco India Mid Cap Fund.
# Requires pg_trgm:  CREATE EXTENSION IF NOT EXISTS pg_trgm;
FUZZY = """
SELECT c.canonical_scheme_code AS scheme_code,
       c.scheme_name,
       c.amc_name,
       vc.category
FROM v_fund_canonical c
LEFT JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
WHERE EXISTS (SELECT 1 FROM mf_holding h
               WHERE h.scheme_code = c.canonical_scheme_code)
  AND similarity(regexp_replace(lower(c.scheme_name), '[^a-z0-9]', '', 'g'),
                 %(q)s) > 0.25
ORDER BY similarity(regexp_replace(lower(c.scheme_name), '[^a-z0-9]', '', 'g'),
                    %(q)s) DESC
LIMIT 12
"""

RESOLVE_BY_ISIN = """
SELECT c.canonical_scheme_code AS code, c.scheme_name, s.amc_name
FROM mf_scheme s
JOIN v_fund_canonical c ON c.scheme_name = s.scheme_name
                       AND c.amc_name IS NOT DISTINCT FROM s.amc_name
WHERE s.scheme_isin = %(key)s LIMIT 1
"""

RESOLVE_BY_CODE = """
SELECT c.canonical_scheme_code AS code, c.scheme_name, s.amc_name
FROM mf_scheme s
JOIN v_fund_canonical c ON c.scheme_name = s.scheme_name
                       AND c.amc_name IS NOT DISTINCT FROM s.amc_name
WHERE s.scheme_code = %(key)s LIMIT 1
"""

RESOLVE_BY_NAME = """
SELECT c.canonical_scheme_code AS code, c.scheme_name, c.amc_name
FROM v_fund_canonical c
WHERE fc_norm(c.scheme_name) = fc_norm(%(key)s) LIMIT 1
"""

# Latest portfolio AT OR BEFORE the target month. Each fund's own newest
# disclosure would compare March against April and call it overlap.
AS_OF_FOR_FUND = """
SELECT MAX(as_of_date) AS d FROM mf_holding
WHERE scheme_code = %(code)s AND as_of_date <= %(target)s
"""

HOLDINGS = """
SELECT h.isin,
       COALESCE(m.company_name, h.instrument_name) AS name,
       COALESCE(m.sector, m.industry) AS sector,
       cc.cap_class,
       h.pct_of_nav
FROM mf_holding h
LEFT JOIN stock_master m ON m.isin = h.isin
LEFT JOIN LATERAL (
    SELECT c.cap_class
    FROM stock_cap_class c
    WHERE c.isin = h.isin
    ORDER BY (c.as_of_period <= h.as_of_date) DESC,
             abs(c.as_of_period - h.as_of_date)
    LIMIT 1
) cc ON true
WHERE h.scheme_code = %(code)s AND h.as_of_date = %(as_of)s
  AND h.pct_of_nav IS NOT NULL
"""

# What the promote dropped: cash, debt, REITs. From mf_holding alone the
# remainder below 100% is unattributable, and "3% cash" and "3% Embassy
# REIT" mean very different things to someone reading an overlap number.
GAP_BREAKDOWN = """
SELECT r.instrument_type AS kind,
       ROUND(SUM(r.pct_of_nav)::numeric, 2) AS pct
FROM mf_holding_raw r
JOIN mf_scheme_map sm ON sm.amc_fund_name = r.amc_fund_name
LEFT JOIN stock_master m ON m.isin = r.isin
WHERE sm.scheme_code = %(code)s AND r.as_of_date = %(as_of)s
  AND m.isin IS NULL AND r.pct_of_nav IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC
"""

# Not pinned to an algo_version: the engine has moved fund-v1 -> fund-v2
# once already and a pinned string reports nothing rather than failing.
FUND_SCORE = """
SELECT DISTINCT ON (s.scheme_code)
       s.category_rank,
       ROUND(s.coverage_pct, 1) AS coverage_pct,
       c.category,
       c.rank_meaningful
FROM mf_score s
JOIN v_scheme_category c USING (scheme_code)
WHERE s.scheme_code = %(code)s
ORDER BY s.scheme_code, s.as_of_date DESC, s.algo_version DESC
LIMIT 1
"""

FUND_SCORE_PEERS = """
SELECT COUNT(*) AS n
FROM mf_score s
JOIN v_scheme_category c USING (scheme_code)
WHERE c.category = %(cat)s
  AND s.as_of_date   = (SELECT MAX(as_of_date) FROM mf_score)
  AND s.algo_version = (SELECT algo_version FROM mf_score
                         ORDER BY as_of_date DESC, algo_version DESC LIMIT 1)
"""

# bench_cagr is NULL where no TRI is mapped, which is common for sectoral
# funds. Null is the honest answer rather than substituting another index.
FUND_RETURNS = """
SELECT r.period,
       ROUND(r.fund_cagr, 2)   AS fund_cagr,
       ROUND(r.bench_cagr, 2)  AS bench_cagr,
       ROUND(r.excess_cagr, 2) AS excess_cagr,
       ROUND(r.fund_cagr - cr.median_cagr, 2) AS vs_category,
       r.category_rank, r.category_count,
       COALESCE(cr.rank_meaningful, false) AS rank_meaningful,
       b.display_name AS benchmark_name
FROM mf_returns r
LEFT JOIN benchmark_master b USING (benchmark_id)
LEFT JOIN mf_category_return cr
       ON cr.category = r.category AND cr.as_of_date = r.as_of_date
      AND cr.period = r.period
WHERE r.scheme_code = %(code)s
  AND r.as_of_date = (SELECT MAX(as_of_date) FROM mf_returns
                       WHERE scheme_code = %(code)s)
  AND r.period IN ('1Y', '3Y', '5Y')
ORDER BY r.years
"""


# ---------------------------------------------------------------------
# What the fund's disclosed holdings look like TODAY.
#
# This is a decomposition, not a verdict, and the distinction matters.
# The fund score is the weighted mean of these same stock scores, and that
# mean showed no forward power -- Spearman +0.014 at one month, negative at
# three -- so a count taken from the same inputs predicts nothing either.
# What it does do is show WHICH companies, which a single number cannot,
# and lets someone check rather than trust.
#
# Two honesty constraints follow from that, both enforced in the wording
# on the page rather than buried here:
#
#   * Holdings are month-end, stock signals are today. Weeks of trading
#     sit in between and the manager may already have sold the stock.
#   * These indicators are MOMENTUM -- they describe what already happened
#     to the price. A stock in a downtrend has already fallen; the fund's
#     NAV already reflects it. It is not "dragging the fund down", which
#     would be a claim about the future.
#
# WEIGHT MATTERS MORE THAN COUNT. Four stocks might be 2% of the portfolio
# or 30%. Both are returned; the page leads with weight.
# ---------------------------------------------------------------------
TREND = """
WITH newest AS (
    SELECT as_of_date, algo_version
    FROM stock_score
    ORDER BY as_of_date DESC, algo_version DESC
    LIMIT 1
)
SELECT h.isin,
       COALESCE(m.company_name, h.instrument_name) AS name,
       m.symbol,
       h.pct_of_nav,
       ss.supertrend_dir,
       ss.total_score,
       ss.rsi,
       tech.bb_upper,
       tech.bb_lower,
       tech.close_price,
       n.as_of_date AS score_date
FROM mf_holding h
CROSS JOIN newest n
LEFT JOIN stock_master m  ON m.isin = h.isin
LEFT JOIN stock_score  ss ON ss.isin = h.isin
                         AND ss.as_of_date   = n.as_of_date
                         AND ss.algo_version = n.algo_version
-- Bollinger bands live in stock_technical, not stock_score, so the latest
-- bar is pulled on the SAME timeframe the score was computed on. Matching
-- timeframes matters: a monthly score against a daily band is two
-- different questions.
LEFT JOIN LATERAL (
    SELECT t.bb_upper, t.bb_lower, t.close_price
    FROM stock_technical t
    WHERE t.isin = h.isin
      AND t.timeframe = ss.timeframe_used
    ORDER BY t.as_of_date DESC
    LIMIT 1
) tech ON TRUE
WHERE h.scheme_code = %(code)s
  AND h.as_of_date  = %(as_of)s
  AND h.pct_of_nav IS NOT NULL
"""

# Sideways: inside the Bollinger bands AND RSI in the middle of its range.
#
# The intent was "high below the upper band and low above the lower band",
# but stock_technical stores no high or low -- only close_price -- so this
# is close-inside-the-bands. That is a LOOSER test: the bands contain about
# 95% of closes by construction, so RSI is doing most of the work here and
# the band check mainly excludes stocks at an extreme.
RSI_FLOOR, RSI_CEIL = 40.0, 60.0


def _is_sideways(row):
    rsi = row.get("rsi")
    up, low, close = row.get("bb_upper"), row.get("bb_lower"), row.get("close_price")
    if rsi is None or up is None or low is None or close is None:
        return False
    if not (RSI_FLOOR <= float(rsi) <= RSI_CEIL):
        return False
    return float(low) < float(close) < float(up)


def _direction(value):
    """supertrend_dir may be stored as a word or a sign depending on when
    the row was written. Read both rather than assuming."""
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in ("UP", "1", "1.0", "TRUE", "BULLISH"):
        return "up"
    if text in ("DOWN", "-1", "-1.0", "FALSE", "BEARISH"):
        return "down"
    return None


def _trend_summary(rows):
    """Counts AND weights, plus the biggest positions on each side so the
    person can see which companies rather than take a tally on faith."""
    out = {"up": 0, "down": 0, "sideways": 0, "unscored": 0,
           "up_pct": 0.0, "down_pct": 0.0, "sideways_pct": 0.0,
           "unscored_pct": 0.0,
           "score_date": None, "top_down": [], "top_up": []}
    ups, downs = [], []
    for r in rows:
        pct = float(r["pct_of_nav"])
        out["score_date"] = out["score_date"] or (
            str(r.get("score_date")) if r.get("score_date") else None)
        d = _direction(r["supertrend_dir"])

        # SIDEWAYS WINS over the supertrend direction where both apply.
        # A stock sitting in a range is not really trending, and supertrend
        # flips late in flat markets -- calling it an uptrend on the strength
        # of a signal that is about to reverse says more than the data does.
        if d is not None and _is_sideways(r):
            out["sideways"] += 1
            out["sideways_pct"] += pct
        elif d == "up":
            out["up"] += 1
            out["up_pct"] += pct
            ups.append((pct, r.get("name"), r.get("symbol")))
        elif d == "down":
            out["down"] += 1
            out["down_pct"] += pct
            downs.append((pct, r.get("name"), r.get("symbol")))
        else:
            out["unscored"] += 1
            out["unscored_pct"] += pct

    for key, src in (("top_down", downs), ("top_up", ups)):
        out[key] = [{"name": n, "symbol": sym, "pct": round(p, 2)}
                    for p, n, sym in sorted(src, reverse=True)[:5]]
    for k in ("up_pct", "down_pct", "sideways_pct", "unscored_pct"):
        out[k] = round(out[k], 1)
    return out


@router.get("/search")
def search_funds(q: str):
    """Typeahead for the fund picker. Only funds we actually hold a
    portfolio for -- offering one we cannot explode wastes the person's
    time at the point they are least able to tell."""
    if len(q.strip()) < 2:
        return []

    words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in q.split()]
    words = [w for w in words if w][:6]          # cap it; 6 is generous
    if not words:
        return []

    name_n = _NORM % "c.scheme_name"
    amc_n = _NORM % "c.amc_name"

    clauses, params = [], {}
    for i, word in enumerate(words):
        key = "s%d" % i
        clauses.append("(%s LIKE %%(%s)s OR %s LIKE %%(%s)s)"
                       % (name_n, key, amc_n, key))
        params[key] = "%" + word + "%"

    sql = SEARCH_HEAD + " AND " + " AND ".join(clauses) + SEARCH_TAIL
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, sql, params)
        if rows:
            return rows

        # NOTHING MATCHED. Try progressively harder rather than giving up,
        # because a picker that returns nothing tells the person only that
        # they were wrong, not what to type instead.

        # 1. Drop the least distinctive word. "invesco india mid" fails if
        #    the registered name has no "india"; "invesco mid" finds it.
        if len(words) > 1:
            keep = sorted(words, key=len, reverse=True)[:len(words) - 1]
            sub = {}
            cl = []
            for i, word in enumerate(keep):
                key = "s%d" % i
                cl.append("(%s LIKE %%(%s)s OR %s LIKE %%(%s)s)"
                          % (name_n, key, amc_n, key))
                sub[key] = "%" + word + "%"
            rows = _q(cur, SEARCH_HEAD + " AND " + " AND ".join(cl) + SEARCH_TAIL,
                      sub)
            if rows:
                return rows

        # 2. Trigram similarity, for a misremembered or misspelt name.
        #    pg_trgm may not be installed -- if it is not, this raises and
        #    we return the empty list rather than failing the request.
        try:
            return _q(cur, FUZZY, {"q": "".join(words)})
        except psycopg.Error:
            conn.rollback()
            return []


def _score_access(request):
    """Reuse api.py's gating rather than reimplementing it.

    Imported inside the function on purpose: api.py imports THIS module at
    startup, so a module-level import here would be circular. If the import
    fails for any reason we fall back to treating the caller as unpaid,
    which withholds detail rather than leaking it.
    """
    try:
        from api import current_user, has_score_access
        return has_score_access(current_user(request))
    except Exception:
        return False


def _has_subscription(request):
    """Is the caller subscribed? The question for the PAID PRODUCTS.

    Separate from _score_access on purpose: scores can be switched off for
    everyone (MF_SCORES_ENABLED), and that must not lock subscribers out of
    look-through, the screener or the plan page, which are not scores.
    Fails closed, like _score_access.
    """
    try:
        from api import current_user, has_subscription
        return has_subscription(current_user(request))
    except Exception:
        return False


def _scores_on():
    """Are scores switched on at all, for anybody? Used by routes that are
    score-derived through and through, so they can refuse outright."""
    try:
        from api import SCORES_ENABLED
        return bool(SCORES_ENABLED)
    except Exception:
        return False


def _require_scores(request):
    """For routes whose ONLY content is score-derived (the rising split's
    history and its batch form). With scores switched off there is nothing
    to return, so the route answers 404 for everyone rather than 402 --
    "subscribe" would be the wrong message for something nobody can buy.
    """
    if not _scores_on():
        raise HTTPException(404, "Not available yet.")
    _require_subscription(request)


def _require_subscription(request):
    """403 unless the caller has a live subscription.

    RAISES rather than degrading, for the endpoints where a partial answer
    would be worse than none: half a look-through is not a cheaper
    look-through, it is a wrong one.

    The message is deliberately plain and mentions no price. The page
    decides how to sell; this only decides who is let in.
    """
    if not _has_subscription(request):
        raise HTTPException(
            402, "This is part of the subscription. Sign in with a "
                 "subscribed account to see it.")


FUND_MAKEUP = """
SELECT COALESCE(cc.cap_class, 'Unclassified') AS cap_class,
       COALESCE(m.sector, m.industry)         AS sector,
       h.pct_of_nav
FROM mf_holding h
LEFT JOIN stock_master m ON m.isin = h.isin
-- AMFI's categorisation nearest to the date these holdings were disclosed,
-- preferring one at or before it. Reading a single stored column instead
-- would mean March holdings silently reclassified by September's list --
-- the fund would be redescribed with nothing on the page admitting it.
LEFT JOIN LATERAL (
    SELECT c.cap_class
    FROM stock_cap_class c
    WHERE c.isin = h.isin
    ORDER BY (c.as_of_period <= h.as_of_date) DESC,
             abs(c.as_of_period - h.as_of_date)
    LIMIT 1
) cc ON true
WHERE h.scheme_code = %(code)s
  AND h.as_of_date  = %(as_of)s
  AND h.isin IS NOT NULL
  AND h.pct_of_nav IS NOT NULL
"""


GOAL_CARD_META = """
SELECT s.scheme_code, s.scheme_name, s.amc_name, vc.category
FROM mf_scheme s
LEFT JOIN v_scheme_category vc ON vc.scheme_code = s.scheme_code
WHERE s.scheme_code = ANY(%(codes)s)
"""

# Every period we hold, not just 1/3/5 -- a goal can be seven or ten years
# out, and rounding a ten-year horizon down to five would answer a
# different question from the one asked.
#
# as_of_date is scoped PER FUND via the latest CTE. A bare MAX over the
# whole table blanks every fund missing from a partial nightly run.
GOAL_CARD_RETURNS = """
WITH latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d
    FROM mf_returns WHERE scheme_code = ANY(%(codes)s)
    GROUP BY scheme_code
)
SELECT r.scheme_code, r.period, r.years,
       ROUND(r.fund_cagr, 2)   AS fund_cagr,
       ROUND(r.bench_cagr, 2)  AS bench_cagr,
       ROUND(r.excess_cagr, 2) AS excess_cagr,
       ROUND(r.fund_cagr - cr.median_cagr, 2) AS vs_category,
       r.category_rank, r.category_count,
       COALESCE(cr.rank_meaningful, false) AS rank_meaningful,
       b.display_name AS benchmark_name,
       r.as_of_date
FROM mf_returns r
JOIN latest l ON l.scheme_code = r.scheme_code AND l.d = r.as_of_date
LEFT JOIN benchmark_master b USING (benchmark_id)
LEFT JOIN mf_category_return cr
       ON cr.category = r.category AND cr.as_of_date = r.as_of_date
      AND cr.period   = r.period
ORDER BY r.scheme_code, r.years
"""


class FundCardsRequest(BaseModel):
    codes: List[str]


# Funds whose PAST return over one window reached a given figure.
#
# Built on v_fund_canonical, so a fund appears once rather than once per
# plan -- Direct and Regular of the same scheme are not two answers.
#
# as_of_date is scoped per fund, and the count of everything considered is
# returned alongside the matches: "31 of 412" is a materially different
# statement from a bare list of 31, and the list alone reads like a
# shortlist somebody assembled.
SCREEN = """
WITH latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d FROM mf_returns GROUP BY scheme_code
),
universe AS (
    SELECT c.canonical_scheme_code AS scheme_code,
           c.scheme_name, c.amc_name, vc.category,
           ROUND(r.fund_cagr, 2)   AS fund_cagr,
           ROUND(r.excess_cagr, 2) AS excess_cagr,
           ROUND(r.fund_cagr - cr.median_cagr, 2) AS vs_category,
           r.category_rank, r.category_count,
           COALESCE(cr.rank_meaningful, false) AS rank_meaningful,
           r.as_of_date,
           -- Risk, carried alongside return so the two can be filtered
           -- and shown together. A fund that cleared the bar by taking
           -- twice the volatility of its peers cleared it differently,
           -- and a screen that only ever showed CAGR could not say so.
           risk.sharpe, risk.sortino, risk.volatility, risk.max_drawdown,
           -- Rolling-window figures, alongside the point-to-point ones
           -- above. pct_above_hurdle is the share of rolling windows that
           -- cleared hurdle_pct a year -- "consistent", not "lucky once".
           risk.pct_above_hurdle, risk.hurdle_pct
    FROM v_fund_canonical c
    JOIN mf_returns r ON r.scheme_code = c.canonical_scheme_code
    JOIN latest l ON l.scheme_code = r.scheme_code AND l.d = r.as_of_date
    LEFT JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
    LEFT JOIN mf_category_return cr
           ON cr.category = r.category AND cr.as_of_date = r.as_of_date
          AND cr.period   = r.period
    -- LATERAL, not a join to a MAX() subquery: one indexed lookup per
    -- fund taking that fund's newest row, which is exactly what the
    -- index on (scheme_code, window_years, as_of_date DESC) is for.
    LEFT JOIN LATERAL (
        SELECT g.sharpe, g.sortino, g.volatility, g.max_drawdown,
               g.pct_above_hurdle, g.hurdle_pct
        FROM mf_rolling g
        WHERE g.scheme_code = c.canonical_scheme_code
          AND g.window_years = %(risk_window)s
        ORDER BY g.as_of_date DESC
        LIMIT 1
    ) risk ON true
    WHERE r.period = %(period)s
      AND r.fund_cagr IS NOT NULL
)
SELECT * FROM universe
"""


# Funds too new to have a record over the window being screened on.
#
# A fund launched two years ago has no 3Y row, so the screen above cannot
# see it at all -- and a screen that silently drops every new fund is
# telling the reader the category holds nothing new, which is a claim it
# never checked. Absent is not zero.
#
# The figure used is the SI row: annualised from the fund's FIRST NAV, with
# its benchmark measured over that same stretch. That last part is what
# makes the number worth showing -- a fund up 24% in its first eighteen
# months during a market that rose 22% has told you almost nothing, and the
# excess says so. `years` carries the real age so the page can print it.
#
# Deliberately NOT joined to mf_category_return: there is no category
# median over an arbitrary since-launch window, and inventing one by
# borrowing the 3Y median would compare two different stretches of market.
SCREEN_YOUNG = """
WITH latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d FROM mf_returns GROUP BY scheme_code
),
universe AS (
    SELECT c.canonical_scheme_code AS scheme_code,
           c.scheme_name, c.amc_name, vc.category,
           ROUND(r.fund_cagr, 2)   AS fund_cagr,
           ROUND(r.excess_cagr, 2) AS excess_cagr,
           ROUND(r.years, 2)       AS age_years,
           r.start_date, r.as_of_date
    FROM v_fund_canonical c
    JOIN mf_returns r ON r.scheme_code = c.canonical_scheme_code
    JOIN latest l ON l.scheme_code = r.scheme_code AND l.d = r.as_of_date
    LEFT JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
    WHERE r.period = 'SI'
      AND r.fund_cagr IS NOT NULL
      AND r.years IS NOT NULL
)
SELECT * FROM universe
"""

# How long each screening window is, in years. Used only to decide which
# funds count as too new for it.
PERIOD_YEARS = {"1Y": 1.0, "3Y": 3.0, "5Y": 5.0, "7Y": 7.0, "10Y": 10.0}


# Kinds of fund that do not belong in a core plan, whatever their record.
#
# A sector or thematic fund is a bet on one industry: if it is the only
# thing clearing a 30% requirement, that says the sector has just run, not
# that it will carry someone's goal. ELSS carries a lock-in, and index
# funds and ETFs are a different decision from picking a manager.
SCREEN_EXCLUDE = ("Sectoral/Thematic", "ELSS", "ETFs", "Index Funds",
                  "FoF Domestic", "FoF Overseas", "Gold ETF", "Debt ETF",
                  "Other Debt Scheme")


# =====================================================================
# NARROWING THE UNIVERSE -- shared by the plan screen and the selector.
#
# These filters run BEFORE anything is ranked, scored or combined. They
# do not recommend; they decide what the rest of the machinery is
# allowed to consider.
#
# The ordering is the design decision. Filtering the OUTPUT of a fund
# search breaks it -- the selector picks funds that work as a SET, so
# removing one afterwards leaves a set that no longer lands where it was
# chosen to land. Narrowing the input leaves every downstream rule
# intact.
#
# ONE IMPLEMENTATION, imported by both. There were nearly two: the plan
# page calls /portfolio/screen and the selector page calls
# /selector/suggest, and a filter that meant one thing on one screen and
# something slightly different on the other is the kind of divergence
# nobody finds until a client asks why two pages disagree.
#
# A MISSING VALUE FAILS AN ACTIVE FILTER, and it is counted separately.
# A fund with no rolling record cannot be shown to have a Sharpe above
# 0.3, and admitting it "because we don't know" would let precisely the
# funds with no history through a filter meant to demand history. But
# "dropped because it failed" and "dropped because we hold nothing" are
# different facts, so the caller gets both numbers and can say which.
# =====================================================================
FUND_FILTERS = ("require_beat_benchmark", "min_excess_cagr",
                "max_category_rank_pct", "min_sharpe", "min_sortino",
                "max_volatility", "max_drawdown_pct", "min_pct_above_hurdle")


def apply_fund_filters(pool, opts):
    """Narrow a list of fund rows. Returns (kept, [step, ...]).

    Each step says what it tested, how many it removed, and how many of
    those had no value at all -- so a page can show its working, and an
    empty result can name the filter that emptied it rather than leaving
    somebody to loosen them one at a time.
    """
    def get(name):
        return getattr(opts, name, None)

    def num(v):
        return None if v is None else float(v)

    period = getattr(opts, "period", None) or getattr(
        opts, "return_period", None) or "3Y"

    # EVERY LIMIT IS BOUND AS A DEFAULT ARGUMENT, not captured by closure.
    #
    # `lim` is reassigned by each branch below, and a lambda that reads it
    # by reference sees only the LAST value once the loop runs. That bug
    # was live for about four minutes: asking for "top 25% of category"
    # together with "Sharpe at least 0.35" tested the category share
    # against 0.35 and removed every fund, including one ranked 5th of 29.
    # Silent, plausible, and wrong -- it looked like a strict filter doing
    # its job.
    tests = []
    if get("require_beat_benchmark"):
        tests.append((f"beat its benchmark over {period}",
                      lambda f: num(f.get("excess_cagr")),
                      lambda v: v > 0))
    if get("min_excess_cagr") is not None:
        lim = float(get("min_excess_cagr"))
        tests.append((f"beat benchmark by {lim:g} pts a year",
                      lambda f: num(f.get("excess_cagr")),
                      lambda v, lim=lim: v >= lim))
    if get("max_category_rank_pct") is not None:
        lim = float(get("max_category_rank_pct"))

        # A SHARE of the category, not a place in it. "Top 25%" means the
        # same thing in a 12-fund category and a 40-fund one; "top 10"
        # silently means "top 83%" in the first and "top 25%" in the
        # second, which is not a filter anyone would choose on purpose.
        def rank_share(f):
            r, n = f.get("category_rank"), f.get("category_count")
            if not r or not n:
                return None
            return 100.0 * float(r) / float(n)

        tests.append((f"top {lim:g}% of its category", rank_share,
                      lambda v, lim=lim: v <= lim))
    if get("min_sharpe") is not None:
        lim = float(get("min_sharpe"))
        tests.append((f"Sharpe at least {lim:g}",
                      lambda f: num(f.get("sharpe")),
                      lambda v, lim=lim: v >= lim))
    if get("min_sortino") is not None:
        lim = float(get("min_sortino"))
        tests.append((f"Sortino at least {lim:g}",
                      lambda f: num(f.get("sortino")),
                      lambda v, lim=lim: v >= lim))
    if get("min_pct_above_hurdle") is not None:
        lim = float(get("min_pct_above_hurdle"))
        # A share of rolling windows, not a single figure -- "cleared its
        # hurdle 8 years out of 10" rather than "was up last year".
        tests.append((f"cleared its hurdle in at least {lim:g}% of "
                      f"rolling {get('risk_window') or 3}-year stretches",
                      lambda f: num(f.get("pct_above_hurdle")),
                      lambda v, lim=lim: v >= lim))
    if get("max_volatility") is not None:
        lim = float(get("max_volatility"))
        tests.append((f"volatility no more than {lim:g}%",
                      lambda f: num(f.get("volatility")),
                      lambda v, lim=lim: v <= lim))
    if get("max_drawdown_pct") is not None:
        lim = float(get("max_drawdown_pct"))
        # Stored negative: -32.4 means it fell 32.4%. The caller gives a
        # positive depth, because "no worse than 35%" is how it is said.
        tests.append((f"worst fall no deeper than {lim:g}%",
                      lambda f: num(f.get("max_drawdown")),
                      lambda v, lim=lim: abs(v) <= lim))

    steps, kept = [], list(pool)
    for label, read, ok in tests:
        survivors, failed, missing = [], 0, 0
        for f in kept:
            v = read(f)
            if v is None:
                missing += 1
            elif ok(v):
                survivors.append(f)
            else:
                failed += 1
        steps.append({"filter": label, "removed": failed + missing,
                      "no_data": missing, "left": len(survivors)})
        kept = survivors
    return kept, steps


class ScreenRequest(BaseModel):
    period: str = "3Y"
    min_cagr: float
    limit: int = 10

    # Narrowing filters. All optional, all off by default -- a screen
    # that quietly applied a Sharpe floor nobody asked for would return
    # different funds for the same goal depending on a default, which is
    # impossible to notice and impossible to explain later.
    require_beat_benchmark: bool = False
    min_excess_cagr: Optional[float] = None
    max_category_rank_pct: Optional[float] = None
    min_sharpe: Optional[float] = None
    min_sortino: Optional[float] = None
    min_pct_above_hurdle: Optional[float] = None
    max_volatility: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    # Which rolling window the risk figures are read from. Three years is
    # the house default and the one score_rolling.py always computes.
    risk_window: int = 3
    # Top N in each category, whether or not they clear the bar. Showing
    # only what clears it hides the shape of the market: if one thematic
    # fund is the sole name above 30%, the useful answer is "here is the
    # best large cap, the best mid cap and the best small cap, and none of
    # them reaches it either".
    per_category: int = 0
    # Top N funds in each category that are too NEW to have a record over
    # the window at all. Shown apart from the ranked list, never inside it:
    # a since-launch figure over eighteen months and a three-year CAGR are
    # two different measurements, and sorting them together would put the
    # newest funds on top for no reason a reader could defend.
    young_per_category: int = 0
    categories: List[str] = []
    include_thematic: bool = False


@router.get("/screen-universe")
def screen_universe(request: Request, period: str = "3Y", risk_window: int = 3):
    """Every measurable fund's five numbers, plus what the real spread is.

    WHY THE WHOLE SET, IN ONE CALL
        A filter the user cannot see the effect of is not a filter, it is
        a form. Sending the numbers once lets the page recount as a slider
        moves -- instantly, with no round trip -- so moving a handle and
        watching 1,400 funds become 180 is the interaction, rather than
        pressing a button and hoping.

        It is a few hundred kilobytes of small integers. Cheaper than the
        five round trips the alternative needs, and it makes the page
        honest: every count on screen is computed from the same rows the
        search will use.

    WHY THE PERCENTILES MATTER MORE THAN THE MIN AND MAX
        Nobody knows what a good Sharpe ratio is -- not because they are
        careless but because the number means nothing without the
        distribution behind it. 0.35 is meaningless; "better than two
        funds in three" is not.

        So every cut point below is measured from the funds that actually
        exist today rather than from a threshold somebody typed once. If
        the market changes, the presets change with it.
    """
    # Same gate as /screen, which this exists to drive. It carries the
    # per-fund figures for the entire universe, so it is more of the paid
    # product than the screen is, not less.
    _require_subscription(request)

    if period not in ("1Y", "3Y", "5Y", "7Y", "10Y"):
        raise HTTPException(400, "Unsupported period.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, SCREEN, {"period": period,
                                "risk_window": max(1, int(risk_window))})
        # Funds too new to have a record over this window. Sent as well,
        # not instead: a fund launched eighteen months ago has no
        # three-year figure, which is not the same as a bad one, and
        # leaving it out of the list entirely tells somebody the market
        # holds nothing new -- a claim nobody checked.
        yrows = _q(cur, SCREEN_YOUNG)
        have = {str(r["scheme_code"]) for r in rows}
        yrows = [r for r in yrows
                 if str(r["scheme_code"]) not in have
                 and r["age_years"] is not None
                 and float(r["age_years"]) < PERIOD_YEARS.get(period, 3.0)]

    def out(rows, key, sign=1.0):
        vals = sorted(sign * float(r[key]) for r in rows if r[key] is not None)
        if len(vals) < 20:
            # Too few to describe a distribution. Returning a shape built
            # from eleven numbers would invite a slider that looks
            # authoritative and is not.
            return None
        def pct(p):
            return round(vals[min(len(vals) - 1, int(p * (len(vals) - 1)))], 2)
        return {"n": len(vals), "min": round(vals[0], 2),
                "max": round(vals[-1], 2),
                "p10": pct(0.10), "p25": pct(0.25), "p50": pct(0.50),
                "p75": pct(0.75), "p90": pct(0.90)}

    def rank_share(r):
        a, b = r["category_rank"], r["category_count"]
        return None if not a or not b else 100.0 * float(a) / float(b)

    ranked = [{"category_rank": r["category_rank"],
               "category_count": r["category_count"],
               "share": rank_share(r)} for r in rows]

    return {
        "period": period,
        "funds_total": len(rows),
        "spread": {
            "excess_cagr": out(rows, "excess_cagr"),
            # Stored negative. Flipped to a positive depth here, because
            # "fell 32%" is the sentence a reader thinks in and a slider
            # running from -60 to 0 reads backwards to everyone.
            "max_drawdown": out(rows, "max_drawdown", -1.0),
            "sharpe": out(rows, "sharpe"),
            "sortino": out(rows, "sortino"),
            "volatility": out(rows, "volatility"),
            "pct_above_hurdle": out(rows, "pct_above_hurdle"),
            "rank_share": out([{"v": r["share"]} for r in ranked
                               if r["share"] is not None], "v"),
        },
        # Uniform across every row for a given run (one env-configured
        # hurdle rate), but read off the data rather than the env var
        # directly -- so the number shown always matches what was actually
        # screened against, even if the setting changes before a redeploy.
        "hurdle_pct": next((float(r["hurdle_pct"]) for r in rows
                            if r["hurdle_pct"] is not None), None),
        # Compact keys on purpose: this is the bulk of the payload and the
        # names are never read by a person.
        # Everything a row needs to be drawn, so the list itself is
        # client-side too. The whole interaction -- pick a category, tick
        # "beats its benchmark", watch 737 become 181 -- then happens
        # without a round trip, which is the difference between a filter
        # and a form.
        "funds": [{
            "c": str(r["scheme_code"]),
            "n": r["scheme_name"],
            "a": r["amc_name"],
            "k": r["category"],
            "cg": None if r["fund_cagr"] is None else float(r["fund_cagr"]),
            "rk": r["category_rank"], "rn": r["category_count"],
            "rm": bool(r["rank_meaningful"]),
            "ex": None if r["excess_cagr"] is None else float(r["excess_cagr"]),
            "rs": rank_share(r),
            "sh": None if r["sharpe"] is None else float(r["sharpe"]),
            "so": None if r["sortino"] is None else float(r["sortino"]),
            "vo": None if r["volatility"] is None else float(r["volatility"]),
            "dd": None if r["max_drawdown"] is None
                  else abs(float(r["max_drawdown"])),
            "ph": None if r["pct_above_hurdle"] is None
                  else float(r["pct_above_hurdle"]),
        } for r in rows],
        # Kept in their own list rather than mixed into the one above.
        # A since-launch figure over eighteen months and a three-year
        # CAGR are two different measurements; sorting them together
        # would float the newest funds to the top for no reason anybody
        # could defend. `y` is the age in years, `cg` the return since
        # launch -- named the same as the main list so one row renderer
        # can draw both.
        "young": [{
            "c": str(r["scheme_code"]),
            "n": r["scheme_name"],
            "a": r["amc_name"],
            "k": r["category"],
            "cg": None if r["fund_cagr"] is None else float(r["fund_cagr"]),
            "ex": None if r["excess_cagr"] is None else float(r["excess_cagr"]),
            "y": float(r["age_years"]),
            "since": str(r["start_date"]) if r["start_date"] else None,
            "new": True,
        } for r in yrows],
    }


@router.post("/screen")
def screen(body: ScreenRequest, request: Request):
    """Funds that HAVE returned at least min_cagr over one window.

    A screen of history, and nothing more. It says which funds cleared a
    figure in the past; it does not say which will clear it again, and the
    response deliberately carries the numbers a reader needs to see that
    for themselves:

      considered   -- how many funds had a record over this window at all
      matched      -- how many cleared the figure
      also_beat_benchmark -- of those, how many also beat their own index

    That last one matters most. A fund returning 16% in a category whose
    index returned 19% cleared the bar without adding anything, and a list
    that hid it would flatter the fund.
    """
    # The goal and what-it-needs tabs are open to everyone; the funds tab
    # is where the paid product starts.
    _require_subscription(request)

    if body.period not in ("1Y", "3Y", "5Y", "7Y", "10Y"):
        raise HTTPException(400, "Unsupported period.")
    limit = max(1, min(int(body.limit or 10), 50))

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, SCREEN, {"period": body.period,
                                "risk_window": max(1, int(body.risk_window))})
        yrows = _q(cur, SCREEN_YOUNG) if body.young_per_category > 0 else []

    # Captured BEFORE the filters below narrow `rows`: a fund is "young"
    # only if it has no row at this period at all. Testing against the
    # filtered list would relabel every fund the category filter removed.
    has_period = {str(r["scheme_code"]) for r in rows}

    # NARROW FIRST, before the CAGR bar, the category cut or the ranking.
    #
    # Applied to `rows` and NOT to `yrows`: the young list is funds with
    # no record over this window, so they have no excess return, no rank
    # and no rolling risk figures either. Every filter would remove all
    # of them for having no data, which is true and useless -- they are
    # shown separately precisely because they cannot be judged yet.
    screened = len(rows)
    rows, screen_steps = apply_fund_filters(rows, body)
    screened_out = screened - len(rows)

    if not body.include_thematic:
        rows = [r for r in rows if r["category"] not in SCREEN_EXCLUDE]
    if body.categories:
        want = {c.lower() for c in body.categories}
        rows = [r for r in rows if (r["category"] or "").lower() in want]

    # The same exclusions, applied to the same standard: a thematic fund is
    # no more suitable for being new, and a category nobody asked for is
    # still a category nobody asked for.
    window = PERIOD_YEARS.get(body.period, 3.0)
    if yrows:
        if not body.include_thematic:
            yrows = [r for r in yrows if r["category"] not in SCREEN_EXCLUDE]
        if body.categories:
            want = {c.lower() for c in body.categories}
            yrows = [r for r in yrows if (r["category"] or "").lower() in want]
        yrows = [r for r in yrows
                 if str(r["scheme_code"]) not in has_period
                 and float(r["age_years"]) < window]

    considered = len(rows)
    if not considered:
        raise HTTPException(404, "No returns are recorded for that window.")

    hits = [r for r in rows if float(r["fund_cagr"]) >= body.min_cagr]
    beat = sum(1 for r in hits
               if r["excess_cagr"] is not None and float(r["excess_cagr"]) > 0)

    # Ranked by the figure being screened on, because that is the only
    # ordering the request implies. It is NOT a ranking of quality, and the
    # page says so -- the biggest past returns cluster in the most volatile
    # categories, which is why category travels with every row.
    hits.sort(key=lambda r: -float(r["fund_cagr"]))

    # The best few in each category, clearing the bar or not.
    by_cat = {}
    for r in rows:
        cat = r["category"] or "Other"
        by_cat.setdefault(cat, []).append(r)
    for cat in by_cat:
        by_cat[cat].sort(key=lambda r: -float(r["fund_cagr"]))

    def shape(r):
        return {
            "scheme_code": str(r["scheme_code"]),
            "scheme_name": r["scheme_name"],
            "amc_name": r["amc_name"],
            "category": r["category"],
            "fund_cagr": float(r["fund_cagr"]),
            "excess_cagr": float(r["excess_cagr"]) if r["excess_cagr"] is not None else None,
            "vs_category": float(r["vs_category"]) if r["vs_category"] is not None else None,
            "category_rank": r["category_rank"],
            "category_count": r["category_count"],
            "rank_meaningful": bool(r["rank_meaningful"]),
            "clears_bar": float(r["fund_cagr"]) >= body.min_cagr,
        }

    def shape_young(r):
        return {
            "scheme_code": str(r["scheme_code"]),
            "scheme_name": r["scheme_name"],
            "amc_name": r["amc_name"],
            "category": r["category"],
            # Annualised since the fund's first NAV -- NOT the same window
            # as fund_cagr above, which is why it travels under its own name
            # and with the age beside it.
            "since_cagr": float(r["fund_cagr"]),
            "excess_cagr": float(r["excess_cagr"]) if r["excess_cagr"] is not None else None,
            "age_years": float(r["age_years"]),
            "since": str(r["start_date"]) if r["start_date"] else None,
            "clears_bar": float(r["fund_cagr"]) >= body.min_cagr,
        }

    young_by_cat = {}
    for r in yrows:
        young_by_cat.setdefault(r["category"] or "Other", []).append(r)
    for cat in young_by_cat:
        young_by_cat[cat].sort(key=lambda r: -float(r["fund_cagr"]))

    per_cat = []
    if body.per_category > 0:
        for cat in sorted(by_cat, key=lambda c: -float(by_cat[c][0]["fund_cagr"])):
            best = by_cat[cat][:body.per_category]
            per_cat.append({
                "category": cat,
                "fund_count": len(by_cat[cat]),
                # How many in this whole category clear it -- the number
                # that says whether the requirement is reachable here at
                # all, rather than whether one fund happened to.
                "clearing": sum(1 for r in by_cat[cat]
                                if float(r["fund_cagr"]) >= body.min_cagr),
                "funds": [shape(r) for r in best],
                # Counted over the whole category, not over the two shown,
                # so "3 funds here are newer than that" is a true statement
                # about the category rather than about this list.
                "young_count": len(young_by_cat.get(cat, [])),
                "young": [shape_young(r)
                          for r in young_by_cat.get(cat, [])[:body.young_per_category]],
            })

        # A category whose funds are ALL too new has no entry above, and
        # dropping it would hide the newest corner of the market entirely.
        for cat in sorted(young_by_cat,
                          key=lambda c: -float(young_by_cat[c][0]["fund_cagr"])):
            if any(g["category"] == cat for g in per_cat):
                continue
            per_cat.append({
                "category": cat,
                "fund_count": 0,
                "clearing": 0,
                "funds": [],
                "young_count": len(young_by_cat[cat]),
                "young": [shape_young(r)
                          for r in young_by_cat[cat][:body.young_per_category]],
            })

    return {
        "period": body.period,
        "min_cagr": body.min_cagr,
        "considered": considered,
        # What the narrowing removed, step by step, before the CAGR bar
        # was applied. Returned whether or not any filter was set, so the
        # page can always show its working -- and so `considered` above
        # is never a number the reader has to take on trust.
        "screen": screen_steps,
        "screened_out": screened_out,
        # Kept OUT of `considered`, `matched` and `also_beat_benchmark` on
        # purpose: those three describe the funds measured over `period`,
        # and quietly folding funds measured over a different stretch into
        # them would make all three numbers mean nothing in particular.
        "young_considered": len(yrows),
        "categories_available": sorted(by_cat),
        "by_category": per_cat,
        "matched": len(hits),
        "also_beat_benchmark": beat,
        "as_of": str(hits[0]["as_of_date"]) if hits else None,
        "funds": [{
            "scheme_code": str(r["scheme_code"]),
            "scheme_name": r["scheme_name"],
            "amc_name": r["amc_name"],
            "category": r["category"],
            "fund_cagr": float(r["fund_cagr"]),
            "excess_cagr": float(r["excess_cagr"]) if r["excess_cagr"] is not None else None,
            "vs_category": float(r["vs_category"]) if r["vs_category"] is not None else None,
            "category_rank": r["category_rank"],
            "category_count": r["category_count"],
            "rank_meaningful": bool(r["rank_meaningful"]),
        } for r in hits[:limit]],
    }


# ---------------------------------------------------------------------
# TWO FUNDS, NAME BY NAME.
#
# The overlap FIGURE already existed in two places -- the look-through
# and the selector -- and both report a single number. A number is where
# the question starts, not where it ends: "these two are 31% the same"
# invites "the same in WHAT", and neither could answer.
# ---------------------------------------------------------------------
LATEST_HOLDING = """
SELECT MAX(as_of_date) AS d FROM mf_holding WHERE scheme_code = %(code)s
"""


class OverlapRequest(BaseModel):
    a: str
    b: str


@router.post("/overlap")
def overlap(body: OverlapRequest):
    """What two funds hold in common, and what each holds alone.

    The headline is the standard measure and the same one used elsewhere
    on the site: the SMALLER of the two weights in every shared company,
    added up. Two funds holding forty of the same names in tiny size
    overlap very little, and counting names rather than money would say
    the opposite.

    WHAT THIS DELIBERATELY DOES NOT DO
        It returns no verdict. Whether 31% is too much depends on why
        someone holds both funds, and the response carries the numbers a
        reader needs to decide that instead of deciding it for them.
    """
    a, b = str(body.a).strip(), str(body.b).strip()
    if not a or not b:
        raise HTTPException(400, "Two funds are needed.")
    if a == b:
        raise HTTPException(400, "That is the same fund twice.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        meta = {str(r["scheme_code"]): r
                for r in _q(cur, GOAL_CARD_META, {"codes": [a, b]})}
        for code in (a, b):
            if code not in meta:
                raise HTTPException(404, "One of those funds was not found.")

        own = {}
        for code in (a, b):
            row = _q(cur, LATEST_HOLDING, {"code": code}, one=True)
            own[code] = row["d"] if row else None
        for code in (a, b):
            if not own[code]:
                raise HTTPException(
                    422, "No portfolio has been loaded for "
                         + meta[code]["scheme_name"] + ".")

        # ALIGNED, and to the EARLIER month. A fund that has disclosed
        # August measured against one that has only disclosed July is not
        # an overlap -- it is a month of trading, counted as difference.
        # The response says which month each side was read at so a reader
        # can see when they are not the same.
        target = min(own[a], own[b])
        as_of, held, gaps = {}, {}, {}
        for code in (a, b):
            r = _q(cur, AS_OF_FOR_FUND, {"code": code, "target": target}, one=True)
            as_of[code] = r["d"] if r else None
            held[code] = _q(cur, HOLDINGS, {"code": code, "as_of": as_of[code]})
            gaps[code] = _q(cur, GAP_BREAKDOWN, {"code": code, "as_of": as_of[code]})

    def by_isin(rows):
        out = {}
        for r in rows:
            if not r["isin"] or r["pct_of_nav"] is None:
                continue
            # A fund can list one company twice (two series of the same
            # scrip). Adding rather than overwriting keeps the weight
            # right; overwriting silently lost the smaller line.
            cur_row = out.setdefault(r["isin"], {
                "isin": r["isin"], "name": r["name"],
                "sector": r["sector"], "cap": r["cap_class"], "pct": 0.0})
            cur_row["pct"] += float(r["pct_of_nav"])
        return out

    ha, hb = by_isin(held[a]), by_isin(held[b])
    shared = set(ha) & set(hb)

    both = [{
        "isin": k, "name": ha[k]["name"], "sector": ha[k]["sector"],
        "cap": ha[k]["cap"],
        "pct_a": round(ha[k]["pct"], 2), "pct_b": round(hb[k]["pct"], 2),
        # What this company contributes to the headline -- the list is
        # ordered by it, so the top of the list explains the number at
        # the top of the page rather than merely illustrating it.
        "shared_pct": round(min(ha[k]["pct"], hb[k]["pct"]), 2),
    } for k in shared]
    both.sort(key=lambda r: -r["shared_pct"])

    def alone(mine, theirs):
        out = [{"isin": k, "name": v["name"], "sector": v["sector"],
                "cap": v["cap"], "pct": round(v["pct"], 2)}
               for k, v in mine.items() if k not in theirs]
        out.sort(key=lambda r: -r["pct"])
        return out

    def side(code, mine):
        m = meta[code]
        return {
            "scheme_code": code,
            "name": m["scheme_name"],
            "amc_name": m["amc_name"],
            "category": m["category"],
            "as_of": str(as_of[code]),
            "holdings": len(mine),
            # Priced equity only. Everything the promote could not match
            # to a known stock is listed beside it rather than folded in,
            # because a total reading 72 with no explanation looks like a
            # fault in the arithmetic.
            "equity_pct": round(sum(v["pct"] for v in mine.values()), 2),
            "missing": [{"kind": g["kind"], "pct": float(g["pct"])}
                        for g in gaps[code]],
        }

    return {
        "aligned": as_of[a] == as_of[b],
        "as_of": str(target),
        "overlap_pct": round(sum(r["shared_pct"] for r in both), 2),
        "common_count": len(both),
        "a": side(a, ha),
        "b": side(b, hb),
        "both": both,
        "only_a": alone(ha, hb),
        "only_b": alone(hb, ha),
    }


# =====================================================================
# WHAT CHANGED SINCE LAST MONTH
#
# Two views of one comparison: a fund's portfolio month on month, and one
# company's owners month on month.
#
# THE DISTINCTION THIS IS BUILT AROUND
#     A holding going from 5% to 6% of a fund has two entirely different
#     explanations. The manager bought more -- or the manager did nothing
#     and the share price rose faster than the rest of the portfolio. In
#     pct_of_nav those are indistinguishable, and a change log that
#     reported only weight would tell a reader a manager had conviction
#     when no order was placed.
#
#     So both are returned. The SHARE COUNT says whether anyone acted; the
#     WEIGHT says what it means for the money. Every row carries both, and
#     the four combinations are all real:
#
#         shares up,   weight up    bought
#         shares flat, weight up    the market did it, nobody traded
#         shares down, weight up    sold into strength, still ended bigger
#         shares flat, weight down  other holdings grew, or this one fell
# =====================================================================

# The two most recent disclosures FOR THIS FUND, not the two most recent
# in the table. A fund that has not filed August must be compared July to
# June, or not at all -- never August-to-July with its August missing,
# which would report every holding as sold.
CHANGE_DATES = """
SELECT DISTINCT as_of_date FROM mf_holding
WHERE scheme_code = %(code)s
ORDER BY as_of_date DESC LIMIT 2
"""

# Summed per ISIN: a fund can list one company on two rows (two series of
# the same scrip). Comparing row to row would read a re-labelled line as a
# sale and a purchase.
CHANGE_ROWS = """
WITH cur AS (
    SELECT isin, SUM(pct_of_nav) AS pct, SUM(quantity) AS qty,
           MIN(instrument_name) AS nm
    FROM mf_holding
    WHERE scheme_code = %(code)s AND as_of_date = %(now)s AND isin IS NOT NULL
    GROUP BY isin
),
prv AS (
    SELECT isin, SUM(pct_of_nav) AS pct, SUM(quantity) AS qty,
           MIN(instrument_name) AS nm
    FROM mf_holding
    WHERE scheme_code = %(code)s AND as_of_date = %(prev)s AND isin IS NOT NULL
    GROUP BY isin
)
SELECT COALESCE(c.isin, p.isin) AS isin,
       COALESCE(m.company_name, c.nm, p.nm) AS name,
       COALESCE(m.sector, m.industry) AS sector,
       cc.cap_class,
       c.pct AS pct_now, p.pct AS pct_prev,
       c.qty AS qty_now, p.qty AS qty_prev
FROM cur c
FULL OUTER JOIN prv p ON p.isin = c.isin
LEFT JOIN stock_master m ON m.isin = COALESCE(c.isin, p.isin)
LEFT JOIN LATERAL (
    SELECT x.cap_class FROM stock_cap_class x
    WHERE x.isin = COALESCE(c.isin, p.isin)
    ORDER BY (x.as_of_period <= %(now)s) DESC,
             abs(x.as_of_period - %(now)s)
    LIMIT 1
) cc ON true
"""

# A bonus or a split changes the share count without anyone trading -- and
# it changes it for EVERY fund holding that company, by the same ratio.
# So the ratio agreeing across funds is the signature of a corporate
# action, and disagreeing is the signature of a trade. Without this, a 1:1
# bonus reports every holder as having doubled their position in a month.
CHANGE_CORP_ACTION = """
WITH cur AS (
    SELECT scheme_code, isin, SUM(quantity) AS q FROM mf_holding
    WHERE as_of_date = %(now)s AND isin = ANY(%(isins)s) AND quantity > 0
    GROUP BY 1, 2
),
prv AS (
    SELECT scheme_code, isin, SUM(quantity) AS q FROM mf_holding
    WHERE as_of_date = %(prev)s AND isin = ANY(%(isins)s) AND quantity > 0
    GROUP BY 1, 2
),
ratio AS (
    SELECT c.isin, c.q / p.q AS r
    FROM cur c JOIN prv p ON p.scheme_code = c.scheme_code AND p.isin = c.isin
    WHERE p.q > 0
)
SELECT isin, COUNT(*) AS holders,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r) AS median_ratio
FROM ratio GROUP BY isin HAVING COUNT(*) >= 3
"""

# Every fund that held this company in either month -- but only those that
# actually filed for BOTH dates. A fund missing its August disclosure has
# not sold anything; it has simply not reported, and counting it as an exit
# would invent 25 sellers out of Bandhan alone.
STOCK_CHANGE_ROWS = """
WITH filed AS (
    SELECT scheme_code FROM mf_holding WHERE as_of_date = %(now)s
    GROUP BY 1
    INTERSECT
    SELECT scheme_code FROM mf_holding WHERE as_of_date = %(prev)s
    GROUP BY 1
),
cur AS (
    SELECT scheme_code, SUM(pct_of_nav) AS pct, SUM(quantity) AS qty
    FROM mf_holding WHERE isin = %(isin)s AND as_of_date = %(now)s
    GROUP BY 1
),
prv AS (
    SELECT scheme_code, SUM(pct_of_nav) AS pct, SUM(quantity) AS qty
    FROM mf_holding WHERE isin = %(isin)s AND as_of_date = %(prev)s
    GROUP BY 1
)
SELECT f.scheme_code, s.scheme_name, s.amc_name, vc.category,
       c.pct AS pct_now, p.pct AS pct_prev,
       c.qty AS qty_now, p.qty AS qty_prev
FROM filed f
LEFT JOIN cur c ON c.scheme_code = f.scheme_code
LEFT JOIN prv p ON p.scheme_code = f.scheme_code
JOIN mf_scheme s ON s.scheme_code = f.scheme_code
LEFT JOIN v_scheme_category vc ON vc.scheme_code = f.scheme_code
WHERE c.scheme_code IS NOT NULL OR p.scheme_code IS NOT NULL
"""

# Share counts are exact, but AMCs round them and a re-stated line can
# wobble. Below this a "change" is noise, not an order.
QTY_NOISE = 0.005          # 0.5 per cent of the position
CA_BAND = 0.03             # how close to the cross-fund median counts as
                           # the same corporate action


def _classify(pct_now, pct_prev, qty_now, qty_prev, corp):
    """What happened, in the four combinations that are actually possible."""
    if pct_prev is None:
        return "new", "first appearance in this portfolio"
    if pct_now is None:
        return "exited", "gone from the portfolio"

    dq = None
    if qty_now is not None and qty_prev not in (None, 0):
        dq = float(qty_now) / float(qty_prev) - 1.0

    if corp:
        # The share count moved for everyone at once, so it says nothing
        # about this manager. Weight is still worth reporting.
        return "corporate action", ("share count changed for every holder "
                                    "-- a bonus or split, not a trade")
    if dq is None:
        return "unknown", "no share count to compare"
    if dq > QTY_NOISE:
        return "bought", "the fund owns more shares than last month"
    if dq < -QTY_NOISE:
        return "sold", "the fund owns fewer shares than last month"
    return "held", "same number of shares -- any weight change is the market"


class StockChangeRequest(BaseModel):
    isin: str


@router.get("/fund-changes/{scheme_code}")
def fund_changes(scheme_code: str):
    """What this fund did between its last two disclosures."""
    code = str(scheme_code).strip()
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        dates = [r["as_of_date"] for r in _q(cur, CHANGE_DATES, {"code": code})]
        if len(dates) < 2:
            # One month is not a change. Saying so is the answer, not an
            # error -- it is what every fund looked like before tonight.
            return {"comparable": False,
                    "as_of": str(dates[0]) if dates else None,
                    "reason": "Only one portfolio has been disclosed for this "
                              "fund so far, so there is nothing to compare it "
                              "against yet.",
                    "changes": []}

        now, prev = dates[0], dates[1]
        rows = _q(cur, CHANGE_ROWS, {"code": code, "now": now, "prev": prev})
        isins = [r["isin"] for r in rows if r["isin"]]
        corp = {}
        if isins:
            for r in _q(cur, CHANGE_CORP_ACTION,
                        {"now": now, "prev": prev, "isins": isins}):
                corp[r["isin"]] = float(r["median_ratio"])

    out = []
    for r in rows:
        pn = float(r["pct_now"]) if r["pct_now"] is not None else None
        pp = float(r["pct_prev"]) if r["pct_prev"] is not None else None
        qn = float(r["qty_now"]) if r["qty_now"] is not None else None
        qp = float(r["qty_prev"]) if r["qty_prev"] is not None else None

        # A corporate action only if the market-wide ratio really moved AND
        # this fund moved with it. A fund that traded on top of a bonus
        # will not sit on the median, and should not be excused as one.
        is_ca = False
        med = corp.get(r["isin"])
        if med and abs(med - 1.0) > CA_BAND and qn and qp:
            mine = qn / qp
            is_ca = abs(mine - med) <= CA_BAND * max(1.0, med)

        kind, why = _classify(pn, pp, qn, qp, is_ca)
        out.append({
            "isin": r["isin"], "name": r["name"], "sector": r["sector"],
            "cap": r["cap_class"],
            "pct_now": round(pn, 2) if pn is not None else None,
            "pct_prev": round(pp, 2) if pp is not None else None,
            "pct_delta": round((pn or 0) - (pp or 0), 2),
            "qty_now": qn, "qty_prev": qp,
            "qty_delta_pct": (round((qn / qp - 1) * 100, 1)
                              if qn is not None and qp else None),
            "kind": kind, "why": why,
        })

    order = {"new": 0, "bought": 1, "sold": 2, "exited": 3,
             "corporate action": 4, "held": 5, "unknown": 6}
    out.sort(key=lambda r: (order.get(r["kind"], 9), -abs(r["pct_delta"])))

    counts = {}
    for r in out:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1

    return {
        "comparable": True,
        "as_of": str(now), "compared_with": str(prev),
        "counts": counts,
        # The share of the fund that moved for a reason other than the
        # market -- the one number that says whether this was an active
        # month or a quiet one.
        "traded_pct": round(sum(abs(r["pct_delta"]) for r in out
                                if r["kind"] in ("new", "exited",
                                                 "bought", "sold")), 2),
        "changes": out,
    }


@router.get("/stock-changes/{isin}")
def stock_changes(isin: str, request: Request):
    """Which funds moved on one company, between the last two month-ends.

    SUBSCRIPTION ONLY, like the list of funds that hold a stock: it names
    the funds, which is the paid half. The fund-side change log
    (/fund-changes) is about one fund's own portfolio and stays open.
    """
    _require_subscription(request)
    key = str(isin).strip().upper()
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        dates = [r["as_of_date"] for r in
                 _q(cur, "SELECT DISTINCT as_of_date FROM mf_holding "
                         "ORDER BY as_of_date DESC LIMIT 2")]
        if len(dates) < 2:
            return {"comparable": False, "reason": "Only one month of "
                    "portfolios has been loaded so far.", "funds": []}
        now, prev = dates[0], dates[1]
        rows = _q(cur, STOCK_CHANGE_ROWS,
                  {"isin": key, "now": now, "prev": prev})
        corp = {}
        for r in _q(cur, CHANGE_CORP_ACTION,
                    {"now": now, "prev": prev, "isins": [key]}):
            corp[r["isin"]] = float(r["median_ratio"])
        # Funds that hold it but have not filed for `now`. Named, not
        # silently dropped: "we cannot see" is not "they sold".
        silent = _q(cur, """
            SELECT count(*) AS n FROM (
                SELECT scheme_code FROM mf_holding
                WHERE isin = %(isin)s AND as_of_date = %(prev)s
                EXCEPT
                SELECT scheme_code FROM mf_holding WHERE as_of_date = %(now)s
            ) x
        """, {"isin": key, "now": now, "prev": prev}, one=True)

    med = corp.get(key)
    out = []
    for r in rows:
        pn = float(r["pct_now"]) if r["pct_now"] is not None else None
        pp = float(r["pct_prev"]) if r["pct_prev"] is not None else None
        qn = float(r["qty_now"]) if r["qty_now"] is not None else None
        qp = float(r["qty_prev"]) if r["qty_prev"] is not None else None
        is_ca = False
        if med and abs(med - 1.0) > CA_BAND and qn and qp:
            is_ca = abs((qn / qp) - med) <= CA_BAND * max(1.0, med)
        kind, why = _classify(pn, pp, qn, qp, is_ca)
        out.append({
            "scheme_code": str(r["scheme_code"]),
            "scheme_name": r["scheme_name"], "amc_name": r["amc_name"],
            "category": r["category"],
            "pct_now": round(pn, 2) if pn is not None else None,
            "pct_prev": round(pp, 2) if pp is not None else None,
            "pct_delta": round((pn or 0) - (pp or 0), 2),
            "qty_delta_pct": (round((qn / qp - 1) * 100, 1)
                              if qn is not None and qp else None),
            "kind": kind, "why": why,
        })

    order = {"new": 0, "bought": 1, "sold": 2, "exited": 3,
             "corporate action": 4, "held": 5, "unknown": 6}
    out.sort(key=lambda r: (order.get(r["kind"], 9), -abs(r["pct_delta"])))
    counts = {}
    for r in out:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1

    return {
        "comparable": True,
        "as_of": str(now), "compared_with": str(prev),
        "counts": counts,
        "not_disclosed": int(silent["n"]) if silent else 0,
        "funds": out,
    }


@router.post("/fund-cards")
def fund_cards(body: FundCardsRequest, request: Request):
    """Returns, benchmark and category verdicts, and the trend split, for a
    set of funds. Read-only and public -- it exposes nothing the fund pages
    do not already show.

    Built for the goal planner, which needs the return over the GOAL's
    horizon rather than a fixed window. Every period held is returned and
    the page picks; the alternative, a period argument, would have the
    server guess which window the page meant.
    """
    codes = [str(c).strip() for c in body.codes if str(c).strip()][:20]
    if not codes:
        raise HTTPException(400, "No funds given.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        meta = {str(r["scheme_code"]): r
                for r in _q(cur, GOAL_CARD_META, {"codes": codes})}

        returns, category = defaultdict(dict), {}
        for r in _q(cur, GOAL_CARD_RETURNS, {"codes": codes}):
            code = str(r["scheme_code"])
            returns[code][r["period"]] = {
                "period": r["period"],
                "years": float(r["years"]) if r["years"] is not None else None,
                "fund_cagr": float(r["fund_cagr"]) if r["fund_cagr"] is not None else None,
                "bench_cagr": float(r["bench_cagr"]) if r["bench_cagr"] is not None else None,
                "excess_cagr": float(r["excess_cagr"]) if r["excess_cagr"] is not None else None,
                "vs_category": float(r["vs_category"]) if r["vs_category"] is not None else None,
                "category_rank": r["category_rank"],
                "category_count": r["category_count"],
                "rank_meaningful": bool(r["rank_meaningful"]),
                "benchmark_name": r["benchmark_name"],
                "as_of": str(r["as_of_date"]),
            }

        # Rolling returns and risk, with the peer median beside each.
        # One grouped read for every fund on the card, for the same reason
        # the trend split is batched: a plan with eight funds must not be
        # eight extra round trips.
        rolling = defaultdict(dict)
        for r in _q(cur, ROLLING_CARDS, {"codes": codes}):
            rolling[str(r["scheme_code"])][r["window_years"]] = {
                "avg_cagr": float(r["avg_cagr"]) if r["avg_cagr"] is not None else None,
                "worst_cagr": float(r["worst_cagr"]) if r["worst_cagr"] is not None else None,
                "pct_above_hurdle": float(r["pct_above_hurdle"]) if r["pct_above_hurdle"] is not None else None,
                "hurdle_pct": float(r["hurdle_pct"]),
                "observations": r["observations"],
                "sortino": float(r["sortino"]) if r["sortino"] is not None else None,
                "sharpe": float(r["sharpe"]) if r["sharpe"] is not None else None,
                "volatility": float(r["volatility"]) if r["volatility"] is not None else None,
                "max_drawdown": float(r["max_drawdown"]) if r["max_drawdown"] is not None else None,
                "peer_avg_cagr": float(r["peer_avg"]) if r["peer_avg"] is not None else None,
                "peer_worst_cagr": float(r["peer_worst"]) if r["peer_worst"] is not None else None,
                "peer_sortino": float(r["peer_sortino"]) if r["peer_sortino"] is not None else None,
                "peer_above_hurdle": float(r["peer_hurdle"]) if r["peer_hurdle"] is not None else None,
                "peer_funds": r["peer_funds"],
            }

        # One batched call for the split across all funds, not one per fund.
        grouped = defaultdict(list)
        for r in _q(cur, TREND_BATCH, {"codes": codes}):
            grouped[str(r["scheme_code"])].append(r)
        splits = {c: _trend_summary(rs) for c, rs in grouped.items()}

        # The peer average, gated on rank_meaningful exactly as elsewhere:
        # where a placing is not meaningful the category is a bucket, not a
        # peer group, and an average over it looks like information while
        # carrying none.
        cat_trend = {}
        for code in codes:
            cat = (meta.get(code) or {}).get("category")
            any_rank = next((p for p in returns.get(code, {}).values()
                             if p["rank_meaningful"]), None)
            if cat and any_rank:
                cat_trend[code] = _q(cur, CATEGORY_TREND, {"cat": cat}, one=True)

    out = []
    for code in codes:
        m = meta.get(code)
        if not m:
            continue
        t = splits.get(code)
        out.append({
            "scheme_code": code,
            "scheme_name": m["scheme_name"],
            "amc_name": m["amc_name"],
            "category": m["category"],
            "returns": returns.get(code, {}),
            "trend": {k: t[k] for k in ("up", "down", "sideways", "unscored",
                                        "up_pct", "down_pct", "sideways_pct",
                                        "unscored_pct", "score_date")} if t else None,
            "trend_category": cat_trend.get(code),
            # Keyed by window years, so the page can pick the window that
            # matches the goal rather than the server guessing which one
            # the reader meant.
            "rolling": rolling.get(code) or None,
        })

    # Returns are public market fact and stay open; the trend is the paid
    # reading. Stripped rather than refused, because the rest of this
    # response is free data the caller is entitled to -- and `trend_locked`
    # distinguishes "withheld" from "we hold nothing", which is the same
    # distinction this codebase insists on everywhere else.
    if not _score_access(request):
        for card in out:
            card["trend"] = None
            card["trend_category"] = None
            card["trend_locked"] = True
    return out


@router.get("/fund-makeup/{scheme_code}")
def fund_makeup(scheme_code: str):
    """What one fund holds, by company size and by industry.

    Both are shares of the fund's LISTED EQUITY, not of the fund. A fund
    holding 8% cash would otherwise show percentages that quietly sum to 92
    and look like a rounding fault. equity_pct is returned so the page can
    state the difference instead of hiding it.

    Descriptive, like everything else on this page: it reports what the
    fund disclosed, and offers no view on whether the mix is right.
    """
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        # Same resolution as holdings-trend, for the same reason: the
        # displayed code is not always the one carrying the holdings, and a
        # fund with good data must not 404 here while working there.
        fund = _q(cur, """
            SELECT s.scheme_code, c.canonical_scheme_code
            FROM mf_scheme s
            LEFT JOIN v_fund_canonical c
                   ON c.scheme_name = s.scheme_name
                  AND c.amc_name IS NOT DISTINCT FROM s.amc_name
            WHERE s.scheme_code = %(code)s
            LIMIT 1
        """, {"code": scheme_code}, one=True)
        if not fund:
            raise HTTPException(404, "No such fund.")

        as_of, code = None, scheme_code
        for cand in [c for c in (scheme_code, fund["canonical_scheme_code"]) if c]:
            d = _q(cur, """
                SELECT MAX(as_of_date) AS d FROM mf_holding
                WHERE scheme_code = %(code)s
            """, {"code": cand}, one=True)["d"]
            if d is not None:
                as_of, code = d, cand
                break
        if as_of is None:
            raise HTTPException(404, "No portfolio held for that fund.")

        rows = _q(cur, FUND_MAKEUP, {"code": code, "as_of": as_of})

    equity = sum(float(r["pct_of_nav"]) for r in rows) or 0.0
    if equity <= 0:
        raise HTTPException(404, "That fund holds no listed shares we can read.")

    caps, sectors = defaultdict(float), defaultdict(float)
    for r in rows:
        pct = float(r["pct_of_nav"])
        caps[r["cap_class"]] += pct
        sectors[r["sector"] or "Unclassified"] += pct

    return {
        "scheme_code": code,
        "holdings_as_of": str(as_of),
        # How much of the FUND is listed equity at all. Everything below is
        # a share of this, not of the fund.
        "equity_pct": round(equity, 1),
        "holding_count": len(rows),
        # Large, Mid, Small, Unclassified -- ALWAYS in that order, never
        # sorted by size. It is a scale, and reordering it by whichever
        # bucket happens to be biggest makes two funds harder to compare,
        # which is the whole point of showing it.
        "caps": [{"cap": c, "pct": round(100 * caps[c] / equity, 1)}
                 for c in ("Large", "Mid", "Small", "Unclassified")
                 if caps.get(c)],
        "sectors": [{"sector": s, "pct": round(100 * v / equity, 1)}
                    for s, v in sorted(sectors.items(), key=lambda kv: -kv[1])],
    }


class ImportRow(BaseModel):
    name: str
    amount: Optional[float] = None


class MatchRequest(BaseModel):
    rows: List[ImportRow]


def match_rows(cur, rows, held=None):
    """Match pasted names to funds. Returns matched / ambiguous / unmatched.

    WRITES NOTHING, and takes no portfolio -- both pages that import call
    this, one saving to a stored portfolio and one filling a picker in the
    browser. Keeping it here means the two can never disagree about what a
    name matches, which would be baffling to explain to anyone.

    Matching reuses search_funds, the function behind the picker itself,
    for the same reason.
    """
    held = held or set()
    matched, ambiguous, unmatched, seen = [], [], [], set()

    for row in rows:
        name = (row.name or "").strip()
        if not name:
            continue
        if row.amount is not None and row.amount <= 0:
            unmatched.append({"name": name, "amount": row.amount,
                              "why": "amount must be more than zero"})
            continue

        # An exact registered name wins outright. An export from another
        # system usually carries one, and fuzzy search on an exact name can
        # still rank a different plan of the same fund first.
        exact = _q(cur, RESOLVE_BY_NAME, {"key": name}, one=True)
        hits = ([{"scheme_code": exact["code"],
                  "scheme_name": exact["scheme_name"],
                  "amc_name": exact["amc_name"]}] if exact
                else search_funds(name))

        if not hits:
            unmatched.append({"name": name, "amount": row.amount,
                              "why": "no fund of that name has a portfolio we hold"})
            continue

        top = hits[0]
        code = str(top["scheme_code"])
        entry = {"name": name, "amount": row.amount,
                 "scheme_code": code,
                 "scheme_name": top["scheme_name"],
                 "amc_name": top.get("amc_name"),
                 "already_in_portfolio": code in held,
                 "duplicate_in_file": code in seen}
        seen.add(code)

        # More than one plausible hit goes to the person rather than being
        # guessed at. "HDFC Mid Cap" can mean several schemes, and quietly
        # picking one puts a fund in a portfolio nobody chose.
        if len(hits) > 1:
            entry["alternatives"] = [
                {"scheme_code": str(h["scheme_code"]),
                 "scheme_name": h["scheme_name"],
                 "amc_name": h.get("amc_name")} for h in hits[:5]]
            ambiguous.append(entry)
        else:
            matched.append(entry)

    return {"matched": matched, "ambiguous": ambiguous, "unmatched": unmatched}


@router.post("/match")
def match_import(body: MatchRequest):
    """Match pasted rows to funds. Public, because it reads only fund data
    that the picker already exposes, and writes nothing."""
    if not body.rows:
        raise HTTPException(400, "Nothing to match.")
    if len(body.rows) > 200:
        raise HTTPException(400, "That is more than 200 rows.")
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        return match_rows(cur, body.rows)


@router.get("/holdings-trend/{scheme_code}")
def holdings_trend(scheme_code: str, request: Request):
    """The same decomposition for ONE fund, for the fund detail page.

    Takes a canonical scheme code. Reads that fund's latest disclosed
    portfolio and reports how its holdings are trading today: counts and
    weights on each side, plus the largest positions in downtrends.

    Descriptive only. See the note above TREND for why this is a
    decomposition rather than a verdict, and why the wording on the page
    has to carry the timing gap -- month-end holdings against today's
    price readings -- rather than leaving it implied.
    """
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        # Resolve by NAME, not by insisting the caller hands us a canonical
        # code.
        #
        # The earlier version required scheme_code to appear as a
        # canonical_scheme_code in v_fund_canonical and 404'd otherwise. MO
        # Midcap's holdings sit under 127042 while the view names a different
        # code as canonical -- fallout from the null plan_type problem -- so
        # a fund with perfectly good data returned "No such fund".
        #
        # Holdings are what this endpoint actually needs, so prefer whichever
        # of the two codes has them.
        fund = _q(cur, """
            SELECT s.scheme_code, s.scheme_name, s.amc_name,
                   c.canonical_scheme_code
            FROM mf_scheme s
            LEFT JOIN v_fund_canonical c
                   ON c.scheme_name = s.scheme_name
                  AND c.amc_name IS NOT DISTINCT FROM s.amc_name
            WHERE s.scheme_code = %(code)s
            LIMIT 1
        """, {"code": scheme_code}, one=True)
        if not fund:
            raise HTTPException(404, "No such fund.")

        candidates = [c for c in (scheme_code, fund["canonical_scheme_code"]) if c]
        as_of, code = None, scheme_code
        for cand in candidates:
            d = _q(cur, """
                SELECT MAX(as_of_date) AS d FROM mf_holding
                WHERE scheme_code = %(code)s
            """, {"code": cand}, one=True)["d"]
            if d is not None:
                as_of, code = d, cand
                break
        if as_of is None:
            raise HTTPException(404, "No portfolio held for that fund.")
        fund = dict(fund, code=code)

        rows = _q(cur, TREND, {"code": fund["code"], "as_of": as_of})

    summary = _trend_summary(rows)
    summary.update({
        "scheme_code": fund["code"],
        "scheme_name": fund["scheme_name"],
        "amc_name": fund["amc_name"],
        "holdings_as_of": str(as_of),
        "total_holdings": len(rows),
    })

    # The whole reading is now subscription-only, not just the named
    # stocks. It used to publish the counts and weights and withhold the
    # names; "38 of 75 holdings in uptrends, 74.5% of NAV" is the finding,
    # and the names are detail on top of it.
    #
    # Zeroed rather than refused, because the band is loaded after the page
    # is on screen and a 402 there would look like a broken page rather
    # than a locked feature. The page reads `locked` and says so.
    if not _score_access(request):
        for key in ("up", "down", "sideways", "unscored",
                    "up_pct", "down_pct", "sideways_pct", "unscored_pct"):
            summary[key] = None
        summary["top_down"] = []
        summary["top_up"] = []
        summary["names_locked"] = True
        summary["locked"] = True
    else:
        summary["names_locked"] = False
        summary["locked"] = False
    return summary


# One query for a whole listing page. Per-fund calls would be fifty round
# trips for a category listing; this is a single grouped read.
TREND_BATCH = """
WITH newest AS (
    SELECT as_of_date, algo_version
    FROM stock_score ORDER BY as_of_date DESC, algo_version DESC LIMIT 1
),
latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d
    FROM mf_holding
    WHERE scheme_code = ANY(%(codes)s)
    GROUP BY scheme_code
),
hold AS (
    SELECT h.scheme_code, h.isin, h.pct_of_nav
    FROM mf_holding h
    JOIN latest l ON l.scheme_code = h.scheme_code AND l.d = h.as_of_date
    WHERE h.pct_of_nav IS NOT NULL
),
sc AS (
    SELECT ss.isin, ss.supertrend_dir, ss.rsi, ss.timeframe_used
    FROM stock_score ss, newest n
    WHERE ss.as_of_date   = n.as_of_date
      AND ss.algo_version = n.algo_version
      AND ss.isin IN (SELECT isin FROM hold)
),
tech AS (
    -- Driven by the DEDUPED ISIN list, one index lookup each.
    --
    -- Two earlier shapes were both wrong. A LATERAL per HOLDING row meant
    -- 4,800 lookups at 80 funds, most of them repeats. Replacing it with
    -- "WHERE t.isin IN (SELECT isin FROM hold)" let the planner pick a
    -- merge join and scan all 1.6M rows of stock_technical -- 13 seconds,
    -- of which 12.9 were that scan.
    --
    -- Joining from sc fixes both: sc already holds one row per distinct
    -- ISIN, and asking for a single timeframe with LIMIT 1 is an index
    -- lookup on (isin, timeframe, as_of_date DESC). 960 holdings across a
    -- fund are only about 107 distinct stocks.
    SELECT sc.isin, sc.timeframe_used AS timeframe,
           t.bb_upper, t.bb_lower, t.close_price
    FROM sc
    LEFT JOIN LATERAL (
        SELECT t2.bb_upper, t2.bb_lower, t2.close_price
        FROM stock_technical t2
        WHERE t2.isin = sc.isin
          AND t2.timeframe = sc.timeframe_used
        ORDER BY t2.as_of_date DESC
        LIMIT 1
    ) t ON TRUE
)
SELECT hold.scheme_code,
       hold.pct_of_nav,
       sc.supertrend_dir,
       sc.rsi,
       tech.bb_upper,
       tech.bb_lower,
       tech.close_price,
       (SELECT as_of_date FROM newest) AS score_date
FROM hold
LEFT JOIN sc   ON sc.isin = hold.isin
LEFT JOIN tech ON tech.isin = hold.isin AND tech.timeframe = sc.timeframe_used
"""


# The same reading, AS AT A PAST DATE.
#
# WHY THIS CAN EXIST AT ALL
#     Every input is already a time series and nothing prunes them:
#     stock_score is keyed (isin, as_of_date, algo_version) and never
#     deleted from, stock_technical keeps every day, and mf_holding keeps
#     every monthly disclosure. So a fund's rising split on any past date
#     is not lost history -- it is a query nobody had written.
#
#     That matters for alerts. "Has this fund's rising score fallen since
#     the day I invested" sounds like it needs a diary kept from that day
#     forward. It does not: the answer can be computed today for a date
#     two years ago.
#
# WHY THE ALGO VERSION IS PINNED RATHER THAN "WHATEVER WAS NEWEST"
#     Because the scorer has been rewritten. stock-v2 and stock-v3 give
#     different numbers for the same stock on the same day, so a series
#     that took "the newest algo at each date" would show a step change
#     where the CODE changed and report it as the market moving. Every
#     alert built on that would be firing on our own release history.
#
#     Pinned to one version, a date with no rows for it simply has no
#     reading, which is a gap the caller can see rather than a lie it
#     cannot.
#
# It returns score_date so the caller knows WHICH day answered: asking for
# the 15th and being served the 12th is fine, being served it silently is
# not.
TREND_BATCH_AT = """
WITH newest AS (
    SELECT as_of_date, algo_version
    FROM stock_score
    WHERE as_of_date <= %(as_of)s
      AND algo_version = %(algo)s
    ORDER BY as_of_date DESC
    LIMIT 1
),
latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d
    FROM mf_holding
    WHERE scheme_code = ANY(%(codes)s)
      AND as_of_date <= %(as_of)s
    GROUP BY scheme_code
),
hold AS (
    SELECT h.scheme_code, h.isin, h.pct_of_nav
    FROM mf_holding h
    JOIN latest l ON l.scheme_code = h.scheme_code AND l.d = h.as_of_date
    WHERE h.pct_of_nav IS NOT NULL
),
sc AS (
    SELECT ss.isin, ss.supertrend_dir, ss.rsi, ss.timeframe_used
    FROM stock_score ss, newest n
    WHERE ss.as_of_date   = n.as_of_date
      AND ss.algo_version = n.algo_version
      AND ss.isin IN (SELECT isin FROM hold)
),
tech AS (
    SELECT sc.isin, sc.timeframe_used AS timeframe,
           t.bb_upper, t.bb_lower, t.close_price
    FROM sc
    LEFT JOIN LATERAL (
        SELECT t2.bb_upper, t2.bb_lower, t2.close_price
        FROM stock_technical t2
        WHERE t2.isin = sc.isin
          AND t2.timeframe = sc.timeframe_used
          AND t2.as_of_date <= %(as_of)s
        ORDER BY t2.as_of_date DESC
        LIMIT 1
    ) t ON TRUE
)
SELECT hold.scheme_code,
       hold.pct_of_nav,
       sc.supertrend_dir,
       sc.rsi,
       tech.bb_upper,
       tech.bb_lower,
       tech.close_price,
       (SELECT as_of_date FROM newest) AS score_date
FROM hold
LEFT JOIN sc   ON sc.isin = hold.isin
LEFT JOIN tech ON tech.isin = hold.isin AND tech.timeframe = sc.timeframe_used
"""


# Rolling figures for a set of funds, each already joined to the middle
# fund of its own category. Done in SQL rather than two passes in Python
# because the peer row is a join, not a computation.
ROLLING_CARDS = """
SELECT r.scheme_code, r.window_years, r.observations,
       r.avg_cagr, r.worst_cagr, r.pct_above_hurdle, r.hurdle_pct,
       r.sharpe, r.sortino, r.volatility, r.max_drawdown,
       cat.avg_cagr         AS peer_avg,
       cat.worst_cagr       AS peer_worst,
       cat.sortino          AS peer_sortino,
       cat.pct_above_hurdle AS peer_hurdle,
       cat.funds            AS peer_funds
FROM mf_rolling r
JOIN (SELECT MAX(as_of_date) AS d FROM mf_rolling) latest
     ON r.as_of_date = latest.d
LEFT JOIN v_scheme_category vc ON vc.scheme_code = r.scheme_code
LEFT JOIN mf_rolling_category cat
       ON cat.category = vc.category
      AND cat.window_years = r.window_years
      AND cat.as_of_date = r.as_of_date
WHERE r.scheme_code = ANY(%(codes)s)
ORDER BY r.scheme_code, r.window_years
"""


class TrendBatchRequest(BaseModel):
    codes: List[str]


# The split at every portfolio date this fund has, each computed with the
# stock readings AS THEY STOOD THEN -- not today's readings applied to an
# old portfolio, which would say nothing about the past.
#
# Two LATERALs, both bounded by the holdings date: the newest stock_score
# on or before it, and the matching technicals on or before it. Mixing algo
# versions across time is possible here (a 2023 month may only have an
# older version); the alternative is an empty series, and the direction and
# band readings are comparable across versions even where the point weights
# are not.
TREND_HISTORY = """
SELECT h.as_of_date,
       h.pct_of_nav,
       ss.supertrend_dir,
       ss.rsi,
       tech.bb_upper,
       tech.bb_lower,
       tech.close_price
FROM mf_holding h
LEFT JOIN LATERAL (
    SELECT s2.supertrend_dir, s2.rsi, s2.timeframe_used
    FROM stock_score s2
    WHERE s2.isin = h.isin
      AND s2.as_of_date <= h.as_of_date
    ORDER BY s2.as_of_date DESC, s2.algo_version DESC
    LIMIT 1
) ss ON TRUE
LEFT JOIN LATERAL (
    SELECT t.bb_upper, t.bb_lower, t.close_price
    FROM stock_technical t
    WHERE t.isin = h.isin
      AND t.timeframe = ss.timeframe_used
      AND t.as_of_date <= h.as_of_date
    ORDER BY t.as_of_date DESC
    LIMIT 1
) tech ON TRUE
WHERE h.scheme_code = %(code)s
  AND h.pct_of_nav IS NOT NULL
ORDER BY h.as_of_date
"""


@router.get("/trend-history/{scheme_code}")
def trend_history(scheme_code: str, request: Request):
    """How the split moved, one point per disclosed portfolio.

    Most funds will return a single point today -- monthly holdings history
    exists for only part of the universe. That is the honest answer, and the
    series fills in on its own as portfolios are loaded.
    """
    _require_scores(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, TREND_HISTORY, {"code": scheme_code})

    by_date = defaultdict(list)
    for r in rows:
        by_date[r["as_of_date"]].append(r)

    keep = ("up", "down", "sideways", "unscored",
            "up_pct", "down_pct", "sideways_pct", "unscored_pct")
    points = []
    for d in sorted(by_date):
        t = _trend_summary(by_date[d])
        point = {k: t[k] for k in keep}
        point["as_of"] = str(d)
        points.append(point)

    # The same latest portfolio read against TODAY's prices, returned
    # separately.
    #
    # Without it the chart ends on a number that disagrees with the band on
    # the holdings tab for the same month, and the two look like a bug. They
    # are different questions: every point above asks how the holdings were
    # trading AT that date, while the band asks how the latest holdings are
    # trading NOW. Returning both lets the page show the line arriving at the
    # figure the reader has already seen, and say why it moved.
    current = None
    if by_date:
        newest_holding = max(by_date)
        with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
            rows_now = _q(cur, TREND, {"code": scheme_code,
                                       "as_of": newest_holding})
        if rows_now:
            t = _trend_summary(rows_now)
            current = {k: t[k] for k in keep}
            current["as_of"] = str(newest_holding)
            current["score_date"] = t["score_date"]

    return {"scheme_code": scheme_code, "points": points, "current": current}


# Peers in the same category, on the one-year window.
#
# Canonical codes only: mf_returns holds every plan and option variant, so
# without the filter a category of 29 funds returns 119 rows and each fund
# appears four times.
CATEGORY_PEERS = """
WITH asof AS (
    SELECT MAX(as_of_date) AS d FROM mf_returns
),
me AS (
    SELECT r.category
    FROM mf_returns r, asof
    WHERE r.scheme_code = %(code)s AND r.as_of_date = asof.d
    LIMIT 1
)
SELECT r.scheme_code,
       s.scheme_name,
       s.amc_name,
       r.category,
       ROUND(r.fund_cagr, 2)   AS fund_cagr,
       ROUND(r.bench_cagr, 2)  AS bench_cagr,
       ROUND(r.excess_cagr, 2) AS excess_cagr,
       r.category_rank,
       r.category_count,
       COALESCE(cr.rank_meaningful, false) AS rank_meaningful
-- Explicit joins throughout. Written as "FROM mf_returns r, asof, me JOIN
-- mf_scheme s ON s.scheme_code = r.scheme_code" this fails: the JOIN binds
-- to me alone, so r is not in scope inside its ON clause and Postgres
-- rejects it with "invalid reference to FROM-clause entry for table r".
FROM mf_returns r
CROSS JOIN asof
CROSS JOIN me
JOIN mf_scheme s ON s.scheme_code = r.scheme_code
LEFT JOIN mf_category_return cr
       ON cr.category = r.category AND cr.as_of_date = r.as_of_date
      AND cr.period = r.period
WHERE r.as_of_date = asof.d
  AND r.period = %(period)s
  AND r.category = me.category
  AND r.scheme_code IN (SELECT canonical_scheme_code FROM v_fund_canonical)
ORDER BY r.category_rank NULLS LAST, s.scheme_name
"""

# Sectoral/Thematic runs to 210 funds. Past this the table is unreadable and
# the split query gets expensive, so the list is capped -- the fund being
# viewed is always kept, wherever it places.
PEER_CAP = 80


@router.get("/category-peers/{scheme_code}")
def category_peers(scheme_code: str,
                    period: str = Query("1Y", pattern="^(1Y|3Y|5Y|7Y|10Y)$")):
    """The fund's category, with return rank, benchmark verdict and split.

    Return rank comes straight from mf_returns -- ranking on realised return
    is a fact about what happened. The split is descriptive and carries no
    ordering; the table sorts on it only if the person asks.
    """
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        peers = _q(cur, CATEGORY_PEERS, {"code": scheme_code, "period": period})
        if not peers:
            # Same mismatch as holdings-trend: the displayed code may not be
            # the one carrying the returns row. Try the canonical twin before
            # giving up.
            alt = _q(cur, """
                SELECT c.canonical_scheme_code AS code
                FROM mf_scheme s
                JOIN v_fund_canonical c
                       ON c.scheme_name = s.scheme_name
                      AND c.amc_name IS NOT DISTINCT FROM s.amc_name
                WHERE s.scheme_code = %(code)s
                LIMIT 1
            """, {"code": scheme_code}, one=True)
            if alt and alt["code"] and alt["code"] != scheme_code:
                scheme_code = alt["code"]
                peers = _q(cur, CATEGORY_PEERS, {"code": scheme_code, "period": period})
        if not peers:
            raise HTTPException(404, "No category returns for that fund.")

        total = len(peers)
        kept = peers[:PEER_CAP]
        if not any(p["scheme_code"] == scheme_code for p in kept):
            mine = next((p for p in peers if p["scheme_code"] == scheme_code), None)
            if mine:
                kept = kept[:PEER_CAP - 1] + [mine]

    # The split is NOT computed here.
    #
    # Rank and benchmark come out of mf_returns in milliseconds; the split
    # needs a scan over every held stock for every peer, which is seconds.
    # Bundling them meant the whole table waited on the slow half and the
    # tab sat on "Loading" long enough to look broken. The page now renders
    # this immediately and fills the Rising column from /trend-batch after.
    for p in kept:
        p["is_self"] = p["scheme_code"] == scheme_code

    return {"scheme_code": scheme_code,
            "category": kept[0]["category"] if kept else None,
            "total": total, "shown": len(kept), "funds": kept}


CATEGORY_TREND = """
SELECT fund_count, up_pct, sideways_pct, down_pct, unscored_pct, as_of_date
FROM mf_category_trend
WHERE category = %(cat)s
ORDER BY as_of_date DESC
LIMIT 1
"""


@router.post("/trend-batch")
def trend_batch(req: TrendBatchRequest, request: Request):
    """The up / sideways / down split for many funds at once.

    Feeds the category listing. Returns WEIGHTS ONLY -- no rank, no score,
    no ordering. The listing sorts client-side when the person asks it to:
    a sort is a view they chose, where a rank would be a verdict this data
    cannot support. See the note above TREND.
    """
    # The category listing's Rising column. Refused rather than zeroed:
    # this one is requested by an explicit action, so a locked answer is
    # legible where a silent row of blanks would not be.
    _require_scores(request)
    codes = [c for c in {str(c).strip() for c in req.codes} if c][:200]
    if not codes:
        return {}

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, TREND_BATCH, {"codes": codes})

    grouped = defaultdict(list)
    for r in rows:
        grouped[r["scheme_code"]].append(r)

    keep = ("up", "down", "sideways", "unscored",
            "up_pct", "down_pct", "sideways_pct", "unscored_pct", "score_date")
    return {code: {k: _trend_summary(rs)[k] for k in keep}
            for code, rs in grouped.items()}


@router.post("/lookthrough")
def lookthrough(req: LookthroughRequest, request: Request):
    # This endpoint had no authentication of ANY kind -- not signed-in,
    # not subscribed, not even a Request object to check. Anyone who found
    # the URL got the whole look-through.
    _require_subscription(request)
    # The look-through is not a score and stays on for subscribers. The
    # score rank, peer count, coverage and rising split inside it ARE, and
    # are left out while scores are switched off.
    scores_ok = _score_access(request)

    if not req.holdings:
        raise HTTPException(400, "Add at least one fund.")
    if len(req.holdings) > MAX_FUNDS:
        raise HTTPException(400, f"That is more than {MAX_FUNDS} funds.")
    for h in req.holdings:
        has_dates = bool(h.invested_on and h.invest_mode and h.invest_amount)
        if not has_dates and (h.amount is None or h.amount <= 0):
            raise HTTPException(
                400, f"{h.key} needs either what it is worth today, or the "
                     f"date and amount you have been investing.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        target = req.as_of or _q(
            cur, "SELECT MAX(as_of_date) AS d FROM mf_holding", one=True)["d"]

        # ---- resolve first, then PRICE, then build ------------------
        #
        # Split into two passes on purpose. Pricing needs the canonical
        # scheme code, which only resolution produces, and it wants
        # every code at once -- one NAV query for the whole portfolio
        # rather than one per fund, and certainly not one per SIP
        # instalment.
        pairs = []
        for item in req.holdings:
            key = item.key.strip()
            if ISIN_RE.match(key.upper()):
                row = _q(cur, RESOLVE_BY_ISIN, {"key": key.upper()}, one=True)
            elif key.isdigit():
                row = _q(cur, RESOLVE_BY_CODE, {"key": key}, one=True)
            else:
                row = _q(cur, RESOLVE_BY_NAME, {"key": key}, one=True)
            pairs.append((item, row))

        # One entry per PAIR, in order, so priced[i] is about pairs[i].
        # A holding that did not resolve contributes a placeholder rather
        # than being skipped, because skipping would shift every entry
        # after it onto the wrong fund.
        priced = value_holdings(cur, [
            {"scheme_code": row["code"] if row else None,
             "invested_on": item.invested_on,
             "invest_mode": item.invest_mode,
             "invest_amount": item.invest_amount,
             "plan": item.plan}
            for item, row in pairs])

        # ---- WHICH PLAN'S RETURNS TO QUOTE -------------------------
        #
        # Valuation already uses Regular NAV for a client's holdings --
        # that is what value_holdings does above. Returns did not, and
        # the mismatch was visible to the client: their money was
        # valued correctly and its performance was then described using
        # the Direct plan's numbers, which run about 1.8 points a year
        # better on the funds they actually hold. Right total, flattering
        # story.
        #
        # RESOLVED SEPARATELY FROM PRICING, deliberately. value_holdings
        # skips any holding without a date, a mode and an amount, so
        # priced[i] is None for every holding where somebody just typed
        # a figure -- which today is most of them. Reading the plan only
        # out of priced[i] would fix the funds that already had dates and
        # silently leave the rest on Direct, which is the kind of
        # half-fix that looks done and is not.
        #
        # One batch query per plan rather than one per fund: a
        # twenty-fund portfolio is two round trips, not twenty.
        want_plan = {}
        for i, (item, row) in enumerate(pairs):
            if not row:
                continue
            p = priced[i] or {}
            plan = (p.get("plan_used") or item.plan or "").upper() or None
            # DIRECT needs no lookup: the canonical code IS the Direct one.
            if plan and plan != "DIRECT":
                want_plan.setdefault(plan, set()).add(row["code"])
        returns_sibling = {}
        for plan, plan_codes in want_plan.items():
            for c, sib in plan_siblings(cur, plan_codes, plan).items():
                returns_sibling[(c, plan)] = sib

        def returns_for(code, plan):
            """(scheme code to read returns from, the plan that is, fell back).

            A fund with no Regular sibling falls back to Direct rather
            than showing nothing -- but it says so, per fund, exactly as
            the valuation does. One fund quoted on a different basis
            from its neighbours is a fact about that fund, not a
            footnote for the whole report.
            """
            plan = (plan or "").upper() or None
            if not plan or plan == "DIRECT":
                return code, "DIRECT", False
            sib = returns_sibling.get((code, plan))
            if sib:
                return sib, plan, False
            return code, "DIRECT", True

        def amount_of(item, code, i):
            """What this holding is worth, and where that figure came from.

            The NAV-priced figure WINS over anything typed. It is
            derived from the fund's own history rather than from
            somebody's recollection, and the typed box is the one that
            produced a 76% loss out of a healthy portfolio by quietly
            accepting a monthly instalment."""
            p = priced[i]
            if p and p.get("priced"):
                return float(p["value"]), "nav", p
            return (float(item.amount) if item.amount else None), "typed", p

        funds, unresolved = [], []
        for i, (item, row) in enumerate(pairs):
            key = item.key.strip()
            if not row:
                unresolved.append({"key": key, "amount": item.amount,
                                   "why": "no matching scheme"})
                continue

            code = row["code"]
            amount, amount_source, price_note = amount_of(item, code, i)
            if amount is None or amount <= 0:
                unresolved.append({
                    "key": key, "amount": None,
                    "why": (price_note or {}).get("why")
                           or "could not work out what this is worth"})
                continue
            as_of = _q(cur, AS_OF_FOR_FUND,
                       {"code": code, "target": target}, one=True)["d"]
            if as_of is None:
                unresolved.append({"key": key, "amount": amount,
                                   "why": f"no portfolio on or before {target}"})
                continue

            holdings = _q(cur, HOLDINGS, {"code": code, "as_of": as_of})
            score = _q(cur, FUND_SCORE, {"code": code}, one=True)
            peers = None
            if score and score.get("category"):
                peers = _q(cur, FUND_SCORE_PEERS,
                           {"cat": score["category"]}, one=True)["n"]

            # The category's average split, for comparison against this
            # fund's own.
            #
            # GATED ON rank_meaningful, the same flag that decides whether a
            # return placing is shown. Where it is false -- Sectoral/
            # Thematic, ETFs, index funds -- the "category" is a bucket, not
            # a peer group: the average Sectoral/Thematic fund spans defence,
            # pharma and PSU banks, which have no reason to move together.
            # Comparing against that average would look like information and
            # carry none. One rule, applied in both places.
            cat_trend = None
            if score and score.get("category") and score.get("rank_meaningful"):
                cat_trend = _q(cur, CATEGORY_TREND,
                               {"cat": score["category"]}, one=True)

            # The plan this fund's RETURNS should be quoted on. Takes the
            # plan the valuation actually used where there was one, and
            # the holding's own plan otherwise -- so a fund with a typed
            # amount and no dates is still quoted on Regular for a
            # client, which is the case that would otherwise slip through.
            returns_code, returns_plan, returns_fallback = returns_for(
                code, (price_note or {}).get("plan_used") or item.plan)

            funds.append({
                "code": code,
                "name": row["scheme_name"],
                "amc": row["amc_name"],
                "amount": amount,
                # Where that figure came from: "nav" means we priced
                # every instalment from the fund's own NAV history,
                # "typed" means somebody entered it. The page says
                # which, because they are not equally trustworthy.
                "amount_source": amount_source,
                "priced": price_note if amount_source == "nav" else None,
                # Which plan this fund was valued on, and whether we had
                # to fall back to Direct because it has no Regular we
                # can read. Marked per fund, not as a blanket caveat:
                # one fund on a different basis from its neighbours is a
                # fact about that fund.
                "plan": (price_note or {}).get("plan_used") or item.plan,
                "plan_fallback": bool((price_note or {}).get("plan_fallback")),
                # Echoed back rather than validated here. The page sent
                # them, the page computes with them, and a look-through
                # stores nothing -- so the only job is not to lose them
                # between the request and the response.
                "invested_on": _invest_date(item.invested_on),
                "invest_mode": (item.invest_mode
                                if item.invest_mode in ("lumpsum", "sip")
                                else None),
                "invest_amount": (float(item.invest_amount)
                                  if item.invest_amount and item.invest_amount > 0
                                  else None),
                "as_of": str(as_of),
                "stale": (target - as_of).days > 75,
                "resolved_pct": round(
                    float(sum(h["pct_of_nav"] for h in holdings if h["isin"])), 1),
                "gap": _q(cur, GAP_BREAKDOWN, {"code": code, "as_of": as_of}),
                "score_rank": (score["category_rank"]
                               if scores_ok and score else None),
                "score_peers": peers if scores_ok else None,
                "rank_meaningful": bool(score and score["rank_meaningful"]),
                "coverage_pct": (float(score["coverage_pct"])
                                 if scores_ok and score
                                 and score["coverage_pct"] is not None
                                 else None),
                "category": score["category"] if score else None,
                # Returns read from the plan the client actually holds,
                # not from the canonical Direct row -- see returns_for.
                "returns": _q(cur, FUND_RETURNS, {"code": returns_code}),
                # What basis those returns are on, and whether we had to
                # fall back. Separate from the valuation's "plan" field
                # above on purpose: a fund can be VALUED on Regular and
                # still have no Regular returns row, and the page must be
                # able to tell the client which of the two numbers in
                # front of them is on a different footing.
                "returns_plan": returns_plan,
                "returns_plan_fallback": returns_fallback,
                "trend": (_trend_summary(
                    _q(cur, TREND, {"code": code, "as_of": as_of}))
                    if scores_ok else None),
                # None where there is no peer group worth comparing to, or
                # no built average yet. The page shows the fund alone then,
                # rather than inventing a comparison.
                "trend_category": cat_trend if scores_ok else None,
                "holdings": holdings,
            })

    if not funds:
        raise HTTPException(
            422, "None of those funds could be matched to a portfolio we hold.")

    exploded = sum(f["amount"] for f in funds)
    grand = exploded + sum(u["amount"] for u in unresolved)

    # Rupees from pct_of_nav, NEVER market_value: AMCs report value in
    # their own units -- UTI in lakhs, others in crores -- and scale_applied
    # rescales only pct. market_value is not comparable across AMCs.
    exposure, holders, names, sectors = defaultdict(float), defaultdict(set), {}, {}
    caps = {}
    for f in funds:
        for h in f["holdings"]:
            if not h["isin"]:
                continue
            exposure[h["isin"]] += f["amount"] * float(h["pct_of_nav"]) / 100.0
            holders[h["isin"]].add(f["name"])
            names.setdefault(h["isin"], h["name"])
            sectors.setdefault(h["isin"], h["sector"])
            caps.setdefault(h["isin"], h["cap_class"])

    equity_total = sum(exposure.values()) or 1.0
    ranked = sorted(exposure.items(), key=lambda kv: -kv[1])

    # Share of equity that ONLY this fund provides -- the column that
    # answers "why am I holding this" without offering a verdict.
    sole = defaultdict(float)
    for isin, value in exposure.items():
        if len(holders[isin]) == 1:
            sole[next(iter(holders[isin]))] += value

    overlaps = []
    for i in range(len(funds)):
        for j in range(i + 1, len(funds)):
            a = {h["isin"]: float(h["pct_of_nav"])
                 for h in funds[i]["holdings"] if h["isin"]}
            b = {h["isin"]: float(h["pct_of_nav"])
                 for h in funds[j]["holdings"] if h["isin"]}
            shared = set(a) & set(b)
            overlaps.append({
                "a": funds[i]["name"], "b": funds[j]["name"],
                # The codes travel with the names so the page can open the
                # pair name by name. Without them a reader is told two
                # funds are 31% the same and given no way to ask which 31%.
                "a_code": funds[i]["code"], "b_code": funds[j]["code"],
                "common": len(shared),
                "overlap_pct": round(sum(min(a[k], b[k]) for k in shared), 1),
            })

    by_cap = defaultdict(float)
    for isin, value in exposure.items():
        by_cap[caps.get(isin) or "Unclassified"] += value

    by_sector = defaultdict(float)
    for isin, value in exposure.items():
        by_sector[sectors.get(isin) or "Unclassified"] += value

    weights = [v / equity_total for _, v in ranked]
    hhi = sum(w * w for w in weights) or 1.0

    for f in funds:
        f["unique_pct"] = round(100 * sole.get(f["name"], 0.0) / equity_total, 1)
        del f["holdings"]          # not needed by the page, and it is large

    return {
        "as_of": str(target),
        "total": grand,
        "exploded": exploded,
        "unresolved": unresolved,
        "funds": funds,
        "equity_total": round(equity_total, 2),
        "equity_pct": round(100 * equity_total / grand, 1),
        "stocks": [{
            "isin": isin,
            "name": names[isin],
            "sector": sectors.get(isin),
            "value": round(value, 2),
            "pct_of_equity": round(100 * value / equity_total, 2),
            "held_by": sorted(holders[isin]),
        } for isin, value in ranked[:req.top]],
        "stock_count": len(ranked),
        "effective_stocks": round(1 / hhi),
        "top10_pct": round(100 * sum(weights[:10]), 1),
        "duplicated_pct": round(
            100 * sum(v for i, v in exposure.items() if len(holders[i]) > 1)
            / equity_total, 1),
        "overlaps": sorted(overlaps, key=lambda o: -o["overlap_pct"]),
        # Large, Mid, Small, Unclassified -- ALWAYS that order, never sorted
        # by size. It is a scale; reordering by whichever bucket is biggest
        # makes two reports incomparable at a glance.
        "caps": [{"cap": c, "pct": round(100 * by_cap[c] / equity_total, 1)}
                 for c in ("Large", "Mid", "Small", "Unclassified")
                 if by_cap.get(c)],
        "sectors": [{"sector": s, "pct": round(100 * v / equity_total, 1)}
                    for s, v in sorted(by_sector.items(), key=lambda kv: -kv[1])],
    }
