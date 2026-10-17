"""
snapshot_api.py -- the one number the Snapshot tab of a fund page needs that no other endpoint gives: how the fund
did in each calendar year, and where that put it among the funds of its category.

Everything else on the Snapshot (trailing and rolling returns, Sharpe, Sortino, capture, drawdown, active share,
sector mix) already comes from /api/funds/{code} and /api/portfolio/fund-makeup/{code}.
------------------------------------------------------------------------
CALENDAR-YEAR RETURN
    The change in NAV from the last NAV on or before 31 Dec of the year before, to the last NAV on or before
    31 Dec of the year. A fund that did not exist on 31 Dec of the year before has no return for that year (a
    part-year return is not comparable). A NAV more than 10 days older than the date is not used.
    Only complete years are shown: this year is not over.

RANK
    Among the canonical funds of the same category (one entry per fund, not one per plan and option) that have a
    return for that year. Shown only where the category's rank means something: for ETFs, index funds and
    sectoral/thematic funds, which group things that are not alternatives to each other, the returns are shown
    and the rank is left out (the same rule the Returns tab uses).

READ-ONLY. Open to everyone: it is arithmetic on published NAV, like the trailing returns.
"""

import math
from datetime import date, timedelta

import psycopg
from fastapi import APIRouter, HTTPException
from psycopg.rows import dict_row

from portfolio_api import DB, _q

router = APIRouter(prefix="/api/snapshot", tags=["snapshot"])

YEARS_SHOWN = 5              # complete calendar years
NAV_SLACK_DAYS = 10          # a year-end NAV older than this is not "the year-end NAV"

PEER_CODES = """
WITH asof AS (SELECT MAX(as_of_date) AS d FROM mf_returns),
me AS (
    SELECT r.category FROM mf_returns r, asof
    WHERE r.scheme_code = %(code)s AND r.as_of_date = asof.d LIMIT 1
)
SELECT DISTINCT r.scheme_code, r.category
FROM mf_returns r
CROSS JOIN asof CROSS JOIN me
WHERE r.as_of_date = asof.d AND r.category = me.category
  AND r.scheme_code IN (SELECT canonical_scheme_code FROM v_fund_canonical)
"""

RANK_MEANINGFUL = """
SELECT COALESCE(bool_or(cr.rank_meaningful), false) AS ok
FROM mf_category_return cr
WHERE cr.category = %(cat)s AND cr.as_of_date = (SELECT MAX(as_of_date) FROM mf_category_return)
"""

# one backward index seek per (fund, date): mf_nav is keyed (scheme_code, nav_date)
YEAR_END_NAVS = """
SELECT p.code AS scheme_code, d.d AS wanted, n.nav_date, n.nav
FROM unnest(%(codes)s::text[]) AS p(code)
CROSS JOIN unnest(%(dates)s::date[]) AS d(d)
CROSS JOIN LATERAL (
    SELECT nav_date, nav FROM mf_nav
    WHERE scheme_code = p.code AND nav_date <= d.d AND nav_date > d.d - %(slack)s::int
    ORDER BY nav_date DESC LIMIT 1
) n
WHERE n.nav > 0
"""


def year_ends(today, count=YEARS_SHOWN):
    """The 31 Decembers needed: the last `count` complete years and the one before the first."""
    last = today.year - 1
    return [date(y, 12, 31) for y in range(last - count, last + 1)]


def median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def compute_years(navs, me, ends, ranked):
    """navs: {code: {31-Dec date: nav}}. me: this fund's code. ends: the 31 Decembers, oldest first.
    Returns [{"year", "return_pct", "rank", "count", "median_pct", "top_quarter"}] oldest first, for the years the
    fund has a return in. rank/count/top_quarter are None when `ranked` is false."""
    out = []
    for i in range(1, len(ends)):
        a, b = ends[i - 1], ends[i]
        rets = {}
        for code, by in navs.items():
            if a in by and b in by and by[a] > 0:
                rets[code] = (by[b] / by[a] - 1.0) * 100.0
        if me not in rets:
            continue
        mine = rets[me]
        row = {"year": b.year, "return_pct": round(mine, 1), "rank": None, "count": None,
               "median_pct": round(median(list(rets.values())), 1) if len(rets) >= 3 else None,
               "top_quarter": None}
        if ranked and len(rets) >= 5:
            rank = 1 + sum(1 for v in rets.values() if v > mine)
            row.update(rank=rank, count=len(rets), top_quarter=rank <= math.ceil(len(rets) / 4.0))
        out.append(row)
    return out


@router.get("/fund/{scheme_code}/years")
def fund_years(scheme_code: str):
    today = date.today()
    ends = year_ends(today)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        # ALWAYS THE CANONICAL PLAN (Direct, Growth where there is one). The peers are canonical funds, so a Regular or
        # IDCW code compared with them would mix plans with different expense ratios and payouts; the page says which
        # plan the year-by-year figures are for.
        canon = _q(cur, """
            SELECT c.canonical_scheme_code AS code, m.plan_type, m.option_type
            FROM mf_scheme s
            JOIN v_fund_canonical c ON c.scheme_name = s.scheme_name AND c.amc_name IS NOT DISTINCT FROM s.amc_name
            LEFT JOIN mf_scheme m ON m.scheme_code = c.canonical_scheme_code
            WHERE s.scheme_code = %(code)s LIMIT 1""", {"code": scheme_code}, one=True)
        code = canon["code"] if canon and canon["code"] else scheme_code
        plan = " ".join(x.title() for x in (canon["plan_type"], canon["option_type"]) if x) if canon else None
        peers = _q(cur, PEER_CODES, {"code": code})
        if not peers:
            raise HTTPException(404, "No category for that fund.")
        category = peers[0]["category"]
        ranked = bool(_q(cur, RANK_MEANINGFUL, {"cat": category}, one=True)["ok"])
        codes = sorted({p["scheme_code"] for p in peers} | {code}) if ranked else [code]
        rows = _q(cur, YEAR_END_NAVS, {"codes": codes, "dates": ends, "slack": NAV_SLACK_DAYS})
    navs = {}
    for r in rows:
        navs.setdefault(r["scheme_code"], {})[r["wanted"]] = float(r["nav"])
    return {"scheme_code": code, "plan": plan, "category": category, "ranked": ranked,
            "peers": len(codes) if ranked else None, "years": compute_years(navs, code, ends, ranked)}
