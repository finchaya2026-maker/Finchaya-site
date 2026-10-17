"""
multiyear_breakout.py -- the "Multi-year breakout" stock scanner: stocks that have just gone above the highest price of the last
10, 7, 5, 3, 2 or 1 years. Mounted by screener_api.py; the helpers are the same ones the chart screens and the back-test use
(price_bars.py for the bad-tick limits, price_adjust.py for splits and bonuses).
------------------------------------------------------------------------
THE IDEA (as the owner put it: "where the stock has broken the high of the last 10 years, 5 years, 3 years, 2 years, like that")

    A high that stood for years is a price where sellers kept turning up. When a stock goes above it, that ceiling is gone.
    The scan lists the stocks that did it in the last few sessions, and says how many years the ceiling had stood.

THE RULE, for one stock, one number of years N, one chart type, looking at the LATEST bar T
    * the level is the highest price of the N years before the last 20 sessions: from (the date 20 sessions ago minus N
      years) up to the session before those 20.  LINE chart: the highest CLOSE.  CANDLESTICKS: the highest HIGH.
    * the stock "broke" it on the most recent session, among the last 20, that crossed the level: a close above it (line) or a
      high above it (candlesticks), the session before being at or under it. If it crossed, fell back and crossed again, the
      latest crossing is the one reported.
    * the stock is listed for "within the last k sessions" when that crossing is one of the last k bars (k = 1, 2, 3, 5, 10, 20).
    Because the level is fixed before the last 20 sessions, a stock that broke out 8 sessions ago and keeps climbing still shows
    the OLD ceiling it broke (not its own new highs since), with the true day it first went through. A stock that broke out
    more than 20 sessions ago is not listed: it is already a trend, not a breakout.

NESTED YEARS. The 10-year high is also the 7-, 5-, 3-, 2- and 1-year high, so the years control means "at least": ask for 5
and the stocks that broke their 10-year or 7-year high are in the list too, each marked with the LONGEST ceiling it broke and
the shorter ones too.

OTHER FILTERS (each can be switched off)
    still above  the latest close is above the old high, so a break that has already been given back is not listed
    old high set at least ... ago   a ceiling that is only weeks old is the stock's own recent run, not a multi-year resistance
    volume       the breakout session's volume against the average of the 50 sessions before it

HISTORY AND DATA
    The prices are stock_ohlc_daily, about 12 years of daily bars (September 2014 on). A stock needs N years (less a week) of
    history to be tested for N, so a company listed three years ago can be tested for 1, 2 and 3 years only: it is shown in
    the count of stocks "with too little history", never silently treated as having no breakout.
    Splits and bonuses: stored history is a mix of adjusted and unadjusted prices, so the same confirmed adjustment the price
    chart and the back-test use is applied first (price_adjust.py: an action is divided out only where the prices show it).
    Anything that still looks like an unexplained step afterwards (a close 1.8 times, or 1/1.8 of, the day before) makes every
    number of years whose window contains it untestable for that stock; a step in the stored prices would fake or hide a break.
    Bad ticks: each bar's high and low are held to the limits price_bars.clean_bar applies for the other chart scanners.
    Rights issues are not adjusted for, as elsewhere.

THE LICENCE NOTE. The prices before 1 August 2024 came from Zerodha's Kite feed, whose terms restrict showing them to the public
    on another site (api.CHART_FLOOR). So a row whose OLD HIGH was set before that date is listed, with the date it was set,
    but the rupee level and the % above it are withheld ("set Mar 2018, price withheld") unless the reader is an administrator or
    MF_CHART_FULL_HISTORY=1 is set. The listing itself (that the stock went through a ceiling that old) is a finding,
    not a published price; if that is stricter than the licence needs, set the switch.

COST. Reading 10 years of days for 2,500 stocks is about 3.7 million rows, so it is read in batches of stocks and only a few
    numbers per stock are kept (about 1 MB in all), once per trading day. The first request after a new day is stored waits
    for the read; warm_async() starts it when somebody opens the screener page.

NOT ADVICE. It reports that a ceiling was taken out. Breakouts fail as often as they work; this says nothing about what comes next.
"""

import os
import threading
import time
from bisect import bisect_left
from datetime import date, timedelta

