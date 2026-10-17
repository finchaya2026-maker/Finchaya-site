"""
tradeplan_api.py -- "My trade plan".
---------------------------------------------------------------------------
Mounted with the one block enable_tradeplan.py appends to the live api.py, or by
api.py itself (_mount_optional("tradeplan_api", ...) plus its /tradeplan page route):

    GET  /tradeplan                          the page
    GET  /api/tradeplan/access               may I use it? (never an error: the page asks this first)
    GET  /api/tradeplan/catalog              instruments, time frames, indicators and their tests
    GET  /api/tradeplan/plans                my saved plans
    POST /api/tradeplan/plans                save a plan (new, or {plan_id, ...} to change one)
    POST /api/tradeplan/plans/{id}/delete    delete one of mine
    POST /api/tradeplan/describe             the plan in words, or why it cannot be used yet (nothing saved)
    POST /api/tradeplan/check                {plan_id | plan, isin, view} -> each condition met / not met / no data
    GET  /api/tradeplan/alerts               my alerts, and whether email and the checker are actually set up
    POST /api/tradeplan/alerts               "email me when this is met" (one condition, or the whole plan)
    POST /api/tradeplan/alerts/{id}/delete   remove one of mine
    POST /api/tradeplan/broker-link          make a broker's terminal address typed by the person safe to link to
    POST /api/tradeplan/requests             ask for an indicator (or for data) we do not have
    GET  /api/tradeplan/requests/mine        what I have asked for
    GET  /api/tradeplan/requests             EVERYONE's requests, counted -- administrators only

WHAT IT DOES
    A trader or investor writes a plan once: an instrument, the time frame they trade on, and a list of
    conditions, each on its own time frame ("hourly MACD above its signal line", "daily RSI above 50") joined
    with AND / OR. They save it. Later they pick a stock, say whether they are bullish or bearish, and this
    reads the latest candles and says which of THEIR conditions are met. It is their own checklist, checked;
    it does not say what to do. The maths is tradeplan_engine.py (pure, tested without a database); this
    file does the parts that need the site: who may use it, saving, reading candles, and the requests.

    It reuses the back-tester's candle reader (backtest_api._frame: the same split/bonus adjustment and bad-tick
    trimming) and its indicator code (backtest_engine._compute), so a number here is the number there. So this
    module needs backtest_engine.py, backtest_api.py, price_adjust.py and indicators.py on the server too --
    enable_tradeplan.py checks they are there.

WHO MAY USE IT
    MF_TRADEPLAN_ACCESS decides, and the DEFAULT IS ADMIN ONLY, exactly as the back-tester and "Check your view"
    are: uploading this changes nothing for anyone but an administrator until it is deliberately opened.
      admin       administrators only (the app_admin table)     <- default
      premium     premium members on a Stocks tier (not lapsed) and administrators
      signed_in   any signed-in account
    Anything else is read as admin: a typo can only ever shut people out. The check is made on every call; a
    hidden link protects nothing.   MF_TRADEPLAN_ENABLED=false is a hard off for everybody, admins too.
    Plans belong to the person who made them: every query is scoped to user_id, and somebody else's plan is a
    404, not a 403 (a 403 would confirm the number exists).

WHAT WE HOLD CANDLES FOR -- AND WHAT WE DO NOT, SAID PLAINLY
    Stocks, on daily, weekly and monthly candles (weekly and monthly are built from the daily ones, as the
    site does everywhere). NOT held today, and so reported as "no data" rather than guessed: any intraday
    candle (5 minutes to 4 hours), and Nifty 50, Bank Nifty, Sensex, natural gas, gold, silver, crude and
    "other" on any time frame (the index series we hold are total-return values with no highs or lows, and
    nothing at all is held for commodities). A plan can still be written and saved for all of them; each
    condition simply reads "no data yet" until candles exist, and the person can ask for them. Adding a source
    means changing two small things: tradeplan_engine.coverage_for (what is held) and intraday_frame /
    _daily_frame here (how to read it). Nothing else knows.

ALERTS
    A person can press "Alert me when this is met" on a condition that is not met. It is stored (trade_alert) exactly as
    evaluated, and tradeplan_alerts.py -- run by cron -- looks again as data arrives and sends ONE email the first time
    it is met. This file never sends anything and never claims to be watching: /alerts reports whether the server has
    email settings and whether the checker has run lately, and the page puts that in front of the person. The alert is
    refused if its condition is already met or can never be read.

BROKER LINKS
    When the plan's conditions are met the page offers to open the person's broker terminal. Outbound link only: no
    order is placed or prefilled, no login is handled, no referral code is added, nothing records which broker was
    opened, and the broker a person names under "Other" is kept in their own browser, not here. The only server
    part is /broker-link, which checks an address typed by the person; the list of brokers is in the catalogue.

REQUESTS
    "Request an indicator" (Ichimoku Cloud, Stochastic, VWAP ...) and "request this data" (hourly candles,
    Nifty 50 ...) go into indicator_request, one row per person per thing. The same request spelled
    differently ("Ichimoku Cloud", "ichimoku-cloud") counts once. A request for something we already have
    ("RSI") is answered with "it is already there" and not stored. An administrator sees them counted by how
    many people asked.

WORDING
    FinChaya is not a SEBI-registered Research Analyst or Investment Adviser. The answer is "met / not met" for
    the person's OWN conditions. Nothing here says buy, sell, good or bad.
"""

import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

import tradeplan_engine as te

router = APIRouter(prefix="/api/tradeplan", tags=["tradeplan"])
page_router = APIRouter()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENABLED = os.getenv("MF_TRADEPLAN_ENABLED", "true").lower() == "true"
ACCESS = os.getenv("MF_TRADEPLAN_ACCESS", "admin").strip().lower()

