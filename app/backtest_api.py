"""
backtest_api.py -- "Back-test your rules".
---------------------------------------------------------------------------
Mounted in api.py with the one block that enable_backtest.py appends:

    import backtest_api
    backtest_api.mount(app)

(api.py's own _mount_optional("backtest_api", ...) also works: it includes
`router`. mount() adds the /backtest page as well, so this module can be
deployed on its own, without any other change to api.py.)

    GET  /backtest                the page
    GET  /api/rule-backtest/access     may I use it? (never an error: the page asks this first)
    GET  /api/rule-backtest/catalog    the indicators, comparisons and limits the page builds from
    GET  /api/rule-backtest/groups     the ready-made stock groups (cap segments, NSE indices)
    POST /api/rule-backtest/describe   the rules in words, or why they cannot be used yet
    POST /api/rule-backtest/run        {isins | group, entry, exit, ...} -> trades, statistics, equity curve

WHAT IT DOES
    A person describes a way of getting into a stock (any mix of price,
    averages, RSI, MACD, Bollinger, ATR, ADX, Supertrend, N-day highs and
    lows, joined with AND / OR in groups) and a way out (a rule, a stop-loss,
    a target, a trailing stop, a holding limit). This replays those rules over
    the daily candles we hold, up to ten years, and reports what they would
    have done: the trades, how often they worked, how large the wins and losses
    were, how it compares with simply holding, how it did year by year, and
    what the price did after each signal whatever the exit was.

    The maths is backtest_engine.py (pure, tested without a database). This
    file does the parts that need the site: who may use it, reading candles
    and corporate actions, and putting the answer in order.

WHO MAY USE IT
    MF_BACKTEST_ACCESS decides, and the DEFAULT IS PREMIUM: part of the Stocks plan.
    (It was administrators-only while the tool was being tested; it was opened to
    Stocks subscribers on 11 Oct 2026 at the owner's request. Setting
    MF_BACKTEST_ACCESS=admin shuts it again.)
      premium     members on a plan that covers stocks (Stocks, or Mutual funds +
                  stocks; not lapsed) and administrators              <- default
      admin       administrators only (the app_admin table)
      signed_in   any signed-in account
    Any other value is read as admin: a typo can only ever shut people out.
    Premium means premium: the account is asked directly (as viewcheck_api does),
    so a paywall switched off by accident cannot open it to the public. The check
    is made on every call because a hidden link protects nothing.
      MF_BACKTEST_ENABLED=false     hard off for everybody, admins too
      MF_BACKTEST_SLOTS             how many run at once (default 2); the rest are
                                    told to try again, not queued behind each other
      MF_BACKTEST_SECONDS           calculation allowed for up to 25 stocks (10)
      MF_BACKTEST_GROUP_SECONDS     ... for a whole group (90)

STOCK GROUPS
    Cap segments come from stock_cap_class, the AMFI list already in the database
    (top 100 by market value = Large, next 150 = Mid, the rest = Small; Small is
    cut to its largest 250 because thousands cannot be tested in one go).
    NSE index lists (Nifty 50, Nifty 100, Midcap 150, ...) come from the table
    index_constituent, filled by load_index_constituents.py from NSE's own
    constituent files. NSE does not publish full constituents anywhere else and
    nothing in the database held them, so until that is loaded only the cap
    segments appear -- and the AMFI groups are NOT the Nifty indices (the Nifty
    ones weigh free-float and balance by sector), so they are never called that.
    Either way the list is TODAY'S members, not who was in it in earlier years,
    which flatters a test over many years; the page says so.
    A group is resolved on the server when the test runs, so the page never
    sends (or stores in a link) a hundred ISINs.

WORDING
    FinChaya is not a SEBI-registered Research Analyst or Investment Adviser.
    Nothing here ranks, rates or recommends: it reports what rules would have
    done in the past, and says so. "Entry" and "exit" describe the mechanics of
    a test, not an instruction.

DATA, AND WHAT THE READER IS TOLD
    * Candles come from stock_ohlc_daily. They are a MIX of adjusted and
      unadjusted history (see the price chart notes in api.py). The same
      confirmed split/bonus adjustment the chart uses (price_adjust.py, a copy
      kept in step by a test) is applied first; any one-day fall of 30% or a rise
      of 60% it cannot explain is flagged, and trades spanning one are set aside.
      If the corporate-action table cannot be read the test still runs, says so,
      and relies on the flagging alone.
    * The history before 1 Aug 2024 came from a feed whose terms restrict
      showing it publicly. The chart is therefore cut there by default
      (api._chart_floor). A 10-year test needs the older candles, so they ARE
      used here for the calculation, but the PRICES of individual trades AND
      the daily buy-and-hold curve (which is the price history, rescaled -- an
      independent review rebuilt every close from it) are withheld from the
      answer unless MF_CHART_FULL_HISTORY=1, the same switch the chart uses.
      WHILE THE TOOL IS ADMIN-ONLY they are shown, because the audience is the
      owner checking the numbers against a chart; the moment access is widened
      (which is now the default: Stocks subscribers) they are withheld again
      unless the switch is set. Dates, returns, the
      rules' own equity curve and all the statistics are always shown. This is a
      holding position until the licence question is settled, not a legal
      opinion.
    * Candles with a stored 0, or an open/high/low far from the same day's
      close, are trimmed before use and the count is reported per stock: one
      such bar inside a trade can otherwise turn a stop into a -100% loss or a
      loser into a target win.
    * The database holds stocks listed today. A stock that was delisted is
      not here, which flatters any test run across many stocks. The page says
      so.
"""

