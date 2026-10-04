"""
admin_api.py -- what is happening across the whole site.
---------------------------------------------------------------------------
Mounted at /api/admin. Every endpoint refuses anyone not in app_admin.

WHAT THIS IS FOR
    Every other portfolio query on the site is scoped
    WHERE owner_user_id = the caller, which is right and stays right.
    That leaves nobody able to answer "how many portfolios are there, and
    is any of this doing the investors any good" -- a question the person
    who built the place has a legitimate need to ask.

WHAT IT DELIBERATELY DOES NOT ANSWER
    "What return are our users getting", as one site-wide number. Over
    any window short enough to be interesting that figure is the market,
    not the product: a good quarter makes this place look brilliant and a
    flat one makes it look useless, and neither is true. A median XIRR
    across portfolios of different ages and start dates is an average of
    incomparable things.

    Per portfolio the return is real and is returned. Aggregated, it
    would be a number that feels like evidence and is not, which is worse
    than no number.

    The figure the summary leads with instead is how many portfolios can
    be measured at all. Everything else is computable only on those, so
    that fraction caps every other claim -- and unlike returns, it is
    something the product can actually move.

THE DRILL-DOWN IS LOGGED
    /portfolios/{id} returns somebody's actual holdings. It writes a row
    to admin_access_log every time, before returning anything. That is
    not decoration: an admin who can read any user's finances without a
    trace is a control failure, and the log is the cheap version of the
    control. Run create_admin_log.py first -- without the table the
    drill-down refuses rather than quietly serving data it cannot record.

VALUATION COMES FROM value_holdings
    The same function the portfolio page and the look-through use. A
    census that computed its own returns would eventually disagree with
    what a user sees on their own screen, and then neither number could
    be trusted.
"""

from datetime import date
from typing import Optional

import psycopg
from fastapi import APIRouter, HTTPException, Request
from psycopg.rows import dict_row

from portfolio_api import DB, _q
from saved_portfolio_api import _user_id
from invest_value import value_holdings, instalment_dates

router = APIRouter(prefix="/api/admin", tags=["admin"])

# A page that lists every portfolio and values each one is a page that
# gets slower the more successful the site becomes. Capped, with the cap
# reported, so the number on screen is never quietly partial.
MAX_ROWS = 500


def _require_admin(request: Request):
    """Checked on the SERVER, on every call.

    Hiding the menu item protects nobody -- the endpoint is the thing
    that has to refuse, because the URL is guessable and the page is
    just HTML.
    """
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _q(cur, "SELECT 1 AS ok FROM app_admin WHERE user_id = %(u)s",
                 {"u": user_id}, one=True)
    if not row:
        raise HTTPException(403, "Administrators only.")
    return user_id


def _xirr(flows):
    """Bisection, not Newton.

    Newton diverges on exactly the inputs this gets -- a few months of
    history with a large proportional gain -- and returns a confident
    wrong number rather than failing. Bisection either converges inside
    its bracket or reports that it could not, and "could not" is a fine
    answer to show as a dash.
    """
    if len(flows) < 2:
        return None
    t0 = min(d for d, _ in flows)

    def npv(r):
        return sum(a / ((1.0 + r) ** ((d - t0).days / 365.25)) for d, a in flows)

    lo, hi = -0.9999, 10.0
    try:
        f_lo, f_hi = npv(lo), npv(hi)
    except (OverflowError, ZeroDivisionError):
        return None
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        try:
            f_mid = npv(mid)
        except (OverflowError, ZeroDivisionError):
            return None
        if abs(f_mid) < 1e-7:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


PORTFOLIOS = """
SELECT p.portfolio_id, p.name, p.owner_user_id, p.client_id,
       p.created_at::date AS created,
       p.updated_at::date AS updated,
       u.email, u.full_name,
       c.name             AS client_name,
       g.target_amount, g.target_date,
       COUNT(h.scheme_code)   AS funds,
       COUNT(h.invested_on)   AS funds_dated,
       SUM(h.amount)          AS typed_total
FROM portfolio p
LEFT JOIN mf_user u        ON u.user_id   = p.owner_user_id
LEFT JOIN client c         ON c.client_id = p.client_id
LEFT JOIN portfolio_goal g ON g.portfolio_id = p.portfolio_id
-- ON rather than USING: portfolio_goal is already joined and also
-- carries portfolio_id, and USING then leaves two columns of that name
-- on the left for Postgres to refuse.
LEFT JOIN portfolio_holding h ON h.portfolio_id = p.portfolio_id
GROUP BY p.portfolio_id, p.name, p.owner_user_id, p.client_id,
         p.created_at, p.updated_at,
         u.email, u.full_name, c.name, g.target_amount, g.target_date
ORDER BY p.updated_at DESC
"""