MAX_PLANS = 20                  # per person
MAX_REQUESTS_PER_DAY = 20       # per person
MAX_REQUEST_DETAILS = 500
STALE_DAYS = 5                  # the newest daily candle older than this gets a note

_WHO = {"admin": "administrators", "premium": "subscribers to the Stocks plan and administrators",
        "signed_in": "signed-in members"}


# =====================================================================
# WHO MAY USE IT
# =====================================================================
# Same short-lived cache and the same fail-CLOSED stance as backtest_api / viewcheck_api: an error
# leaves the list empty, which can only ever mean fewer people get in.
_ADMIN_TTL = 30
_admin_cache = {"ids": frozenset(), "at": None}


def _admin_ids():
    now = time.monotonic()
    at = _admin_cache["at"]
    if at is None or now - at > _ADMIN_TTL:
        try:
            from api import query
            ids = frozenset(r["user_id"] for r in query("SELECT user_id FROM app_admin"))
        except Exception:                                    # noqa: BLE001
            ids = frozenset()
        _admin_cache.update(ids=ids, at=now)
    return _admin_cache["ids"]


def _premium_now(user) -> bool:
    """is_premium, not lapsed, and on a tier that covers the STOCK tools ("stocks" or "all"; no scope on
    record means everything) -- WITHOUT the paywall-off shortcut."""
    if not user or not user.get("is_premium"):
        return False
    if (user.get("premium_scope") or "all") not in ("stocks", "all"):
        return False
    until = user.get("premium_until")
    if until is None:
        return True
    if hasattr(until, "date"):
        until = until.date()
    return until >= date.today()


def mode() -> str:
    return ACCESS if ACCESS in ("admin", "premium", "signed_in") else "admin"


def is_admin(user) -> bool:
    return bool(user) and user.get("user_id") in _admin_ids()


def allowed(user) -> bool:
    if not ENABLED or not user:
        return False
    m = mode()
    if m == "signed_in":
        return True
    if m == "premium" and _premium_now(user):
        return True
    return is_admin(user)


def _gate(request):
    from api import current_user
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in to use this.")
    if not allowed(user):
        raise HTTPException(403, "This is available to %s." % _WHO[mode()])
    return user


# =====================================================================
# SAVED PLANS
# =====================================================================
_COLS = ("plan_id, name, instrument, instrument_label, direction, trade_tf, closed_only, notes, "
         "conditions, created_at, updated_at")


def _as_plan(row):
    """A stored row as the plan dict the engine validates. The conditions column is jsonb (already a list
    from psycopg; text in some other drivers)."""
    conds = row["conditions"]
    if isinstance(conds, (str, bytes)):
        conds = json.loads(conds)
    return {"name": row["name"], "instrument": row["instrument"], "instrument_label": row["instrument_label"],
            "direction": row["direction"], "trade_tf": row["trade_tf"], "closed_only": bool(row["closed_only"]),
            "notes": row["notes"] or "", "conditions": conds}


def _present(row):
    """A stored plan as the page wants it: the plan, its id and dates, and its sentence. A stored plan that no
    longer validates (an indicator was withdrawn) is still listed, flagged, so it can be edited or deleted
    rather than silently vanishing."""
    raw = _as_plan(row)
    out = {"plan_id": row["plan_id"], "created_at": row["created_at"], "updated_at": row["updated_at"],
           "plan": raw, "ok": True}
    try:
        clean = te.clean_plan(raw)
        out["plan"] = clean
        out["description"] = te.describe_plan(clean)
    except te.SpecError as e:
        out["ok"] = False
        out["problem"] = str(e)
    return out


def _load_plan(query, user, plan_id):
    try:
        pid = int(plan_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "That is not a plan I know.")
    try:
        row = query("SELECT %s FROM trade_plan WHERE plan_id = %%(id)s AND user_id = %%(u)s" % _COLS,
                    {"id": pid, "u": user["user_id"]}, one=True)
    except Exception as e:                                       # noqa: BLE001
        raise _plan_db_trouble(e, user, "read")
    if not row:
        raise HTTPException(404, "No such plan.")
    return row


def _plan_db_trouble(e, user, what):
    """A failed read or write on the plans table, as words the page can show. The commonest cause on a fresh deploy is
    that create_trade_plan_tables.py was never run: the page and the code are live, but the table that holds the plans
    does not exist, so every list and every Save fails. Without this the person sees "Something went wrong (500)" and a
    page that quietly does nothing. The raw database error is shown to an administrator only."""
    text = str(e).strip()
    first = text.splitlines()[0][:200] if text else ""
    missing = (getattr(e, "sqlstate", None) == "42P01" or type(e).__name__ == "UndefinedTable"
               or "no such table" in text.lower() or ("does not exist" in text.lower() and "trade_plan" in text.lower()))
    print("mfapi: trade plan %s failed: %s: %s" % (what, type(e).__name__, first), flush=True)
    if missing:
        detail = "Trade plans are not set up on this server yet: the table that holds them has not been created."
        if is_admin(user):
            detail += (" Run `venv/bin/python3 create_trade_plan_tables.py --admin doadmin` in /opt/mfapi on the server "
                       "(DEPLOY_TRADEPLAN.md, step 4; it asks for the database admin password), restart, then reload this page. "
                       "Nothing you write until then can be saved.")
    else:
        detail = "Your plans could not be %s because of a problem on the server." % what
        if is_admin(user):
            detail += " (%s: %s)" % (type(e).__name__, first)
    return HTTPException(503, detail)