import os
import threading
import time
from datetime import date

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

import backtest_engine as be
import price_adjust as pa

router = APIRouter(prefix="/api/rule-backtest", tags=["rule-backtest"])
page_router = APIRouter()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BACKTEST_ENABLED = os.getenv("MF_BACKTEST_ENABLED", "true").lower() == "true"
ACCESS = os.getenv("MF_BACKTEST_ACCESS", "premium").strip().lower()
_SLOTS = threading.BoundedSemaphore(max(1, int(os.getenv("MF_BACKTEST_SLOTS", "2") or 2)))

STALE_DAYS = 5
_TICK = 1.6          # a candle's open/high/low may sit this far (x or /) from its own open/close
TIME_BUDGET = float(os.getenv("MF_BACKTEST_SECONDS", "10") or 10)
GROUP_TIME_BUDGET = float(os.getenv("MF_BACKTEST_GROUP_SECONDS", "90") or 90)
CHUNK = 20           # stocks read from the database per query; bounds memory for a big group
MAX_GROUP = be.MAX_GROUP_STOCKS
GROUP_TTL = 600      # seconds a resolved group is remembered


# =====================================================================
# WHO MAY USE IT
# =====================================================================
# Same short-lived cache and the same fail-CLOSED stance as viewcheck_api:
# an error leaves the list empty, which can only ever mean fewer people get in.
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
    """is_premium, not lapsed, and on a tier that covers the STOCK tools
    ("stocks" or "all"; no scope on record means everything) -- WITHOUT
    the paywall-off shortcut."""
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
    """admin | premium | signed_in. Anything else is admin: a typo shuts people out."""
    return ACCESS if ACCESS in ("admin", "premium", "signed_in") else "admin"


def allowed(user) -> bool:
    if not BACKTEST_ENABLED or not user:
        return False
    m = mode()
    if m == "signed_in":
        return True
    if m == "premium" and _premium_now(user):
        return True
    return user.get("user_id") in _admin_ids()


def show_prices() -> bool:
    """Trade prices and the holding line: only while the tool is admin-only, or once
    the history licence is settled (MF_CHART_FULL_HISTORY=1). See the module note."""
    return mode() == "admin" or os.getenv("MF_CHART_FULL_HISTORY", "").strip() == "1"


_WHO = {"admin": "administrators", "premium": "subscribers to the Stocks plan and administrators",
        "signed_in": "signed-in members"}


