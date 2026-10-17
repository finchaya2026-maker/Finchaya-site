"""
stockalerts_api.py -- "Set alert" on a stock, and the page that lists a member's alerts.
---------------------------------------------------------------------------
Mounted with the one block enable_stockalerts.py appends to the live api.py, or by api.py itself:

    GET  /stock-alerts                       the page (My stock alerts)
    GET  /stockalerts.js                     the "Set alert" window used on the stock page
    GET  /api/stockalerts/access             may I use it? (never an error: the page asks this first)
    GET  /api/stockalerts/catalog            kinds, time frames, indicators and their tests
    GET  /api/stockalerts                    my alerts (optionally ?isin=), and whether email and the checker are really set up
    POST /api/stockalerts/describe           {isin, alert} -> the alert in words, or why it cannot be made (nothing saved)
    POST /api/stockalerts                    {isin, alert} -> make it
    POST /api/stockalerts/{id}/delete        remove one of mine
    POST /api/stockalerts/test-email         email ME a test message (so I know alerts can reach me)

WHAT IT IS
    A member opens a stock, presses "Set alert", chooses WHAT to be told about (a price, a close above a recent high, a
    pullback to a moving average, a change of trend, an indicator, the next results) and fills in the numbers. The
    engine (stockalerts_engine.py, pure) validates it, turns it into one plain sentence, and refuses what could not work
    (already true, or unreadable). stockalerts_checker.py, run by cron, looks again whenever there is new data and sends
    ONE email the first time it happens. This file never sends alerts and never claims to be watching: /api/stockalerts
    reports whether the server has email settings and whether the checker has run lately, and the page puts that in front
    of the person.

WHO MAY USE IT
    Subscribers to the Stocks plan (Stocks, or Mutual funds + stocks; not lapsed) and administrators -- stock_access.py,
    checked on every call. MF_STOCKALERTS_ACCESS=signed_in | everyone | admin changes the audience;
    MF_STOCKALERTS_ENABLED=false is a hard off. Alerts belong to the person who set them: every query is scoped to
    user_id and somebody else's alert is a 404, not a 403.

NEEDS (on the server): stock_access.py, stockalerts_engine.py, stockalerts_checker.py, mailer.py, tradeplan_engine.py,
    tradeplan_api.py (its candle reader), backtest_api.py, backtest_engine.py, price_adjust.py, indicators.py, dow_change.py,
    price_bars.py -- enable_stockalerts.py checks they are there.
"""

import json
import os
import threading
import time
from datetime import date

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

import stock_access
import stockalerts_checker as chk
import stockalerts_engine as se

router = APIRouter(prefix="/api/stockalerts", tags=["stockalerts"])
page_router = APIRouter()
GATE = stock_access.Gate("MF_STOCKALERTS_ACCESS", "MF_STOCKALERTS_ENABLED")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STALE_DAYS = 6                  # the newest daily candle older than this gets a note
TEST_EMAILS_PER_HOUR = 3        # per person

_COLS = ("alert_id, isin, symbol, company_name, kind, spec, sentence, status, note, last_checked_at, last_state, triggered_at, "
         "trigger_reading, trigger_as_of, email_status, email_attempts, email_sent_at, created_at")
_NAMES = "SELECT isin, symbol, company_name FROM stock_master WHERE isin = %(isin)s"


def _is_admin(user):
    return stock_access.is_admin(user)


# =====================================================================
# DATABASE TROUBLE, SAID IN WORDS
# =====================================================================
def _db_trouble(e, user, what):
    """A failed read or write on the alert tables, as words the page can show. The commonest cause on a fresh deploy is that
    create_stock_alert_tables.py has not been run yet. The raw database error is shown to an administrator only."""
    text = str(e).strip()
    first = text.splitlines()[0][:200] if text else ""
    missing = (getattr(e, "sqlstate", None) == "42P01" or type(e).__name__ == "UndefinedTable"
               or "no such table" in text.lower() or ("does not exist" in text.lower() and "stock_alert" in text.lower()))
    print("mfapi: stock alert %s failed: %s: %s" % (what, type(e).__name__, first), flush=True)
    if missing:
        detail = "Alerts are not set up on this server yet: the table that holds them has not been created."
        if _is_admin(user):
            detail += (" Run `python create_stock_alert_tables.py --admin doadmin` on the server (DEPLOY_STOCKALERTS.md), "
                       "then try again. Nothing you set until then is saved.")
    else:
        detail = "Alerts could not be %s because of a problem on the server." % what
        if _is_admin(user):
            detail += " (%s: %s)" % (type(e).__name__, first)
    return HTTPException(503, detail)