def _plan_params(user, plan):
    return {"u": user["user_id"], "name": plan["name"], "instrument": plan["instrument"],
            "label": plan["instrument_label"], "direction": plan["direction"], "tf": plan["trade_tf"],
            "closed": plan["closed_only"], "notes": plan["notes"], "conds": json.dumps(plan["conditions"])}


# =====================================================================
# CANDLES
# =====================================================================
def intraday_frame(isin, tf):
    """Intraday candles for a stock: a DataFrame, or a string saying why there are none. NONE ARE HELD TODAY.
    This is the seam: when a source is chosen and loaded, read it here and nothing else changes."""
    return "We do not hold %s candles yet." % te.TF_LABEL[tf].lower()


def _daily_frame(query, isin):
    """(DataFrame of adjusted daily candles | reason string, notes). Read exactly as the back-tester reads
    them, so the split/bonus adjustment and the bad-tick trimming are the same."""
    import backtest_api as ba
    rows = query(ba._OHLC, {"isins": [isin]})
    if not rows:
        return "No price history is held for this stock.", []
    notes = []
    try:
        events = query(ba._EVENTS, {"isins": [isin]})
    except Exception as e:                                   # noqa: BLE001
        print("mfapi: trade plan could not read stock_event (%s: %s); candles are not adjusted for splits/bonuses"
              % (type(e).__name__, str(e).split("\n")[0]), flush=True)
        events = []
        notes.append("The record of splits and bonus issues could not be read just now, so prices were not adjusted "
                     "for them.")
    df, fnotes = ba._frame(rows, events)
    return df, notes + fnotes


def _funds_part(isin):
    """How many mutual funds newly bought, added to, trimmed or sold out of this stock between the last two month-end portfolios, and how many hold it: the counts the
    stock page's own "Funds that moved" shows (portfolio_api.stock_changes), worked out with the same query and the same classification (a bonus or split is not
    buying). Counts only: no fund is named here. None when fewer than two months are loaded."""
    import psycopg
    from psycopg.rows import dict_row
    import portfolio_api as pa
    key = str(isin).strip().upper()
    with psycopg.connect(pa.DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        dates = [r["as_of_date"] for r in pa._q(cur, "SELECT DISTINCT as_of_date FROM mf_holding ORDER BY as_of_date DESC LIMIT 2")]
        if len(dates) < 2:
            return None
        now, prev = dates[0], dates[1]
        rows = pa._q(cur, pa.STOCK_CHANGE_ROWS, {"isin": key, "now": now, "prev": prev})
        corp = {r["isin"]: float(r["median_ratio"]) for r in pa._q(cur, pa.CHANGE_CORP_ACTION, {"now": now, "prev": prev, "isins": [key]})}
    med = corp.get(key)
    counts, holders = {}, 0
    for r in rows:
        pn = float(r["pct_now"]) if r["pct_now"] is not None else None
        pp = float(r["pct_prev"]) if r["pct_prev"] is not None else None
        qn = float(r["qty_now"]) if r["qty_now"] is not None else None
        qp = float(r["qty_prev"]) if r["qty_prev"] is not None else None
        is_ca = bool(med and abs(med - 1.0) > pa.CA_BAND and qn and qp and abs((qn / qp) - med) <= pa.CA_BAND * max(1.0, med))
        kind, _why = pa._classify(pn, pp, qn, qp, is_ca)
        counts[kind] = counts.get(kind, 0) + 1
        holders += pn is not None
    return {"as_of": str(now), "compared_with": str(prev), "counts": counts, "holders": holders}


def company_frame(query, isin, parts=None):
    """What the company-data conditions read: {"results": part, "ratios": part, "ownership": part}, each part the dict tradeplan_engine reads or a string saying
    why it could not be had. Only the parts asked for are read. Everything comes through the SAME functions the stock page uses (api.build_financials's own
    helpers for the results, api.stock_ratios, api.stock_shareholding), so a rule and the page cannot disagree."""
    import api as h
    parts = set(parts or ("results", "ratios", "ownership", "events", "funds"))
    out = {}
    if "results" in parts:
        try:
            import viewcheck_api as vc
            rows = vc._prepare(h, vc._results_rows(h, isin))
            out["results"] = te.results_part(rows, neighbour=h._fin_neighbour, eps_like=h._fin_eps_comparable) or "No results are on file for this stock yet."
        except Exception as e:                           # noqa: BLE001  one part failing must not take the others (or the candles) down
            print("mfapi: trade plan could not read company results for %s (%s: %s)" % (isin, type(e).__name__, str(e).split("\n")[0]), flush=True)
            out["results"] = "The company's results could not be read just now."
    if "ratios" in parts:
        try:
            out["ratios"] = h.stock_ratios(isin)
        except Exception as e:                           # noqa: BLE001
            print("mfapi: trade plan could not read ratios for %s (%s: %s)" % (isin, type(e).__name__, str(e).split("\n")[0]), flush=True)
            out["ratios"] = "The ratios could not be worked out just now."
    if "funds" in parts:
        try:
            out["funds"] = _funds_part(isin) or "Only one month of fund portfolios has been loaded so far, so there is nothing to compare."
        except Exception as e:                           # noqa: BLE001
            print("mfapi: trade plan could not read fund flows for %s (%s: %s)" % (isin, type(e).__name__, str(e).split("\n")[0]), flush=True)
            out["funds"] = "The mutual-fund portfolios could not be read just now."
    if "events" in parts:
        try:
            rows = query("SELECT DISTINCT event_date FROM stock_event WHERE isin = %(i)s AND kind = 'result' AND event_date >= CURRENT_DATE - 1 ORDER BY event_date",
                         {"i": isin})
            out["events"] = {"results": [r["event_date"] for r in rows]}
        except Exception as e:                           # noqa: BLE001
            print("mfapi: trade plan could not read results dates for %s (%s: %s)" % (isin, type(e).__name__, str(e).split("\n")[0]), flush=True)
            out["events"] = "The results dates could not be read just now."
    if "ownership" in parts:
        try:
            sh = h.stock_shareholding(isin, quarters=8)
            out["ownership"] = sh if sh.get("quarters") else "No shareholding pattern is on file for this stock yet."
        except Exception as e:                           # noqa: BLE001
            print("mfapi: trade plan could not read shareholding for %s (%s: %s)" % (isin, type(e).__name__, str(e).split("\n")[0]), flush=True)
            out["ownership"] = "The shareholding pattern could not be read just now."
    return out


def load_frames(query, instrument, isin, tfs, parts=None):
    """{timeframe: DataFrame | reason string}, plus notes. Only the time frames the plan uses are built. The company-data time frame "R" is a dict
    {"results", "ratios", "ownership"} (see company_frame): only the `parts` named are read (all three when None)."""
    import backtest_engine as be
    cover = te.coverage_for(instrument)
    frames, notes = {}, []
    daily = None
    for tf in sorted(tfs, key=[k for k, _ in te.TIMEFRAMES].index):
        if cover.get(tf):
            frames[tf] = cover[tf]                       # not held for this instrument
            continue
        if tf == te.COMPANY_TF:
            frames[tf] = company_frame(query, isin, parts)
            continue
        if tf in te.INTRADAY:
            frames[tf] = intraday_frame(isin, tf)
            continue
        if daily is None:
            daily = _daily_frame(query, isin)
            notes.extend(daily[1])
        df = daily[0]
        if isinstance(df, str) or tf == "D":
            frames[tf] = df
            continue
        try:
            frames[tf] = be._candles(df, tf)
        except te.SpecError as e:
            frames[tf] = str(e)
    return frames, notes


# =====================================================================
# ENDPOINTS
# =====================================================================
@page_router.get("/tradeplan", include_in_schema=False)
def page():
    # Served to anyone, like /backtest: a static file, and the API behind it is what refuses. It asks
    # /api/tradeplan/access first and shows a notice.
    path = os.path.join(BASE_DIR, "tradeplan.html")
    if not os.path.exists(path):
        raise HTTPException(404, "tradeplan.html is missing from %s" % BASE_DIR)
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})


