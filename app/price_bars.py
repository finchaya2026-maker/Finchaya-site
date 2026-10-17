"""
price_bars.py -- daily, weekly and monthly open/high/low/close bars for every stock, read once and kept.
Used by the chart scanners (dow_change.py, fib_retracement.py).
------------------------------------------------------------------------
WHERE THE BARS COME FROM
    stock_ohlc_daily, the same candles the price chart draws. A week is Monday to Sunday and a month is a calendar
    month; a bar's open is the first open in it, its high the highest high, its low the lowest low and its close
    the last close. The CURRENT week or month is included as it stands so far. (The precomputed weekly and monthly
    rows in stock_technical are not used: they hold duplicate parameter sets and snapshots of unfinished bars,
    miss about a third of the weeks and months, and 52 of 2,700 sampled weekly closes disagreed with the candles.)

BAD TICKS
    A stored high far above the day's open and close, or a low far below, would fake a swing in candlestick mode
    (a line chart never sees it). Of 620,000 daily bars in the last 400 days only 3 were affected, but a scanner
    reads all of them, so each bar's high is held to SPIKE times the larger of its open and close, and its low to
    one SPIKEth of the smaller. A bar with no usable open, high or low falls back to its close.

SPLITS AND BONUSES
    The stored history is a mix of adjusted and unadjusted prices. A stock is "unadjusted" when it has a split or
    bonus ex-date in the window AND its stored closes show a step of UNADJUSTED_STEP or more there; such a stock
    cannot be read (a split would fake a fall). Where the stored closes show no step they were adjusted already.

CACHE
    Reading is the slow part (a year of days for 2,500 stocks is about 620,000 rows), so each timeframe is read
    once and kept until a newer trading day is stored (or six hours pass). One request reads while the others
    wait for it. warm_async() starts the reads in the background when somebody opens the screener page.
"""

import threading
import time
from array import array
from datetime import date, timedelta

TIMEFRAMES = ("DAILY", "WEEKLY", "MONTHLY")
# Calendar days read for each timeframe: about a year of days, two of weeks, four of months.
LOOKBACK_DAYS = {"DAILY": 400, "WEEKLY": 740, "MONTHLY": 1500}
SPIKE = 1.4                  # a high above 1.4x the body, or a low below the body / 1.4, is a bad tick
UNADJUSTED_STEP = 1.35       # a split/bonus whose ex-date shows a step this big in the stored closes is unadjusted
CACHE_SECONDS = 6 * 3600
REFERENCE_SYMBOLS = ("RELIANCE", "HDFCBANK", "TCS", "INFY", "ICICIBANK", "SBIN")   # their dates are the trading calendar


class Bars:
    """One stock's bars, oldest first, in compact arrays: d is the date's ordinal, o/h/l/c the prices."""
    __slots__ = ("d", "o", "h", "l", "c")

    def __init__(self):
        self.d, self.o, self.h, self.l, self.c = array("i"), array("d"), array("d"), array("d"), array("d")

    def __len__(self):
        return len(self.c)

    def add(self, day, o, h, l, c):
        self.d.append(day.toordinal())
        self.o.append(o)
        self.h.append(h)
        self.l.append(l)
        self.c.append(c)

    def date(self, i):
        return date.fromordinal(self.d[i])


def clean_bar(o, h, l, c):
    """(o, h, l, c, trimmed) with the high and low held to sane limits, or None when there is no usable close."""
    if c is None or c <= 0:
        return None
    o = o if o and o > 0 else c
    body_hi, body_lo = max(o, c), min(o, c)
    h_raw = h if h and h > 0 else c
    l_raw = l if l and l > 0 else c
    h2 = min(max(h_raw, body_hi), body_hi * SPIKE)
    l2 = max(min(l_raw, body_lo), body_lo / SPIKE)
    return o, h2, l2, c, (h2 != h_raw and h_raw > body_hi) or (l2 != l_raw and l_raw < body_lo)


