"""
saved_portfolio_api.py -- portfolios a user builds and keeps.
------------------------------------------------------------
portfolio_api.py explodes a list of funds handed to it in one request and
forgets it. This stores the list against a user, so it survives the tab
closing and can be reopened, edited and re-exploded.

WHY ITS OWN MODULE
    portfolio_api.py is long and working. Everything shared -- the DB
    handle, the query helper, the split logic and TREND_BATCH -- is
    imported from there, so there is exactly one copy of the trend rules
    and this file can be deleted in one line if it does not earn its place.

MOUNTING IT
    In api.py, next to the existing portfolio router:

        from saved_portfolio_api import router as saved_portfolio_router
        app.include_router(saved_portfolio_router)

AUTH
    api.current_user() returns a dict with user_id from mf_session joined
    to mf_user, or None when there is no valid session. _user_id() below
    reads user_id off it and 401s on None. The other key names it tries
    are belt and braces for if that query's columns ever change.

THE SHAPE, AND WHY
    portfolio.client_id is NULL for a person's own portfolio and filled in
    for a distributor's client. Every query here is written against that
    column being NULL-or-set rather than assuming NULL, so the distributor
    tier is auth plus billing plus a client list -- not a second build of
    this same feature.
"""

import os
from collections import defaultdict
from bisect import bisect_right
from calendar import monthrange
from datetime import date, timedelta
from typing import List, Optional

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

# Per-HOLDING valuation, from each fund's own dates and amounts.
# _value_from_nav() further down does something related but coarser:
# it values ONE portfolio-level SIP and has to guess how it was split
# across funds. Where a holding carries its own invested_on and
# invest_amount there is nothing to guess, so this is used instead.
from invest_value import value_holdings, instalment_dates

from portfolio_api import (
    DB,
    MAX_FUNDS,
    TREND_BATCH,
    _q,
    _scores_on,
    _trend_summary,
    Holding,
    LookthroughRequest,
    lookthrough as _lookthrough,
    match_rows,
)

router = APIRouter(prefix="/api/portfolio/saved", tags=["saved-portfolio"])


def _exec(cur, sql, params=None):
    """Run a statement that returns NO rows.

    portfolio_api._q always fetches, which is right for a SELECT or an
    INSERT ... RETURNING but raises "the last operation didn't produce a
    result" on a plain INSERT or UPDATE. Every write below that has no
    RETURNING clause goes through here instead.
    """
    cur.execute(sql, params or {})
    return cur.rowcount

# One person does not need fifty portfolios, and an unbounded count is a
# free way for anyone with a login to fill the table.
MAX_PORTFOLIOS = 25


# ---------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------
def _user_id(request: Request):
    """The caller's user id, or 401.

    Imported inside the function for the same reason _score_access does it:
    api.py imports this module at startup, so a module-level import would
    be circular.

    Unlike _score_access, this must NOT fall back to a default on error.
    That helper fails closed by withholding detail; here a fallback would
    silently write one person's funds into another person's portfolio.
    """
    try:
        from api import current_user
        user = current_user(request)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Sign in to use portfolios.")

    if not user:
        raise HTTPException(401, "Sign in to use portfolios.")

    for key in ("user_id", "id", "uid", "sub"):
        value = user.get(key) if isinstance(user, dict) else getattr(user, key, None)
        if value is not None:
            return value

    # Loud rather than silent: a wrong id here is a data leak, not a 500.
    raise HTTPException(500, "Could not read a user id from the session.")


def _owned(cur, portfolio_id: int, user_id):
    """Fetch a portfolio, or 404 if it is not this user's.

    404 and not 403 on purpose. 403 confirms the portfolio exists, which
    tells an enumerating caller which ids are real.

    EVERY endpoint below that takes a portfolio_id goes through this. There
    is no path that trusts the id in the URL.
    """
    row = _q(cur, """
        SELECT portfolio_id, name, client_id, created_at, updated_at
        FROM portfolio
        WHERE portfolio_id = %(pid)s AND owner_user_id = %(uid)s
    """, {"pid": portfolio_id, "uid": user_id}, one=True)
    if not row:
        raise HTTPException(404, "No such portfolio.")
    return row


# ---------------------------------------------------------------------
# Resolving what the user picked to a code we can actually explode
# ---------------------------------------------------------------------
# The picker calls /api/portfolio/search, which already returns canonical
# codes for funds we hold a portfolio for. But a code can also arrive from
# a URL or an older saved row, and the canonical code is not always the one
# carrying the holdings -- MO Midcap's sit under 127042 while the view names
# a different code canonical, fallout from the null plan_type problem.
#
# So resolve at ADD time and store the code that has holdings. Doing it on
# every read would repeat the work on every page load, and doing it never
# means a fund that resolves fine in the picker shows an empty split here.
RESOLVE_ADD = """
SELECT s.scheme_code, s.scheme_name, s.amc_name, c.canonical_scheme_code
FROM mf_scheme s
LEFT JOIN v_fund_canonical c
       ON c.scheme_name = s.scheme_name
      AND c.amc_name IS NOT DISTINCT FROM s.amc_name
WHERE s.scheme_code = %(code)s
LIMIT 1
"""

HAS_HOLDINGS = """
SELECT 1 FROM mf_holding WHERE scheme_code = %(code)s LIMIT 1
"""


def _storable_code(cur, code: str):
    """Prefer whichever of (given, canonical) actually has holdings."""
    row = _q(cur, RESOLVE_ADD, {"code": str(code).strip()}, one=True)
    if not row:
        return None, None
    for cand in (row["scheme_code"], row["canonical_scheme_code"]):
        if cand and _q(cur, HAS_HOLDINGS, {"code": cand}, one=True):
            return cand, row
    # No holdings under either. Still storable -- returns and rank work
    # without them and portfolios get loaded over time -- but the split
    # will read as unscored, which is the honest answer rather than a gap.
    return row["scheme_code"], row


# ---------------------------------------------------------------------
# The per-fund table
# ---------------------------------------------------------------------
PF_META = """
SELECT s.scheme_code,
       s.scheme_name,
       s.amc_name,
       vc.category
FROM mf_scheme s
LEFT JOIN v_scheme_category vc ON vc.scheme_code = s.scheme_code
WHERE s.scheme_code = ANY(%(codes)s)
"""

# Returns for every held fund in one read.
#
# as_of_date is scoped PER FUND, not by a bare MAX(as_of_date) over the
# whole table. api.py's unqualified version blanks the panel for every fund
# missing from a partial nightly run; this cannot, because each fund is
# matched to its own newest row.
PF_RETURNS = """
WITH latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d
    FROM mf_returns
    WHERE scheme_code = ANY(%(codes)s)
    GROUP BY scheme_code
)
SELECT r.scheme_code,
       r.period,
       r.years,
       r.category,
       ROUND(r.fund_cagr, 2)   AS fund_cagr,
       ROUND(r.bench_cagr, 2)  AS bench_cagr,
       ROUND(r.excess_cagr, 2) AS excess_cagr,
       r.category_rank,
       r.category_count,
       COALESCE(cr.rank_meaningful, false) AS rank_meaningful,
       b.display_name AS benchmark_name,
       r.as_of_date
FROM mf_returns r
JOIN latest l ON l.scheme_code = r.scheme_code AND l.d = r.as_of_date
LEFT JOIN benchmark_master b USING (benchmark_id)
LEFT JOIN mf_category_return cr
       ON cr.category = r.category AND cr.as_of_date = r.as_of_date
      AND cr.period   = r.period
WHERE r.period IN ('1Y', '3Y', '5Y')
ORDER BY r.scheme_code, r.years
"""

# Each fund's latest disclosed portfolio date, for the "as at" line under
# the split. Not cosmetic: a fund disclosing in March next to one
# disclosing in July is a fact the reader should see, not one to smooth over.
PF_HOLDING_DATE = """
SELECT scheme_code, MAX(as_of_date) AS as_of
FROM mf_holding
WHERE scheme_code = ANY(%(codes)s)
GROUP BY scheme_code
"""

SPLIT_KEYS = ("up", "down", "sideways", "unscored",
              "up_pct", "down_pct", "sideways_pct", "unscored_pct",
              "score_date")