HOLDINGS = """
SELECT h.scheme_code, h.amount, h.invested_on, h.invest_mode,
       h.invest_amount, h.plan_type,
       s.scheme_name, s.amc_name
FROM portfolio_holding h
LEFT JOIN mf_scheme s ON s.scheme_code = h.scheme_code
WHERE h.portfolio_id = %(p)s
ORDER BY s.scheme_name
"""


def _value(cur, rows, client_id):
    """Invested, worth now and XIRR for one set of holdings, or None."""
    if not rows:
        return None
    default_plan = "REGULAR" if client_id else "DIRECT"
    priced = value_holdings(cur, [
        {"scheme_code": r["scheme_code"], "invested_on": r["invested_on"],
         "invest_mode": r["invest_mode"], "invest_amount": r["invest_amount"],
         "plan": r["plan_type"] or default_plan} for r in rows])

    got = [(r, p) for r, p in zip(rows, priced) if p and p.get("priced")]
    if not got:
        return None

    today = date.today()
    paid = val = 0.0
    flows = []
    for r, p in got:
        paid += float(p["paid"])
        val += float(p["value"])
        first = p.get("first_priced")
        first = date.fromisoformat(str(first)[:10]) if first else None
        for d in instalment_dates(r["invested_on"], r["invest_mode"], today):
            if first and d < first:
                continue
            flows.append((d, -float(r["invest_amount"])))
    if val <= 0:
        return None
    flows.append((today, val))
    rate = _xirr(flows)
    return {"invested": round(paid, 2), "value": round(val, 2),
            "gain": round(val - paid, 2),
            "xirr": round(rate, 4) if rate is not None else None,
            "funds_priced": len(got)}


@router.get("/overview")
def overview(request: Request):
    """The census: who, how many, and how much of it is measurable."""
    _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, PORTFOLIOS)
        users_total = _q(cur, "SELECT COUNT(*) AS n FROM mf_user", one=True)["n"]

    with_funds = [r for r in rows if r["funds"]]
    dated = [r for r in with_funds if r["funds_dated"] == r["funds"]]
    part = [r for r in with_funds if 0 < r["funds_dated"] < r["funds"]]

    spread = {}
    for r in with_funds:
        spread[r["funds"]] = spread.get(r["funds"], 0) + 1

    return {
        "users_total": users_total,
        "users_with_portfolio": len({r["owner_user_id"] for r in rows}),
        "portfolios": len(rows),
        "for_clients": sum(1 for r in rows if r["client_id"]),
        "with_goal": sum(1 for r in rows if r["target_amount"] is not None),
        "with_funds": len(with_funds),
        "fully_dated": len(dated),
        "partly_dated": len(part),
        "no_dates": len(with_funds) - len(dated) - len(part),
        # Revised after the day it was created. A portfolio entered once
        # and never touched is a visitor; one revised later is a user.
        "revisited": sum(1 for r in rows if r["updated"] > r["created"]),
        "fund_spread": [{"funds": k, "portfolios": spread[k]}
                        for k in sorted(spread)],
        "avg_funds": (round(sum(r["funds"] for r in with_funds) / len(with_funds), 1)
                      if with_funds else None),
    }


