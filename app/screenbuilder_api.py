"""
screenbuilder_api.py -- "Build your own screener": the page at /screen-builder and its API.
---------------------------------------------------------------------------
Mounted with the one block enable_screenbuilder.py appends to the live api.py, or by api.py itself:

    GET  /screen-builder                        the page
    GET  /api/screenbuilder/access              may I use it? (never an error: the page asks this first)
    GET  /api/screenbuilder/catalog             every field a screen can use (groups, settings, units, where its numbers come from, whether it can be used
                                                on this server right now), the sectors and industries to narrow to, and the limits
    GET  /api/screenbuilder/status              which readings are prepared (the first run after a new trading day reads ten years of prices)
    POST /api/screenbuilder/describe            {spec} -> the screen in plain English, or why it cannot be run (nothing runs)
    POST /api/screenbuilder/run                 {spec, limit, sort} -> the stocks that match, with each condition's reading, and how many were unknown
    GET  /api/screenbuilder/saved               my saved screens
    POST /api/screenbuilder/saved               {name, spec} -> save one (up to 30)
    POST /api/screenbuilder/saved/{id}          {name?, spec?} -> change one of mine
    POST /api/screenbuilder/saved/{id}/delete   remove one of mine
    POST /api/screenbuilder/requests            {kind, name, details} -> "please add this field": a technical indicator, a fundamental, mutual-fund or
                                                ownership data, or something else
    GET  /api/screenbuilder/requests            what I have asked for
    GET  /api/screenbuilder/requests/all        ADMIN: everyone's requests, counted by how many people asked

WHO MAY USE IT
    Subscribers to the Stocks plan (Stocks, or Mutual funds + stocks; not lapsed) and administrators, as for the results page and stock alerts
    (stock_access.py; checked on every call). MF_SCREENBUILDER_ACCESS=signed_in | everyone | admin changes the audience;
    MF_SCREENBUILDER_ENABLED=false is a hard off. A saved screen belongs to the person who made it: somebody else's id is a 404.

NEEDS (on the server): stock_access.py, screenbuilder_engine.py, screenbuilder_data.py, screenbuilder_tech.py, screener_api.py and what it reads
    (price_bars.py, dow_change.py, fib_retracement.py, expanded_flat.py, multiyear_breakout.py, ownership_screens.py), price_adjust.py, api.py (the ratios).
    A chart-pattern file that is missing only removes that pattern from the list. Saved screens need the table made by create_screenbuilder_tables.py;
    running a screen does not.
"""

import json
import os
import time

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

import feature_requests as fr
import screenbuilder_data as D
import screenbuilder_engine as E
import stock_access

router = APIRouter(prefix="/api/screenbuilder", tags=["screenbuilder"])
page_router = APIRouter()
GATE = stock_access.Gate("MF_SCREENBUILDER_ACCESS", "MF_SCREENBUILDER_ENABLED")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_SAVED = 30
MAX_NAME = 80
MAX_REQUESTS_PER_DAY = fr.MAX_REQUESTS_PER_DAY
REQUEST_KINDS = {"technical": "indicator", "fundamental": "data", "funds": "data", "ownership": "data", "other": "data"}
KIND_WORD = {"technical": "a technical indicator", "fundamental": "a fundamental figure", "funds": "mutual-fund data", "ownership": "shareholding data",
             "other": "something else"}


def _register_patterns():
    """Add the chart-pattern screens that exist on this server to the catalogue (a missing file only removes its own pattern)."""
    found = []
    for modname, attrs in (("dow_change", ("SCREEN_UP", "SCREEN_DOWN")), ("fib_retracement", ("SCREEN",)), ("expanded_flat", ("SCREEN_UP", "SCREEN_DOWN")),
                           ("multiyear_breakout", ("SCREEN",)), ("ipo_trend", ("SCREEN",)), ("higher_low", ("SCREEN",))):
        try:
            mod = __import__(modname)
        except ImportError:
            continue
        for a in attrs:
            spec = getattr(mod, a, None)
            if spec and spec.get("controls"):
                found.append((spec["key"], spec))
    E.register_patterns(found)
    return [k for k, _ in found]


