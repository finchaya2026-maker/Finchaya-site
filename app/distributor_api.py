"""
distributor_api.py -- clients under a distributor's ARN.
---------------------------------------------------------
Mounted in api.py:

    from distributor_api import router as distributor_router
    app.include_router(distributor_router)

WHAT THIS IS NOT
    It does not check that an ARN is valid or that anyone is entitled to
    use it. The ARN is a label printed on reports, stored because retyping
    it into every report is worse. Nothing here grants a permission.

OWNERSHIP
    Every read and write goes through _owned_client(), which filters on
    owner_user_id in the SQL itself. There is no path that trusts a
    client_id from the URL -- these rows name real people and their
    financial position, so a leak here is a different order of problem
    from a leak of fund data.
"""

from collections import defaultdict
from datetime import date
from typing import List, Optional

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

# TREND_BATCH and _trend_summary are reused rather than reimplemented.
# TREND_BATCH exists because per-fund trend calls were "fifty round
# trips for a category listing"; a second copy here would be a second
# thing to keep in step with the stock scores.
from portfolio_api import (DB, _q, _require_subscription, _scores_on,
                           TREND_BATCH, _trend_summary)
from saved_portfolio_api import _user_id

router = APIRouter(prefix="/api/distributor", tags=["distributor"])

MAX_CLIENTS = 500

PURPOSES = [
    "Retirement", "Child's education", "Child's marriage", "Buying a house",
    "Wealth creation", "Tax saving", "Emergency fund", "Regular income",
    "Other",
]


def _require_admin(request):
    """The user id, but only if they are an admin. 403 otherwise.

    The allocation rules are GLOBAL -- one set for the whole site -- so an
    ordinary signed-in user being able to write them meant any registered
    account could rewrite the allocations every distributor sees. Nothing
    would have looked broken: the numbers would simply have been somebody
    else's.

    Checked on the SERVER, on every write. Hiding the page stops nobody;
    the endpoint is the thing that has to refuse.
    """
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _q(cur, "SELECT 1 AS ok FROM app_admin WHERE user_id = %(u)s",
                 {"u": user_id}, one=True)
    if not row:
        raise HTTPException(403, "Only an administrator can change the "
                                 "allocation rules.")
    return user_id


@router.get("/me")
def whoami(request: Request):
    """Who is looking, so the navigation can show only what applies.

    Answers for a signed-out visitor too, rather than refusing: the header
    has to render for everyone, and "signed out" is a legitimate answer to
    "who is this". Refusing would leave the menu either empty or showing
    everything, and showing everything is how a retail user ends up on a
    page about managing clients.

    Note this decides only what is DISPLAYED. Every endpoint still checks
    entitlement itself -- hiding a link protects nobody, and a menu is not
    a permission system.
    """
    try:
        user_id = _user_id(request)
    except HTTPException:
        return {"signed_in": False, "admin": False, "distributor": False}

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        admin = _q(cur, "SELECT 1 AS ok FROM app_admin WHERE user_id = %(u)s",
                   {"u": user_id}, one=True)
        # A distributor is someone who has told us they are one, or who has
        # a client on file. Either is a deliberate act; neither happens by
        # wandering onto a page.
        dist = _q(cur, """
            SELECT 1 AS ok WHERE EXISTS (
                SELECT 1 FROM distributor WHERE user_id = %(u)s
                  AND (arn IS NOT NULL OR display_name IS NOT NULL))
               OR EXISTS (SELECT 1 FROM client WHERE owner_user_id = %(u)s)
        """, {"u": user_id}, one=True)

    return {"signed_in": True, "admin": bool(admin),
            "distributor": bool(dist) or bool(admin)}


def _owned_client(cur, client_id: int, user_id):
    """A client row, or 404 if it is not this user's.

    404 rather than 403 on someone else's client: 403 confirms the id is
    real, which tells anyone enumerating ids how many clients exist and
    which numbers are live.
    """
    row = _q(cur, """
        SELECT * FROM client
        WHERE client_id = %(c)s AND owner_user_id = %(u)s
    """, {"c": client_id, "u": user_id}, one=True)
    if not row:
        raise HTTPException(404, "No such client.")
    return row


def _shape(row, portfolios=0):
    """One client, as the page sees it.

    Age is COMPUTED from the birth year every time rather than stored. A
    stored age is wrong within a year and goes on being wrong quietly,
    which matters when the number is used to talk about time horizons.
    """
    birth = row.get("birth_year")
    return {
        "client_id": row["client_id"],
        "name": row["name"],
        "birth_year": birth,
        "age": (date.today().year - birth) if birth else None,
        "purpose": row.get("purpose"),
        "goal_note": row.get("goal_note"),
        "consent_on": str(row["consent_on"]) if row.get("consent_on") else None,
        "archived": row.get("archived", False),
        "portfolios": portfolios,
        "created_at": str(row["created_at"]),
    }


# ---------------------------------------------------------------------
# The distributor's own details
# ---------------------------------------------------------------------
class ProfileIn(BaseModel):
    display_name: Optional[str] = None
    arn: Optional[str] = None
    contact: Optional[str] = None


@router.get("/profile")
def get_profile(request: Request):
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _q(cur, "SELECT * FROM distributor WHERE user_id = %(u)s",
                 {"u": user_id}, one=True)
    return {"display_name": (row or {}).get("display_name"),
            "arn": (row or {}).get("arn"),
            "contact": (row or {}).get("contact")}


@router.put("/profile")
def put_profile(body: ProfileIn, request: Request):
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _q(cur, """
            INSERT INTO distributor (user_id, display_name, arn, contact)
            VALUES (%(u)s, %(n)s, %(a)s, %(c)s)
            ON CONFLICT (user_id) DO UPDATE
               -- COALESCE so saving one field does not blank the other two.
               -- Same shape as the amount fix in add_funds: a new value can
               -- fill a blank, never blank a filled one.
               SET display_name = COALESCE(EXCLUDED.display_name,
                                           distributor.display_name),
                   arn          = COALESCE(EXCLUDED.arn, distributor.arn),
                   contact      = COALESCE(EXCLUDED.contact, distributor.contact),
                   updated_at   = now()
            RETURNING *
        """, {"u": user_id, "n": body.display_name,
              "a": body.arn, "c": body.contact}, one=True)
        conn.commit()
    return {"display_name": row["display_name"], "arn": row["arn"],
            "contact": row["contact"]}


# ---------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------
class ClientIn(BaseModel):
    name: str
    birth_year: Optional[int] = None
    purpose: Optional[str] = None
    goal_note: Optional[str] = None
    consent_on: Optional[str] = None