# =====================================================================
# DATA
# =====================================================================
# Prices are cast to float in SQL: NUMERIC arrives as Decimal objects, which are slow to
# build for hundreds of thousands of rows and are converted to float straight away anyway.
_OHLC = """
    SELECT isin, as_of_date, open::float8 AS open, high::float8 AS high, low::float8 AS low,
           close::float8 AS close, volume
    FROM stock_ohlc_daily
    WHERE isin = ANY(%(isins)s) AND close IS NOT NULL AND close > 0
    ORDER BY isin, as_of_date
"""
_LAST_DAY = "SELECT max(as_of_date) AS d FROM stock_ohlc_daily WHERE isin = ANY(%(isins)s)"
_EVENTS = """
    SELECT isin, kind, event_date, headline
    FROM stock_event
    WHERE isin = ANY(%(isins)s) AND kind IN ('split', 'bonus', 'corp_action_other')
    ORDER BY isin, event_date
"""
_NAMES = "SELECT isin, symbol, company_name FROM stock_master WHERE isin = ANY(%(isins)s)"


def _f(v, default=None):
    return float(v) if v is not None else default


def _frame(rows, events):
    """(DataFrame of adjusted daily candles, notes) for one stock.

    Rows with no usable close were dropped in SQL. A missing open/high/low is
    filled with the close and a candle is forced to contain its own open and
    close: a bar that only knows its close is still a day the stock traded, and
    leaving a hole would silently change what "yesterday" means for every rule.
    """
    import pandas as pd

    raw, repaired = [], 0
    for r in rows:
        c = float(r["close"])
        o, hi, lo = _f(r["open"]), _f(r["high"]), _f(r["low"])
        # A stored 0 is a missing value, not a price (NUMERIC allows it and nothing
        # upstream filters it). Left in, a low of 0 turns any stop into a -100% loss.
        bad = o is None or o <= 0 or hi is None or hi <= 0 or lo is None or lo <= 0
        o = o if o is not None and o > 0 else c
        # An open, high or low that is far from the same day's own close is a bad
        # tick (a spiked high would "hit" any target). Trim it to what the day
        # demonstrably did; the factor matches the engine's jump threshold.
        if not (c / _TICK <= o <= c * _TICK):
            o, bad = c, True
        top, bottom = max(o, c), min(o, c)
        hi = hi if hi is not None and hi > 0 else top
        lo = lo if lo is not None and lo > 0 else bottom
        if hi > top * _TICK:
            hi, bad = top, True
        if lo < bottom / _TICK:
            lo, bad = bottom, True
        repaired += bad
        raw.append({"d": r["as_of_date"], "open": o, "high": hi, "low": lo, "close": c,
                    "volume": r["volume"] or 0})
    actions = pa.collect_actions([e for e in events if e["kind"] in ("split", "bonus")])
    applied, _unconfirmed = pa.confirm_actions(actions, raw)
    bars = pa.adjust_bars(raw, applied)

    df = pd.DataFrame(bars)
    df.index = pd.DatetimeIndex(pd.to_datetime(df["d"]))
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    df.index.name = "date"

    notes = []
    for a in applied[-3:]:
        notes.append("Prices before %s are adjusted for the %s." % (pa._fmt_day(a["date"]), a["label"]))
    if len(applied) > 3:
        notes.insert(0, "%d earlier splits or bonus issues were adjusted for as well." % (len(applied) - 3))
    if repaired:
        notes.append("%d daily candle%s had an open, high or low that was zero or far from that day's close, "
                     "and %s trimmed to what the day's open and close show."
                     % (repaired, "" if repaired == 1 else "s", "was" if repaired == 1 else "were"))
    return df, notes


def _settings_lines(spec):
    out = ["%s trades" % ("Long" if spec["direction"] == "long" else "Short"),
           "%s for each stock" % be._inr(spec["capital"]),
           "Last %d year%s" % (spec["years"], "" if spec["years"] == 1 else "s"),
           "Orders filled at the %s" % ("next day's open" if spec["fill"] == "next_open"
                                        else "same day's close"),
           "Cost %g%% on each side" % spec["cost_pct"]]
    return out