@router.get("/access")
def access(request: Request):
    """May this visitor use it? Never an error: the page asks this before anything else, so a signed-out
    visitor, a member without access and an administrator each get the right screen."""
    from api import current_user
    user = current_user(request)
    return {"enabled": ENABLED, "signed_in": bool(user), "allowed": allowed(user),
            "admin": is_admin(user), "audience": mode(), "who": _WHO[mode()],
            "email": (user or {}).get("email") if allowed(user) else None}     # shown back to the person: "we will email you at ..."


@router.get("/catalog")
def catalog(request: Request):
    _gate(request)
    c = te.catalog()
    c["max_plans"] = MAX_PLANS
    return c


@router.get("/plans")
def plans(request: Request):
    from api import query
    user = _gate(request)
    try:
        rows = query("SELECT %s FROM trade_plan WHERE user_id = %%(u)s ORDER BY updated_at DESC, plan_id DESC" % _COLS,
                     {"u": user["user_id"]})
    except Exception as e:                                       # noqa: BLE001
        raise _plan_db_trouble(e, user, "loaded")
    return {"plans": [_present(r) for r in rows], "max_plans": MAX_PLANS}


@router.post("/describe")
def describe(request: Request, body: dict = Body(...)):
    """The plan in words, or the reason it cannot be used yet. Nothing saved, no database: the page calls this
    as the person builds, so a mistake shows beside the condition that has it. A problem is an ordinary answer
    here (ok: false), not an HTTP error."""
    _gate(request)
    probe = dict(body) if isinstance(body, dict) else body
    if isinstance(probe, dict) and not str(probe.get("name") or "").strip():
        probe["name"] = "Untitled plan"                  # the name is not needed to read the conditions
    try:
        plan = te.clean_plan(probe)
    except te.SpecError as e:
        return {"ok": False, "error": str(e)}
    d = te.describe_plan(plan)
    cover = te.coverage_for(plan["instrument"])
    used = sorted({c["tf"] for c in plan["conditions"]})
    d["missing"] = [{"tf": t, "label": te.TF_LABEL[t], "reason": cover[t]} for t in used if cover[t]]
    return {"ok": True, "description": d}


@router.post("/plans")
def save_plan(request: Request, body: dict = Body(...)):
    from api import execute, query
    user = _gate(request)
    try:
        plan = te.clean_plan(body)
    except te.SpecError as e:
        raise HTTPException(400, str(e))
    pid = body.get("plan_id")
    params = _plan_params(user, plan)
    try:
        if pid in (None, ""):
            n = (query("SELECT COUNT(*) AS n FROM trade_plan WHERE user_id = %(u)s", {"u": user["user_id"]},
                       one=True) or {}).get("n", 0)
            if n >= MAX_PLANS:
                raise HTTPException(400, "You can keep up to %d plans. Delete one you no longer use first." % MAX_PLANS)
            row = execute(
                "INSERT INTO trade_plan (user_id, name, instrument, instrument_label, direction, trade_tf, closed_only, "
                "notes, conditions) VALUES (%(u)s, %(name)s, %(instrument)s, %(label)s, %(direction)s, %(tf)s, "
                "%(closed)s, %(notes)s, %(conds)s::jsonb) RETURNING plan_id", params, returning=True)
            pid = row["plan_id"]
        else:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                raise HTTPException(400, "That is not a plan I know.")
            params["id"] = pid
            row = execute(
                "UPDATE trade_plan SET name = %(name)s, instrument = %(instrument)s, instrument_label = %(label)s, "
                "direction = %(direction)s, trade_tf = %(tf)s, closed_only = %(closed)s, notes = %(notes)s, "
                "conditions = %(conds)s::jsonb, updated_at = now() "
                "WHERE plan_id = %(id)s AND user_id = %(u)s RETURNING plan_id", params, returning=True)
            if not row:
                raise HTTPException(404, "No such plan.")
    except HTTPException:
        raise
    except Exception as e:                                       # noqa: BLE001
        raise _plan_db_trouble(e, user, "saved")
    saved = _present(_load_plan(query, user, pid))
    return {"ok": True, "plan": saved}