def _rows_for(cur, codes: List[str]):
    """Everything the table needs, in four queries regardless of fund count.

    Not four per fund. The 236ms TREND_BATCH number comes from deduping
    ISINs ACROSS funds -- 960 holding rows over a portfolio are only about
    107 distinct stocks -- and calling the single-fund endpoint once per row
    throws exactly that away.
    """
    if not codes:
        # The fourth element is a PAIR, because the caller unpacks it as
        # (dates, splits). Returning a bare {} here raised ValueError on
        # every empty portfolio -- which is every portfolio the moment it
        # is created, so nothing could be opened after being made.
        return {}, {}, {}, ({}, {})

    meta = {r["scheme_code"]: r for r in _q(cur, PF_META, {"codes": codes})}

    # Keyed by period, not a list, and the PLACING travels with each
    # period's numbers rather than being pinned to 1Y.
    #
    # The page shows one window at a time. If the reader switches to 5Y and
    # the placing silently stays on 1Y, the row reads as one coherent
    # statement about a fund when it is two statements about two windows.
    returns = defaultdict(dict)
    category = {}
    for r in _q(cur, PF_RETURNS, {"codes": codes}):
        returns[r["scheme_code"]][r["period"]] = {
            "period": r["period"],
            "fund_cagr": float(r["fund_cagr"]) if r["fund_cagr"] is not None else None,
            "bench_cagr": float(r["bench_cagr"]) if r["bench_cagr"] is not None else None,
            "excess_cagr": float(r["excess_cagr"]) if r["excess_cagr"] is not None else None,
            "benchmark_name": r["benchmark_name"],
            "category_rank": r["category_rank"],
            "category_count": r["category_count"],
            # Honoured here as everywhere: where it is false (Sectoral/
            # Thematic, ETFs, index funds) the page shows "210 funds", not
            # "of 210", which reads like a number that failed to load.
            "rank_meaningful": bool(r["rank_meaningful"]),
            "as_of": str(r["as_of_date"]),
        }
        category.setdefault(r["scheme_code"], r["category"])
    rank = category

    dates = {r["scheme_code"]: str(r["as_of"])
             for r in _q(cur, PF_HOLDING_DATE, {"codes": codes})}

    grouped = defaultdict(list)
    for r in _q(cur, TREND_BATCH, {"codes": codes}):
        grouped[r["scheme_code"]].append(r)
    splits = {code: {k: _trend_summary(rs)[k] for k in SPLIT_KEYS}
              for code, rs in grouped.items()}

    return meta, returns, rank, (dates, splits)


# ---------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------
class CreatePortfolio(BaseModel):
    name: str
    client_id: Optional[int] = None      # distributor tier fills this


class RenamePortfolio(BaseModel):
    name: str


class FundEntry(BaseModel):
    scheme_code: str
    # Optional on purpose. A portfolio with no amounts still shows category,
    # rank, returns and the split -- everything except the look-through,
    # which needs rupees. Equal-weighting to force one would invent a number
    # the person never gave.
    amount: Optional[float] = None

    # WHEN AND HOW THE MONEY WENT IN. All optional, all three or none.
    #
    # `amount` above is what the holding is worth TODAY. These say what
    # went IN and when, which is the only way to compute what it earned.
    # A holding without them behaves exactly as it did before.
    invested_on: Optional[str] = None          # ISO date
    invest_mode: Optional[str] = None          # 'lumpsum' | 'sip'
    invest_amount: Optional[float] = None      # the lump, or one instalment

    # Whether the caller is SPEAKING ABOUT the three fields above.
    #
    # Needed because "leave it alone" and "clear it" are different
    # instructions that look identical on the wire -- both arrive as
    # three nulls. The fund picker re-adds a fund without any of this
    # and must not wipe a date somebody typed; the editor sends all
    # three every time and must be able to blank them. So the editor
    # sets this and the picker does not.
    invest_given: bool = False


class AddFunds(BaseModel):
    funds: List[FundEntry]


INVEST_MODES = ("lumpsum", "sip")


def _clean_invest(f):
    """(date, mode, amount) if the three are a valid set, None if all
    empty, False if half-filled or nonsense.

    Three outcomes rather than two because "nothing to store" and "the
    caller got it wrong" must not be handled the same way: the first is
    a holding without a date, the second is a mistake worth reporting.
    """
    on = (f.invested_on or "").strip() or None
    mode = (f.invest_mode or "").strip().lower() or None
    amt = f.invest_amount

    if on is None and mode is None and amt is None:
        return None
    if on is None or mode not in INVEST_MODES or amt is None or amt <= 0:
        return False
    try:
        d = date.fromisoformat(on)
    except ValueError:
        return False
    # A date in the future is not a typo we can interpret. Nor is one
    # before Indian mutual funds had daily NAVs to compute against.
    if d > date.today() or d.year < 1993:
        return False
    return (d, mode, round(float(amt), 2))



class ImportRow(BaseModel):
    name: str
    amount: Optional[float] = None


class ImportRequest(BaseModel):
    rows: List[ImportRow]



# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------
@router.post("")
def create_portfolio(body: CreatePortfolio, request: Request):
    user_id = _user_id(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Give the portfolio a name.")
    if len(name) > 120:
        raise HTTPException(400, "That name is too long.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        n = _q(cur, """
            SELECT COUNT(*) AS n FROM portfolio WHERE owner_user_id = %(uid)s
        """, {"uid": user_id}, one=True)["n"]
        if n >= MAX_PORTFOLIOS:
            raise HTTPException(
                400, f"That is more than {MAX_PORTFOLIOS} portfolios. "
                     "Delete one first.")

        row = _q(cur, """
            INSERT INTO portfolio (owner_user_id, client_id, name)
            VALUES (%(uid)s, %(cid)s, %(name)s)
            RETURNING portfolio_id, name, client_id, created_at
        """, {"uid": user_id, "cid": body.client_id, "name": name}, one=True)
        conn.commit()

    return {"portfolio_id": row["portfolio_id"],
            "name": row["name"],
            "client_id": row["client_id"],
            "created_at": str(row["created_at"]),
            "fund_count": 0}


@router.get("")
def list_portfolios(request: Request, client_id: Optional[int] = None):
    """This user's portfolios, newest activity first.

    client_id is a FILTER, not a requirement: omitted returns everything the
    user owns, so the same endpoint serves the personal page today and a
    distributor's per-client view later without a second route.
    """
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, """
            SELECT p.portfolio_id, p.name, p.client_id,
                   p.created_at, p.updated_at,
                   COUNT(h.scheme_code)                       AS fund_count,
                   SUM(h.amount)                              AS total_amount,
                   COUNT(*) FILTER (WHERE h.amount IS NULL)   AS missing_amounts
            FROM portfolio p
            LEFT JOIN portfolio_holding h USING (portfolio_id)
            WHERE p.owner_user_id = %(uid)s
              AND (%(cid)s::bigint IS NULL OR p.client_id = %(cid)s::bigint)
            GROUP BY p.portfolio_id
            ORDER BY p.updated_at DESC
        """, {"uid": user_id, "cid": client_id})

    return [{
        "portfolio_id": r["portfolio_id"],
        "name": r["name"],
        "client_id": r["client_id"],
        "fund_count": r["fund_count"],
        "total_amount": float(r["total_amount"]) if r["total_amount"] is not None else None,
        # The page needs this to decide whether to offer the look-through
        # button at all, rather than offering it and failing on click.
        "can_lookthrough": r["fund_count"] > 0 and r["missing_amounts"] == 0,
        "updated_at": str(r["updated_at"]),
    } for r in rows]


@router.get("/{portfolio_id}")
def get_portfolio(portfolio_id: int, request: Request):
    """The portfolio with everything the table shows for each fund:
    category, 1Y return rank, 1Y/3Y/5Y returns, and the rising / sideways /
    falling split by weight.

    NO GATE ON THIS ONE. holdings-trend withholds top_down / top_up because
    naming five stocks discloses a per-stock reading in bulk, which is the
    paid view. TREND_BATCH returns weights only and never a company name, so
    there is nothing here to withhold.

    Default order is by NAME. Any other default -- by rank, by rising weight
    -- implies the table is telling you which fund is better, which is the
    claim the score was removed for making.
    """
    user_id = _user_id(request)

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        portfolio = _owned(cur, portfolio_id, user_id)

        held = _q(cur, """
            SELECT scheme_code, amount, added_at,
                   invested_on, invest_mode, invest_amount, plan_type
            FROM portfolio_holding
            WHERE portfolio_id = %(pid)s
        """, {"pid": portfolio_id})

        codes = [h["scheme_code"] for h in held]
        meta, returns, rank, (dates, splits) = _rows_for(cur, codes)

        # ---- which plan is this person actually in? -----------------
        #
        # A client bought through a distributor holds REGULAR, whose NAV
        # carries the commission -- around a point a year behind Direct,
        # which is roughly 3% less money over five years. Everything
        # this platform SCORES runs on Direct, correctly, because a
        # category median mixing the two measures commission rather than
        # the manager. But a client's own holding has to be valued on
        # what they actually own.
        #
        # NULL in the column means nobody has said, and it resolves by
        # rule rather than being backfilled: a portfolio with a client
        # attached is Regular, one of your own is Direct. Backfilling
        # would make the guess indistinguishable from a choice.
        default_plan = "REGULAR" if portfolio["client_id"] else "DIRECT"
        for h in held:
            h["plan"] = (h.get("plan_type") or default_plan).upper()

        # Priced from NAV wherever the dates are there, so this page and
        # the look-through report show the same number. Two bits of code
        # computing the same figure is how they come to disagree.
        priced = value_holdings(cur, held)

    funds = []
    for i, h in enumerate(held):
        code = h["scheme_code"]
        m = meta.get(code, {})
        p = priced[i]
        funds.append({
            "scheme_code": code,
            "scheme_name": m.get("scheme_name") or code,
            "amc_name": m.get("amc_name"),
            # v_scheme_category is the display vocabulary; mf_returns.category
            # is the normalized peer group. They agree in the ordinary case
            # and the returns one is the fallback.
            "category": m.get("category") or rank.get(code),
            # The NAV-priced value wins over anything typed: it comes
            # from the fund's own history rather than from somebody's
            # recollection, and the typed box is the one that turned a
            # healthy portfolio into a 76% loss by quietly accepting a
            # monthly instalment.
            "amount": (round(float(p["value"]), 2) if p and p.get("priced")
                       else (float(h["amount"]) if h["amount"] is not None
                             else None)),
            "amount_source": "nav" if (p and p.get("priced")) else "typed",
            # Which plan this was valued on, whether that was stated or
            # assumed, and whether we had to fall back because the fund
            # has no Regular plan we can read. All three go to the page,
            # because a fund measured on a different basis from its
            # neighbours is a fact the reader is entitled to.
            "plan": h["plan"],
            "plan_stated": bool(h.get("plan_type")),
            "plan_fallback": bool(p and p.get("plan_fallback")),
            "priced": p if (p and p.get("priced")) else None,
            # Kept so the editor can still show what was typed, if
            # anything was, beside the figure that replaced it.
            "amount_typed": (float(h["amount"])
                             if h["amount"] is not None else None),
            # What went in and when, if anyone said. Null on all three
            # is the ordinary case and the page treats it as "not known"
            # rather than as zero.
            "invested_on": str(h["invested_on"]) if h["invested_on"] else None,
            "invest_mode": h["invest_mode"],
            "invest_amount": (float(h["invest_amount"])
                              if h["invest_amount"] is not None else None),
            # {"1Y": {...}, "3Y": {...}, "5Y": {...}} -- a period the fund
            # is too young for is simply absent, which the page reads as
            # "not yet" rather than as a zero.
            "returns": returns.get(code, {}),
            # Absent means no holdings loaded for this fund, which is not the
            # same as a fund whose holdings are all unscored. The page should
            # say "no portfolio loaded" for null and show the bar for zeroes.
            # The rising / sideways / falling split is built from the stock
            # scores, so it goes when scores are switched off. Absent here
            # is "not shown", which the page treats like "no portfolio
            # loaded" -- it draws nothing.
            "split": splits.get(code) if _scores_on() else None,
            "holdings_as_of": dates.get(code),
        })

    funds.sort(key=lambda f: (f["scheme_name"] or "").lower())

    amounts = [f["amount"] for f in funds]
    complete = bool(funds) and all(a is not None for a in amounts)

    return {
        "portfolio_id": portfolio["portfolio_id"],
        "name": portfolio["name"],
        "client_id": portfolio["client_id"],
        "updated_at": str(portfolio["updated_at"]),
        "funds": funds,
        "fund_count": len(funds),
        "total_amount": round(sum(a for a in amounts if a is not None), 2) if funds else 0.0,
        "can_lookthrough": complete,
        # So the page can drop the rising / sideways / falling column
        # without waiting on anything else to learn that scores are off.
        "scores_enabled": _scores_on(),
        # Funds disclose on their own schedule. Showing the spread lets the
        # page say so instead of implying one snapshot date.
        "holdings_dates": sorted({f["holdings_as_of"] for f in funds
                                  if f["holdings_as_of"]}),
    }


@router.post("/{portfolio_id}/funds")
def add_funds(portfolio_id: int, body: AddFunds, request: Request):
    """Add or update funds. Takes an ARRAY -- a five-fund paste is one
    request, not five."""
    user_id = _user_id(request)
    if not body.funds:
        raise HTTPException(400, "No funds sent.")

    for f in body.funds:
        if f.amount is not None and f.amount <= 0:
            raise HTTPException(
                400, f"Amount for {f.scheme_code} must be more than zero.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned(cur, portfolio_id, user_id)

        existing = _q(cur, """
            SELECT COUNT(*) AS n FROM portfolio_holding
            WHERE portfolio_id = %(pid)s
        """, {"pid": portfolio_id}, one=True)["n"]

        added, skipped = [], []
        for f in body.funds:
            code, row = _storable_code(cur, f.scheme_code)
            if not code:
                skipped.append({"scheme_code": f.scheme_code,
                                "why": "no matching scheme"})
                continue

            already = _q(cur, """
                SELECT 1 FROM portfolio_holding
                WHERE portfolio_id = %(pid)s AND scheme_code = %(code)s
            """, {"pid": portfolio_id, "code": code}, one=True)

            if not already and existing + len(added) >= MAX_FUNDS:
                skipped.append({"scheme_code": f.scheme_code,
                                "why": f"portfolio is full at {MAX_FUNDS} funds"})
                continue

            # Checked here as well as by the database's CHECK constraint.
            # The constraint is the guarantee; this is so a typo comes
            # back as a sentence instead of a 500 from psycopg.
            inv = _clean_invest(f) if f.invest_given else None
            if f.invest_given and inv is False:
                skipped.append({"scheme_code": f.scheme_code,
                                "why": "needs a date, a mode of 'lumpsum' or "
                                       "'sip', and an amount above zero -- "
                                       "or all three left empty"})
                continue

            # COALESCE, not EXCLUDED.amount.
            #
            # Re-adding a fund from the picker sends no amount, and a bare
            # EXCLUDED would blank an amount the person had typed. This is
            # the same shape as the plan_type fix in load_mf_schemes.py:
            # a new value can fill a blank, never blank a filled one.
            # Clearing an amount is a delete-and-re-add.
            #
            # The three invest columns work the OTHER way round, and
            # deliberately: when invest_given is set they are written
            # exactly as sent, nulls included, because clearing a date
            # someone mistyped has to be possible without deleting the
            # holding and losing its amount too.
            _exec(cur, """
                INSERT INTO portfolio_holding (portfolio_id, scheme_code,
                    amount, invested_on, invest_mode, invest_amount)
                VALUES (%(pid)s, %(code)s, %(amt)s,
                        %(on)s, %(mode)s, %(iamt)s)
                ON CONFLICT (portfolio_id, scheme_code) DO UPDATE
                   SET amount = COALESCE(EXCLUDED.amount,
                                         portfolio_holding.amount),
                       invested_on = CASE WHEN %(given)s
                            THEN EXCLUDED.invested_on
                            ELSE portfolio_holding.invested_on END,
                       invest_mode = CASE WHEN %(given)s
                            THEN EXCLUDED.invest_mode
                            ELSE portfolio_holding.invest_mode END,
                       invest_amount = CASE WHEN %(given)s
                            THEN EXCLUDED.invest_amount
                            ELSE portfolio_holding.invest_amount END
            """, {"pid": portfolio_id, "code": code, "amt": f.amount,
                  "given": bool(f.invest_given),
                  "on": inv[0] if inv else None,
                  "mode": inv[1] if inv else None,
                  "iamt": inv[2] if inv else None})

            if not already:
                added.append(code)

        _exec(cur, """
            UPDATE portfolio SET updated_at = now()
            WHERE portfolio_id = %(pid)s
        """, {"pid": portfolio_id})
        conn.commit()

    return {"added": added, "skipped": skipped}


# ---------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------
def _fv(monthly_rate, lump, sip, months):
    """Value of a lump sum plus a monthly SIP after `months`.

    The trailing (1+i) puts each instalment at the START of its month,
    which is when a SIP is actually debited. The end-of-month version
    understates the fund by one month's growth and so overstates the
    return needed -- the wrong direction to err when the output is
    "are you on track".
    """
    if months <= 0:
        return lump
    if abs(monthly_rate) < 1e-12:
        return lump + sip * months
    g = (1 + monthly_rate) ** months
    return lump * g + sip * ((g - 1) / monthly_rate) * (1 + monthly_rate)


def _solve_rate(target, lump, sip, months):
    """Annual rate that turns these contributions into `target`.

    Bisection rather than Newton: the function rises monotonically in the
    rate, so halving a bracket always converges, and Newton fails near
    zero exactly where plans that are already funded sit.

    Returns None when nothing is being invested, 0.0 when no growth is
    needed, and None again when the answer exceeds 50% a year -- past that
    the number is arithmetically real and practically meaningless, and
    printing it as a target would be worse than saying it cannot be done.
    """
    if lump <= 0 and sip <= 0 or months <= 0:
        return None
    if _fv(0, lump, sip, months) >= target:
        return 0.0

    lo, hi = 0.0, 0.06
    while _fv(hi, lump, sip, months) < target and hi < 1:
        hi *= 2
    if _fv(hi, lump, sip, months) < target:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if _fv(mid, lump, sip, months) < target:
            lo = mid
        else:
            hi = mid
    annual = (1 + (lo + hi) / 2) ** 12 - 1
    return None if annual > 0.50 else annual


def _months_between(a, b):
    return max(0, (b.year - a.year) * 12 + (b.month - a.month))


def _progress(g, valued=None):
    """Everything the tracker states, derived in one place.

    Deliberately reports two SEPARATE returns:

      required_from_here -- what the money still to come must earn
      achieved_so_far    -- what it has earned up to the valuation date

    Comparing those two is the whole judgement, and keeping them apart
    lets the page show its working instead of announcing a verdict. Where
    no value has been entered, achieved is simply absent -- not zero, and
    not assumed.
    """
    today = date.today()
    target = float(g["target_amount"])
    sip = float(g["sip_amount"] or 0)
    lump = float(g["lumpsum_amount"] or 0)
    started, target_date = g["started_on"], g["target_date"]

    elapsed = _months_between(started, today)
    remaining = _months_between(today, target_date)
    invested = lump + sip * elapsed

    # Years still to run, which is what picks the allocation band. Derived
    # from the target date rather than stored, so it cannot go stale.
    out = {
        "years_remaining": round(remaining / 12.0, 2),
        "months_elapsed": elapsed,
        "months_remaining": remaining,
        "invested_so_far": round(invested, 2),
        "target_amount": round(target, 2),
        "target_date": str(target_date),
        "overdue": remaining == 0 and today > target_date,
        # What the plan asked for at the outset, kept so a reader can see
        # whether the goal has got harder since they set it.
        "required_at_start": _solve_rate(
            target, lump, sip, _months_between(started, target_date)),
    }

    # A NAV-derived valuation wins over anything typed in. The stored
    # columns remain as a fallback for a fund whose NAV history we do not
    # hold -- and the page is told which of the two it is looking at,
    # because "computed from NAV" and "you told us in March" deserve
    # different amounts of trust.
    if valued and valued.get("ok"):
        value = valued["current_value"]
        out["valued_on"] = str(valued["valued_on"])
        out["value_source"] = "nav"
        out["priced_instalments"] = valued["priced"]
        out["instalments_due"] = valued["due"]
        out["split_by"] = valued["split_by"]
        # Invested is what NAV pricing could actually account for, which
        # can be less than sip x months when history is short.
        out["invested_so_far"] = valued["invested"]
        # Which record the figures came from. "holdings" means each fund's
        # own dates -- the same basis as the look-through report, so the
        # two agree. "plan" means the portfolio-level SIP split across
        # funds, which is the older, coarser path and an assumption the
        # page should keep admitting to.
        out["contributions_from"] = valued.get("source", "plan")
        if valued.get("funds_total"):
            out["funds_priced"] = valued["funds_priced"]
            out["funds_total"] = valued["funds_total"]
    else:
        value = float(g["current_value"]) if g["current_value"] is not None else None
        out["valued_on"] = str(g["valued_on"]) if g["valued_on"] else None
        out["value_source"] = "entered" if value is not None else None
        if valued and not valued.get("ok"):
            out["value_note"] = valued.get("why")
    out["current_value"] = value

    if value is None:
        out["required_from_here"] = None
        out["achieved_so_far"] = None
        out["projected"] = None
        return out

    # From here: today's value is the new lump sum, and the SIP continues.
    out["required_from_here"] = _solve_rate(target, value, sip, remaining)

    # Achieved: what the contributions to date actually earned.
    #
    # Where the valuation came from the holdings we have the real dated
    # cashflows, so this is a XIRR over what actually happened. The
    # _solve_rate fallback assumes a flat monthly SIP from the plan's own
    # start date, which is only right when the plan IS the record -- and
    # gives a different answer from the report whenever it is not.
    #
    # The quarter-of-history guard stays either way, and is measured from
    # the first real contribution rather than from the plan's start: a
    # plan set up in January whose first instalment landed in August has
    # one month of history, not eight.
    flows = (valued or {}).get("cashflows") or []
    if flows and (valued or {}).get("source") == "holdings":
        first_in = min(d for d, a in flows if a < 0)
        months_in = _months_between(first_in, today)
        out["months_invested"] = months_in
        out["achieved_so_far"] = _xirr(flows) if months_in >= 3 else None
    else:
        out["achieved_so_far"] = _solve_rate(value, lump, sip, elapsed) \
            if elapsed >= 3 else None      # under a quarter it is noise

    # Projected at the rate achieved so far. NOT a forecast -- it answers
    # "if the next years look like the last ones", which is a conditional
    # the page states rather than a prediction it makes.
    if out["achieved_so_far"] is not None:
        m = (1 + out["achieved_so_far"]) ** (1 / 12) - 1
        out["projected"] = round(_fv(m, value, sip, remaining), 2)
        out["gap"] = round(out["projected"] - target, 2)
    return out


# Every NAV we hold for these funds, in ONE query. Pricing instalment by
# instalment would be one round trip per month per fund -- 24 months across
# 5 funds is 120 queries for a page load.
#
# It starts BEFORE the plan does. Filtering from started_on exactly meant
# that a plan beginning on a Sunday had no NAV on or before its first
# instalment -- the fallback to the previous trading day had been filtered
# away -- so the first instalment silently went unpriced and the total
# invested came up one month short. Markets close for weekends and for
# stretches of holidays, so the lead-in has to cover the longest of those.
NAV_HISTORY = """
SELECT scheme_code, nav_date, nav
FROM mf_nav
WHERE scheme_code = ANY(%(codes)s) AND nav_date >= %(from_date)s
ORDER BY scheme_code, nav_date
"""


# Enough to clear any run of market holidays before the first instalment.
NAV_LEAD_IN = timedelta(days=30)


def _sip_dates(started, today):
    """One date per month from `started`, on the same day of the month.

    Clamped to the month's length, so a plan started on the 31st buys on
    the 30th in April and the 28th in February rather than skipping those
    months entirely.
    """
    out, y, m, day = [], started.year, started.month, started.day
    while True:
        d = date(y, m, min(day, monthrange(y, m)[1]))
        if d > today:
            break
        if d >= started:
            out.append(d)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _xirr(flows):
    """Annual rate that makes a set of dated cashflows net to zero.

    Contributions negative, the closing value positive.

    BISECTION, NOT NEWTON. Newton's method diverges on exactly the inputs
    this gets -- a few months of history with a large proportional gain --
    and returns a confident nonsense number rather than failing. Bisection
    over a bracket cannot do that: it either converges inside the bracket
    or reports that it could not.
    """
    if len(flows) < 2:
        return None
    t0 = min(d for d, _ in flows)

    def npv(rate):
        total = 0.0
        for d, amt in flows:
            years = (d - t0).days / 365.25
            total += amt / ((1.0 + rate) ** years)
        return total

    lo, hi = -0.9999, 10.0
    try:
        f_lo, f_hi = npv(lo), npv(hi)
    except (OverflowError, ZeroDivisionError):
        return None
    if f_lo * f_hi > 0:            # no sign change: no root in the bracket
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        try:
            f_mid = npv(mid)
        except (OverflowError, ZeroDivisionError):
            return None
        if abs(f_mid) < 1e-7:
            return round(mid, 6)
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return round((lo + hi) / 2.0, 6)


def _value_from_holdings(cur, portfolio_id, client_id):
    """What the money did, taken from each fund's OWN dates and amounts.

    THE POINT OF THIS FUNCTION IS THAT IT IS NOT A SEPARATE CALCULATION.
    It calls value_holdings -- the same code the look-through report uses
    -- so the summary on the portfolio page and the report behind it are
    arithmetically the same answer rather than two implementations that
    agree until they don't.

    They did not agree. The goal tracker read portfolio_goal, which holds
    ONE SIP for the whole portfolio, and reported 25,000 put in. The
    report read the four holdings' own dates and reported 91,000. Both
    were computed correctly from different records of the same fact, and
    the page showed them a scroll apart without noticing.

    The per-fund dates win because they are the better record: they say
    which fund got the money and when, where the plan-level SIP has to
    guess the split from whatever amounts happen to be filled in. The
    plan's SIP keeps its real job -- what is still to be invested, which
    is the one thing the holdings cannot know.

    Returns None when no holding carries enough to be priced, so the
    caller falls back to the old plan-split path rather than showing
    nothing.
    """
    rows = _q(cur, """
        SELECT scheme_code, invested_on, invest_mode, invest_amount, plan_type
        FROM portfolio_holding
        WHERE portfolio_id = %(p)s
    """, {"p": portfolio_id})
    if not rows:
        return None

    default_plan = "REGULAR" if client_id else "DIRECT"
    priced = value_holdings(cur, [
        {"scheme_code": r["scheme_code"],
         "invested_on": r["invested_on"],
         "invest_mode": r["invest_mode"],
         "invest_amount": r["invest_amount"],
         "plan": r["plan_type"] or default_plan}
        for r in rows])

    got = [(r, p) for r, p in zip(rows, priced) if p and p.get("priced")]
    if not got:
        return None

    today = date.today()
    invested = value = 0.0
    n_priced = n_unpriced = 0
    valued_on = None
    flows = []

    for r, p in got:
        invested += float(p["paid"])
        value += float(p["value"])
        n_priced += int(p.get("instalments") or 0)
        n_unpriced += int(p.get("unpriced") or 0)
        d = date.fromisoformat(str(p["nav_date"])[:10])
        # The OLDEST valuation date across the funds, not the newest. If
        # one fund's NAV is a day behind, the portfolio is worth what it
        # was worth on that day -- claiming today's date for a mixed
        # basket overstates how current the figure is.
        valued_on = d if valued_on is None else min(valued_on, d)

        # Real cashflows, per instalment, so the rate below is a XIRR over
        # what actually happened rather than a formula fed a flat monthly
        # figure. Instalments before the fund had a NAV are left out --
        # they were counted as unpriced above and no money can be shown
        # going in on a day nobody could have bought.
        first = p.get("first_priced")
        first = date.fromisoformat(str(first)[:10]) if first else None
        amt = float(r["invest_amount"])
        on = r["invested_on"]
        if isinstance(on, str):
            on = date.fromisoformat(on[:10])
        for d_i in instalment_dates(on, r["invest_mode"], today):
            if first and d_i < first:
                continue
            flows.append((d_i, -amt))

    if value <= 0:
        return None
    flows.append((valued_on or today, value))

    return {"ok": True,
            "current_value": round(value, 2),
            "valued_on": valued_on,
            "invested": round(invested, 2),
            "priced": n_priced,
            "due": n_priced + n_unpriced,
            "unpriced": n_unpriced,
            "funds_priced": len(got),
            "funds_total": len(rows),
            "source": "holdings",
            "split_by": "the dates and amounts entered against each fund",
            "cashflows": flows}


def _value_from_nav(cur, portfolio_id, g, client_id=None):
    """What the plan is worth today, from NAV history.

    TRIES THE HOLDINGS FIRST. Where the funds carry their own dates and
    amounts, _value_from_holdings above answers this from the same code
    the look-through report uses, and everything below is skipped. The
    plan-level split that follows is the fallback for portfolios where
    nobody has filled those in -- which was every portfolio until the
    invest fields were added, and is fewer every week.

    Each instalment buys units at the NAV on or before its date; the units
    are then valued at each fund's latest NAV. This is what the money did,
    not what a steady-return formula says it should have done.

    THE CONTRIBUTION IS SPLIT ACROSS FUNDS BY THE AMOUNTS ALREADY ENTERED
    against each holding, or equally when they are missing. That split is
    an assumption -- the plan records one SIP for the portfolio, not one
    per fund -- and it is returned so the page can say so rather than
    present a valuation as though the allocation were known.

    Returns None when it cannot be done honestly: no funds, no NAV history,
    or too few instalments priceable to mean anything.
    """
    better = _value_from_holdings(cur, portfolio_id, client_id)
    if better:
        return better

    holdings = _q(cur, """
        SELECT scheme_code, amount FROM portfolio_holding
        WHERE portfolio_id = %(p)s
    """, {"p": portfolio_id})
    if not holdings:
        return None

    codes = [str(h["scheme_code"]) for h in holdings]
    amounts = [float(h["amount"]) if h["amount"] is not None else 0.0
               for h in holdings]
    named = sum(amounts)

    if named > 0:
        # Weight by the amounts that exist, and give NOTHING to a fund
        # without one.
        #
        # The old fallback split equally across ALL funds the moment any
        # single amount was blank -- so a fund you never put money into
        # collected a share of every instalment and turned up in the
        # valuation. A missing amount means no money in that fund, not an
        # unknown amount to be guessed at.
        weights = {c: a / named for c, a in zip(codes, amounts)}
        funded = [c for c, a in zip(codes, amounts) if a > 0]
        skipped = len(codes) - len(funded)
        split_by = "the amounts you entered against each fund"
        if skipped:
            split_by += (", leaving out %d fund%s with no amount"
                         % (skipped, "" if skipped == 1 else "s"))
        codes = funded
    else:
        # No amounts anywhere: an equal split is the only thing left, and
        # the page says so rather than implying the allocation is known.
        weights = {c: 1.0 / len(codes) for c in codes}
        split_by = "an equal split, because no fund has an amount against it"

    started = g["started_on"]
    rows = _q(cur, NAV_HISTORY,
              {"codes": codes, "from_date": started - NAV_LEAD_IN})
    if not rows:
        return None

    # scheme -> (sorted dates, navs), for a bisect lookup per instalment.
    series = defaultdict(lambda: ([], []))
    for r in rows:
        d, n = series[str(r["scheme_code"])]
        d.append(r["nav_date"])
        n.append(float(r["nav"]))

    def nav_on_or_before(code, when):
        dates, navs = series.get(code, ([], []))
        if not dates:
            return None
        i = bisect_right(dates, when)
        return navs[i - 1] if i else None

    today = date.today()
    sip = float(g["sip_amount"] or 0)
    lump = float(g["lumpsum_amount"] or 0)
    dates_due = _sip_dates(started, today) if sip > 0 else []

    units = defaultdict(float)
    priced, unpriced, invested = 0, 0, 0.0
    cashflows = []                      # (date, amount) for the achieved rate

    if lump > 0:
        for code in codes:
            nav = nav_on_or_before(code, started)
            share = lump * weights[code]
            if nav:
                units[code] += share / nav
                invested += share
            else:
                unpriced += 1
        if invested > 0:
            cashflows.append((started, invested))

    for due in dates_due:
        got = 0.0
        for code in codes:
            nav = nav_on_or_before(code, due)
            share = sip * weights[code]
            if nav:
                units[code] += share / nav
                got += share
        if got > 0:
            priced += 1
            invested += got
            cashflows.append((due, got))
        else:
            unpriced += 1

    if not units:
        return None

    # Too little priced history is not a valuation, it is a guess with a
    # decimal point. Say so rather than publish it.
    if sip > 0 and priced < max(1, len(dates_due) // 2):
        return {"ok": False, "priced": priced, "due": len(dates_due),
                "why": "we do not hold NAV history far enough back"}

    value, valued_on = 0.0, None
    for code, u in units.items():
        dates, navs = series[code]
        if not dates:
            continue
        value += u * navs[-1]
        valued_on = dates[-1] if valued_on is None else min(valued_on, dates[-1])

    return {"ok": True,
            "current_value": round(value, 2),
            "valued_on": valued_on,
            "invested": round(invested, 2),
            "priced": priced, "due": len(dates_due), "unpriced": unpriced,
            "split_by": split_by,
            "cashflows": cashflows}


class GoalIn(BaseModel):
    target_amount: float
    target_date: str                  # YYYY-MM-DD
    sip_amount: float = 0
    lumpsum_amount: float = 0
    started_on: Optional[str] = None  # defaults to today
    current_value: Optional[float] = None
    valued_on: Optional[str] = None
    # What the money is FOR and how much risk this particular goal can
    # take. Both live on the goal rather than the client, because the same
    # person can be aggressive about a grandchild's education and defensive
    # about their own retirement.
    purpose: Optional[str] = None
    risk_band: Optional[str] = None
    # A REGULAR-INCOME PLAN, stored beside the corpus it works out to.
    #
    # target_amount already carries that corpus and every consumer -- the
    # progress tracker, the valuation, the alerts -- keeps working with no
    # change, because "the sum needed on the target date" is true of both
    # kinds of plan. What target_amount cannot carry is the QUESTION it
    # answers. Without these three, reopening an income plan shows a
    # corpus with no idea it came from wanting a monthly income, and the
    # planner repaints it as a saving plan with the income boxes empty.
    #
    # NULL in all three means a saving plan, which is what every goal
    # saved before this was.
    income_amount: Optional[float] = None
    income_mode: Optional[str] = None        # 'keep' | 'spend'
    draw_years: Optional[int] = None         # only meaningful for 'spend'


@router.get("/{portfolio_id}/goal")
def get_goal(portfolio_id: int, request: Request):
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        # Captured, not discarded: client_id decides whether this
        # portfolio's holdings are priced on Regular or Direct, and the
        # valuation below needs to know.
        p = _owned(cur, portfolio_id, user_id)
        g = _q(cur, "SELECT * FROM portfolio_goal WHERE portfolio_id = %(p)s",
               {"p": portfolio_id}, one=True)
        valued = (_value_from_nav(cur, portfolio_id, g, p.get("client_id"))
                  if g else None)
    if not g:
        return {"tracking": False}
    return {"tracking": True,
            "goal": {"target_amount": float(g["target_amount"]),
                     "target_date": str(g["target_date"]),
                     "sip_amount": float(g["sip_amount"]),
                     "lumpsum_amount": float(g["lumpsum_amount"]),
                     "started_on": str(g["started_on"]),
                     "current_value": float(g["current_value"])
                                      if g["current_value"] is not None else None,
                     "valued_on": str(g["valued_on"]) if g["valued_on"] else None,
                     "purpose": g.get("purpose"),
                     "risk_band": g.get("risk_band"),
                     # .get, not [...]: a server running against a
                     # database where add_income_columns.py has not been
                     # run yet answers without them rather than 500ing
                     # every goal on the site.
                     "income_amount": (float(g["income_amount"])
                                       if g.get("income_amount") is not None
                                       else None),
                     "income_mode": g.get("income_mode"),
                     "draw_years": (int(g["draw_years"])
                                    if g.get("draw_years") is not None
                                    else None)},
            "progress": _progress(g, valued)}


@router.put("/{portfolio_id}/goal")
def put_goal(portfolio_id: int, body: GoalIn, request: Request):
    user_id = _user_id(request)
    if body.target_amount <= 0:
        raise HTTPException(400, "The target has to be more than zero.")
    if body.sip_amount <= 0 and body.lumpsum_amount <= 0:
        raise HTTPException(400, "Enter a monthly amount, a lump sum, or both.")
    if (body.current_value is None) != (body.valued_on is None):
        # The check constraint enforces this too; catching it here gives a
        # sentence instead of a database error.
        raise HTTPException(400, "A current value needs the date it was true.")
    if body.income_mode is not None and body.income_mode not in ("keep", "spend"):
        raise HTTPException(400, "income_mode is 'keep' or 'spend'.")
    if body.income_mode == "spend" and not (body.draw_years or 0) > 0:
        # Drawing down over no years is not a plan, and stored that way it
        # would come back as an income lasting zero months.
        raise HTTPException(400, "Spending the capital down needs a number "
                                 "of years to spend it over.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned(cur, portfolio_id, user_id)
        g = _q(cur, """
            INSERT INTO portfolio_goal
                (portfolio_id, target_amount, target_date, sip_amount,
                 lumpsum_amount, started_on, current_value, valued_on,
                 purpose, risk_band,
                 income_amount, income_mode, draw_years)
            VALUES (%(p)s, %(t)s, %(d)s, %(s)s, %(l)s,
                    COALESCE(%(st)s::date, CURRENT_DATE), %(v)s, %(vd)s,
                    %(pu)s, %(rb)s,
                    %(ia)s, %(im)s, %(dy)s)
            ON CONFLICT (portfolio_id) DO UPDATE
               SET target_amount  = EXCLUDED.target_amount,
                   target_date    = EXCLUDED.target_date,
                   sip_amount     = EXCLUDED.sip_amount,
                   lumpsum_amount = EXCLUDED.lumpsum_amount,
                   started_on     = EXCLUDED.started_on,
                   -- COALESCE, not EXCLUDED: saving the plan without
                   -- retyping the valuation must not erase it. Same shape
                   -- as the amount fix in add_funds.
                   current_value  = COALESCE(EXCLUDED.current_value,
                                             portfolio_goal.current_value),
                   valued_on      = COALESCE(EXCLUDED.valued_on,
                                             portfolio_goal.valued_on),
                   purpose        = COALESCE(EXCLUDED.purpose,
                                             portfolio_goal.purpose),
                   risk_band      = COALESCE(EXCLUDED.risk_band,
                                             portfolio_goal.risk_band),
                   -- EXCLUDED, not COALESCE, and deliberately unlike the
                   -- lines above. A goal REWORKED from an income plan
                   -- into a saving plan sends NULL here, and COALESCE
                   -- would keep the old income beside the new target --
                   -- two plans in one row, with the planner believing the
                   -- stale half. Purpose and the valuation are omitted
                   -- when unchanged; these three are always sent
                   -- together by the one screen that owns them.
                   income_amount  = EXCLUDED.income_amount,
                   income_mode    = EXCLUDED.income_mode,
                   draw_years     = EXCLUDED.draw_years,
                   updated_at     = now()
            RETURNING *
        """, {"p": portfolio_id, "t": body.target_amount,
              "d": body.target_date, "s": body.sip_amount,
              "l": body.lumpsum_amount, "st": body.started_on,
              "v": body.current_value, "vd": body.valued_on,
              "pu": body.purpose, "rb": body.risk_band,
              "ia": body.income_amount,
              "im": body.income_mode,
              "dy": body.draw_years}, one=True)
        valued = _value_from_nav(cur, portfolio_id, g)
        conn.commit()
    return {"tracking": True, "progress": _progress(g, valued)}


@router.delete("/{portfolio_id}/goal")
def delete_goal(portfolio_id: int, request: Request):
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned(cur, portfolio_id, user_id)
        cur.execute("DELETE FROM portfolio_goal WHERE portfolio_id = %(p)s",
                    {"p": portfolio_id})
        conn.commit()
    return {"tracking": False}


@router.post("/{portfolio_id}/import/preview")
def import_preview(portfolio_id: int, body: ImportRequest, request: Request):
    """Match pasted rows to funds and report what WOULD be added.

    WRITES NOTHING. Import is a two-step on purpose: a name like "HDFC
    Mid Cap" can match several schemes, and silently picking one puts a
    fund in someone's portfolio that they never chose and may not notice
    until a report goes to a client. The person sees the matches, fixes
    what is wrong, and only then saves through the ordinary add endpoint.

    Matching REUSES portfolio_api.search_funds -- the same function behind
    the picker. A second matcher here would drift from it, and the failure
    would be invisible: the picker finds a fund, the import does not, and
    nothing explains why.
    """
    user_id = _user_id(request)
    if not body.rows:
        raise HTTPException(400, "Nothing to import.")
    if len(body.rows) > 200:
        raise HTTPException(400, "That is more than 200 rows.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned(cur, portfolio_id, user_id)
        held = {str(r["scheme_code"]) for r in _q(cur, """
            SELECT scheme_code FROM portfolio_holding WHERE portfolio_id = %(pid)s
        """, {"pid": portfolio_id})}

        out = match_rows(cur, body.rows, held)

    return {
        **out,
        "missing_amounts": sum(1 for e in out["matched"] + out["ambiguous"]
                               if e["amount"] is None),
        # How many more funds fit. The page needs this to warn BEFORE the
        # person confirms, rather than silently dropping the overflow.
        "room": max(0, MAX_FUNDS - len(held)),
    }


@router.delete("/{portfolio_id}/funds/{scheme_code}")
def remove_fund(portfolio_id: int, scheme_code: str, request: Request):
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned(cur, portfolio_id, user_id)
        cur.execute("""
            DELETE FROM portfolio_holding
            WHERE portfolio_id = %(pid)s AND scheme_code = %(code)s
        """, {"pid": portfolio_id, "code": scheme_code})
        removed = cur.rowcount
        _exec(cur, """
            UPDATE portfolio SET updated_at = now()
            WHERE portfolio_id = %(pid)s
        """, {"pid": portfolio_id})
        conn.commit()
    return {"removed": removed}


@router.patch("/{portfolio_id}")
def rename_portfolio(portfolio_id: int, body: RenamePortfolio, request: Request):
    user_id = _user_id(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Give the portfolio a name.")
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned(cur, portfolio_id, user_id)
        _exec(cur, """
            UPDATE portfolio SET name = %(name)s, updated_at = now()
            WHERE portfolio_id = %(pid)s
        """, {"pid": portfolio_id, "name": name})
        conn.commit()
    return {"portfolio_id": portfolio_id, "name": name}


@router.delete("/{portfolio_id}")
def delete_portfolio(portfolio_id: int, request: Request):
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned(cur, portfolio_id, user_id)
        # portfolio_holding is ON DELETE CASCADE, so the rows go with it.
        cur.execute("DELETE FROM portfolio WHERE portfolio_id = %(pid)s",
                    {"pid": portfolio_id})
        conn.commit()
    return {"deleted": portfolio_id}


@router.post("/{portfolio_id}/lookthrough")
def lookthrough_saved(portfolio_id: int, request: Request,
                      as_of: Optional[str] = None, top: int = 30):
    """Explode a saved portfolio through the existing look-through.

    Deliberately a thin adapter. The consolidation rules -- one target month
    across all funds, coverage as share of NAV, rupees from pct_of_nav and
    never market_value -- live in portfolio_api.lookthrough and are not
    reimplemented here.

    Note the difference from GET above: that shows each fund's OWN latest
    disclosure, which is right for a per-fund row. This pins one target month
    across every fund, because comparing March against April and calling the
    difference overlap is the error the single target date exists to prevent.
    """
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        p = _owned(cur, portfolio_id, user_id)
        # THE INVEST FIELDS TRAVEL TOO.
        #
        # This used to select scheme_code and amount alone, so a holding
        # with a SIP date and a monthly figure arrived here as a bare
        # rupee total: no dates, no mode, no plan. The report then had
        # nothing to compute a XIRR from and nothing to tell it which
        # plan to price -- for a stored portfolio, which is the one place
        # those details are actually on file. The browser path carried
        # them and this one did not, so the same portfolio gave two
        # different answers depending on which button was pressed.
        held = _q(cur, """
            SELECT scheme_code, amount, invested_on, invest_mode,
                   invest_amount, plan_type
            FROM portfolio_holding
            WHERE portfolio_id = %(pid)s
        """, {"pid": portfolio_id})

    if not held:
        raise HTTPException(400, "That portfolio has no funds yet.")

    # A holding is valuable if somebody typed a total OR if it carries
    # enough to be priced from NAV history. Demanding a typed amount
    # from a holding that states "5,000 a month since March" would
    # reject the better-specified of the two.
    def priceable(h):
        return (h["invested_on"] and h["invest_mode"]
                and h["invest_amount"] and float(h["invest_amount"]) > 0)

    missing = [h["scheme_code"] for h in held
               if h["amount"] is None and not priceable(h)]
    if missing:
        raise HTTPException(
            400, "Add an amount, or a start date and monthly figure, for "
                 f"every fund before running the look-through. "
                 f"Missing: {', '.join(missing)}")

    # Regular for a client's portfolio, Direct for the user's own -- the
    # same default the rest of the app applies, so a holding that states
    # no plan is still priced on the one they hold.
    default_plan = "REGULAR" if p.get("client_id") else "DIRECT"

    return _lookthrough(LookthroughRequest(
        holdings=[Holding(
            key=str(h["scheme_code"]),
            amount=float(h["amount"]) if h["amount"] is not None else None,
            invested_on=str(h["invested_on"]) if h["invested_on"] else None,
            invest_mode=h["invest_mode"],
            invest_amount=(float(h["invest_amount"])
                           if h["invest_amount"] is not None else None),
            plan=(h["plan_type"] or default_plan),
        ) for h in held],
        as_of=as_of,
        top=top,
    ))


# =====================================================================
# ALERTS -- what a person has asked to be told about
# =====================================================================
#
# NOTHING HERE SENDS ANYTHING, AND THE SCREEN SAYS SO.
#
# These endpoints record choices. The delivery side -- email, WhatsApp,
# a push notification -- does not exist yet. That is stated on the page in
# plain words rather than implied by a switch that looks live, because a
# control labelled "alert me" that quietly does nothing does more harm
# than no control: it converts somebody's attention into a false sense of
# being watched over, which is the opposite of what this product is for.
#
# EVERYTHING IS MEASURED FROM THE DAY THE MONEY WENT IN.
#
#     Not from "last month", and not from whenever the nightly job last
#     ran. The holder's question is never "has this fund had a bad week" --
#     it is "what has happened to MY money since I put it in". Two people
#     in the same fund who bought a year apart have different answers to
#     that, and an alert that ignores the difference is telling at least
#     one of them about somebody else's investment.
#
#     Each holding in a saved portfolio carries invested_on, so the
#     baseline is per fund, not per portfolio. A fund added last month is
#     judged from last month even if the rest have been held for years.
#
#     Where a holding has no date recorded, the rule falls back to the
#     oldest reading held -- and says so rather than pretending.
#
# WHY EVERY RULE CARRIES A DATA STATUS
#     Two of the rules asked for cannot be evaluated from what this
#     database currently computes. Offering them as equal checkboxes would
#     be drawing a control panel for a machine that is not connected.
#
#     So each rule reports what it would need, measured against the actual
#     tables, every time the screen loads:
#
#       ready     everything it reads exists now
#       accruing  it reads a history that is still being built
#       missing   it reads something nothing currently computes
#
#     A reader can still switch on a rule that is not ready -- the choice
#     is recorded and honoured the day the data arrives. That is a promise
#     that can be kept. A silent one is not.
#
# A CORRECTION WORTH RECORDING
#     The first version of this called the two rising-score rules
#     "accruing", on the grounds that nothing had ever recorded a fund's
#     rising score so there was no yesterday to compare against.
#
#     That was wrong. Every input is a time series that nothing prunes:
#     stock_score is keyed (isin, as_of_date, algo_version) and never
#     deleted from, stock_technical keeps every day, mf_holding keeps
#     every monthly disclosure. The past readings were never lost -- they
#     were a query nobody had written. TREND_BATCH_AT is that query and
#     backfill_fund_trend.py runs it back over two years of month ends.
#
#     The lesson is narrow and worth keeping: "we do not store X" is not
#     the same as "X is unknowable", and the difference is two years of
#     history for one afternoon's work.

# The catalogue. ONE definition, read by the screen today and by the
# evaluator when it is written, so the two can never drift apart about
# what a rule means or what its default is.
#
# `needs` names the table the rule reads. `window` is how many readings of
# history it wants before it can say anything.
ALERT_RULES = [
    {
        "key": "rising_drop", "group": "How the holdings are trending",
        "name": "A fund's rising score falls sharply",
        "what": "The share of this fund's holdings that are trending up "
                "drops this far below where it stood on the day you "
                "invested in it.",
        "why": "Measured against your own starting point, not last month's. "
               "It is a reading of what the manager owns today, not a "
               "forecast -- and it moves back as often as it moves on.",
        "unit": "% below your start", "default": 20, "min": 5, "max": 60,
        "needs": "mf_fund_trend", "window": 2,
    },
    {
        "key": "rising_streak", "group": "How the holdings are trending",
        "name": "A fund's rising score falls three months running",
        "what": "Three consecutive month ends of the rising score going "
                "down, however small each step is.",
        "why": "One fall is noise. Three in a row is a direction, and it is "
               "the shape a single large drop can hide. Compared month end "
               "to month end, because the portfolio underneath only changes "
               "monthly -- a daily series would re-score the same holdings "
               "against moving prices and call the wobble a trend.",
        "unit": None, "default": None,
        "needs": "mf_fund_trend", "window": 4,
    },
    {
        "key": "behind_benchmark", "group": "Returns since you invested",
        "name": "A fund falls behind its own benchmark",
        "what": "Since the day you invested, the fund has returned less "
                "than the index it is measured against.",
        "why": "A fund behind its own index handed you less than simply "
               "buying that index would have -- over your holding period, "
               "not over a calendar window you were not invested for.",
        "unit": None, "default": None,
        "needs": "mf_nav", "window": 1,
    },
    {
        "key": "below_category", "group": "Returns since you invested",
        "name": "A fund drops below its category average",
        "what": "Since the day you invested, the fund has returned less "
                "than the median fund doing the same job.",
        "why": "This compares like with like. When mid caps have a poor "
               "stretch every mid cap fund looks weak against an index, "
               "but only some look weak against each other.",
        "unit": None, "default": None,
        "needs": "mf_nav", "window": 1,
    },
    {
        "key": "rank_slip", "group": "Returns since you invested",
        "name": "A fund drops out of the better half of its category",
        "what": "Its placing among funds doing the same job falls below "
                "halfway, having been above it when you invested.",
        "why": "Slower to move than a single month's return, and harder to "
               "explain away as a bad patch for the whole category.",
        "unit": None, "default": None,
        "needs": "mf_returns", "window": 1,
    },
    {
        "key": "overlap_high", "group": "The portfolio as a whole",
        "name": "Two funds start holding the same companies",
        "what": "Any two funds you hold overlap by more than this much of "
                "their weight, having been below it when you invested.",
        "why": "Two funds that overlap this much do less for spreading risk "
               "than two names suggest. Managers drift towards each other "
               "without telling anyone, which is why this is worth watching "
               "rather than checking once.",
        "unit": "% overlap", "default": 50, "min": 25, "max": 90,
        "needs": "mf_holding", "window": 1,
    },
    {
        "key": "mix_drift", "group": "The portfolio as a whole",
        "name": "The mix drifts away from what you planned",
        "what": "The large/mid/small split moves this many points from "
                "where it stood when you invested.",
        "why": "Nobody rebalances a drift they cannot see. This is the "
               "number that decides the outcome and the one people skip "
               "past.",
        "unit": "points", "default": 10, "min": 3, "max": 30,
        "needs": "fund_profile", "window": 1,
    },
    {
        "key": "mandate_drift", "group": "The portfolio as a whole",
        "name": "A fund stops being what its name says",
        "what": "A mid cap fund carrying mostly large caps, say -- measured "
                "through to the shares, not taken from the name.",
        "why": "You chose the fund for a job. If it quietly stops doing that "
               "job, your plan's shape changes without you changing "
               "anything.",
        "unit": None, "default": None,
        "needs": "fund_profile", "window": 1,
    },
    {
        "key": "no_disclosure", "group": "Whether the numbers can be trusted",
        "name": "A fund stops publishing its portfolio",
        "what": "No new holdings disclosure for two months.",
        "why": "This one protects the others. Overlap, cap mix and rising "
               "are all read from the last disclosure -- if that stops "
               "arriving, those numbers quietly go stale rather than go "
               "wrong, which is much harder to notice.",
        "unit": None, "default": None,
        "needs": "mf_holding", "window": 1,
    },
]

ALERT_CHANNELS = [
    {"key": "email", "name": "Email"},
    {"key": "whatsapp", "name": "WhatsApp"},
    {"key": "push", "name": "App notification"},
]

# The two "rising score" rules read the stock-score trend, so they are
# withdrawn while scores are switched off (MF_SCORES_ENABLED). Filtered here,
# before RULE_KEYS is built, so the screen, the validation and the saved
# settings all agree on which rules exist.
SCORE_RULE_KEYS = {"rising_drop", "rising_streak"}
if not _scores_on():
    ALERT_RULES = [r for r in ALERT_RULES if r["key"] not in SCORE_RULE_KEYS]

RULE_KEYS = {r["key"] for r in ALERT_RULES}
CHANNEL_KEYS = {c["key"] for c in ALERT_CHANNELS}


def _alert_readiness(cur):
    """What each rule's data actually looks like, right now.

    Measured rather than declared. A hard-coded "this one is ready" would
    be wrong the day a pipeline changes, and wrong in the direction that
    makes the screen lie.
    """
    # ASKED, NOT ATTEMPTED.
    #
    # The obvious shape here is to query the table and catch the error if
    # it does not exist. That is wrong inside a transaction: a failed
    # statement aborts the whole thing, so every query after it fails too
    # and the endpoint 500s instead of reporting "not ready". to_regclass
    # returns NULL for a missing table without raising.
    exists = _q(cur, """
        SELECT to_regclass('public.mf_fund_trend') IS NOT NULL AS trend,
               to_regclass('public.mf_returns')    IS NOT NULL AS returns,
               to_regclass('public.mf_nav')        IS NOT NULL AS nav
    """, {}, one=True)

    # How much per-fund trend history has been kept. build_category_trend
    # started recording it; before that run there is none, and no amount
    # of code can make a question about last month answerable.
    dates, first = 0, None
    if exists["trend"]:
        row = _q(cur, """
            SELECT count(DISTINCT as_of_date) AS n, MIN(as_of_date) AS first
            FROM mf_fund_trend
        """, {}, one=True)
        dates, first = int(row["n"] or 0), row["first"]

    # Which return periods are actually stored. "6M" is not one of them --
    # score_returns computes 1Y, 3Y, 5Y and 10Y.
    periods = set()
    if exists["returns"]:
        periods = {r["period"] for r in _q(cur, """
            SELECT DISTINCT period FROM mf_returns
        """, {})}

    out = {}
    for rule in ALERT_RULES:
        needs = rule["needs"]
        if needs == "mf_fund_trend":
            want = rule["window"]
            if dates >= want:
                out[rule["key"]] = {"status": "ready", "note": ""}
            elif dates:
                out[rule["key"]] = {
                    "status": "accruing",
                    "note": ("The month-by-month record holds %d reading%s, "
                             "starting %s. This rule needs %d. Running "
                             "backfill_fund_trend.py fills the rest in from "
                             "the score history already stored -- it does "
                             "not have to be waited for."
                             % (dates, "" if dates == 1 else "s", first, want))}
            else:
                out[rule["key"]] = {
                    "status": "accruing",
                    "note": ("The month-by-month record has not been built "
                             "yet. It is not lost history -- the readings can "
                             "be computed back over two years from scores "
                             "already stored, by running "
                             "backfill_fund_trend.py once.")}
        elif needs.startswith("mf_returns:"):
            wanted = needs.split(":", 1)[1]
            out[rule["key"]] = ({"status": "ready", "note": ""}
                                if wanted in periods else
                                {"status": "missing",
                                 "note": ("Returns are computed over 1, 3, 5 "
                                          "and 10 years -- not over %s."
                                          % wanted)})
        elif needs == "mf_nav":
            # Return SINCE A GIVEN DAY needs the NAV series, not a
            # precomputed 1Y/3Y/5Y figure -- and the NAV series is the one
            # thing this database has most of. The look-through already
            # prices holdings from an arbitrary date this way.
            out[rule["key"]] = ({"status": "ready", "note": ""}
                                if exists["nav"] else
                                {"status": "missing",
                                 "note": "No NAV history is loaded."})
        else:
            out[rule["key"]] = {"status": "ready", "note": ""}
    return out


class AlertSettings(BaseModel):
    rules: dict = {}          # {rule_key: {"enabled": bool, "threshold": num}}
    channels: dict = {}       # {channel_key: bool}


@router.get("/{portfolio_id}/alerts")
def get_alerts(portfolio_id: int, request: Request):
    """The catalogue, this portfolio's choices, and what the data supports."""
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned(cur, portfolio_id, user_id)

        chosen = {r["rule_key"]: r for r in _q(cur, """
            SELECT rule_key, enabled, threshold
            FROM portfolio_alert WHERE portfolio_id = %(pid)s
        """, {"pid": portfolio_id})}
        channels = {r["channel"]: r["enabled"] for r in _q(cur, """
            SELECT channel, enabled
            FROM portfolio_alert_channel WHERE portfolio_id = %(pid)s
        """, {"pid": portfolio_id})}
        ready = _alert_readiness(cur)

    rules = []
    for r in ALERT_RULES:
        got = chosen.get(r["key"])
        rules.append(dict(
            r,
            enabled=bool(got["enabled"]) if got else False,
            threshold=(float(got["threshold"]) if got and got["threshold"]
                       is not None else r.get("default")),
            **ready.get(r["key"], {"status": "ready", "note": ""}),
        ))

    return {
        "rules": rules,
        "channels": [dict(c, enabled=bool(channels.get(c["key"], False)))
                     for c in ALERT_CHANNELS],
        # Said once, by the server, so every client shows the same thing
        # and nobody has to remember to update a hard-coded sentence.
        "delivery_live": False,
    }


@router.put("/{portfolio_id}/alerts")
def put_alerts(portfolio_id: int, body: AlertSettings, request: Request):
    """Save the choices. Unknown keys are refused rather than ignored.

    Ignoring them would let a stale client switch on a rule that no longer
    exists and be told it worked.
    """
    user_id = _user_id(request)

    bad = set(body.rules) - RULE_KEYS
    if bad:
        raise HTTPException(400, "Unknown alert rule: %s"
                            % ", ".join(sorted(bad)))
    bad_c = set(body.channels) - CHANNEL_KEYS
    if bad_c:
        raise HTTPException(400, "Unknown channel: %s"
                            % ", ".join(sorted(bad_c)))

    by_key = {r["key"]: r for r in ALERT_RULES}
    rows = []
    for key, spec in body.rules.items():
        rule = by_key[key]
        enabled = bool(spec.get("enabled"))
        threshold = spec.get("threshold")
        if threshold is not None and rule.get("unit"):
            try:
                threshold = float(threshold)
            except (TypeError, ValueError):
                raise HTTPException(400, "%s needs a number." % rule["name"])
            # Clamped, not rejected. A slider that snaps is friendlier than
            # a form that refuses, and the bounds are the catalogue's.
            lo, hi = rule.get("min", 0), rule.get("max", 100)
            threshold = max(lo, min(hi, threshold))
        elif not rule.get("unit"):
            threshold = None          # nothing to tune on this rule
        rows.append((key, enabled, threshold))

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned(cur, portfolio_id, user_id)
        for key, enabled, threshold in rows:
            cur.execute("""
                INSERT INTO portfolio_alert
                    (portfolio_id, rule_key, enabled, threshold)
                VALUES (%(pid)s, %(k)s, %(e)s, %(t)s)
                ON CONFLICT (portfolio_id, rule_key) DO UPDATE
                   SET enabled = EXCLUDED.enabled,
                       threshold = EXCLUDED.threshold,
                       updated_at = now()
            """, {"pid": portfolio_id, "k": key, "e": enabled, "t": threshold})
        for key, on in body.channels.items():
            cur.execute("""
                INSERT INTO portfolio_alert_channel
                    (portfolio_id, channel, enabled)
                VALUES (%(pid)s, %(c)s, %(e)s)
                ON CONFLICT (portfolio_id, channel) DO UPDATE
                   SET enabled = EXCLUDED.enabled, updated_at = now()
            """, {"pid": portfolio_id, "c": key, "e": bool(on)})
        conn.commit()

    on = sum(1 for _, e, _ in rows if e)
    return {"saved": True, "rules_on": on, "delivery_live": False}