def _exit_lines(spec):
    out = []
    if spec["stop_loss_pct"]:
        out.append("Stop-loss: leave if the price moves %g%% against the trade (from the entry price)"
                   % spec["stop_loss_pct"])
    if spec.get("step_every_pct"):
        out.append("Stepped stop: each time the price has risen another %g%% above the entry price, the stop-loss "
                   "moves up %g%% of the entry price (and never back down)"
                   % (spec["step_every_pct"], spec["step_by_pct"]))
    if spec["target_pct"]:
        out.append("Target: leave once the price has moved %g%% in the trade's favour" % spec["target_pct"])
    if spec["trailing_pct"]:
        out.append("Trailing stop: leave if the price gives back %g%% from its best level since entry"
                   % spec["trailing_pct"])
    if spec["max_hold_days"]:
        out.append("Holding limit: leave after %d trading day%s" % (
            spec["max_hold_days"], "" if spec["max_hold_days"] == 1 else "s"))
    return out


# =====================================================================
# STOCK GROUPS
# =====================================================================
# (key, label, AMFI class, how many at most, what to tell the reader)
CAP_GROUPS = (
    ("cap_large", "Large caps", "Large", None,
     "The 100 largest listed companies by market value, as AMFI classes them."),
    ("cap_top50", "Top 50 by size", "Large", 50,
     "The 50 largest listed companies by market value (AMFI). Close to the Nifty 50, but not the same list."),
    ("cap_mid", "Mid caps", "Mid", None,
     "The next 150 companies by market value, as AMFI classes them."),
    ("cap_small", "Small caps", "Small", 250,
     "The largest 250 of AMFI's small caps. There are thousands in all, too many to test at once."),
)

# The NSE lists, biggest picture first, then the rest alphabetically.
_INDEX_ORDER = ("Nifty 50", "Nifty Next 50", "Nifty 100", "Nifty 200", "Nifty 500",
                "Nifty Midcap 100", "Nifty Midcap 150", "Nifty Smallcap 100", "Nifty Smallcap 250")

_CAP_SQL = """
    SELECT c.isin
    FROM stock_cap_class c
    JOIN stock_master m ON m.isin = c.isin
    WHERE c.as_of_period = (SELECT max(as_of_period) FROM stock_cap_class)
      AND c.cap_class = %(cls)s
      AND EXISTS (SELECT 1 FROM stock_ohlc_daily o WHERE o.isin = c.isin)
    ORDER BY c.avg_mcap_cr DESC NULLS LAST, c.isin
"""
_CAP_PERIOD_SQL = "SELECT max(as_of_period) AS p FROM stock_cap_class"
_INDEX_LIST_SQL = "SELECT index_name, count(*) AS n, max(as_of_date) AS as_of FROM index_constituent GROUP BY index_name"
_INDEX_SQL = """
    SELECT i.isin
    FROM index_constituent i
    JOIN stock_master m ON m.isin = i.isin
    WHERE i.index_name = %(name)s
      AND EXISTS (SELECT 1 FROM stock_ohlc_daily o WHERE o.isin = i.isin)
    ORDER BY i.isin
"""

_group_cache = {}


_logged = set()


def _log_once(what, e):
    if what not in _logged:
        _logged.add(what)
        print("mfapi: back-test groups: %s unavailable (%s: %s). Those groups will not be offered."
              % (what, type(e).__name__, str(e).split("\n")[0]), flush=True)


def _fmt(d):
    return pa._fmt_day(d) if d is not None else ""


def _resolve_cap(query, spec):
    key, label, cls, limit, note = spec
    rows = query(_CAP_SQL, {"cls": cls})
    isins = [r["isin"] for r in rows][:limit] if limit else [r["isin"] for r in rows]
    period = (query(_CAP_PERIOD_SQL, one=True) or {}).get("p")
    return {"key": key, "label": label, "isins": isins, "count": len(isins),
            "note": note + (" Classification as of %s." % _fmt(period) if period else "")}