import price_adjust as pa
import price_bars as pb

YEARS = (10, 7, 5, 3, 2, 1)                 # the numbers of years the page offers, longest first
RECENT_MAX = 20                             # the level is fixed before the last 20 sessions, so a break can be up to 20 old
RECENT_CHOICES = (1, 2, 3, 5, 10, 20)
READ_DAYS = round(10 * 365.25) + 70         # calendar days of daily bars read: ten years, the 20-session lead and a margin
HISTORY_SLACK = 7                           # days a stock's first bar may fall short of the N years it is tested for
MIN_BARS = 60
STALE_DAYS = 7                              # a stock whose newest bar is older than this (before the market date) is left out
STEP = 1.8                                  # an unexplained one-day move of this size (up, or down as 1/STEP) spoils the window
VOLUME_DAYS = 50
VOLUME_MIN_DAYS = 20
CACHE_SECONDS = 6 * 3600
BATCH = 80
MAX_ROWS = 200
ALL_ROWS = 100000            # a caller inside the site (the screener builder) may ask for every match; the web route caps at MAX_ROWS first
CHART_FLOOR = date(2024, 8, 1)              # kept in step with api.CHART_FLOOR (a test compares the two)

SCREEN = {
    "key": "multiyear_breakout", "group": "chart", "name": "Multi-year breakout",
    "what": "Stocks that have just gone above the highest price of the last 10, 7, 5, 3, 2 or 1 years: a ceiling that stood for "
            "years, taken out in the last few sessions.",
    "why": "A high that stood for years is a price where sellers kept appearing. When the stock closes above it, that ceiling is "
           "gone. Each row shows the longest ceiling it broke (a 10-year high is also a 5-year and a 3-year high), when it broke "
           "it, how old that high was and how far above it the price stands. The level is fixed before the last 20 sessions, so "
           "a stock that broke out last week still shows the old ceiling it went through, not its own new highs. A stock that "
           "broke out earlier than that is a trend by now and is not listed. On a line chart the level is the highest close; "
           "with candlesticks it is the highest high and a wick through it counts. A breakout can fail; this reports that it "
           "happened and says nothing about what comes next.",
    "controls": [
        {"param": "years", "label": "Broke the high of the last (at least)", "kind": "choice", "default": 5,
         "options": [[10, "10 years"], [7, "7 years"], [5, "5 years"], [3, "3 years"], [2, "2 years"], [1, "1 year"]]},
        {"param": "basis", "label": "Chart", "kind": "choice", "default": "LINE",
         "options": [["LINE", "Line (closing prices)"], ["CANDLE", "Candlesticks (highs and lows)"]]},
        {"param": "recent", "label": "Broke it within the last", "kind": "choice", "default": 10,
         "options": [[1, "1 session (the latest)"], [2, "2 sessions"], [3, "3 sessions"], [5, "5 sessions"],
                     [10, "10 sessions"], [20, "20 sessions"]]},
        {"param": "hold", "label": "Price now", "kind": "choice", "default": "above",
         "options": [["above", "Still above the old high"], ["any", "Anywhere (a break given back too)"]]},
        {"param": "age", "label": "The old high was set at least", "kind": "choice", "default": 90,
         "options": [[0, "any time ago"], [90, "3 months ago"], [180, "6 months ago"], [365, "1 year ago"],
                     [730, "2 years ago"]]},
        {"param": "volume", "label": "Volume that session", "kind": "choice", "default": 0.0,
         "options": [[0.0, "any"], [1.5, "1.5x the 50-day average or more"], [2.0, "2x or more"], [3.0, "3x or more"]]},
    ],
}
SCREENS = [SCREEN]
SCREEN_KEYS = {SCREEN["key"]}


def _floor():
    """The first day whose prices may be shown to the public (None once the licence question is settled)."""
    return None if os.getenv("MF_CHART_FULL_HISTORY", "").strip() == "1" else CHART_FLOOR


# =====================================================================
# ONE STOCK (pure: bars in, a few numbers out)
# =====================================================================
def last_step(c):
    """Index of the newest bar whose close is STEP times, or 1/STEP of, the bar before it; -1 when there is none."""
    for i in range(len(c) - 1, 0, -1):
        a, b = c[i - 1], c[i]
        if b >= a * STEP or b * STEP <= a:
            return i
    return -1