PATTERNS = _register_patterns()


def _is_admin(user):
    return stock_access.is_admin(user)


def _connect():
    import psycopg
    from psycopg.rows import dict_row
    from portfolio_api import DB
    return psycopg.connect(DB, row_factory=dict_row)


def _db_trouble(e, user, what):
    """A failed read or write on the saved-screens table, as words. The commonest cause on a fresh deploy: create_screenbuilder_tables.py has not been run."""
    text = str(e).strip()
    first = text.splitlines()[0][:200] if text else ""
    missing = getattr(e, "sqlstate", None) == "42P01" or type(e).__name__ == "UndefinedTable" or "does not exist" in text.lower()
    print("mfapi: screen builder %s failed: %s: %s" % (what, type(e).__name__, first), flush=True)
    if missing:
        detail = "Saved screens are not set up on this server yet: the table that holds them has not been created."
        if _is_admin(user):
            detail += " Run `python create_screenbuilder_tables.py --admin doadmin` on the server (DEPLOY_SCREENBUILDER.md), then try again."
    else:
        detail = "Your screens could not be %s because of a problem on the server." % what
        if _is_admin(user):
            detail += " (%s: %s)" % (type(e).__name__, first)
    return HTTPException(503, detail)


def _spec(body):
    """The cleaned screen from a request body, or HTTP 400 with the reason."""
    if not isinstance(body, dict):
        raise HTTPException(400, "The screen is not in a form I can read.")
    try:
        return E.clean_spec(body.get("spec") if "spec" in body else body)
    except E.SpecError as e:
        raise HTTPException(400, str(e))


# =====================================================================
# THE PAGE
# =====================================================================
@page_router.get("/screen-builder", include_in_schema=False)
def page():
    # Served to anyone, like /stock-alerts: a static file whose API refuses. It asks /api/screenbuilder/access first.
    path = os.path.join(BASE_DIR, "screenbuilder.html")
    if not os.path.exists(path):
        raise HTTPException(404, "screenbuilder.html is missing from %s" % BASE_DIR)
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})


# =====================================================================
# THE ENDPOINTS
# =====================================================================
@router.get("/access")
def access(request: Request):
    return GATE.access(request)


@router.get("/catalog")
def catalog(request: Request):
    GATE.require(request)
    with _connect() as conn, conn.cursor() as cur:
        try:
            avail = D.availability(cur)
            prof = D.profile(cur)
        except D.ProviderError as e:
            avail, prof = {}, None
            print("mfapi: screen builder catalogue: %s" % e, flush=True)
    D.warm_async()                            # start reading the prices now: somebody has opened the page
    try:
        import price_bars
        price_bars.warm_async()               # the chart-pattern conditions read these bars (cached with them, six hours)
    except Exception:                         # noqa: BLE001  a warm-up must never hurt anything
        pass
    out = E.public_catalog(avail)
    out["patterns"] = PATTERNS
    out["max_saved"] = MAX_SAVED
    out["request_kinds"] = [{"key": k, "label": KIND_WORD[k]} for k in REQUEST_KINDS]
    if prof:
        sectors, industries = {}, {}
        for s in prof["stocks"].values():
            if s["sector"]:
                sectors[s["sector"]] = sectors.get(s["sector"], 0) + 1
            if s["industry"]:
                industries[s["industry"]] = industries.get(s["industry"], 0) + 1
        out["sectors"] = [{"name": k, "count": v} for k, v in sorted(sectors.items())]
        out["industries"] = [{"name": k, "count": v} for k, v in sorted(industries.items())]
        out["stocks"] = len(prof["stocks"])
        out["as_of"] = str(prof["market_date"])
    return out