# =====================================================================
# READING THE STOCK
# =====================================================================
def _stock(query, isin):
    import backtest_engine as be
    isin = str(isin or "").strip().upper()
    if not be.ISIN_RE.match(isin):
        raise HTTPException(400, "Choose the stock to set the alert on.")
    row = query(_NAMES, {"isin": isin}, one=True)
    if not row:
        raise HTTPException(404, "No stock with that ISIN.")
    return row


def _clean_for(query, stock, raw, today):
    """(spec, frames, ctx, notes) for an alert asked for on `stock`, or an HTTPException(400) saying why not."""
    if not isinstance(raw, dict):
        raise HTTPException(400, "The alert is not in a form I can read.")
    kind = raw.get("kind")
    tfs = {"D"}
    if kind in ("ema_support", "trend_change"):
        tfs.add(raw.get("tf") if raw.get("tf") in ("D", "W", "M") else "D")
    elif kind == "indicator":
        c = raw.get("condition") if isinstance(raw.get("condition"), dict) else raw
        tfs.add(c.get("tf") if c.get("tf") in ("D", "W", "M") else "D")
    try:
        frames, ctx = chk.load_stock(query, stock["isin"], tfs, kind == "results")
    except (ImportError, AttributeError) as e:
        print("mfapi: stock alerts could not read candles: the back-test files on this server look older than alerts need "
              "(%s: %s)" % (type(e).__name__, str(e).split("\n")[0]), flush=True)
        raise HTTPException(503, "The candle reader on this server is older than stock alerts need. Upload the current "
                                 "backtest_api.py, backtest_engine.py, price_adjust.py, indicators.py, tradeplan_api.py and "
                                 "tradeplan_engine.py, then restart the service.")
    try:
        spec = se.clean_spec(raw, frames, today, ctx)
        se.assess_new(spec, frames, today, ctx)
    except se.AlertError as e:
        raise HTTPException(400, str(e))
    notes = []
    d = frames.get("D")
    if d is not None and not isinstance(d, str) and len(d) and (today - d.index[-1].date()).days > STALE_DAYS:
        notes.append("The newest daily price held is from %s, so this stock's recent sessions may be missing."
                     % se.dmy(d.index[-1].date()))
    if spec["kind"] == "trend_change":
        last = se.trend_info(spec, frames, today)
        if last:
            notes.append("This chart's latest change of trend of this kind was on %s. You will be told about the NEXT one."
                         % se.dmy(last))
    if spec["kind"] == "results" and spec["event"] == "filed":
        up = [e for e in ctx.get("events", []) if e["event_date"] >= today]
        if up:
            notes.append("The board meeting to consider results is on %s. We email you once the figures are filed."
                         % se.dmy(min(e["event_date"] for e in up)))
        else:
            notes.append("No results date has been announced yet. We email you when the figures are filed.")
    if spec["kind"] == "price":
        notes.append("It is judged on the day's CLOSE: we hold end-of-day prices, so a touch during the day that does not "
                     "hold at the close does not count.")
    return spec, frames, ctx, notes


def _readiness(query, user):
    """What is actually set up: whether this server can send email at all, and whether the checker has run lately. The raw
    reason mail is not configured names settings, so only an administrator is shown it."""
    import mailer
    cfg = mailer.mail_config()
    return {"email": {"configured": cfg["configured"], "transport": cfg["transport"],
                      "why": cfg["why"] if _is_admin(user) else None},
            "checker": chk.checker_status(query), "max_alerts": chk.MAX_ALERTS, "max_per_stock": chk.MAX_PER_STOCK}


def _last_closes(query, isins):
    if not isins:
        return {}
    rows = query("SELECT DISTINCT ON (isin) isin, close::float8 AS close, as_of_date FROM stock_ohlc_daily "
                 "WHERE isin = ANY(%(i)s) AND close IS NOT NULL ORDER BY isin, as_of_date DESC", {"i": list(isins)})
    return {r["isin"]: (r["close"], r["as_of_date"]) for r in rows}