def analyse(d, h, c, v, tiers=YEARS):
    """The summary of one stock from its (adjusted) bars, oldest first. d: date ordinals, h: highs, c: closes, v: volumes.
    Returns None when there are too few bars, else
      {"n", "newest" (ordinal), "close", "valid": [N...], "why": {N: "short"|"step"},
       "events": {(basis, N): (level, level_ord, cross_ord, price_at_cross, sessions_ago, volume_ratio_or_None)}}
    An event exists only when the stock crossed the N-year level in the last RECENT_MAX sessions."""
    n = len(c)
    if n < MIN_BARS:
        return None
    lead = n - RECENT_MAX                                           # the first session a break may fall on
    anchor = d[lead]
    step = last_step(c)
    valid, why, starts = [], {}, {}
    for N in sorted(tiers):
        start = anchor - round(N * 365.25)
        if d[0] > start + HISTORY_SLACK:
            why[N] = "short"
            continue
        s = bisect_left(d, start)
        if s < step:                                                # the window contains a jump the prices cannot explain
            why[N] = "step"
            continue
        if lead - s < MIN_BARS // 2:
            why[N] = "short"
            continue
        valid.append(N)
        starts[N] = s
    events = {}
    for basis, P in (("LINE", c), ("CANDLE", h)):
        for N, s in starts.items():
            seg = P[s:lead]
            level = max(seg)
            cross, above = None, False
            for i in range(lead, n):
                up = P[i] > level
                if up and not above:
                    cross = i
                above = up
            if cross is None:
                continue
            level_i = s + len(seg) - 1 - seg[::-1].index(level)   # the latest session that made the level
            ratio = None
            lo = max(0, cross - VOLUME_DAYS)
            if cross - lo >= VOLUME_MIN_DAYS:
                avg = sum(v[lo:cross]) / (cross - lo)
                if avg > 0 and v[cross] is not None:
                    ratio = v[cross] / avg
            events[(basis, N)] = (level, d[level_i], d[cross], P[cross], n - 1 - cross, ratio)
    return {"n": n, "newest": d[-1], "close": c[-1], "valid": valid, "why": why, "events": events}


def clean_ohlc(rows):
    """[(date, open, high, low, close, volume)] raw -> [(date, high, close, volume)] with the bad-tick limits of
    price_bars.clean_bar applied. The database does this itself in BARS_SQL (a test proves the two agree); this is the same rule
    in Python, for the tests and for anyone feeding candles from somewhere else."""
    out = []
    for day, op, hi, lo, cl, vo in rows:
        bar = pb.clean_bar(*(float(x) if x is not None else None for x in (op, hi, lo, cl)))
        if bar is not None:
            out.append((day, bar[1], bar[3], float(vo) if vo is not None else 0.0))
    return out


def summarise(rows, actions):
    """analyse() on one stock's cleaned rows [(date, high, close, volume)], oldest first, after the confirmed split/bonus
    adjustment. `actions` is price_adjust.collect_actions(...) for the stock."""
    d, h, c, v = [], [], [], []
    last = None
    for day, hi, cl, vo in rows:
        if last is not None and day <= last:                        # two rows for one day: keep the first
            continue
        last = day
        d.append(day.toordinal())
        h.append(hi)
        c.append(cl)
        v.append(vo if vo is not None else 0.0)
    if len(c) < MIN_BARS:
        return None
    if actions:
        raw = [{"d": date.fromordinal(d[i]), "open": None, "high": h[i], "low": None, "close": c[i], "volume": v[i]}
               for i in range(len(c))]
        applied, _ = pa.confirm_actions(actions, raw)
        if applied:
            adj = pa.adjust_bars(raw, applied)
            h = [b["high"] for b in adj]
            c = [b["close"] for b in adj]
            v = [b["volume"] for b in adj]
    return analyse(d, h, c, v)