@router.get("/status")
def status(request: Request):
    GATE.require(request)
    r = D.ready()
    return {"ready": r, "warming": D._WARM["running"], "all_ready": all(k in r for k in ("profile", "tech"))}


@router.post("/describe")
def describe(request: Request, body: dict = Body(...)):
    """The screen in words, or the reason it cannot be run. Nothing is run or saved. Answers 200 either way so the page can show the reason beside the
    form instead of as an error."""
    GATE.require(request)
    try:
        spec = E.clean_spec(body.get("spec") if isinstance(body, dict) and "spec" in body else body)
    except E.SpecError as e:
        return {"ok": False, "problem": str(e)}
    return {"ok": True, "spec": spec, **E.describe(spec)}


@router.post("/run")
def run(request: Request, body: dict = Body(...)):
    GATE.require(request)
    spec = _spec(body)
    limit = body.get("limit", 300) if isinstance(body, dict) else 300
    try:
        if isinstance(limit, bool):
            raise ValueError
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):               # "many", null, true, Infinity, 1e999
        raise HTTPException(400, "limit must be a whole number.")
    sort = body.get("sort", "size") if isinstance(body, dict) else "size"
    started = time.time()
    try:
        with _connect() as conn, conn.cursor() as cur:
            res = D.run(cur, spec, limit=limit, sort=sort)
    except D.ProviderError as e:
        raise HTTPException(503, str(e))
    res["elapsed"] = round(time.time() - started, 2)
    res.update(E.describe(spec))
    return res


# ---- saved screens ----------------------------------------------------------------------------------------------------
_COLS = "screen_id, name, spec, sentence, created_at, updated_at"


def _name(v):
    if not isinstance(v, str) or not v.strip():
        raise HTTPException(400, "Give the screen a name.")
    v = " ".join(v.split())
    if len(v) > MAX_NAME:
        raise HTTPException(400, "A name can be up to %d characters." % MAX_NAME)
    return v


def _sentence(spec):
    return "\n".join(E.outline_text(E.describe(spec)["outline"]))[:4000]