def _present(row, closes=None):
    spec = chk._j(row["spec"])
    out = {k: row[k] for k in ("alert_id", "isin", "symbol", "company_name", "kind", "sentence", "status", "note",
                               "last_checked_at", "last_state", "triggered_at", "trigger_reading", "trigger_as_of",
                               "email_status", "email_attempts", "email_sent_at", "created_at")}
    out["kind_title"] = se.KIND_TITLE.get(row["kind"], row["kind"])
    # the person sees that the email did not go, not the provider's raw error
    out["email_problem"] = (row["email_status"] == "failed")
    if row["kind"] == "price":
        out["level"] = spec.get("level")
        out["direction"] = spec.get("direction")
    if closes and row["isin"] in closes:
        out["last_close"], out["last_close_date"] = closes[row["isin"]]
    return out


# =====================================================================
# THE PAGE AND THE SCRIPT
# =====================================================================
def _file(name, media):
    path = os.path.join(BASE_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, "%s is missing from %s" % (name, BASE_DIR))
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-cache"})


@page_router.get("/stock-alerts", include_in_schema=False)
def page():
    # Served to anyone: a static file, and the API behind it is what refuses. It asks /api/stockalerts/access first.
    return _file("stockalerts.html", "text/html")


@page_router.get("/stockalerts.js", include_in_schema=False)
def script():
    return _file("stockalerts.js", "application/javascript")


# =====================================================================
# THE ENDPOINTS
# =====================================================================
@router.get("/access")
def access(request: Request):
    return GATE.access(request)


@router.get("/catalog")
def catalog(request: Request):
    GATE.require(request)
    c = se.catalog()
    c["limits"] = {"max_alerts": chk.MAX_ALERTS, "max_per_stock": chk.MAX_PER_STOCK, "max_emails_per_day": chk.MAX_EMAILS_PER_DAY}
    return c


@router.get("")
def my_alerts(request: Request, isin: str = ""):
    from api import query
    user = GATE.require(request)
    sql = "SELECT %s FROM stock_alert WHERE user_id = %%(u)s" % _COLS
    params = {"u": user["user_id"]}
    if isin:
        sql += " AND isin = %(isin)s"
        params["isin"] = isin.strip().upper()
    try:
        rows = query(sql + " ORDER BY created_at DESC, alert_id DESC LIMIT 300", params)
    except Exception as e:                                       # noqa: BLE001
        raise _db_trouble(e, user, "loaded")
    closes = _last_closes(query, {r["isin"] for r in rows})
    return {"alerts": [_present(r, closes) for r in rows], "my_email": user.get("email"), **_readiness(query, user)}


@router.post("/describe")
def describe(request: Request, body: dict = Body(...)):
    """The alert in words, or the reason it cannot be made. Nothing saved. Answers 200 either way so the window can show the
    reason beside the form instead of as an error."""
    from api import query
    user = GATE.require(request)
    stock = _stock(query, body.get("isin"))
    try:
        spec, _f, _c, notes = _clean_for(query, stock, body.get("alert"), date.today())
    except HTTPException as e:
        if e.status_code == 400:
            return {"ok": False, "problem": e.detail}
        raise
    return {"ok": True, "sentence": se.describe(spec), "notes": notes, "stock": {"isin": stock["isin"], "symbol": stock["symbol"],
                                                                                "name": stock["company_name"]}}