# =====================================================================
# THE READ (once a trading day)
# =====================================================================
ISINS_SQL = """SELECT DISTINCT isin FROM stock_ohlc_daily WHERE as_of_date >= %(recent)s"""
# The high is held to SPIKE times the larger of the open and the close, as price_bars.clean_bar does (and as price_bars.PERIOD_SQL
# does in the database); doing it here keeps three columns, as floats, crossing the wire instead of six, as Decimals.
# `spike` is a constant of ours, never input.
BARS_SQL = """
WITH d AS (
    SELECT isin, as_of_date, close, COALESCE(NULLIF(open, 0), close) AS o, COALESCE(NULLIF(high, 0), close) AS hh, volume
    FROM stock_ohlc_daily
    WHERE isin = ANY(%(isins)s) AND as_of_date >= %(since)s AND close IS NOT NULL AND close > 0
)
SELECT isin, as_of_date, LEAST(GREATEST(hh, close, o), GREATEST(close, o) * {spike})::float8 AS h,
       close::float8 AS c, COALESCE(volume, 0)::float8 AS v
FROM d ORDER BY isin, as_of_date
""".format(spike=pb.SPIKE)
EVENTS_SQL = """
SELECT isin, kind, event_date, headline FROM stock_event
WHERE kind IN ('split', 'bonus') AND isin = ANY(%(isins)s)
"""


def build(cur, today=None):
    """{"market_date", "stocks": {isin: summary}, "stale": n, "short_history": n, "seconds", "bars"} or None without prices."""
    started = time.time()
    today = today or date.today()
    cal = [r[0] for r in pb._run_tuples(cur, pb.CALENDAR_SQL, {"symbols": list(pb.REFERENCE_SYMBOLS),
                                                            "since": today - timedelta(days=15)})]
    if not cal:
        return None
    market_date = max(cal)
    since = market_date - timedelta(days=READ_DAYS)
    isins = sorted(r[0] for r in pb._run_tuples(cur, ISINS_SQL, {"recent": market_date - timedelta(days=45)}))
    stocks, stale, bars_read = {}, 0, 0
    for at in range(0, len(isins), BATCH):
        batch = isins[at:at + BATCH]
        events = {}
        for isin, kind, ex, headline in pb._run_tuples(cur, EVENTS_SQL, {"isins": batch}):
            events.setdefault(isin, []).append({"kind": kind, "event_date": ex, "headline": headline})
        mine, cur_isin = {}, None
        for isin, day, hi, cl, vo in pb._run_tuples(cur, BARS_SQL, {"isins": batch, "since": since}):
            if isin != cur_isin:
                cur_isin, rows = isin, mine.setdefault(isin, [])
            rows.append((day, hi, cl, vo))
            bars_read += 1
        for isin, rows in mine.items():
            if (market_date - rows[-1][0]).days > STALE_DAYS:
                stale += 1
                continue
            s = summarise(rows, pa.collect_actions(events.get(isin, [])))
            if s is not None:
                stocks[isin] = s
        time.sleep(0.02)                                             # let the site's own requests in between batches
    return {"market_date": market_date, "stocks": stocks, "stale": stale, "bars": bars_read,
            "seconds": round(time.time() - started, 1)}


_CACHE = {"data": None, "at": 0.0}
_LOCK = threading.Lock()


def get_data(cur, today=None, force=False):
    """The cached summaries, read again when a newer trading day has been stored or six hours have passed. One caller reads
    while the others wait for it."""
    with _LOCK:
        hit = _CACHE["data"]
        if (hit and not force and time.time() - _CACHE["at"] < CACHE_SECONDS
                and (today is not None or hit["market_date"] >= (pb.latest_market_date(cur) or date.min))):
            return hit
        data = build(cur, today)
        if data is None:
            return None
        _CACHE.update(data=data, at=time.time())
        return data


def clear():
    with _LOCK:
        _CACHE.update(data=None, at=0.0)


def warm_async():
    """Read in the background (called when somebody opens the screener page), so the first click does not pay for it."""
    if _CACHE["data"] and time.time() - _CACHE["at"] < CACHE_SECONDS:
        return False

    def work():
        try:
            import psycopg
            from psycopg.rows import dict_row
            from portfolio_api import DB
            with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
                get_data(cur)
        except Exception as e:                                       # noqa: BLE001  a warm-up must never hurt anything
            print("mfapi: could not warm the multi-year breakout read: %s: %s" % (type(e).__name__, str(e)[:120]), flush=True)
    threading.Thread(target=work, daemon=True, name="breakout-warm").start()
    return True