@router.post("/plans/{plan_id}/delete")
def delete_plan(plan_id: int, request: Request):
    from api import execute
    user = _gate(request)
    try:
        row = execute("DELETE FROM trade_plan WHERE plan_id = %(id)s AND user_id = %(u)s RETURNING plan_id",
                      {"id": plan_id, "u": user["user_id"]}, returning=True)
    except Exception as e:                                       # noqa: BLE001
        raise _plan_db_trouble(e, user, "removed")
    if not row:
        raise HTTPException(404, "No such plan.")
    return {"ok": True}


@router.post("/broker-link")
def broker_link(request: Request, body: dict = Body(...)):
    """Make the broker a person types under "Other" safe to use as a link: https only, a plain host name, no
    password or port, and nothing after a ? or # (a pasted terminal link can carry a session token). Nothing is
    stored here: the page keeps the answer in the person's own browser, so FinChaya never holds which broker
    anyone uses. A problem is an ordinary answer (ok: false), like /describe."""
    _gate(request)
    try:
        link = te.clean_terminal_link(body.get("name") if isinstance(body, dict) else None,
                                      body.get("url") if isinstance(body, dict) else None)
    except te.SpecError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **link}


_NAMES = "SELECT isin, symbol, company_name FROM stock_master WHERE isin = %(isin)s"


def _view_of(body):
    view = str(body.get("view") or "").strip().lower()
    if view not in te.DIRECTIONS:
        raise HTTPException(400, "Say whether you are bullish or bearish.")
    return view


def _resolve(query, user, body):
    """The plan to work on (saved, by id, or an unsaved one sent inline) and, for a stock plan, the stock.
    Returns (plan, plan_id, stock, isin). Shared by the check and by alerts, so both refuse in the same words."""
    plan_id = None
    if body.get("plan_id") not in (None, ""):
        row = _load_plan(query, user, body["plan_id"])
        plan_id = row["plan_id"]
        raw = _as_plan(row)
    elif isinstance(body.get("plan"), dict):
        raw = body["plan"]
    else:
        raise HTTPException(400, "Choose a plan to check.")
    try:
        plan = te.clean_plan(raw)
    except te.SpecError as e:
        raise HTTPException(409 if plan_id is not None else 400, str(e))

    stock = None
    isin = None
    if plan["instrument"] == "stock":
        isin = str(body.get("isin") or "").strip().upper()
        import backtest_engine as be
        if not be.ISIN_RE.match(isin):
            raise HTTPException(400, "Choose the stock to check this plan on.")
        stock = query(_NAMES, {"isin": isin}, one=True)
        if not stock:
            raise HTTPException(404, "No stock with that ISIN.")
    return plan, plan_id, stock, isin


def plan_parts(plan):
    """Which company readings (results / ratios / ownership) a plan's conditions need, so only those are read."""
    return {te._IND[c["indicator"]]["part"] for c in plan["conditions"] if te._IND[c["indicator"]].get("data") == "company"}


def _frames(query, plan, isin):
    tfs = {c["tf"] for c in plan["conditions"]}
    try:
        return load_frames(query, plan["instrument"], isin, tfs, plan_parts(plan))
    except (ImportError, AttributeError) as e:
        # The candle reader and indicator code are the back-tester's. An older copy of those files on the
        # server (they are uploaded separately) shows up here as a missing name, which is a deploy mistake to
        # be told in words, not a bare 500.
        print("mfapi: trade plan could not read candles: the back-test files on this server look older than the "
              "trade plan needs (%s: %s)" % (type(e).__name__, str(e).split("\n")[0]), flush=True)
        raise HTTPException(503, "The candle reader on this server is older than the trade plan needs. "
                                 "Upload the current backtest_api.py, backtest_engine.py, price_adjust.py and "
                                 "indicators.py, then restart the service.")


@router.post("/check")
def check(request: Request, body: dict = Body(...)):
    from api import query
    user = _gate(request)
    return run_check(query, user, body)