# =====================================================================
# READING
# =====================================================================
CALENDAR_SQL = """
SELECT DISTINCT o.as_of_date FROM stock_ohlc_daily o
WHERE o.isin = ANY(SELECT isin FROM stock_master WHERE symbol = ANY(%(symbols)s)) AND o.as_of_date >= %(since)s
"""
DAILY_SQL = """
SELECT isin, as_of_date, open, high, low, close FROM stock_ohlc_daily
WHERE as_of_date >= %(since)s AND close IS NOT NULL AND close > 0
ORDER BY isin, as_of_date
"""
# Weekly and monthly bars are built in the database (only the finished bars cross the network). The same limits
# as clean_bar() are applied to each day BEFORE the highs and lows are taken. `unit` is a constant, never input.
PERIOD_SQL = """
WITH d AS (
    SELECT isin, as_of_date, close AS c,
           COALESCE(NULLIF(open, 0), close) AS o,
           LEAST(GREATEST(COALESCE(NULLIF(high, 0), close), close, COALESCE(NULLIF(open, 0), close)),
                 GREATEST(close, COALESCE(NULLIF(open, 0), close)) * {spike}) AS h,
           GREATEST(LEAST(COALESCE(NULLIF(low, 0), close), close, COALESCE(NULLIF(open, 0), close)),
                    LEAST(close, COALESCE(NULLIF(open, 0), close)) / {spike}) AS l
    FROM stock_ohlc_daily WHERE as_of_date >= %(since)s AND close IS NOT NULL AND close > 0
)
SELECT isin, max(as_of_date) AS as_of_date,
       (array_agg(o ORDER BY as_of_date))[1] AS o, max(h) AS h, min(l) AS l,
       (array_agg(c ORDER BY as_of_date DESC))[1] AS c
FROM d GROUP BY isin, date_trunc('{unit}', as_of_date)
ORDER BY isin, 2
"""
# Split and bonus ex-dates in the window with the stored close just before and just after each.
UNADJUSTED_SQL = """
SELECT e.isin, e.event_date,
       (SELECT close FROM stock_ohlc_daily a WHERE a.isin = e.isin AND a.as_of_date < e.event_date - 4
        ORDER BY a.as_of_date DESC LIMIT 1) AS before,
       (SELECT close FROM stock_ohlc_daily b WHERE b.isin = e.isin AND b.as_of_date >= e.event_date + 3
        ORDER BY b.as_of_date LIMIT 1) AS after
FROM (SELECT DISTINCT isin, event_date FROM stock_event
      WHERE kind IN ('split', 'bonus') AND event_date >= %(since)s AND event_date <= %(until)s) e
"""


def _q(cur, sql, params):
    cur.execute(sql, params)
    return cur.fetchall()


def load_bars(cur, timeframe, today=None):
    """{"market_date", "series": {isin: Bars}, "unadjusted": set of isins, "trimmed": bars with a bad tick held back}."""
    today = today or date.today()
    since = today - timedelta(days=LOOKBACK_DAYS[timeframe])
    calendar_dates = [r[0] for r in _run_tuples(cur, CALENDAR_SQL, {"symbols": list(REFERENCE_SYMBOLS), "since": since})]
    if not calendar_dates:
        return None
    series, trimmed = {}, 0
    if timeframe == "DAILY":
        for isin, day, o, h, l, c in _run_tuples(cur, DAILY_SQL, {"since": since}):
            bar = clean_bar(*(float(x) if x is not None else None for x in (o, h, l, c)))
            if bar is None:
                continue
            trimmed += bar[4]
            b = series.get(isin)
            if b is None:
                b = series[isin] = Bars()
            b.add(day, *bar[:4])
    else:
        unit = "week" if timeframe == "WEEKLY" else "month"
        for isin, day, o, h, l, c in _run_tuples(cur, PERIOD_SQL.format(spike=SPIKE, unit=unit), {"since": since}):
            b = series.get(isin)
            if b is None:
                b = series[isin] = Bars()
            b.add(day, float(o), float(h), float(l), float(c))
    unadjusted = set()
    for isin, ex, before, after in _run_tuples(cur, UNADJUSTED_SQL, {"since": since, "until": today}):
        if before and after:
            b_, a_ = float(before), float(after)
            if b_ > 0 and a_ > 0 and max(b_ / a_, a_ / b_) >= UNADJUSTED_STEP:
                unadjusted.add(isin)
    return {"market_date": max(calendar_dates), "series": series, "unadjusted": unadjusted, "trimmed": trimmed}


