"""
fib_retracement.py -- the "Fibonacci retracement (62-78%)" stock scanner. Mounted by screener_api.py; the bars
come from price_bars.py.
------------------------------------------------------------------------
WHAT IT LOOKS FOR
    A stock that rose from its bottom and has since pulled back about 62% to 78% of that rise (the 61.8% and
    78.6% Fibonacci levels):

        1. BOTTOM   the lowest point in the look-back window
        2. TOP      the highest point after that bottom
        3. RISE     the climb from bottom to top must be big enough to be a rise (20% by default)
        4. PULLBACK the price has since given back 62% to 78% of the climb, give or take a little ("around",
                    not exact: 2 points of leeway by default)

        retracement = (top - price now) / (top - bottom)

    Worked example: bottom 100, top 150 (a rise of 50, or 50%). The 61.8% level is 150 - 0.618 x 50 = 119.1 and the
    78.6% level is 150 - 0.786 x 50 = 110.7. A stock at 115 has given back 70% of the climb and is on the list.
    At 130 it has given back only 40%; at 105, 90%: neither is.

LINE OR CANDLESTICKS
    On a LINE chart the bottom, the top and the price are all closing prices. With CANDLESTICKS the bottom is the
    lowest LOW, the top is the highest HIGH, and a stock qualifies when the latest candle (its low to its high)
    trades inside the zone, with the retracement shown at its close.

NOT ADVICE. It reports where the price stands against a rise that has already happened. A Fibonacci level is
a reference many traders watch, not something the market is bound to respect.
"""

import threading

import price_bars as pb

MIN_BARS = 15
STALE_DAYS = 7
MAX_ROWS = 200
ALL_ROWS = 100000            # a caller inside the site (the screener builder) may ask for every match; the web route caps at MAX_ROWS first

_LOOKBACKS = [[60, "Last 60 bars"], [120, "Last 120 bars"], [250, "Last 250 bars"]]
SCREEN = {
    "key": "fib_retrace", "group": "chart", "name": "Fibonacci retracement (62-78%)",
    "what": "Stocks that rose from their bottom and have since pulled back about 62% to 78% of that rise: the "
            "61.8% and 78.6% Fibonacci levels, give or take a little.",
    "why": "The bottom is the lowest point in the look-back window and the top is the highest point after it. The "
           "retracement is how much of that climb the price has given back. On a line chart those points are "
           "closing prices; with candlesticks they are the lowest low and the highest high, and a stock qualifies "
           "when the latest candle trades inside the zone. “Around” is the leeway you set either side of the "
           "two levels. A Fibonacci level is a reference many traders watch, not a promise: this reports where the "
           "price stands, and says nothing about what comes next.",
    "controls": [
        {"param": "timeframe", "label": "Timeframe", "kind": "choice", "default": "DAILY",
         "options": [["DAILY", "Daily bars"], ["WEEKLY", "Weekly bars"], ["MONTHLY", "Monthly bars"]]},
        {"param": "basis", "label": "Chart", "kind": "choice", "default": "LINE",
         "options": [["LINE", "Line (closing prices)"], ["CANDLE", "Candlesticks (highs and lows)"]]},
        {"param": "lookback", "label": "Look back over", "kind": "choice", "default": 120, "options": _LOOKBACKS},
        {"param": "rise", "label": "Rise from the bottom of at least", "kind": "choice", "default": 20,
         "options": [[10, "10%"], [20, "20%"], [30, "30%"], [50, "50%"]]},
        {"param": "retr_low", "label": "Retraced from (%)", "kind": "number", "default": 61.8,
         "min": 30, "max": 95, "step": 0.1},
        {"param": "retr_high", "label": "to (%)", "kind": "number", "default": 78.6, "min": 35, "max": 99, "step": 0.1},
        {"param": "leeway", "label": "Around (give or take, points)", "kind": "number", "default": 2,
         "min": 0, "max": 10, "step": 0.5},
    ],
}


# =====================================================================
# THE DETECTION (pure: prices in, a setup or None out)
# =====================================================================
def find_setup(highs, lows, closes, lookback, min_rise, candles):
    """The rise-and-pullback in the last `lookback` bars, or None. highs/lows/closes: equal-length sequences,
    oldest first (on a line chart pass the closes for all three and candles=False). Returns the bar indexes of
    the bottom and the top, their prices, the rise as a fraction, and the retracement at the latest bar:
    "retr" at the close, and "retr_deep" / "retr_shallow" at its low and high (the same as "retr" on a line)."""
    n = len(closes)
    if n < 3:
        return None                                  # too short to hold a bottom, a top and a pullback
    start = max(0, n - lookback)
    hi_s = highs if candles else closes
    lo_s = lows if candles else closes
    # the bottom: the lowest point in the window (the latest, if tied)
    bottom_i = min(range(start, n), key=lambda i: (lo_s[i], -i))
    if bottom_i >= n - 2:
        return None                                  # nothing after it: no rise, or no pullback yet
    # the top: the highest point after the bottom (the latest, if tied)
    peak_i = max(range(bottom_i + 1, n), key=lambda i: (hi_s[i], i))
    if peak_i >= n - 1:
        return None                                  # the latest bar IS the top: no pullback yet
    bottom, peak = lo_s[bottom_i], hi_s[peak_i]
    if bottom <= 0 or peak <= bottom:
        return None
    rise = peak / bottom - 1
    if rise < min_rise:
        return None
    span = peak - bottom
    retr = (peak - closes[-1]) / span
    return {"bottom_bar": bottom_i, "peak_bar": peak_i, "bottom": bottom, "peak": peak, "rise": rise,
            "retr": retr,
            "retr_deep": (peak - lows[-1]) / span if candles else retr,
            "retr_shallow": (peak - highs[-1]) / span if candles else retr}