def run_check(query, user, body):
    """Check one of the person's plans (or an unsaved one sent inline) on a stock for a view. The answer for /check, and for "Check your view", which runs the
    person's own saved plan first (viewcheck_api). `user` has already passed _gate / allowed()."""
    view = _view_of(body)
    plan, _plan_id, stock, isin = _resolve(query, user, body)
    frames, notes = _frames(query, plan, isin)
    tfs = {c["tf"] for c in plan["conditions"]}

    today = date.today()
    out = te.evaluate_plan(plan, frames, view, today=today)
    out["notes"] = out["notes"] + notes
    d = frames.get("D")
    if d is not None and not isinstance(d, str) and len(d) and (today - d.index[-1].date()).days > STALE_DAYS:
        out["notes"].append("The newest daily candle held is from %s, so a reading may not reflect the most recent sessions."
                            % te.be._dmy(d.index[-1].date().isoformat()))

    cover = te.coverage_for(plan["instrument"])
    out["missing_data"] = [{"tf": t, "label": te.TF_LABEL[t], "reason": cover[t] or
                            (frames[t] if isinstance(frames.get(t), str) else None)}
                           for t in sorted(tfs) if cover[t] or isinstance(frames.get(t), str)]
    inst = next(i for i in te.INSTRUMENTS if i["key"] == plan["instrument"])
    out["plan"] = {"name": plan["name"], "instrument": plan["instrument"],
                   "instrument_label": plan["instrument_label"] or inst["label"],
                   "direction": plan["direction"], "closed_only": plan["closed_only"],
                   "description": te.describe_plan(plan)}
    out["stock"] = ({"isin": stock["isin"], "symbol": stock["symbol"], "name": stock["company_name"]}
                    if stock else None)
    out["ok"] = True
    return out


# ---------------------------------------------------------------------
# ALERTS: "email me when this is met"
# The checking and the emailing are tradeplan_alerts.py, run on a schedule. This is only how a person sets, sees and
# removes one, and how the page learns the truth about whether anything is actually watching.
# ---------------------------------------------------------------------
_ALERT_COLS = ("alert_id, plan_id, plan_name, scope, instrument, isin, symbol, company_name, for_view, spec, sentence, "
               "status, note, last_checked_at, last_state, triggered_at, trigger_reading, email_status, email_attempts, "
               "email_error, email_sent_at, created_at")


def _alert_present(row):
    import tradeplan_alerts as ta
    spec = ta._j(row["spec"])
    out = {k: row[k] for k in ("alert_id", "plan_id", "plan_name", "scope", "instrument", "isin", "symbol", "company_name",
                               "for_view", "sentence", "status", "note", "last_checked_at", "last_state", "triggered_at",
                               "email_status", "email_attempts", "email_sent_at", "created_at")}
    out["closed_only"] = bool(spec.get("closed_only", True))
    out["reading"] = ta._j(row["trigger_reading"]) if row["trigger_reading"] is not None else None
    # the person sees that the email did not go, not the provider's raw error
    out["email_problem"] = (row["email_status"] == "failed")
    return out


def _alert_readiness(query, user):
    """What is actually set up: whether this server can send email at all, and whether the checker has run lately.
    The raw reason mail is not configured names settings, so only an administrator is shown it."""
    import tradeplan_alerts as ta
    cfg = ta.mail_config()
    return {"email": {"configured": cfg["configured"], "transport": cfg["transport"],
                      "why": cfg["why"] if is_admin(user) else None},
            "checker": ta.checker_status(query), "max_alerts": ta.MAX_ALERTS, "max_email_attempts": ta.MAX_EMAIL_ATTEMPTS}


def _alert_db_trouble(e, user, what):
    """A failed read or write on the alert tables, as words the page can show. The commonest cause on a fresh deploy is that
    create_trade_plan_tables.py has not been run since the alert tables were added (the plans table exists, so plans work
    and nothing looks wrong until someone sets an alert). Without this the person sees "Something went wrong (500)" and
    an alerts tab that never changes. The raw database error is shown to an administrator only."""
    text = str(e).strip()
    first = text.splitlines()[0][:200] if text else ""
    missing = (getattr(e, "sqlstate", None) == "42P01" or type(e).__name__ == "UndefinedTable"
               or "no such table" in text.lower() or ("does not exist" in text.lower() and "trade_alert" in text.lower()))
    print("mfapi: trade alert %s failed: %s: %s" % (what, type(e).__name__, first), flush=True)
    if missing:
        detail = "Alerts are not set up on this server yet: the table that holds them has not been created."
        if is_admin(user):
            detail += (" Run `python create_trade_plan_tables.py --admin doadmin` on the server (DEPLOY_TRADEPLAN.md, Part D), "
                       "then try again. Nothing you set until then is saved.")
    else:
        detail = "Alerts could not be %s because of a problem on the server." % what
        if is_admin(user):
            detail += " (%s: %s)" % (type(e).__name__, first)
    return HTTPException(503, detail)


@router.get("/alerts")
def my_alerts(request: Request):
    from api import query
    user = _gate(request)
    try:
        rows = query("SELECT %s FROM trade_alert WHERE user_id = %%(u)s ORDER BY created_at DESC, alert_id DESC LIMIT 200"
                     % _ALERT_COLS, {"u": user["user_id"]})
    except Exception as e:                                       # noqa: BLE001
        raise _alert_db_trouble(e, user, "loaded")
    return {"alerts": [_alert_present(r) for r in rows], "my_email": user.get("email"),
            **_alert_readiness(query, user)}