@router.post("")
def create(request: Request, body: dict = Body(...)):
    from api import execute, query
    user = GATE.require(request)
    stock = _stock(query, body.get("isin"))
    spec, _f, _c, notes = _clean_for(query, stock, body.get("alert"), date.today())
    sentence = se.describe(spec)

    try:
        mine = query("SELECT alert_id, isin, spec FROM stock_alert WHERE user_id = %(u)s AND status IN ('active', 'paused')",
                     {"u": user["user_id"]})
    except Exception as e:                                       # noqa: BLE001
        raise _db_trouble(e, user, "saved")
    # the same thing already being watched is not made twice (the baseline is the only part that differs between days)
    key = json.dumps({k: v for k, v in spec.items() if k != "baseline"}, sort_keys=True)
    for r in mine:
        if r["isin"] == stock["isin"] and json.dumps({k: v for k, v in chk._j(r["spec"]).items() if k != "baseline"},
                                                     sort_keys=True) == key:
            again = query("SELECT %s FROM stock_alert WHERE alert_id = %%(id)s" % _COLS, {"id": r["alert_id"]}, one=True)
            return {"ok": True, "already": True, "alert": _present(again), "notes": notes,
                    "message": "You already have this alert. We are watching it."}
    if len(mine) >= chk.MAX_ALERTS:
        raise HTTPException(400, "You can keep up to %d alerts waiting. Remove one you no longer need first." % chk.MAX_ALERTS)
    if sum(1 for r in mine if r["isin"] == stock["isin"]) >= chk.MAX_PER_STOCK:
        raise HTTPException(400, "You already have %d alerts on this stock, the most we keep. Remove one first." % chk.MAX_PER_STOCK)

    try:
        row = execute("INSERT INTO stock_alert (user_id, isin, symbol, company_name, kind, spec, sentence) "
                      "VALUES (%(u)s, %(isin)s, %(sym)s, %(cn)s, %(kind)s, %(spec)s::jsonb, %(sent)s) RETURNING alert_id",
                      {"u": user["user_id"], "isin": stock["isin"], "sym": stock["symbol"], "cn": stock["company_name"],
                       "kind": spec["kind"], "spec": json.dumps(spec), "sent": sentence}, returning=True)
        made = query("SELECT %s FROM stock_alert WHERE alert_id = %%(id)s" % _COLS, {"id": row["alert_id"]}, one=True)
    except Exception as e:                                       # noqa: BLE001
        raise _db_trouble(e, user, "saved")
    print("mfapi: stock alert set user=%s %s %s" % (user["user_id"], spec["kind"], stock["isin"]), flush=True)
    return {"ok": True, "already": False, "alert": _present(made), "notes": notes, **_readiness(query, user)}


@router.post("/{alert_id}/delete")
def delete(alert_id: int, request: Request):
    from api import execute
    user = GATE.require(request)
    try:
        row = execute("DELETE FROM stock_alert WHERE alert_id = %(id)s AND user_id = %(u)s RETURNING alert_id",
                      {"id": alert_id, "u": user["user_id"]}, returning=True)
    except Exception as e:                                       # noqa: BLE001
        raise _db_trouble(e, user, "removed")
    if not row:
        raise HTTPException(404, "No such alert.")
    return {"ok": True}


_test_log = {}
_test_lock = threading.Lock()


@router.post("/test-email")
def test_email(request: Request):
    """Email the signed-in member a test message at their own address: the proof that alerts can reach them. Limited to a few
    an hour per person. The member is told whether it went; only an administrator is shown the provider's reason."""
    import mailer
    user = GATE.require(request)
    to = (user.get("email") or "").strip()
    if not to:
        raise HTTPException(400, "Your account has no email address to send to.")
    now = time.monotonic()
    with _test_lock:
        recent = [t for t in _test_log.get(user["user_id"], []) if now - t < 3600]
        if len(recent) >= TEST_EMAILS_PER_HOUR:
            raise HTTPException(429, "That is %d test emails in the last hour. Try again a little later." % TEST_EMAILS_PER_HOUR)
        recent.append(now)
        _test_log[user["user_id"]] = recent
    body = ("Hello,\n\nIf you can read this, FinChaya stock alerts can reach you at this address. When an alert you set "
            "happens, the email will look like this one.\n\nTo see or change your alerts: %s/stock-alerts\n\n%s\n"
            % (chk.site_url(), mailer.DISCLAIMER))
    ok, detail = mailer.send_email(to, "FinChaya test email", body)
    print("mfapi: stock alert test email user=%s ok=%s %s" % (user["user_id"], ok, detail if not ok else ""), flush=True)
    out = {"ok": ok, "to": to}
    if _is_admin(user):
        out["detail"] = detail
    elif not ok:
        out["detail"] = "We could not send it just now. This is on our side, not yours."
    return out


# =====================================================================
# Switching it on without replacing api.py
# =====================================================================
def mount(app):
    """Add the API and the page to a running app. This is what enable_stockalerts.py appends to the server's api.py, so the
    feature can go live without uploading the laptop's api.py (which carries every other pending change)."""
    app.include_router(router)
    app.include_router(page_router)