@router.get("/portfolios")
def list_all(request: Request, limit: int = MAX_ROWS,
             dated_only: bool = False):
    """Every portfolio, valued. No holdings -- those need the drill-down."""
    _require_admin(request)
    limit = max(1, min(limit, MAX_ROWS))

    out = []
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, PORTFOLIOS)
        total = len(rows)
        if dated_only:
            rows = [r for r in rows if r["funds"] and r["funds_dated"] == r["funds"]]
        for r in rows[:limit]:
            held = _q(cur, HOLDINGS, {"p": r["portfolio_id"]})
            v = _value(cur, held, r["client_id"])
            out.append({
                "portfolio_id": r["portfolio_id"],
                "name": r["name"],
                "owner": r["full_name"] or r["email"] or "unknown",
                "email": r["email"],
                "client_name": r["client_name"],
                "is_client": bool(r["client_id"]),
                "funds": r["funds"],
                "funds_dated": r["funds_dated"],
                "created": str(r["created"]),
                "updated": str(r["updated"]),
                "has_goal": r["target_amount"] is not None,
                "target_amount": (float(r["target_amount"])
                                  if r["target_amount"] is not None else None),
                "target_date": str(r["target_date"]) if r["target_date"] else None,
                # Typed total, shown only when nothing could be priced --
                # so a portfolio with amounts and no dates still has a
                # size on screen rather than a row of dashes.
                "typed_total": (float(r["typed_total"])
                                if r["typed_total"] is not None else None),
                "valued": v,
            })
    return {"total": total, "shown": len(out),
            "truncated": len(out) < (total if not dated_only else len(out)),
            "rows": out}


@router.get("/portfolios/{portfolio_id}")
def one_portfolio(portfolio_id: int, request: Request,
                  reason: Optional[str] = None):
    """One portfolio's actual holdings. LOGGED, every time.

    The log row is written BEFORE the data is returned and in the same
    transaction, so there is no path that serves somebody's holdings
    without recording it -- including one that fails halfway.
    """
    admin_id = _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        head = _q(cur, """
            SELECT p.portfolio_id, p.name, p.owner_user_id, p.client_id,
                   u.email, u.full_name, c.name AS client_name
            FROM portfolio p
            LEFT JOIN mf_user u ON u.user_id = p.owner_user_id
            LEFT JOIN client c  ON c.client_id = p.client_id
            WHERE p.portfolio_id = %(p)s
        """, {"p": portfolio_id}, one=True)
        if not head:
            raise HTTPException(404, "No such portfolio.")

        if not _q(cur, "SELECT to_regclass('admin_access_log') IS NOT NULL AS ok",
                  one=True)["ok"]:
            raise HTTPException(
                503, "The access log table does not exist, so this view is "
                     "disabled. Run create_admin_log.py --admin doadmin.")

        cur.execute("""
            INSERT INTO admin_access_log
                (admin_user_id, action, portfolio_id, owner_user_id, detail)
            VALUES (%(a)s, 'view_portfolio', %(p)s, %(o)s, %(d)s)
        """, {"a": admin_id, "p": portfolio_id,
              "o": head["owner_user_id"], "d": (reason or "")[:500]})
        conn.commit()

        held = _q(cur, HOLDINGS, {"p": portfolio_id})
        v = _value(cur, held, head["client_id"])

    return {
        "portfolio_id": head["portfolio_id"],
        "name": head["name"],
        "owner": head["full_name"] or head["email"] or "unknown",
        "email": head["email"],
        "client_name": head["client_name"],
        "valued": v,
        "holdings": [{
            "scheme_code": h["scheme_code"],
            "name": h["scheme_name"] or h["scheme_code"],
            "amc": h["amc_name"],
            "amount": float(h["amount"]) if h["amount"] is not None else None,
            "invested_on": str(h["invested_on"]) if h["invested_on"] else None,
            "invest_mode": h["invest_mode"],
            "invest_amount": (float(h["invest_amount"])
                              if h["invest_amount"] is not None else None),
            "plan": h["plan_type"],
        } for h in held],
    }


@router.get("/access-log")
def access_log(request: Request, limit: int = 100):
    """Who has looked at what. Visible to admins, including themselves --
    a log nobody can read is a log nobody is kept honest by."""
    _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        if not _q(cur, "SELECT to_regclass('admin_access_log') IS NOT NULL AS ok",
                  one=True)["ok"]:
            return {"enabled": False, "rows": []}
        rows = _q(cur, """
            SELECT l.at, l.action, l.portfolio_id, l.detail,
                   a.email AS admin_email, o.email AS owner_email
            FROM admin_access_log l
            LEFT JOIN mf_user a ON a.user_id = l.admin_user_id
            LEFT JOIN mf_user o ON o.user_id = l.owner_user_id
            ORDER BY l.at DESC LIMIT %(n)s
        """, {"n": max(1, min(limit, 500))})
    return {"enabled": True, "rows": [{
        "at": str(r["at"])[:19],
        "admin": r["admin_email"],
        "action": r["action"],
        "portfolio_id": r["portfolio_id"],
        "owner": r["owner_email"],
        "detail": r["detail"],
    } for r in rows]}