@router.post("/alerts")
def create_alert(request: Request, body: dict = Body(...)):
    """Watch one condition (scope "condition" and its "index" in the plan), ALL the conditions of one set at once
    (scope "set" and its "set" number: the header of a card of AND-joined conditions), or the whole plan (scope "plan")
    on a stock, and email the person the first time it is met. For a set or a plan that means every condition holding
    AT THE SAME TIME on one look, not each having been true at some point. Refused, in words, when it could never do its
    job: already met (nothing to wait for), or cannot be read at all (it could never become met)."""
    import tradeplan_alerts as ta
    from api import execute, query
    user = _gate(request)
    view = _view_of(body)
    scope = body.get("scope")
    if scope not in ("condition", "set", "plan"):
        raise HTTPException(400, "Choose whether to watch one condition, all the conditions in a set, or the whole plan.")
    closed_only = body.get("closed_only", True)
    if not isinstance(closed_only, bool):
        raise HTTPException(400, "\"Wait for the candle to close\" must be on or off.")

    plan, plan_id, stock, isin = _resolve(query, user, body)
    frames, _notes = _frames(query, plan, isin)
    res = te.evaluate_plan(plan, frames, view, today=date.today())
    used = te.mirror_plan(plan) if res["mirrored"] else plan

    if scope == "condition":
        idx = body.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(used["conditions"]):
            raise HTTPException(400, "Choose which condition to watch.")
        row = res["conditions"][idx]
        if row["state"] == "met":
            raise HTTPException(400, "That condition is already met, so there is nothing to wait for.")
        if row["state"] == "no_data":
            raise HTTPException(400, "We have no candles for that condition yet (%s), so an alert on it could never fire. "
                                     "Ask us for the data instead." % (row["reason"] or "no data").rstrip("."))
        conds = [dict(used["conditions"][idx], join="and")]
        sentence = te.describe_condition(conds[0])
    elif scope == "set":
        # "tell me when ALL of these hold at the same time": one set of AND-joined conditions from the plan
        k = body.get("set")
        if isinstance(k, bool) or not isinstance(k, int) or not 0 <= k < len(res["sets"]):
            raise HTTPException(400, "Choose which set of conditions to watch.")
        st = res["sets"][k]
        if st["state"] == "met":
            raise HTTPException(400, "Those conditions are already all met, so there is nothing to wait for.")
        # Every condition in the set must be READABLE, not merely "something in it is false": a set with one false
        # condition and one we hold no candles for is "not met", yet it could never become met, and an alert on it
        # would promise an email that cannot come.
        lacking = "; ".join(res["conditions"][i]["text"] for i in st["indices"] if res["conditions"][i]["state"] == "no_data")
        if lacking:
            raise HTTPException(400, "We cannot read every condition in that set yet, so an alert on all of them could not tell "
                                     "when they are met. No data for: %s. You can still set an alert on a single condition "
                                     "we can read." % lacking)
        conds = [dict(used["conditions"][i], join="and") for i in st["indices"]]
        sentence = te.describe_plan(dict(used, conditions=conds))["sentence"]
    else:
        state = res["overall"]["state"]
        if state == "met":
            raise HTTPException(400, "Your plan's conditions are already met, so there is nothing to wait for.")
        # At least one set must be fully readable, or the plan could never become met (the same reasoning as for a set)
        readable = [s for s in res["sets"] if all(res["conditions"][i]["state"] != "no_data" for i in s["indices"])]
        if not readable:
            lacking = "; ".join(c["text"] for c in res["conditions"] if c["state"] == "no_data")
            raise HTTPException(400, "We cannot read every condition yet, so an alert on the whole plan could not tell "
                                     "when it is met. No data for: %s. You can still set an alert on a single condition "
                                     "we can read." % lacking)
        conds = [dict(c) for c in used["conditions"]]
        conds[0]["join"] = "and"
        sentence = te.describe_plan(dict(used, conditions=conds))["sentence"]
    spec = {"conditions": conds, "closed_only": closed_only}

    try:
        mine = query("SELECT alert_id, status, isin, for_view, spec FROM trade_alert WHERE user_id = %(u)s "
                     "AND status IN ('active', 'paused')", {"u": user["user_id"]})
    except Exception as e:                                       # noqa: BLE001
        raise _alert_db_trouble(e, user, "saved")
    key = json.dumps(spec, sort_keys=True)
    for r in mine:
        if r["status"] == "active" and r["isin"] == isin and r["for_view"] == view \
                and json.dumps(ta._j(r["spec"]), sort_keys=True) == key:
            again = query("SELECT %s FROM trade_alert WHERE alert_id = %%(id)s" % _ALERT_COLS, {"id": r["alert_id"]}, one=True)
            return {"ok": True, "already": True, "alert": _alert_present(again),
                    "message": "You already have this alert. We are watching it."}
    if len(mine) >= ta.MAX_ALERTS:
        raise HTTPException(400, "You can keep up to %d alerts waiting. Remove one you no longer need first." % ta.MAX_ALERTS)

    try:
        row = execute(
            "INSERT INTO trade_alert (user_id, plan_id, plan_name, scope, instrument, isin, symbol, company_name, for_view, "
            "spec, sentence) VALUES (%(u)s, %(pid)s, %(pn)s, %(sc)s, %(inst)s, %(isin)s, %(sym)s, %(cn)s, %(v)s, "
            "%(spec)s::jsonb, %(sent)s) RETURNING alert_id",
            {"u": user["user_id"], "pid": plan_id, "pn": plan["name"], "sc": scope, "inst": plan["instrument"], "isin": isin,
             "sym": stock["symbol"] if stock else None, "cn": stock["company_name"] if stock else None, "v": view,
             "spec": json.dumps(spec), "sent": sentence}, returning=True)
        made = query("SELECT %s FROM trade_alert WHERE alert_id = %%(id)s" % _ALERT_COLS, {"id": row["alert_id"]}, one=True)
    except Exception as e:                                       # noqa: BLE001
        raise _alert_db_trouble(e, user, "saved")
    print("mfapi: trade alert set user=%s %s %s" % (user["user_id"], scope, isin or plan["instrument"]), flush=True)
    return {"ok": True, "already": False, "alert": _alert_present(made), **_alert_readiness(query, user)}