def in_zone(setup, lo, hi, leeway, candles):
    """Is the latest bar inside the retracement zone (all as fractions of the rise)? On a line: the close. On
    candles: the bar's low-to-high range overlaps the zone."""
    zl, zh = lo - leeway, hi + leeway
    if candles:
        return setup["retr_deep"] >= zl and setup["retr_shallow"] <= zh
    return zl <= setup["retr"] <= zh


# =====================================================================
# THE SCREEN
# =====================================================================
_SETUPS = {}                     # (timeframe, candles, lookback, rise) -> {"version", "market_date", "setups", "tally"}
_LOCK = threading.Lock()


def setups_for(bars, timeframe, candles, lookback, rise_pct):
    """Every stock's rise-and-pullback on these bars, whatever the zone: cached with the bars, so changing the
    retracement band or the leeway costs nothing."""
    key = (timeframe, candles, lookback, rise_pct)
    with _LOCK:
        hit = _SETUPS.get(key)
        if hit and hit["version"] == bars["version"]:
            return hit
    out, tally = [], {"tested": 0, "short": 0, "stale": 0, "unadjusted": 0}
    for isin, b in bars["series"].items():
        n = len(b)
        if n < MIN_BARS:
            tally["short"] += 1
            continue
        if (bars["market_date"].toordinal() - b.d[-1]) > STALE_DAYS:
            tally["stale"] += 1
            continue
        if isin in bars["unadjusted"]:
            tally["unadjusted"] += 1
            continue
        tally["tested"] += 1
        s = find_setup(b.h, b.l, b.c, lookback, rise_pct / 100.0, candles)
        if s is not None:
            s["bottom_date"], s["peak_date"], s["newest_bar"] = b.date(s["bottom_bar"]), b.date(s["peak_bar"]), b.date(n - 1)
            s["bars_since_peak"] = n - 1 - s["peak_bar"]
            s["close"] = b.c[-1]
            out.append((isin, s))
    result = {"version": bars["version"], "market_date": bars["market_date"], "setups": out, "tally": tally}
    with _LOCK:
        _SETUPS[key] = result
    return result


def clean_params(raw):
    p = pb.clean_controls(SCREEN["controls"], raw)
    if p["retr_high"] <= p["retr_low"]:                      # an inverted or empty band: back to the two levels
        p["retr_low"], p["retr_high"] = 61.8, 78.6
    return p


def run_screen(cur, raw_params, cap, limit, meta_fn, attach_fn, today=None):
    """The whole answer. meta_fn(cur, isins, when) -> {isin: meta}; attach_fn(cur, rows) adds each row's price."""
    params = clean_params(raw_params)
    tf, candles = params["timeframe"], params["basis"] == "CANDLE"
    lo, hi, leeway = params["retr_low"] / 100.0, params["retr_high"] / 100.0, params["leeway"] / 100.0
    limit = max(1, min(int(limit), ALL_ROWS))
    bars = pb.get_bars(cur, tf, today)
    base = {"screen": SCREEN["key"], "params": params, "timeframe": tf, "basis": params["basis"]}
    if bars is None:
        return {**base, "rows": [], "total": 0, "considered": 0, "as_of": None,
                "unavailable": "No prices are stored for the period this timeframe reads."}
    res = setups_for(bars, tf, candles, int(params["lookback"]), int(params["rise"]))
    hits = [(isin, s) for isin, s in res["setups"] if in_zone(s, lo, hi, leeway, candles)]
    meta = meta_fn(cur, [i for i, _ in hits], bars["market_date"])
    rows = []
    for isin, s in hits:
        m = meta.get(isin)
        if not m or not m.get("company_name"):
            continue
        if cap and m.get("cap_class") != cap:
            continue
        span = s["peak"] - s["bottom"]
        d_lo, d_hi = abs(s["retr"] - lo), abs(s["retr"] - hi)
        close_in = (lo - leeway) <= s["retr"] <= (hi + leeway)
        # which level the close is at; "touch" = on candlesticks the candle reached the zone but the close is outside it
        near = ("touch" if not close_in else
                "low" if d_lo <= d_hi and d_lo <= max(leeway, 0.0) else
                "high" if d_hi < d_lo and d_hi <= max(leeway, 0.0) else "between")
        rows.append({
            "isin": isin, "symbol": m.get("symbol"), "name": m.get("company_name"), "sector": m.get("sector"),
            "cap": m.get("cap_class"), "timeframe": tf, "basis": params["basis"],
            "retracement": round(s["retr"] * 100, 1), "near": near, "close_in_zone": close_in,
            "distance": round(min(d_lo, d_hi) * 100, 1),
            "bottom": round(s["bottom"], 2), "bottom_date": str(s["bottom_date"]),
            "peak": round(s["peak"], 2), "peak_date": str(s["peak_date"]), "rise_pct": round(s["rise"] * 100, 1),
            "level_low": round(s["peak"] - lo * span, 2), "level_high": round(s["peak"] - hi * span, 2),
            "close": round(s["close"], 2), "bars_since_peak": s["bars_since_peak"],
            "newest_bar": str(s["newest_bar"]),
        })
    rows.sort(key=lambda r: (not r["close_in_zone"], r["distance"], r["name"] or ""))
    total = len(rows)
    rows = rows[:limit]
    attach_fn(cur, rows)
    t = res["tally"]
    return {**base, "rows": rows, "total": total, "considered": t["tested"], "as_of": str(bars["market_date"]),
            "with_a_rise": len(res["setups"]),
            "set_aside": {"unadjusted": t["unadjusted"], "stale": t["stale"], "short": t["short"]}}