def _present(r):
    """A saved screen for the page. One that this server can no longer read (it uses a field that was retired, or a pattern file that is not here) is
    still listed, marked broken with the reason, so that one such screen never hides all the others."""
    spec = r["spec"] if isinstance(r["spec"], dict) else json.loads(r["spec"])
    base = {"screen_id": r["screen_id"], "name": r["name"], "spec": spec, "sentence": r["sentence"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
    try:
        return {**base, **E.describe(E.clean_spec(spec))}
    except Exception as e:                                       # noqa: BLE001
        why = str(e) if isinstance(e, E.SpecError) else "it could not be read"
        return {**base, "broken": True, "universe": "", "outline": {"op": "all", "items": [{"n": 0, "text": "This screen cannot be used on this server: %s" % why}]}}


def _member(request):
    """The signed-in member behind a request that needs one (saving, requests). With the access setting 'everyone' the gate lets a signed-out visitor
    through, who has no account to save anything under."""
    user = GATE.require(request)
    if not user:
        raise HTTPException(401, "Sign in to do this.")
    return user


@router.get("/saved")
def saved(request: Request):
    from api import query
    user = _member(request)
    try:
        rows = query("SELECT %s FROM custom_screen WHERE user_id = %%(u)s ORDER BY updated_at DESC, screen_id DESC LIMIT 100" % _COLS, {"u": user["user_id"]})
    except Exception as e:                                       # noqa: BLE001
        raise _db_trouble(e, user, "loaded")
    return {"screens": [_present(r) for r in rows], "max": MAX_SAVED}


@router.post("/saved")
def save(request: Request, body: dict = Body(...)):
    user = _member(request)
    name, spec = _name(body.get("name")), _spec(body)
    try:
        # one transaction under a lock for this member, so that sixty saves at once cannot all see "29 so far" and slip past the limit of 30
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (user["user_id"],))
            cur.execute("SELECT count(*) AS n FROM custom_screen WHERE user_id = %(u)s", {"u": user["user_id"]})
            if cur.fetchone()["n"] >= MAX_SAVED:
                raise HTTPException(400, "You can keep up to %d screens. Delete one you no longer use first." % MAX_SAVED)
            cur.execute("SELECT 1 AS x FROM custom_screen WHERE user_id = %(u)s AND lower(name) = lower(%(n)s)", {"u": user["user_id"], "n": name})
            if cur.fetchone():
                raise HTTPException(400, "You already have a screen called \"%s\". Choose another name, or change that one." % name)
            cur.execute("INSERT INTO custom_screen (user_id, name, spec, sentence) VALUES (%(u)s, %(n)s, %(s)s::jsonb, %(t)s) RETURNING " + _COLS,
                        {"u": user["user_id"], "n": name, "s": json.dumps(spec), "t": _sentence(spec)})
            made = cur.fetchone()
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:                                       # noqa: BLE001
        raise _db_trouble(e, user, "saved")
    return {"ok": True, "screen": _present(made)}


@router.post("/saved/{screen_id}")
def update(screen_id: int, request: Request, body: dict = Body(...)):
    from api import execute, query
    user = _member(request)
    sets, params = [], {"id": screen_id, "u": user["user_id"]}
    if "name" in body:
        params["n"] = _name(body["name"])
        sets.append("name = %(n)s")
    if "spec" in body:
        spec = _spec(body)
        params["s"], params["t"] = json.dumps(spec), _sentence(spec)
        sets.append("spec = %(s)s::jsonb")
        sets.append("sentence = %(t)s")
    if not sets:
        raise HTTPException(400, "Nothing to change.")
    try:
        if "n" in params and query("SELECT 1 AS x FROM custom_screen WHERE user_id = %(u)s AND lower(name) = lower(%(n)s) AND screen_id <> %(id)s", params, one=True):
            raise HTTPException(400, "You already have a screen called \"%s\"." % params["n"])
        row = execute("UPDATE custom_screen SET %s, updated_at = now() WHERE screen_id = %%(id)s AND user_id = %%(u)s RETURNING screen_id" % ", ".join(sets),
                      params, returning=True)
        if not row:
            raise HTTPException(404, "No such screen.")
        made = query("SELECT %s FROM custom_screen WHERE screen_id = %%(id)s" % _COLS, {"id": screen_id}, one=True)
    except HTTPException:
        raise
    except Exception as e:                                       # noqa: BLE001
        raise _db_trouble(e, user, "changed")
    return {"ok": True, "screen": _present(made)}


@router.post("/saved/{screen_id}/delete")
def delete(screen_id: int, request: Request):
    from api import execute
    user = _member(request)
    try:
        row = execute("DELETE FROM custom_screen WHERE screen_id = %(id)s AND user_id = %(u)s RETURNING screen_id", {"id": screen_id, "u": user["user_id"]}, returning=True)
    except Exception as e:                                       # noqa: BLE001
        raise _db_trouble(e, user, "removed")
    if not row:
        raise HTTPException(404, "No such screen.")
    return {"ok": True}


# ---- "please add this" -----------------------------------------------------------------------------------------------
def known_label(name):
    """The label of a field the builder already has that this request is about ('P/E', 'rsi', 'macd'), or None."""
    k = fr.name_key(name)
    if len(k) < 2:
        return None
    for f in E.FIELDS.values():
        # the key ("pe"), the whole label ("P/E (last 12 months)") and the label without its bracket ("P/E") are all ways to say it
        plain = f["label"].split("(")[0]
        if k in (fr.name_key(f["label"]), fr.name_key(f["key"]), fr.name_key(plain)):
            return f["label"]
    return None


def make_request(user_id, body, query, execute):
    """Take a request for a field the builder lacks. Same rules as feature_requests.make (one row per person per thing, 20 a day, a request for something
    we already have is answered with where it is), but for any kind of data: a technical indicator goes in as an 'indicator' request, the rest as 'data'."""
    if not isinstance(body, dict):
        raise ValueError("The request is not in a form I can read.")
    kind = body.get("kind", "other")
    if not isinstance(kind, str) or kind not in REQUEST_KINDS:
        raise ValueError("Choose what kind of information you need.")
    name = fr._text(body.get("name"), 2, fr.MAX_NAME, "what you need (for example: Ichimoku cloud, ROCE, dividend yield)")
    details = body.get("details")
    details = "" if details in (None, "") else fr._text(details, 0, fr.MAX_DETAILS, "your details")
    nk = fr.name_key(name)
    if not nk:
        raise ValueError("Type what you need.")
    have = known_label(name)
    if have:
        return {"ok": True, "exists": True, "message": "%s is already in the list of things you can use, so there is nothing to request." % have}
    db_kind = REQUEST_KINDS[kind]
    if query("SELECT 1 AS x FROM indicator_request WHERE user_id = %(u)s AND kind = %(k)s AND name_key = %(nk)s", {"u": user_id, "k": db_kind, "nk": nk}, one=True):
        return {"ok": True, "already": True, "message": "You have already asked for this. We have it on the list."}
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(days=1)
    n = (query("SELECT COUNT(*) AS n FROM indicator_request WHERE user_id = %(u)s AND created_at >= %(since)s", {"u": user_id, "since": since}, one=True) or {}).get("n", 0)
    if n >= MAX_REQUESTS_PER_DAY:
        raise OverflowError("That is a lot of requests for one day. Try again tomorrow.")
    tagged = ("[screener builder, %s] %s" % (KIND_WORD[kind], details)).strip()
    execute("INSERT INTO indicator_request (user_id, kind, name, name_key, details) VALUES (%(u)s, %(k)s, %(name)s, %(nk)s, %(d)s) RETURNING request_id",
            {"u": user_id, "k": db_kind, "name": name, "nk": nk, "d": tagged}, returning=True)
    return {"ok": True, "message": "Thank you. We have noted it, and the more people ask for the same thing the sooner it moves up."}


@router.post("/requests")
def request_field(request: Request, body: dict = Body(...)):
    from api import execute, query
    user = _member(request)
    try:
        return make_request(user["user_id"], body, query, execute)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OverflowError as e:
        raise HTTPException(429, str(e))
    except HTTPException:
        raise
    except Exception as e:                                       # noqa: BLE001
        print("mfapi: screen builder request failed: %s: %s" % (type(e).__name__, str(e)[:200]), flush=True)
        raise HTTPException(503, "Requests could not be saved just now because of a problem on the server. Please try again later.")


@router.get("/requests")
def my_requests(request: Request):
    from api import query
    user = _member(request)
    import re
    try:
        rows = fr.mine(query, user["user_id"])
        # the "[screener builder, ...]" tag is for the person reading the list of requests, not for the person who made one
        return {"requests": [dict(r, details=re.sub(r"^\[screener builder[^\]]*\]\s*", "", r.get("details") or "")) for r in rows]}
    except Exception as e:                                       # noqa: BLE001
        print("mfapi: screen builder requests list failed: %s: %s" % (type(e).__name__, str(e)[:200]), flush=True)
        raise HTTPException(503, "Your requests could not be loaded just now.")


@router.get("/requests/all")
def all_requests(request: Request):
    from api import query
    user = _member(request)
    if not _is_admin(user):
        raise HTTPException(403, "This is for administrators.")
    return {"requests": fr.admin_list(query)}


# =====================================================================
# Switching it on without replacing api.py
# =====================================================================
def mount(app):
    """Add the API and the page to a running app. This is what enable_screenbuilder.py appends to the server's api.py, so the feature can go live
    without uploading the laptop's api.py (which carries every other pending change)."""
    app.include_router(router)
    app.include_router(page_router)