def _resolve_index(query, name, as_of=None):
    rows = query(_INDEX_SQL, {"name": name})
    isins = [r["isin"] for r in rows]
    return {"key": "idx:" + name, "label": name, "isins": isins, "count": len(isins),
            "note": "Today's members of the NSE index %s%s." % (
                name, " (list loaded %s)" % _fmt(as_of) if as_of else "")}


def _cached(name, ttl_ok, ttl_fail, build):
    """build() -> dict, remembered for ttl_ok seconds. If it raises, [] is remembered for
    the shorter ttl_fail and the reason is logged once: a table that does not exist yet
    (index_constituent, before the NSE lists are loaded) must not be queried on every
    page open, and a passing database error must not hide the groups for ten minutes."""
    hit = _group_cache.get(name)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    try:
        out, ttl = build(), ttl_ok
    except Exception as e:                                   # noqa: BLE001
        _log_once(name, e)
        out, ttl = {}, ttl_fail
    _group_cache[name] = (time.monotonic() + ttl, out)
    return out


def _build_caps(query):
    out = {}
    for spec in CAP_GROUPS:
        g = _resolve_cap(query, spec)
        if g["count"]:
            out[g["key"]] = g
    return out


def _build_indices(query):
    found = {r["index_name"]: r for r in query(_INDEX_LIST_SQL)}
    ordered = [n for n in _INDEX_ORDER if n in found] + sorted(n for n in found if n not in _INDEX_ORDER)
    out = {}
    for name in ordered:
        g = _resolve_index(query, name, found[name].get("as_of"))
        if g["count"]:
            out[g["key"]] = g
    return out


def _groups(query):
    """Every group that can be offered, resolved. The lists change a few times a year,
    so each source is remembered for GROUP_TTL seconds."""
    out = dict(_cached("stock_cap_class", GROUP_TTL, 60, lambda: _build_caps(query)))
    out.update(_cached("index_constituent", GROUP_TTL, 120, lambda: _build_indices(query)))
    return out


def _offer(g):
    big = g["count"] > MAX_GROUP
    return {"key": g["key"], "label": g["label"], "count": g["count"], "note": g["note"],
            "too_big": big,
            "why_not": ("%d stocks is more than can be tested in one go (up to %d)." % (g["count"], MAX_GROUP)
                        if big else None)}


# =====================================================================
# ENDPOINTS
# =====================================================================
def _gate(request):
    from api import current_user
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in to use this.")
    if not allowed(user):
        raise HTTPException(403, "This is available to %s." % _WHO[mode()])
    return user


@page_router.get("/backtest", include_in_schema=False)
def page():
    # Served to anyone, like /viewcheck: a static file, and the API behind it is
    # what refuses. It asks /api/rule-backtest/access first and shows a notice.
    path = os.path.join(BASE_DIR, "backtest.html")
    if not os.path.exists(path):
        raise HTTPException(404, "backtest.html is missing from %s" % BASE_DIR)
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})


@router.get("/access")
def access(request: Request):
    """May this visitor use the tool? Never an error: the page asks this before anything
    else, so a signed-out visitor, a member without access and an administrator each get
    the right screen without the page depending on any other script on the site."""
    from api import current_user
    user = current_user(request)
    return {"enabled": BACKTEST_ENABLED, "signed_in": bool(user), "allowed": allowed(user),
            "audience": mode(), "who": _WHO[mode()]}


@router.get("/catalog")
def catalog(request: Request):
    _gate(request)
    c = be.catalog()
    c["show_prices"] = show_prices()
    return c


@router.get("/groups")
def groups(request: Request):
    """Ready-made groups of stocks. Only the ones the database can actually fill."""
    from api import query
    _gate(request)
    return {"groups": [_offer(g) for g in _groups(query).values()],
            "max_group": MAX_GROUP, "max_stocks": be.MAX_STOCKS}