# =====================================================================
# THE SCREEN
# =====================================================================
def clean_params(raw):
    return pb.clean_controls(SCREEN["controls"], raw)


def passing_tiers(st, p):
    """[(N, event)] of the stock's tiers that pass the reader's filters, longest first."""
    out = []
    for N in YEARS:
        ev = st["events"].get((p["basis"], N))
        if ev is None:
            continue
        level, level_ord, cross_ord, price, ago, ratio = ev
        if ago >= p["recent"]:
            continue
        if p["hold"] == "above" and st["close"] <= level:
            continue
        if p["age"] and st["newest"] - level_ord < p["age"]:
            continue
        if p["volume"] and not (ratio is not None and ratio >= p["volume"]):
            continue
        out.append((N, ev))
    return out


def run_screen(cur, key, raw_params, cap, limit, meta_fn, attach_fn, today=None, show_old=False):
    """The whole answer. meta_fn(cur, isins, when) -> {isin: meta}; attach_fn(cur, rows) adds each row's latest price.
    show_old: the reader may see rupee levels from before CHART_FLOOR (an administrator, or the licence switch)."""
    p = clean_params(raw_params)
    limit = max(1, min(int(limit), ALL_ROWS))
    data = get_data(cur, today)
    base = {"screen": SCREEN["key"], "params": p, "basis": p["basis"], "years": p["years"]}
    if data is None:
        return {**base, "rows": [], "total": 0, "considered": 0, "as_of": None,
                "unavailable": "No prices are stored for the last few weeks."}
    stocks = data["stocks"]
    tested = short = stepped = 0
    for st in stocks.values():
        if p["years"] in st["valid"]:
            tested += 1
        elif st["why"].get(p["years"]) == "step":
            stepped += 1
        else:
            short += 1
    cands = []
    for isin, st in stocks.items():
        tiers = passing_tiers(st, p)
        if tiers:
            cands.append((isin, st, tiers))
    meta = meta_fn(cur, [i for i, _, _ in cands], data["market_date"])
    cands = [(i, st, t) for i, st, t in cands
             if meta.get(i) and meta[i].get("company_name") and (not cap or meta[i].get("cap_class") == cap)]
    by_tier = {N: 0 for N in YEARS}
    for _, _, tiers in cands:
        by_tier[tiers[0][0]] += 1
    floor = None if show_old else _floor()
    rows = []
    for isin, st, tiers in cands:
        longest, ev = tiers[0]
        if longest < p["years"]:
            continue
        level, level_ord, cross_ord, price, ago, ratio = ev
        m = meta[isin]
        hidden = floor is not None and date.fromordinal(level_ord) < floor
        rows.append({
            "isin": isin, "symbol": m.get("symbol"), "name": m.get("company_name"), "sector": m.get("sector"),
            "cap": m.get("cap_class"), "basis": p["basis"],
            "tier": longest, "tiers": [N for N, _ in tiers],
            "level": None if hidden else round(level, 2), "level_hidden": hidden,
            "level_date": str(date.fromordinal(level_ord)), "level_age_days": st["newest"] - level_ord,
            "broke_date": str(date.fromordinal(cross_ord)), "bars_ago": ago, "broke_price": round(price, 2),
            "close": round(st["close"], 2), "still_above": st["close"] > level,
            "pct_above": None if hidden else round((st["close"] / level - 1) * 100, 1),
            "volume_ratio": None if ratio is None else round(ratio, 1),
            "newest_bar": str(date.fromordinal(st["newest"])),
        })
    rows.sort(key=lambda r: (-r["tier"], r["bars_ago"], r["name"] or ""))
    total = len(rows)
    rows = rows[:limit]
    attach_fn(cur, rows)
    return {**base, "rows": rows, "total": total, "considered": tested, "as_of": str(data["market_date"]),
            "by_tier": {str(N): by_tier[N] for N in YEARS},
            "withheld": sum(1 for r in rows if r["level_hidden"]),
            "set_aside": {"short": short, "step": stepped, "stale": data["stale"]}}