@router.post("/alerts/{alert_id}/delete")
def delete_alert(alert_id: int, request: Request):
    from api import execute
    user = _gate(request)
    try:
        row = execute("DELETE FROM trade_alert WHERE alert_id = %(id)s AND user_id = %(u)s RETURNING alert_id",
                      {"id": alert_id, "u": user["user_id"]}, returning=True)
    except Exception as e:                                       # noqa: BLE001
        raise _alert_db_trouble(e, user, "removed")
    if not row:
        raise HTTPException(404, "No such alert.")
    return {"ok": True}


# ---------------------------------------------------------------------
# REQUESTS
# ---------------------------------------------------------------------
@router.post("/requests")
def make_request(request: Request, body: dict = Body(...)):
    """Ask for an indicator (or for data) we do not have. Idempotent per person per thing."""
    from api import execute, query
    user = _gate(request)
    kind = str(body.get("kind") or "indicator").strip().lower()
    if kind not in ("indicator", "data"):
        raise HTTPException(400, "kind must be 'indicator' or 'data'.")
    try:
        name = te._text(body.get("name"), "Request", 2, 80, "the name of what you need")
        details = body.get("details")
        details = "" if details in (None, "") else te._text(details, "Details", 0, MAX_REQUEST_DETAILS, "details")
        tf = body.get("timeframe")
        if tf not in (None, "") and tf not in te.TF_LABEL:
            raise te.SpecError("That is not a time frame I know.")
        instrument = body.get("instrument")
        instrument = "" if instrument in (None, "") else te._text(instrument, "Instrument", 0, te.MAX_LABEL, "instrument")
    except te.SpecError as e:
        raise HTTPException(400, str(e))

    if kind == "indicator":
        have = te.known_indicator(name)
        if have:
            label = next(i["label"] for i in te.INDICATORS if i["key"] == have)
            return {"ok": True, "exists": True, "indicator": have,
                    "message": "%s is already in the list (under %s), so there is nothing to request."
                               % (label, next(i["group"] for i in te.INDICATORS if i["key"] == have))}

    nk = te.name_key(name)
    if not nk:
        raise HTTPException(400, "Type the name of the indicator or data you need.")
    if query("SELECT 1 AS x FROM indicator_request WHERE user_id = %(u)s AND kind = %(k)s AND name_key = %(nk)s",
             {"u": user["user_id"], "k": kind, "nk": nk}, one=True):
        return {"ok": True, "already": True, "message": "You have already asked for this. We have it on the list."}

    since = datetime.now(timezone.utc) - timedelta(days=1)
    n = (query("SELECT COUNT(*) AS n FROM indicator_request WHERE user_id = %(u)s AND created_at >= %(since)s",
               {"u": user["user_id"], "since": since}, one=True) or {}).get("n", 0)
    if n >= MAX_REQUESTS_PER_DAY:
        raise HTTPException(429, "That is a lot of requests for one day. Try again tomorrow.")

    execute("INSERT INTO indicator_request (user_id, kind, name, name_key, details, timeframe, instrument) "
            "VALUES (%(u)s, %(k)s, %(name)s, %(nk)s, %(details)s, %(tf)s, %(inst)s) RETURNING request_id",
            {"u": user["user_id"], "k": kind, "name": name, "nk": nk, "details": details or None,
             "tf": tf or None, "inst": instrument or None}, returning=True)
    print("mfapi: trade plan request user=%s %s=%r" % (user["user_id"], kind, name), flush=True)
    return {"ok": True, "message": "Thank you. We have noted it, and the more people ask for the same thing the sooner it moves up."}


@router.get("/requests/mine")
def my_requests(request: Request):
    from api import query
    user = _gate(request)
    rows = query("SELECT kind, name, details, status, created_at FROM indicator_request WHERE user_id = %(u)s "
                 "ORDER BY created_at DESC LIMIT 50", {"u": user["user_id"]})
    return {"requests": rows}


@router.get("/requests")
def all_requests(request: Request):
    """Everyone's requests, counted by how many people asked. Administrators only, whatever the audience of
    the tool is: this is the list of what to build next."""
    from api import query
    user = _gate(request)
    if not is_admin(user):
        raise HTTPException(403, "This is for administrators.")
    rows = query("SELECT request_id, user_id, kind, name, name_key, details, timeframe, instrument, status, created_at "
                 "FROM indicator_request ORDER BY created_at DESC, request_id DESC LIMIT 2000")
    by = {}
    for r in rows:
        g = by.setdefault((r["kind"], r["name_key"]), {
            "kind": r["kind"], "name": r["name"], "people": set(), "latest": r["created_at"],
            "first": r["created_at"], "status": r["status"], "details": [], "timeframes": set(), "instruments": set()})
        g["people"].add(r["user_id"])
        g["first"] = r["created_at"]                      # rows are newest first, so the last seen is the oldest
        g["name"] = r["name"]                             # ... and its spelling is the one shown: the first person to ask, every time
        if r["details"] and len(g["details"]) < 5:
            g["details"].append(r["details"])
        if r["timeframe"]:
            g["timeframes"].add(r["timeframe"])
        if r["instrument"]:
            g["instruments"].add(r["instrument"])
    out = [dict(g, people=len(g["people"]), timeframes=sorted(g["timeframes"]), instruments=sorted(g["instruments"]))
           for g in by.values()]
    out.sort(key=lambda g: str(g["latest"]), reverse=True)       # newest first among equals ...
    out.sort(key=lambda g: -g["people"])                         # ... then the most-asked-for on top (stable)
    return {"requests": out}


def mount(app):
    """Add the page and the API to a FastAPI app. Safe to call twice."""
    if getattr(app, "_tradeplan_mounted", False):
        return
    app.include_router(router)
    app.include_router(page_router)
    app._tradeplan_mounted = True