def _run_tuples(cur, sql, params, chunk=50000):
    """Rows as tuples (a dictionary per row would cost several times the memory on 600,000 rows), in chunks."""
    from psycopg.rows import tuple_row
    old = cur.row_factory
    cur.row_factory = tuple_row
    try:
        cur.execute(sql, params)
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            for r in rows:
                yield r
    finally:
        cur.row_factory = old


# =====================================================================
# THE CACHE
# =====================================================================
_BARS = {}                       # timeframe -> {"bars": ..., "at": time}
_LOCK = threading.Lock()
_VERSION = {"n": 0}              # bumped whenever bars are re-read, so derived caches know to drop


def latest_market_date(cur):
    rows = _q(cur, CALENDAR_SQL, {"symbols": list(REFERENCE_SYMBOLS), "since": date.today() - timedelta(days=10)})
    return max((r["as_of_date"] if isinstance(r, dict) else r[0] for r in rows), default=None)


def get_bars(cur, timeframe, today=None, force=False):
    """The cached bars for a timeframe, read again when a newer trading day has been stored or the cache is old.
    One caller reads while the others wait for it, so a burst of requests costs one read."""
    with _LOCK:
        hit = _BARS.get(timeframe)
        if (hit and not force and time.time() - hit["at"] < CACHE_SECONDS
                and (today is not None or hit["bars"]["market_date"] >= (latest_market_date(cur) or date.min))):
            return hit["bars"]
        bars = load_bars(cur, timeframe, today)
        if bars is None:
            return None
        bars["version"] = _VERSION["n"] = _VERSION["n"] + 1
        _BARS[timeframe] = {"bars": bars, "at": time.time()}
        return bars


def clear():
    with _LOCK:
        _BARS.clear()


def clean_controls(controls, raw):
    """A reader's settings against a screen's catalogue of controls (numbers with a range, choices from a list):
    anything unusable, or outside what is allowed, becomes the default."""
    out = {}
    for c in controls:
        v = raw.get(c["param"])
        if c["kind"] == "number":
            try:
                v = float(v)
                if v != v:                                           # NaN
                    raise ValueError
            except (TypeError, ValueError):
                v = c["default"]
            out[c["param"]] = max(c["min"], min(c["max"], v))
        else:
            allowed = [o[0] for o in c["options"]]
            try:
                v = type(c["default"])(v)
            except (TypeError, ValueError):
                v = c["default"]
            out[c["param"]] = v if v in allowed else c["default"]
    return out


def warm_async(timeframes=TIMEFRAMES):
    """Start reading the bars in the background (called when somebody opens the screener page), one timeframe
    after another, so that the first click on a chart screen does not pay for the read. Harmless if cached."""
    todo = [t for t in timeframes if not (_BARS.get(t) and time.time() - _BARS[t]["at"] < CACHE_SECONDS)]
    if not todo:
        return False

    def work():
        try:
            import psycopg
            from psycopg.rows import dict_row
            from portfolio_api import DB
            with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
                for tf in todo:
                    get_bars(cur, tf)
        except Exception as e:                               # noqa: BLE001  a warm-up must never hurt anything
            print("mfapi: could not warm the price bars: %s: %s" % (type(e).__name__, str(e)[:120]), flush=True)
    threading.Thread(target=work, daemon=True, name="bars-warm").start()
    return True