@router.post("/describe")
def describe(request: Request, body: dict = Body(...)):
    """The rules in words, or the reason they cannot be used yet. No database,
    nothing run: the page calls this as the person builds, so a mistake shows
    up beside the condition that has it instead of after pressing Run. A
    problem is an ordinary answer here (ok: false), not an HTTP error."""
    _gate(request)
    try:
        spec = be.clean_spec(body, need_exit=False)
    except be.SpecError as e:
        return {"ok": False, "error": str(e)}
    has_exit = bool(spec["exit"] or spec["stop_loss_pct"] or spec["target_pct"]
                    or spec["trailing_pct"] or spec["max_hold_days"])
    return {"ok": True, "has_exit": has_exit,
            "rules": {"entry": be.describe_tree(spec["entry"]),
                      "exit": be.describe_tree(spec["exit"]) if spec["exit"] else None,
                      "exit_other": _exit_lines(spec), "settings": _settings_lines(spec)}}


def _clean_isins(raw_isins):
    if not isinstance(raw_isins, list) or not raw_isins:
        raise HTTPException(400, "Choose at least one stock.")
    # Cap the raw list BEFORE doing any per-item work: a body of tens of thousands of
    # ISINs would otherwise cost seconds of CPU just to be refused. Duplicates are
    # tolerated (the page can send one twice), hence the allowance over the limit.
    if len(raw_isins) > be.MAX_STOCKS * 4:
        raise HTTPException(400, "Test up to %d stocks at a time, or pick a group." % be.MAX_STOCKS)
    isins = list(dict.fromkeys(str(x or "").strip().upper() for x in raw_isins))
    for x in isins:
        if not be.ISIN_RE.match(x):
            raise HTTPException(400, "%r does not look like an ISIN." % x[:20])
    if len(isins) > be.MAX_STOCKS:
        raise HTTPException(400, "Test up to %d stocks at a time, or pick a group." % be.MAX_STOCKS)
    return isins