@router.get("/purposes")
def purposes():
    """The standard list, served rather than hardcoded in the page, so the
    page and any future report agree on the wording."""
    return PURPOSES


@router.get("/clients")
def list_clients(request: Request, include_archived: bool = False):
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, """
            SELECT c.*, COUNT(p.portfolio_id) AS portfolios
            FROM client c
            LEFT JOIN portfolio p ON p.client_id = c.client_id
            WHERE c.owner_user_id = %(u)s
              AND (%(all)s OR NOT c.archived)
            GROUP BY c.client_id
            ORDER BY c.name
        """, {"u": user_id, "all": include_archived})
    return [_shape(r, r["portfolios"]) for r in rows]


@router.post("/clients")
def create_client(body: ClientIn, request: Request):
    user_id = _user_id(request)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "The client needs a name.")
    if len(name) > 120:
        raise HTTPException(400, "That name is too long.")
    if body.birth_year is not None and not (1900 <= body.birth_year <= date.today().year):
        raise HTTPException(400, "That year of birth does not look right.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        n = _q(cur, """SELECT COUNT(*) AS n FROM client
                       WHERE owner_user_id = %(u)s AND NOT archived""",
               {"u": user_id}, one=True)["n"]
        if n >= MAX_CLIENTS:
            raise HTTPException(400, f"That is more than {MAX_CLIENTS} clients.")

        # A duplicate name is a warning, not an error -- two clients really
        # can be called the same thing, and refusing would be wrong. The
        # page is told so it can ask rather than the API deciding.
        dupe = _q(cur, """SELECT client_id FROM client
                          WHERE owner_user_id = %(u)s AND lower(name) = lower(%(n)s)
                            AND NOT archived LIMIT 1""",
                  {"u": user_id, "n": name}, one=True)

        row = _q(cur, """
            INSERT INTO client (owner_user_id, name, birth_year, purpose,
                                goal_note, consent_on)
            VALUES (%(u)s, %(n)s, %(b)s, %(p)s, %(g)s, %(c)s)
            RETURNING *
        """, {"u": user_id, "n": name, "b": body.birth_year,
              "p": body.purpose, "g": body.goal_note,
              "c": body.consent_on}, one=True)
        conn.commit()

    out = _shape(row)
    out["duplicate_name"] = bool(dupe)
    return out


@router.get("/clients/{client_id}")
def get_client(client_id: int, request: Request):
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _owned_client(cur, client_id, user_id)
        ports = _q(cur, """
            SELECT p.portfolio_id, p.name, p.updated_at,
                   COUNT(h.scheme_code) AS fund_count,
                   g.target_amount, g.target_date
            FROM portfolio p
            LEFT JOIN portfolio_holding h USING (portfolio_id)
            LEFT JOIN portfolio_goal    g USING (portfolio_id)
            WHERE p.client_id = %(c)s
            GROUP BY p.portfolio_id, g.target_amount, g.target_date
            ORDER BY p.updated_at DESC
        """, {"c": client_id})

    out = _shape(row, len(ports))
    out["portfolio_list"] = [{
        "portfolio_id": p["portfolio_id"], "name": p["name"],
        "fund_count": p["fund_count"],
        "target_amount": float(p["target_amount"]) if p["target_amount"] else None,
        "target_date": str(p["target_date"]) if p["target_date"] else None,
        "updated_at": str(p["updated_at"]),
    } for p in ports]
    return out


@router.put("/clients/{client_id}")
def update_client(client_id: int, body: ClientIn, request: Request):
    user_id = _user_id(request)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "The client needs a name.")
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned_client(cur, client_id, user_id)
        row = _q(cur, """
            UPDATE client
               SET name = %(n)s, birth_year = %(b)s, purpose = %(p)s,
                   goal_note = %(g)s, consent_on = %(c)s, updated_at = now()
             WHERE client_id = %(id)s AND owner_user_id = %(u)s
            RETURNING *
        """, {"id": client_id, "u": user_id, "n": name, "b": body.birth_year,
              "p": body.purpose, "g": body.goal_note,
              "c": body.consent_on}, one=True)
        conn.commit()
    return _shape(row)


@router.post("/clients/{client_id}/archive")
def archive_client(client_id: int, request: Request, archived: bool = True):
    """Hide a client without destroying anything.

    The ordinary case for a client who has left is archiving, not deletion:
    their portfolios and history remain, and an MFD may have a record-keeping
    reason to hold them. Deletion is a separate, louder action below.
    """
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned_client(cur, client_id, user_id)
        cur.execute("""UPDATE client SET archived = %(a)s, updated_at = now()
                       WHERE client_id = %(c)s""",
                    {"c": client_id, "a": archived})
        conn.commit()
    return {"client_id": client_id, "archived": archived}


@router.delete("/clients/{client_id}")
def delete_client(client_id: int, request: Request):
    """Delete a client and everything filed under them.

    Real deletion, not a flag: their portfolios, holdings and goals go with
    them. A client who asks to be forgotten should not survive as rows that
    still name them.

    The portfolios are deleted EXPLICITLY rather than left to a foreign key.
    Adding that constraint needs ownership of the portfolio table, which the
    application's database role does not have, so on this deployment it may
    not exist. Relying on a cascade that might be absent would leave a
    deleted client's financial position sitting in orphaned rows -- exactly
    the outcome the deletion was for. Doing it here works either way, and
    the FK becomes a safety net rather than the mechanism.

    Both statements run in one transaction, so a client is never removed
    while their portfolios survive.

    The count of what will go is returned so the page can say it before
    asking, rather than after.
    """
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned_client(cur, client_id, user_id)
        n = _q(cur, """SELECT COUNT(*) AS n FROM portfolio
                       WHERE client_id = %(c)s""",
               {"c": client_id}, one=True)["n"]
        # Portfolios first: holdings and goals hang off portfolio with their
        # own cascades, which DO exist because those tables were created by
        # this role.
        cur.execute("""DELETE FROM portfolio
                       WHERE client_id = %(c)s AND owner_user_id = %(u)s""",
                    {"c": client_id, "u": user_id})
        cur.execute("""DELETE FROM client
                       WHERE client_id = %(c)s AND owner_user_id = %(u)s""",
                    {"c": client_id, "u": user_id})
        conn.commit()
    return {"deleted": client_id, "portfolios_removed": n}


# ---------------------------------------------------------------------
# Allocation rules
# ---------------------------------------------------------------------
HORIZON_BANDS = [("<3", 0, 3), ("3-7", 3, 7), ("7-15", 7, 15), ("15+", 15, 999)]
BUCKET_ORDER = ["large", "mid", "small", "debt", "gold", "international"]


def _band_for(years: float) -> str:
    """The band a horizon falls in. Boundaries are inclusive at the bottom,
    so exactly 7 years is '7-15' rather than '3-7' -- one rule, applied the
    same way everywhere, beats each caller guessing."""
    for band, lo, hi in HORIZON_BANDS:
        if lo <= years < hi:
            return band
    return "15+"


# How many funds sit behind each category, counted CANONICALLY.
#
# v_scheme_category holds one row per scheme code, and a fund exists under
# up to four -- Direct/Regular x Growth/IDCW. Counting those directly says
# "164 large cap funds" when there are about forty, and a selection built
# the same way would offer the same fund four times over. v_fund_canonical
# collapses them, exactly as the screener does.
#
# match_name_like exists for the one category a category label cannot
# express: there is no gold category, so gold funds are found by name
# inside ETFs and FoF Domestic.
# The ::text casts are load-bearing. Without them Postgres sees a bare
# parameter whose only context is "IS NULL", cannot infer a type, and
# rejects the whole statement with AmbiguousParameter -- at request time,
# not at deploy time, so it looks like a runtime fault rather than a typo.
CATEGORY_FUNDS = """
SELECT count(*) AS n
FROM v_fund_canonical c
JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
WHERE vc.category = ANY(%(cats)s)
  AND (%(like)s::text IS NULL OR c.scheme_name ILIKE %(like)s::text)
"""


def _fund_categories(cur, purpose, band, risk):
    """What to BUY for this profile: category, weight, and whether any
    fund actually sits in it.

    available is returned per row rather than left to the caller because
    a rule naming a category with no funds behind it fails silently --
    the page draws a 30% bar and the selection comes back empty, with
    nothing saying which of the two is wrong.
    """
    # The tables arrive with create_fund_allocation.py, which is a separate
    # deploy step. Until it has run, this layer is absent rather than
    # broken: an unguarded query here would 500 the endpoint and take the
    # working exposure page down with it, turning "a new feature is not set
    # up yet" into "allocations are down".
    #
    # Called LAST in the request on purpose -- a failed statement aborts
    # the transaction, so anything after it would fail too.
    try:
        rows = _q(cur, """
            SELECT r.category_code, r.target_pct, r.min_pct, r.max_pct,
                   r.rationale, r.seeded,
                   c.label, c.asset_class, c.match_categories,
                   c.match_name_like, c.sort_order, c.active
            FROM allocation_fund_rule r
            JOIN allocation_category c ON c.code = r.category_code
            WHERE r.purpose = %(p)s AND r.horizon_band = %(h)s
              AND r.risk_band = %(r)s
            ORDER BY c.sort_order
        """, {"p": purpose, "h": band, "r": risk})
    except psycopg.errors.UndefinedTable:
        return []

    out = []
    for r in rows:
        n = _q(cur, CATEGORY_FUNDS,
               {"cats": r["match_categories"], "like": r["match_name_like"]},
               one=True)["n"]
        out.append({
            "code": r["category_code"],
            "label": r["label"],
            "asset_class": r["asset_class"],
            "target_pct": float(r["target_pct"]),
            "min_pct": float(r["min_pct"]) if r["min_pct"] is not None else None,
            "max_pct": float(r["max_pct"]) if r["max_pct"] is not None else None,
            "rationale": r["rationale"],
            "seeded": r["seeded"],
            "available": n,
            # A retired category still on a rule is not the same as one
            # that never existed, and the page should be able to say so.
            "retired": not r["active"],
        })
    return out


@router.get("/allocation")
def allocation(purpose: str, years: float, request: Request,
               risk: str = "moderate"):
    """What to buy for a profile, and where that should end up.

    TWO LAYERS, and they answer different questions.

    fund_categories -- WHAT TO BUY, and the one the recommendation is
        built from. Large Cap Fund, Mid Cap Fund, Short Duration Fund and
        so on: the vocabulary a distributor actually selects in. Three or
        four per profile, never a slice too thin to be worth a fund.

    buckets -- WHERE THE MONEY ENDS UP, measured through to the stocks:
        large / mid / small / debt / gold / international. This is what
        the look-through can verify, so it is the layer that can say a
        portfolio did not land where the plan intended.

    They are not checks on each other, and neither overrides the other.
    Funds picked from the categories may land on the exposure targets or
    may not: two flexi caps can hold the same mid caps between them,
    which no category label can show and the look-through can. Showing
    both, and saying which is which, is the point.

    Signed in but not admin-only: any distributor needs to read the rules
    to use them. Only writing is restricted.
    """
    _user_id(request)
    if purpose not in ("appreciation", "preservation", "income"):
        raise HTTPException(400, "Unknown purpose.")
    if risk not in ("conservative", "moderate", "aggressive"):
        raise HTTPException(400, "Unknown risk band.")
    band = _band_for(max(0.0, float(years)))

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, """
            SELECT bucket, target_pct, min_pct, max_pct, rationale, seeded
            FROM allocation_rule
            WHERE purpose = %(p)s AND horizon_band = %(h)s AND risk_band = %(r)s
        """, {"p": purpose, "h": band, "r": risk})
        tilts = _active_tilts(cur)
        view = _active_view(cur)
        fund_cats = _fund_categories(cur, purpose, band, risk)

    if not rows:
        raise HTTPException(404, "No rule for that profile yet.")

    by_bucket = {r["bucket"]: r for r in rows}
    total = sum(float(r["target_pct"]) for r in rows)

    base = [{
        "bucket": b,
        "target_pct": float(by_bucket[b]["target_pct"]),
        "min_pct": float(by_bucket[b]["min_pct"]) if by_bucket[b]["min_pct"] is not None else None,
        "max_pct": float(by_bucket[b]["max_pct"]) if by_bucket[b]["max_pct"] is not None else None,
        "rationale": by_bucket[b]["rationale"],
        "seeded": by_bucket[b]["seeded"],
    } for b in BUCKET_ORDER if b in by_bucket]

    tilted, blocked = _apply_tilts(base, tilts)
    equity = sum(x["final_pct"] for x in tilted
                 if x["bucket"] in ("large", "mid", "small"))
    fund_total = sum(c["target_pct"] for c in fund_cats)

    return {
        "purpose": purpose, "risk_band": risk,
        "years": years, "horizon_band": band,
        # base, tilt and final are all returned so every screen and report
        # can show the working. A single number would hide whether an
        # allocation came from the rule or from an opinion.
        "view": {"view_id": view["view_id"], "label": view["label"],
                 "rationale": view["rationale"],
                 "effective_from": str(view["effective_from"]) if view["effective_from"] else None,
                 "review_by": str(view["review_by"]) if view["review_by"] else None
                 } if view else None,
        # What a floor or ceiling refused to let the view do. Reported, not
        # swallowed: a tilt that silently does nothing leaves you believing
        # you are positioned somewhere you are not.
        "blocked": blocked,
        # Ordered by the scale, never by size: two profiles put side by
        # side should line up row for row.
        "buckets": tilted,
        # WHAT TO BUY. Read before "buckets" by anything rendering this.
        #
        # "buckets" says where the money should END UP, measured through to
        # the stocks. Nobody can buy 40% mid cap; they buy a fund. These
        # rows name the fund categories to select from, and the
        # recommendation is built from them.
        #
        # The two layers are not checks on each other. Funds chosen here
        # may land on the exposure targets or may not -- two flexi caps
        # can quietly hold the same mid caps between them, which the
        # category alone cannot show and the look-through can. Both are
        # worth seeing; neither is a verdict on the other.
        "fund_categories": fund_cats,
        # NOT tilted. The market view moves "buckets" only. Until there is
        # a mapping from a bucket tilt to a fund category, an active view
        # leans the exposure picture and leaves the prescription alone --
        # said here rather than left for someone to discover.
        "fund_categories_tilted": False,
        "fund_total_pct": round(fund_total, 1),
        "fund_complete": abs(fund_total - 100) < 0.01 if fund_cats else False,
        "equity_pct": round(equity, 1),
        # Surfaced rather than assumed: a profile that does not sum to 100
        # is a broken rule, and the page should say so instead of drawing
        # a bar that silently falls short.
        "total_pct": round(total, 1),
        "complete": abs(total - 100) < 0.01,
        # True while every row is still the shipped default, so the page
        # can say these have not been reviewed yet.
        "all_seeded": all(r["seeded"] for r in rows),
    }


# ---------------------------------------------------------------------
# Market view -- the tactical layer
# ---------------------------------------------------------------------
# A tilt can lean the allocation; it must not take it over. Fifteen points
# across the whole view is the ceiling: enough to express a strong opinion,
# not enough to turn an aggressive 25-year plan into a cash position or a
# three-year goal into an equity bet.
MAX_TOTAL_TILT = 15.0


def _active_tilts(cur):
    """The tilts in force, as {bucket: points}. Empty when no view is
    active -- which is the neutral state, not a missing one."""
    rows = _q(cur, """
        SELECT t.bucket, t.tilt_pct
        FROM market_view v JOIN market_view_tilt t USING (view_id)
        WHERE v.status = 'active'
          AND (v.effective_from IS NULL OR v.effective_from <= CURRENT_DATE)
    """)
    return {r["bucket"]: float(r["tilt_pct"]) for r in rows}


def _active_view(cur):
    return _q(cur, """
        SELECT view_id, label, rationale, effective_from, review_by
        FROM market_view
        WHERE status = 'active'
          AND (effective_from IS NULL OR effective_from <= CURRENT_DATE)
        LIMIT 1
    """, one=True)


def _apply_tilts(buckets, tilts):
    """base + tilt, clamped to the rule's own floor and ceiling.

    THE CLAMP IS THE POINT. A floor set for a reason -- never less than
    half in debt for a three-year goal -- must survive a macro opinion.
    Someone's house deposit in eighteen months should not move because of
    a view on mid caps, and the clamp makes that automatic rather than
    something to remember.

    What the clamp refuses is reported rather than swallowed, because a
    tilt that silently does nothing is worse than one that is refused: it
    leaves you believing you are positioned somewhere you are not.

    Whatever the clamp holds back is redistributed across the buckets that
    still have room, in proportion to their targets, so the result still
    sums to 100.
    """
    out, blocked = [], []
    for b in buckets:
        tilt = tilts.get(b["bucket"], 0.0)
        raw = b["target_pct"] + tilt
        lo = b["min_pct"] if b["min_pct"] is not None else 0.0
        hi = b["max_pct"] if b["max_pct"] is not None else 100.0
        final = max(lo, min(hi, raw))
        if tilt and abs(final - raw) > 0.005:
            blocked.append({"bucket": b["bucket"], "wanted": round(raw, 1),
                            "allowed": round(final, 1),
                            "limit": "ceiling" if raw > hi else "floor"})
        out.append(dict(b, tilt_pct=tilt, final_pct=round(final, 1)))

    # Clamping breaks the sum. Push the remainder onto whatever still has
    # headroom, weighted by target, rather than leaving a portfolio that
    # adds to 97.
    for _ in range(6):
        total = sum(o["final_pct"] for o in out)
        gap = round(100 - total, 2)
        if abs(gap) < 0.05:
            break
        room = [o for o in out
                if (gap > 0 and o["final_pct"] < (o["max_pct"] if o["max_pct"] is not None else 100))
                or (gap < 0 and o["final_pct"] > (o["min_pct"] if o["min_pct"] is not None else 0))]
        if not room:
            break
        weight = sum(max(o["target_pct"], 0.1) for o in room)
        for o in room:
            share = gap * max(o["target_pct"], 0.1) / weight
            lo = o["min_pct"] if o["min_pct"] is not None else 0.0
            hi = o["max_pct"] if o["max_pct"] is not None else 100.0
            o["final_pct"] = round(max(lo, min(hi, o["final_pct"] + share)), 1)

    return out, blocked


class ViewIn(BaseModel):
    label: str
    rationale: Optional[str] = None
    effective_from: Optional[str] = None
    review_by: Optional[str] = None
    tilts: dict = {}          # {"mid": 8, "large": -8}


@router.get("/views")
def list_views(request: Request):
    """Every view, current and past. The retired ones are the record of
    what was thought and when, which is why they are never deleted."""
    _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, """
            SELECT v.*, COALESCE(json_agg(json_build_object(
                       'bucket', t.bucket, 'tilt_pct', t.tilt_pct)
                   ) FILTER (WHERE t.bucket IS NOT NULL), '[]') AS tilts
            FROM market_view v
            LEFT JOIN market_view_tilt t USING (view_id)
            GROUP BY v.view_id
            ORDER BY v.status = 'active' DESC, v.created_at DESC
        """)
    return [{
        "view_id": r["view_id"], "label": r["label"],
        "rationale": r["rationale"], "status": r["status"],
        "effective_from": str(r["effective_from"]) if r["effective_from"] else None,
        "review_by": str(r["review_by"]) if r["review_by"] else None,
        "retired_at": str(r["retired_at"]) if r["retired_at"] else None,
        "tilts": {t["bucket"]: float(t["tilt_pct"]) for t in r["tilts"]},
        # Surfaced so the page can prompt rather than let a view quietly
        # become permanent.
        "overdue": bool(r["review_by"] and r["status"] == "active"
                        and str(r["review_by"]) < str(date.today())),
    } for r in rows]


@router.post("/views")
def create_view(body: ViewIn, request: Request):
    """Save a view as a DRAFT. Drafts affect nothing until activated."""
    user_id = _require_admin(request)
    label = (body.label or "").strip()
    if not label:
        raise HTTPException(400, "Give the view a label.")
    bad = [b for b in body.tilts if b not in BUCKET_ORDER]
    if bad:
        raise HTTPException(400, "Unknown bucket: %s" % ", ".join(bad))

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        v = _q(cur, """
            INSERT INTO market_view (label, rationale, effective_from,
                                     review_by, author_user_id)
            VALUES (%(l)s, %(r)s, %(e)s, %(rv)s, %(u)s)
            RETURNING view_id
        """, {"l": label, "r": body.rationale, "e": body.effective_from,
              "rv": body.review_by, "u": user_id}, one=True)
        for bucket, pts in body.tilts.items():
            if not pts:
                continue
            cur.execute("""INSERT INTO market_view_tilt (view_id, bucket, tilt_pct)
                           VALUES (%(v)s, %(b)s, %(p)s)
                           ON CONFLICT (view_id, bucket) DO UPDATE
                              SET tilt_pct = EXCLUDED.tilt_pct""",
                        {"v": v["view_id"], "b": bucket, "p": pts})
        conn.commit()
    return {"view_id": v["view_id"], "status": "draft"}


@router.post("/views/{view_id}/activate")
def activate_view(view_id: int, request: Request):
    """Put a view into force, retiring whichever was in force before.

    THE TWO CHECKS HAPPEN HERE, not when the draft is saved: a set under
    construction is legitimately unbalanced, and refusing to save it would
    make it impossible to write.

      sum to zero -- "more mid cap" is not a view. "More mid cap, funded
      out of large cap" is. This forces the second half to be stated.

      total within MAX_TOTAL_TILT -- a tilt may lean the allocation, not
      replace it.
    """
    _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, "SELECT bucket, tilt_pct FROM market_view_tilt "
                       "WHERE view_id = %(v)s", {"v": view_id})
        if not rows:
            raise HTTPException(400, "That view has no tilts in it.")

        net = sum(float(r["tilt_pct"]) for r in rows)
        if abs(net) > 0.01:
            raise HTTPException(400,
                "The tilts add to %+.1f, not zero. Say where the %s is coming "
                "from: every point added to one bucket has to be taken from "
                "another." % (net, "extra" if net > 0 else "shortfall"))

        gross = sum(abs(float(r["tilt_pct"])) for r in rows) / 2
        if gross > MAX_TOTAL_TILT:
            raise HTTPException(400,
                "That moves %.0f points; the limit is %.0f. A view is meant to "
                "lean the allocation, not replace it."
                % (gross, MAX_TOTAL_TILT))

        cur.execute("""UPDATE market_view
                          SET status = 'retired', retired_at = CURRENT_DATE,
                              updated_at = now()
                        WHERE status = 'active'""")
        cur.execute("""UPDATE market_view
                          SET status = 'active',
                              effective_from = COALESCE(effective_from, CURRENT_DATE),
                              updated_at = now()
                        WHERE view_id = %(v)s""", {"v": view_id})
        conn.commit()
    return {"view_id": view_id, "status": "active"}


@router.post("/views/{view_id}/retire")
def retire_view(view_id: int, request: Request):
    """Stand a view down. Nothing is deleted -- with no active view the
    allocation returns to the base rules, which is neutral, not absent."""
    _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("""UPDATE market_view
                          SET status = 'retired', retired_at = CURRENT_DATE,
                              updated_at = now()
                        WHERE view_id = %(v)s""", {"v": view_id})
        conn.commit()
    return {"view_id": view_id, "status": "retired"}


@router.delete("/views/{view_id}")
def delete_view(view_id: int, request: Request):
    """Only a draft can be deleted. A view that was ever in force is the
    record of what was thought at the time, and deleting it destroys the
    only evidence of whether the call was any good."""
    _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _q(cur, "SELECT status FROM market_view WHERE view_id = %(v)s",
                 {"v": view_id}, one=True)
        if not row:
            raise HTTPException(404, "No such view.")
        if row["status"] != "draft":
            raise HTTPException(400, "Only a draft can be deleted. Retire it "
                                     "instead -- the record is the point.")
        cur.execute("DELETE FROM market_view WHERE view_id = %(v)s",
                    {"v": view_id})
        conn.commit()
    return {"deleted": view_id}


class RuleIn(BaseModel):
    target_pct: float
    min_pct: Optional[float] = None
    max_pct: Optional[float] = None
    rationale: Optional[str] = None


@router.get("/rules")
def list_rules(purpose: str, horizon_band: str, risk_band: str,
               request: Request):
    """The editable rows behind one profile.

    Admin-only, because this is the editor's own data source: showing
    someone the form when the save will be refused wastes their time and
    teaches them the controls are decorative.
    """
    _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, """
            SELECT rule_id, bucket, target_pct, min_pct, max_pct,
                   rationale, seeded
            FROM allocation_rule
            WHERE purpose = %(p)s AND horizon_band = %(h)s AND risk_band = %(r)s
        """, {"p": purpose, "h": horizon_band, "r": risk_band})
    order = {b: i for i, b in enumerate(BUCKET_ORDER)}
    rows.sort(key=lambda r: order.get(r["bucket"], 99))
    return [{"rule_id": r["rule_id"], "bucket": r["bucket"],
             "target_pct": float(r["target_pct"]),
             "min_pct": float(r["min_pct"]) if r["min_pct"] is not None else None,
             "max_pct": float(r["max_pct"]) if r["max_pct"] is not None else None,
             "rationale": r["rationale"], "seeded": r["seeded"]} for r in rows]


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleIn, request: Request):
    """Change one bucket.

    Marks the row as no longer seeded, so re-running the seed script leaves
    it alone. Losing an afternoon of your own judgement to a re-run would
    be unforgivable, and a flag is cheaper than remembering not to.

    The profile's new total is returned rather than enforced: a table is
    briefly wrong while you edit it row by row, and refusing the first
    change because the set does not yet sum to 100 would make editing
    impossible. The page shows the running total instead.
    """
    _require_admin(request)
    if not (0 <= body.target_pct <= 100):
        raise HTTPException(400, "A target has to be between 0 and 100.")
    if (body.min_pct is not None and body.max_pct is not None
            and body.min_pct > body.max_pct):
        raise HTTPException(400, "The floor cannot be above the ceiling.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _q(cur, """
            UPDATE allocation_rule
               SET target_pct = %(t)s, min_pct = %(mn)s, max_pct = %(mx)s,
                   rationale = COALESCE(%(why)s, rationale),
                   seeded = false, updated_at = now()
             WHERE rule_id = %(id)s
            RETURNING purpose, horizon_band, risk_band
        """, {"id": rule_id, "t": body.target_pct, "mn": body.min_pct,
              "mx": body.max_pct, "why": body.rationale}, one=True)
        if not row:
            raise HTTPException(404, "No such rule.")
        total = _q(cur, """
            SELECT SUM(target_pct) AS total FROM allocation_rule
            WHERE purpose = %(p)s AND horizon_band = %(h)s AND risk_band = %(r)s
        """, {"p": row["purpose"], "h": row["horizon_band"],
              "r": row["risk_band"]}, one=True)["total"]
        conn.commit()

    return {"rule_id": rule_id, "total_pct": round(float(total), 1),
            "complete": abs(float(total) - 100) < 0.01}


# ---------------------------------------------------------------------
# The fund-category rules -- what to BUY
#
# Same shape as the bucket rules above, with one difference that drives
# the whole design: the six buckets are FIXED, so editing a profile is
# editing six numbers. Fund categories VARY by profile -- an aggressive
# 15-year plan names small cap, a three-year one does not -- so the editor
# also has to add a category to a profile and take one away.
#
# That is why there is a POST and a DELETE here and none for buckets.
# ---------------------------------------------------------------------
class FundRuleIn(BaseModel):
    target_pct: float
    min_pct: Optional[float] = None
    max_pct: Optional[float] = None
    rationale: Optional[str] = None


class FundRuleNew(BaseModel):
    purpose: str
    horizon_band: str
    risk_band: str
    category_code: str
    target_pct: float = 0


@router.get("/categories")
def list_categories(request: Request):
    """The vocabulary, with how many real funds sit behind each.

    Signed in, not admin. Writing the rules is an administrator's job;
    READING which categories exist is what lets a distributor choose a
    different one from the one the rule prescribed, and a list of
    category names is not a secret.

    The count is canonical -- v_fund_canonical, not v_scheme_category --
    because a fund exists under up to four scheme codes and an editor
    offered "Large Cap Fund (164)" would be reading a plan count, not a
    fund count.
    """
    _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        try:
            cats = _q(cur, """
                SELECT code, label, asset_class, match_categories,
                       match_name_like, sort_order, active
                FROM allocation_category ORDER BY sort_order
            """)
        except psycopg.errors.UndefinedTable:
            raise HTTPException(
                503, "The fund-category tables are not set up yet. Run "
                     "create_fund_allocation.py on the server.")
        out = []
        for c in cats:
            n = _q(cur, CATEGORY_FUNDS,
                   {"cats": c["match_categories"], "like": c["match_name_like"]},
                   one=True)["n"]
            out.append({"code": c["code"], "label": c["label"],
                        "asset_class": c["asset_class"],
                        "available": n, "active": c["active"]})
    return out


@router.get("/fund-rules")
def list_fund_rules(purpose: str, horizon_band: str, risk_band: str,
                    request: Request):
    """The editable rows behind one profile's prescription."""
    _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        try:
            rows = _q(cur, """
                SELECT r.rule_id, r.category_code, r.target_pct, r.min_pct,
                       r.max_pct, r.rationale, r.seeded,
                       c.label, c.asset_class, c.match_categories,
                       c.match_name_like, c.active
                FROM allocation_fund_rule r
                JOIN allocation_category c ON c.code = r.category_code
                WHERE r.purpose = %(p)s AND r.horizon_band = %(h)s
                  AND r.risk_band = %(r)s
                ORDER BY c.sort_order
            """, {"p": purpose, "h": horizon_band, "r": risk_band})
        except psycopg.errors.UndefinedTable:
            raise HTTPException(
                503, "The fund-category tables are not set up yet. Run "
                     "create_fund_allocation.py on the server.")

        out = []
        for r in rows:
            n = _q(cur, CATEGORY_FUNDS,
                   {"cats": r["match_categories"], "like": r["match_name_like"]},
                   one=True)["n"]
            out.append({
                "rule_id": r["rule_id"], "category_code": r["category_code"],
                "label": r["label"], "asset_class": r["asset_class"],
                "target_pct": float(r["target_pct"]),
                "min_pct": float(r["min_pct"]) if r["min_pct"] is not None else None,
                "max_pct": float(r["max_pct"]) if r["max_pct"] is not None else None,
                "rationale": r["rationale"], "seeded": r["seeded"],
                "available": n, "retired": not r["active"],
            })
    return out


@router.put("/fund-rules/{rule_id}")
def update_fund_rule(rule_id: int, body: FundRuleIn, request: Request):
    """Change one category's weight.

    Marks the row as no longer seeded, so re-running create_fund_allocation
    leaves it alone. The total is returned rather than enforced: a profile
    is briefly wrong while you edit it row by row, and refusing the first
    change because the set does not yet sum to 100 would make editing
    impossible. The page shows the running total instead.
    """
    _require_admin(request)
    if not (0 <= body.target_pct <= 100):
        raise HTTPException(400, "A target has to be between 0 and 100.")
    if (body.min_pct is not None and body.max_pct is not None
            and body.min_pct > body.max_pct):
        raise HTTPException(400, "The floor cannot be above the ceiling.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _q(cur, """
            UPDATE allocation_fund_rule
               SET target_pct = %(t)s, min_pct = %(mn)s, max_pct = %(mx)s,
                   rationale = COALESCE(%(why)s, rationale),
                   seeded = false, updated_at = now()
             WHERE rule_id = %(id)s
            RETURNING purpose, horizon_band, risk_band
        """, {"id": rule_id, "t": body.target_pct, "mn": body.min_pct,
              "mx": body.max_pct, "why": body.rationale}, one=True)
        if not row:
            raise HTTPException(404, "No such rule.")
        total = _fund_total(cur, row)
        conn.commit()

    return {"rule_id": rule_id, "total_pct": total,
            "complete": abs(total - 100) < 0.01}


@router.post("/fund-rules")
def add_fund_rule(body: FundRuleNew, request: Request):
    """Add a category to a profile, at 0% until you give it a weight.

    Starting at zero rather than guessing a number: an added row that
    immediately changes the allocation would edit the profile as a side
    effect of opening a dropdown.
    """
    _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        cat = _q(cur, "SELECT code, active FROM allocation_category "
                      "WHERE code = %(c)s", {"c": body.category_code}, one=True)
        if not cat:
            raise HTTPException(400, "No such category.")
        if not cat["active"]:
            raise HTTPException(400, "That category has been retired.")

        row = _q(cur, """
            INSERT INTO allocation_fund_rule
                (purpose, horizon_band, risk_band, category_code,
                 target_pct, seeded)
            VALUES (%(p)s, %(h)s, %(r)s, %(c)s, %(t)s, false)
            ON CONFLICT (purpose, horizon_band, risk_band, category_code)
            DO NOTHING
            RETURNING rule_id
        """, {"p": body.purpose, "h": body.horizon_band, "r": body.risk_band,
              "c": body.category_code, "t": body.target_pct}, one=True)
        if not row:
            raise HTTPException(409, "That category is already in this profile.")
        total = _fund_total(cur, {"purpose": body.purpose,
                                  "horizon_band": body.horizon_band,
                                  "risk_band": body.risk_band})
        conn.commit()
    return {"rule_id": row["rule_id"], "total_pct": total}


@router.delete("/fund-rules/{rule_id}")
def delete_fund_rule(rule_id: int, request: Request):
    """Take a category out of a profile.

    A real delete, not a zero. A category left at 0% still prints on the
    page and still reads as a considered decision to hold none of it,
    which is a different statement from not prescribing it at all.
    """
    _require_admin(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _q(cur, """
            DELETE FROM allocation_fund_rule WHERE rule_id = %(id)s
            RETURNING purpose, horizon_band, risk_band
        """, {"id": rule_id}, one=True)
        if not row:
            raise HTTPException(404, "No such rule.")
        total = _fund_total(cur, row)
        conn.commit()
    return {"deleted": rule_id, "total_pct": total}


def _fund_total(cur, profile):
    """What the profile adds up to now. Zero when nothing is left."""
    t = _q(cur, """
        SELECT COALESCE(SUM(target_pct), 0) AS total
        FROM allocation_fund_rule
        WHERE purpose = %(p)s AND horizon_band = %(h)s AND risk_band = %(r)s
    """, {"p": profile["purpose"], "h": profile["horizon_band"],
          "r": profile["risk_band"]}, one=True)["total"]
    return round(float(t), 1)


# ---------------------------------------------------------------------
# Which funds to consider for a category
#
# WHAT THIS DOES NOT DO, DELIBERATELY
#     It does not return a recommendation, and it does not pick. It
#     orders a category's funds by ONE stated measure and says how many
#     it considered, so "8 of 41" reads as a slice of a known universe
#     rather than as a shortlist somebody assembled.
#
# WHY EXCESS OVER BENCHMARK IS THE ORDER
#     Because it is the measure that survives the market moving. A fund
#     up 24% in a year when its index rose 22% has told you almost
#     nothing; the excess says so. Ranking on raw return would put every
#     fund in a good sector at the top regardless of the manager.
#
#     It is still a PAST measure and orders nothing about the future.
#     The page says so, and the other columns -- the rising split, the
#     category rank, the 1Y and 3Y figures -- are there so the order is
#     an opening, not a verdict.
#
# WHY IT RETURNS CODES AND LITTLE ELSE
#     /api/portfolio/fund-cards already assembles returns by period,
#     benchmark excess, category rank and the rising split for up to 20
#     funds, gated on subscription. Rebuilding any of that here would be
#     a second implementation to keep in step with the first.
# ---------------------------------------------------------------------
# Ranked on the RISING SPLIT first, then on return.
#
# WHY RISING LEADS
#     It describes what the fund owns TODAY. A three-year return
#     describes a book the manager may have sold out of, and for a fund
#     launched last year it does not exist at all. Leading on the record
#     quietly said "new funds do not count" -- the same failure as
#     dropping an ISIN because stock_master had not heard of it.
#
#     It is DESCRIPTIVE, not predictive. The trend band says so in as
#     many words: the fund score "showed no forward power, so a tally
#     taken from the same inputs predicts nothing either". So this
#     ordering means "strongest current book", never "will do best", and
#     the page has to say which.
#
# WHY THE PERIOD IS PER FUND
#     3Y where it exists, else 1Y, else since launch -- and the period
#     used is returned with the figure, because 28% over one year and 28%
#     over three are not the same claim and a column that does not say
#     which is lying by omission.
MAX_TREND_UNIVERSE = 150

SUGGEST_UNIVERSE = """
SELECT c.canonical_scheme_code AS scheme_code, c.scheme_name, c.amc_name
FROM v_fund_canonical c
JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
WHERE vc.category = ANY(%(cats)s)
  AND (%(like)s::text IS NULL OR c.scheme_name ILIKE %(like)s::text)
ORDER BY c.scheme_name
"""

# One row per fund: the longest period it actually has.
#
# DISTINCT ON with the ordering below picks 3Y, then 1Y, then SI --
# preference by name, not by whichever row the planner returned first.
SUGGEST_RETURNS = """
WITH latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d FROM mf_returns GROUP BY scheme_code
)
SELECT DISTINCT ON (r.scheme_code)
       r.scheme_code, r.period,
       ROUND(r.fund_cagr, 2)   AS fund_cagr,
       ROUND(r.excess_cagr, 2) AS excess_cagr,
       ROUND(r.years, 2)       AS years,
       r.category_rank, r.category_count
FROM mf_returns r
JOIN latest l ON l.scheme_code = r.scheme_code AND l.d = r.as_of_date
WHERE r.scheme_code = ANY(%(codes)s)
  AND r.period IN ('3Y', '1Y', 'SI')
  AND r.fund_cagr IS NOT NULL
ORDER BY r.scheme_code,
         CASE r.period WHEN '3Y' THEN 1 WHEN '1Y' THEN 2 ELSE 3 END
"""


@router.get("/suggest")
def suggest_funds(category_code: str, request: Request, limit: int = 5,
                  include_etf: bool = False):
    """Funds worth looking at for one category.

    Ordered by how much of each fund's book is in an uptrend today, then
    by return. Subscription-gated: the ordering is derived from the paid
    reading, and a ranked list is the finding even when the numbers
    behind it are withheld.

    The whole route is built on the rising reading, so it is off while
    scores are switched off (MF_SCORES_ENABLED).
    """
    if not _scores_on():
        raise HTTPException(404, "Not available yet.")
    _require_subscription(request)
    if not (1 <= limit <= 200):
        raise HTTPException(400, "Ask for between 1 and 200 funds.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        try:
            cat = _q(cur, """
                SELECT code, label, asset_class, match_categories,
                       match_name_like
                FROM allocation_category WHERE code = %(c)s AND active
            """, {"c": category_code}, one=True)
        except psycopg.errors.UndefinedTable:
            raise HTTPException(503, "The fund-category tables are not set up yet.")
        if not cat:
            raise HTTPException(404, "No such category.")

        universe = _q(cur, SUGGEST_UNIVERSE,
                      {"cats": cat["match_categories"],
                       "like": cat["match_name_like"]})

        # ETFs OUT, unless the category is nothing but ETFs.
        #
        # A distributor cannot place an ETF for a client the way they place
        # a fund -- it needs the client's own demat account and a broker,
        # and it pays no trail. Offering one as the answer to "which mid
        # cap fund" is offering something they cannot act on.
        #
        # The test is the CATEGORY, not the name: where 'ETFs' is one entry
        # among several (momentum sits across Index Funds, ETFs and
        # Thematic) the ETFs are incidental and go. Where a category is
        # deliberately ETFs alone, they are the whole point and stay.
        etf_only = set(cat["match_categories"]) <= {"ETFs",
                                                    "ETFs investing overseas",
                                                    "Debt ETF", "Silver ETF"}
        dropped_etf = 0
        if not include_etf and not etf_only:
            keep = [u for u in universe
                    if "etf" not in (u["scheme_name"] or "").lower()]
            dropped_etf = len(universe) - len(keep)
            universe = keep

        considered = len(universe)
        # A 1,600-fund index category would mean reading every holding of
        # every one of them to sort a list of five. Bounded, and the
        # response says when the bound bit rather than quietly serving a
        # ranking over part of the category.
        truncated = considered > MAX_TREND_UNIVERSE
        universe = universe[:MAX_TREND_UNIVERSE]
        codes = [str(u["scheme_code"]) for u in universe]
        if not codes:
            return {"category": {"code": cat["code"], "label": cat["label"],
                                 "asset_class": cat["asset_class"]},
                    "ranked_on": "nothing -- this category has no funds",
                    "considered": 0, "with_trend": 0, "truncated": False,
                    "funds": []}

        rets = {str(r["scheme_code"]): r
                for r in _q(cur, SUGGEST_RETURNS, {"codes": codes})}

        # One grouped read for the whole category, not one call per fund.
        grouped = defaultdict(list)
        for r in _q(cur, TREND_BATCH, {"codes": codes}):
            grouped[str(r["scheme_code"])].append(r)
        trend = {c: _trend_summary(rs) for c, rs in grouped.items()}

    out = []
    for u in universe:
        code = str(u["scheme_code"])
        t = trend.get(code)
        r = rets.get(code)
        # A SHARE OF THE BOOK, NOT A SUM OF pct_of_nav.
        #
        # up_pct is the raw sum of the disclosed weights that are in
        # uptrends, and those weights do not always total 100: a fund
        # reporting negative net current assets -- borrowed cash, or
        # payables against unsettled trades -- discloses equity adding to
        # more than 100% of NAV. Union Active Momentum came out at
        # "102.3% of what it owns is rising", which is not a sentence.
        #
        # Divided by everything we can see, so the figure means what the
        # page says it means and cannot exceed 100. The fund pages keep
        # the % of NAV reading, where that IS the stated meaning.
        rising = None
        if t:
            seen = (t["up_pct"] + t["down_pct"]
                    + t["sideways_pct"] + t["unscored_pct"])
            rising = round(t["up_pct"] / seen * 100, 1) if seen > 0 else None
        out.append({
            "scheme_code": code,
            "scheme_name": u["scheme_name"],
            "amc_name": u["amc_name"],
            "rising_pct": rising,
            "falling_pct": (round(t["down_pct"]
                            / (t["up_pct"] + t["down_pct"] + t["sideways_pct"]
                               + t["unscored_pct"]) * 100, 1)
                            if t and (t["up_pct"] + t["down_pct"]
                                      + t["sideways_pct"] + t["unscored_pct"]) > 0
                            else None),
            # Kept so a portfolio that discloses more than 100% of NAV is
            # visible rather than silently normalised away.
            "disclosed_pct": (round(t["up_pct"] + t["down_pct"]
                                    + t["sideways_pct"] + t["unscored_pct"], 1)
                              if t else None),
            "period": r["period"] if r else None,
            "years": float(r["years"]) if r and r["years"] is not None else None,
            "fund_cagr": float(r["fund_cagr"]) if r and r["fund_cagr"] is not None else None,
            "excess_cagr": float(r["excess_cagr"]) if r and r["excess_cagr"] is not None else None,
            "category_rank": r["category_rank"] if r else None,
            "category_count": r["category_count"] if r else None,
        })

    out.sort(key=lambda f: (f["rising_pct"] is None,
                            -(f["rising_pct"] or 0),
                            -(f["fund_cagr"] or -999)))

    return {
        "category": {"code": cat["code"], "label": cat["label"],
                     "asset_class": cat["asset_class"]},
        "ranked_on": "how much of the fund is in an uptrend now, then return",
        "considered": considered,
        "with_trend": sum(1 for f in out if f["rising_pct"] is not None),
        "truncated": truncated,
        # Reported, never silent: a category that is mostly ETFs looks
        # thin for a reason, and the reason should be on the page.
        "etfs_excluded": dropped_etf,
        "funds": out[:limit],
    }


class AttachIn(BaseModel):
    portfolio_id: int


@router.post("/clients/{client_id}/portfolios")
def attach_portfolio(client_id: int, body: AttachIn, request: Request):
    """Point an existing portfolio at a client.

    Both sides are checked against the same user, so a portfolio cannot be
    attached to someone else's client or someone else's portfolio to yours.
    """
    user_id = _user_id(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        _owned_client(cur, client_id, user_id)
        owned = _q(cur, """SELECT portfolio_id FROM portfolio
                           WHERE portfolio_id = %(p)s AND owner_user_id = %(u)s""",
                   {"p": body.portfolio_id, "u": user_id}, one=True)
        if not owned:
            raise HTTPException(404, "No such portfolio.")
        cur.execute("""UPDATE portfolio SET client_id = %(c)s, updated_at = now()
                       WHERE portfolio_id = %(p)s""",
                    {"c": client_id, "p": body.portfolio_id})
        conn.commit()
    return {"portfolio_id": body.portfolio_id, "client_id": client_id}