@router.post("/run")
def run(request: Request, body: dict = Body(...)):
    # Lazy: api.py imports this module while it is still being built.
    import pandas as pd
    from api import query

    user = _gate(request)

    group = None
    key = body.get("group")
    if key:
        if not isinstance(key, str):
            raise HTTPException(400, "That group is not one I know.")
        try:
            group = _groups(query).get(key)
        except Exception as e:                               # noqa: BLE001
            print("mfapi: back-test could not read the stock groups (%s: %s)"
                  % (type(e).__name__, str(e).split("\n")[0]), flush=True)
            group = None
        if group is None:
            raise HTTPException(400, "That group is not available. Pick another, or choose stocks one by one.")
        if group["count"] > MAX_GROUP:
            raise HTTPException(400, "%s has %d stocks; up to %d can be tested at once."
                                % (group["label"], group["count"], MAX_GROUP))
        isins = list(group["isins"])
    else:
        isins = _clean_isins(body.get("isins"))
    try:
        spec = be.clean_spec(body)
    except be.SpecError as e:
        raise HTTPException(400, str(e))

    if not _SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Other back-tests are running right now. Try again in a few seconds.")
    t0 = time.monotonic()
    run_notes = []
    try:
        names = {r["isin"]: r for r in query(_NAMES, {"isins": isins})}
        missing = [i for i in isins if i not in names]
        if missing and not group:
            raise HTTPException(404, "No stock with ISIN %s." % missing[0])
        isins = [i for i in isins if i in names]          # a group quietly drops anything unlisted
        meta = {i: {"symbol": names[i]["symbol"], "name": names[i]["company_name"], "notes": []} for i in isins}

        last_day = (query(_LAST_DAY, {"isins": isins}, one=True) or {}).get("d") if isins else None
        if last_day is None:
            raise HTTPException(404, "There is no price history for the chosen stock%s."
                                % ("" if len(isins) == 1 else "s"))
        last_day = pd.Timestamp(last_day)
        window_start = last_day - pd.Timedelta(days=int(spec["years"] * 365.25))
        if (date.today() - last_day.date()).days > STALE_DAYS:
            run_notes.append("The newest price held is from %s, so this test stops there."
                             % pa._fmt_day(last_day.date()))
        state = {"no_events": False}

        def events_for(chunk):
            """Corporate actions for a chunk. A missing or unreadable table is not fatal:
            the test runs on unadjusted history, the jump check does what it can, and the
            answer says so."""
            try:
                return query(_EVENTS, {"isins": chunk})
            except Exception as e:                           # noqa: BLE001
                if not state["no_events"]:
                    print("mfapi: back-test could not read stock_event (%s: %s); running without "
                          "split/bonus adjustment" % (type(e).__name__, str(e).split("\n")[0]), flush=True)
                state["no_events"] = True
                return []

        def stream():
            """Stocks one at a time, read CHUNK at a time: a 250-stock group never has more
            than CHUNK stocks' candles in memory."""
            for k in range(0, len(isins), CHUNK):
                chunk = isins[k:k + CHUNK]
                rows_by = {i: [] for i in chunk}
                for r in query(_OHLC, {"isins": chunk}):
                    rows_by[r["isin"]].append(r)
                ev_by = {i: [] for i in chunk}
                for e in events_for(chunk):
                    ev_by[e["isin"]].append(e)
                for i in chunk:
                    m = meta[i]
                    if not rows_by[i]:
                        yield i, pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
                        continue
                    df, notes = _frame(rows_by[i], ev_by[i])
                    m["notes"].extend(notes)
                    if df.index[0] > window_start + pd.Timedelta(days=45):
                        m["notes"].append(
                            "Price history starts on %s, so this stock was tested over a shorter period "
                            "than the %d years chosen." % (pa._fmt_day(df.index[0].date()), spec["years"]))
                    if (last_day - df.index[-1]).days > STALE_DAYS:
                        m["notes"].append(
                            "The latest price held for this stock is from %s." % pa._fmt_day(df.index[-1].date()))
                    yield i, df

        budget = TIME_BUDGET if len(isins) <= be.MAX_STOCKS else GROUP_TIME_BUDGET
        out = be.run_backtest(stream(), spec, meta, window_start, time_budget=budget,
                              keep_curve=(len(isins) == 1))
        if state["no_events"]:
            run_notes.append("The record of splits and bonus issues could not be read just now, so prices were "
                             "not adjusted for them. Any trade that crosses an unexplained jump is set aside "
                             "instead (if that option is on).")
    except be.SpecError as e:
        raise HTTPException(400, str(e))
    finally:
        _SLOTS.release()

    shown = show_prices()
    if not shown:
        for t in out["trades"]:
            t["entry_price"] = None
            t["exit_price"] = None
        # The daily "holding" curve is the stock's own price history, rescaled, and
        # one known price rebuilds every close from it. Withheld along with the trade
        # prices; the holding figures (return, per-year, deepest fall) stay as numbers.
        if out.get("equity"):
            out["equity"].pop("buy_hold", None)

    out["ok"] = True
    out["show_prices"] = shown
    out["notes"] = run_notes
    out["group"] = ({"key": group["key"], "label": group["label"], "count": len(isins), "note": group["note"]}
                    if group else None)
    out["window"] = {"from": window_start.date().isoformat(), "to": last_day.date().isoformat(),
                     "years": spec["years"]}
    out["rules"] = {
        "entry": be.describe_tree(spec["entry"]),
        "exit": be.describe_tree(spec["exit"]) if spec["exit"] else None,
        "exit_other": _exit_lines(spec),
        "settings": _settings_lines(spec),
    }
    out["spec"] = spec
    out["assumptions"] = be.assumptions(spec)
    out["took_ms"] = int((time.monotonic() - t0) * 1000)
    print("mfapi: backtest user=%s %s stocks=%d trades=%d %dms"
          % (user.get("user_id"), ("group=" + group["key"]) if group else "picked", len(isins),
             out["summary"].get("trades", 0), out["took_ms"]), flush=True)
    return out


# =====================================================================
# "ASK US FOR AN INDICATOR"
# =====================================================================
# Not in the list? Say what you need. Stored in indicator_request (the table the trade plan uses too, made by
# create_stock_alert_tables.py or create_trade_plan_tables.py), one row per person per thing, so the owner reads one list of
# what people want, counted by how many asked. Indicators only: a request for another TIME FRAME is not taken here for now.
_ALIASES = {"movingaverage": "sma", "average": "sma", "bollinger": "bb_middle", "bollingerbands": "bb_middle", "bbands": "bb_middle",
            "bb": "bb_middle", "donchian": "highest", "donchianchannel": "highest", "breakout": "highest", "newhigh": "highest",
            "newlow": "lowest", "percentchange": "roc", "rateofchange": "roc", "momentum": "roc", "averagetruerange": "atr",
            "relativestrengthindex": "rsi", "averagedirectionalindex": "adx", "dmi": "adx", "di": "plus_di", "plusdi": "plus_di",
            "minusdi": "minus_di", "movingaverageconvergencedivergence": "macd", "exponentialmovingaverage": "ema",
            "simplemovingaverage": "sma"}


def _known_operand(name):
    """The label of an indicator this tool already has, however the person spelled it; else None."""
    import feature_requests as fr
    k = fr.name_key(name)
    if not k:
        return None
    by_kind = {o["kind"]: o for o in be.OPERANDS if o["kind"] not in ("number",)}
    for o in be.OPERANDS:
        if o["kind"] == "number":
            continue
        if k in (fr.name_key(o["label"]), fr.name_key(o["kind"])):
            return o["label"]
    hit = by_kind.get(_ALIASES.get(k, ""))
    return hit["label"] if hit else None


def _requests_trouble(e, user):
    text = str(e).strip()
    first = text.splitlines()[0][:200] if text else ""
    missing = (getattr(e, "sqlstate", None) == "42P01" or type(e).__name__ == "UndefinedTable"
               or ("does not exist" in text.lower() and "indicator_request" in text.lower()))
    print("mfapi: back-test indicator request failed: %s: %s" % (type(e).__name__, first), flush=True)
    detail = "Requests are not set up on this server yet, so this could not be saved."
    if missing and user.get("user_id") in _admin_ids():
        detail += " Run `python create_stock_alert_tables.py --admin doadmin` on the server (it also makes the request table)."
    elif user.get("user_id") in _admin_ids():
        detail += " (%s: %s)" % (type(e).__name__, first)
    return HTTPException(503, detail)


@router.post("/requests")
def make_request(request: Request, body: dict = Body(...)):
    """Ask for an indicator the builder does not have. Idempotent per person per thing."""
    import feature_requests as fr
    from api import execute, query
    user = _gate(request)
    try:
        out = fr.make(user["user_id"], body, query, execute, known=_known_operand, tag="Back-test")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OverflowError as e:
        raise HTTPException(429, str(e))
    except Exception as e:                                       # noqa: BLE001
        raise _requests_trouble(e, user)
    if not out.get("exists") and not out.get("already"):
        print("mfapi: back-test indicator request user=%s name=%r" % (user["user_id"], str(body.get("name"))[:80]), flush=True)
    return out


@router.get("/requests/mine")
def my_requests(request: Request):
    import feature_requests as fr
    from api import query
    user = _gate(request)
    try:
        return {"requests": fr.mine(query, user["user_id"])}
    except Exception as e:                                       # noqa: BLE001
        raise _requests_trouble(e, user)


@router.get("/requests")
def all_requests(request: Request):
    """Everyone's requests, counted by how many people asked. Administrators only, whatever the audience of the tool is."""
    import feature_requests as fr
    from api import query
    user = _gate(request)
    if user.get("user_id") not in _admin_ids():
        raise HTTPException(403, "This is for administrators.")
    try:
        return {"requests": fr.admin_list(query)}
    except Exception as e:                                       # noqa: BLE001
        raise _requests_trouble(e, user)


def mount(app):
    """Add the page and the API to a FastAPI app. Safe to call twice."""
    if getattr(app, "_backtest_mounted", False):
        return
    app.include_router(router)
    app.include_router(page_router)
    app._backtest_mounted = True
