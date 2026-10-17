"""
higher_low.py -- "Higher low": stocks whose latest swing low is higher than the swing low before it. Mounted by screener_api.py and offered as a pattern in the
screener builder; the bars come from price_bars.py.
------------------------------------------------------------------------
WHAT IT MEANS
    A swing low is a bar that is lower than the `swing` bars on each side of it; a swing high is the opposite. Walk the bars in order and the swings alternate:
    high, low, high, low ... The LATEST swing low is a HIGHER LOW when it is above the swing low before it:

            H0 ...            (optional: the high before L1)
            L1                the previous swing low
            H                 the swing high after it (the rally the pullback came from)
            L2                the latest swing low: a higher low when L2 > L1

    A stock is listed when
      * L2 is higher than L1 (by at least the "higher by" setting, if one is chosen);
      * the pullback from H down to L2 was at least the "pulled back" setting (a pullback too small to notice is not a pullback);
      * L2 was CONFIRMED within the last N bars (a swing low is only known once `swing` more bars have closed, so this is the day it could first be seen);
      * the price has NOT gone below L2 since (on a line chart: no close below it; on candlesticks: no low below it). A higher low that has already been undercut is
        gone, and the stock is on its way to a lower low;
      * optionally, H was also higher than the high before L1 (H0): a higher high and a higher low, the shape of an uptrend.

    The scan uses only the bars up to each bar, so what it reports is what was visible on that day. On a line chart only closes exist (runs of equal closes count as
    one point); on candlesticks highs and lows are used, so wicks count and the two charts can disagree either way.

WHAT IS LEFT OUT, AND SAID
    * a stock whose stored prices show a split or bonus that was not adjusted for (it would fake a fall);
    * a stock whose newest bar is more than 7 days behind the market (no fresh price);
    * a stock with fewer than 10 bars (not enough for two swing lows).
    The page shows the counts.

NOT ADVICE. It reports a pattern that has already happened, and a pattern can fail.
"""

import threading

import price_bars as pb

MIN_BARS = 10                # two swing lows and the high between them need at least this many bars
STALE_DAYS = 7               # newest bar further behind the market than this: no fresh price
MAX_ROWS = 200
ALL_ROWS = 100000            # a caller inside the site (the screener builder) may ask for every match; the web route caps at MAX_ROWS first

TIMEFRAME_CONTROL = {"param": "timeframe", "label": "Timeframe", "kind": "choice", "default": "DAILY",
                     "options": [["DAILY", "Daily bars"], ["WEEKLY", "Weekly bars"], ["MONTHLY", "Monthly bars"]]}
BASIS_CONTROL = {"param": "basis", "label": "Chart", "kind": "choice", "default": "LINE",
                 "options": [["LINE", "Line (closing prices)"], ["CANDLE", "Candlesticks (highs and lows)"]]}

SCREEN = {
    "key": "higher_low", "group": "chart", "name": "Higher low",
    "what": "Stocks whose latest swing low is higher than the swing low before it: the price pulled back from a high, turned up above where the last pullback ended, and has "
            "not gone below that low since.",
    "why": "A higher low is the first thing an uptrend needs: buyers stepped in at a higher price than last time. It is a swing low, so it is only known a few bars after it "
           "happened (the setting says how many); a pullback that is too small to notice can be filtered with 'pulled back by at least', and 'a higher high came first too' asks "
           "for the full higher-high-and-higher-low shape. A low that has been undercut since is not listed. On a line chart only closing prices count; with candlesticks the "
           "highs and lows do, so the two charts can disagree. It reports a pattern that has already happened, and a pattern can fail.",
    "controls": [
        TIMEFRAME_CONTROL, BASIS_CONTROL,
        {"param": "recent", "label": "Higher low confirmed within the last", "kind": "choice", "default": 3,
         "options": [[1, "1 bar (the latest)"], [2, "2 bars"], [3, "3 bars"], [5, "5 bars"], [10, "10 bars"]]},
        {"param": "swing", "label": "Swing size", "kind": "choice", "default": 2,
         "options": [[1, "Every swing (as on the chart)"], [2, "2 bars each side"], [3, "3 bars each side"]]},
        {"param": "pullback", "label": "Pulled back from the high by at least", "kind": "choice", "default": 5,
         "options": [[0, "Any size"], [3, "3%"], [5, "5%"], [10, "10%"], [15, "15%"]]},
        {"param": "rise", "label": "Higher than the low before by at least", "kind": "choice", "default": 0,
         "options": [[0, "Any amount"], [1, "1%"], [2, "2%"], [5, "5%"], [10, "10%"]]},
        {"param": "trend", "label": "Before it", "kind": "choice", "default": "ANY",
         "options": [["ANY", "Just the higher low"], ["HH", "A higher high came first too (an uptrend)"]]},
    ],
}
SCREEN_KEYS = {SCREEN["key"]}


# =====================================================================
# THE DETECTION (pure: prices in, the latest higher low out)
# =====================================================================
def collapse(closes):
    """Runs of equal closes become one point. Returns (values, original index of the first bar of each run)."""
    vals, where = [], []
    for i, c in enumerate(closes):
        if not vals or c != vals[-1]:
            vals.append(c)
            where.append(i)
    return vals, where


def swing_at(H, L, t, k, ties):
    """The swing that becomes known at bar t, as (index, 'H'|'L'), or None: the bar k before t, a high above the highs of the k bars each side (a low below their lows).
    With `ties` a flat top (or bottom) counts at its first bar. A bar that is both is not a swing."""
    i = t - k
    if i < k:
        return None
    hi, lo = H[i], L[i]
    left_h, right_h = H[i - k:i], H[i + 1:i + k + 1]
    left_l, right_l = L[i - k:i], L[i + 1:i + k + 1]
    is_high = all(hi > x for x in left_h) and all((hi >= x) if ties else (hi > x) for x in right_h)
    is_low = all(lo < x for x in left_l) and all((lo <= x) if ties else (lo < x) for x in right_l)
    if is_high == is_low:
        return None
    return (i, "H") if is_high else (i, "L")


def pivots_of(H, L, k, ties):
    """The alternating swing pivots of these bars, oldest first, each (index, 'H'|'L', price). Two of a kind in a row keep the more extreme. Walks forward, so a pivot is
    exactly what had been confirmed by the last bar."""
    pivots = []
    for t in range(len(H)):
        s = swing_at(H, L, t, k, ties)
        if s is None:
            continue
        idx, kind = s
        price = H[idx] if kind == "H" else L[idx]
        if pivots and pivots[-1][1] == kind:
            if (kind == "H" and price > pivots[-1][2]) or (kind == "L" and price < pivots[-1][2]):
                pivots[-1] = (idx, kind, price)
        else:
            pivots.append((idx, kind, price))
    return pivots


def latest_higher_low(H, L, k=1, ties=False):
    """The latest swing low if it is higher than the one before it, as a dict of INDEXES and prices, else None. H = highs, L = lows (the same list on a line chart).
    The low must not have been undercut since (no later bar's low below it). `conf` is the bar on which the low could first be seen (its bar + k)."""
    pv = pivots_of(H, L, k, ties)
    j = max((i for i, p in enumerate(pv) if p[1] == "L"), default=None)
    if j is None or j < 2 or pv[j - 1][1] != "H" or pv[j - 2][1] != "L":
        return None
    l2, hi, l1 = pv[j], pv[j - 1], pv[j - 2]
    if not l2[2] > l1[2]:
        return None
    if l2[0] + 1 < len(L) and min(L[l2[0] + 1:]) < l2[2]:                      # undercut since
        return None
    out = {"low": l2[2], "low_bar": l2[0], "conf": l2[0] + k, "prev_low": l1[2], "prev_low_bar": l1[0], "high": hi[2], "high_bar": hi[0]}
    if j >= 3 and pv[j - 3][1] == "H":
        out["high0"], out["high0_bar"] = pv[j - 3][2], pv[j - 3][0]
    return out


def scan_ohlc(dates, highs, lows, closes, swing=2, candles=False):
    """The latest higher low on real bars, or None. Returns a dict with the prices and dates, `bars_ago` (bars since it was confirmed), the size of the rise over the previous
    low and of the pullback from the high (as fractions), `held_by` (the latest close over the low, as a fraction) and `up_trend` (a higher high came first too)."""
    n = len(closes)
    if candles:
        h, l, where, ties = list(highs), list(lows), None, True
    else:
        h, where = collapse(closes)
        l, ties = h, False

    def at(i):
        return i if where is None else where[i]
    e = latest_higher_low(h, l, swing, ties)
    if e is None:
        return None
    low = e["low"]
    out = {"low": low, "low_date": dates[at(e["low_bar"])], "prev_low": e["prev_low"], "prev_low_date": dates[at(e["prev_low_bar"])],
           "high": e["high"], "high_date": dates[at(e["high_bar"])], "conf_date": dates[at(e["conf"])], "bars_ago": n - 1 - at(e["conf"]),
           "rise": low / e["prev_low"] - 1, "pullback": (e["high"] - low) / e["high"] if e["high"] > 0 else 0.0, "held_by": closes[-1] / low - 1 if low > 0 else 0.0,
           "close": closes[-1], "up_trend": "high0" in e and e["high"] > e["high0"]}
    if "high0" in e:
        out["high0"], out["high0_date"] = e["high0"], dates[at(e["high0_bar"])]
    return out


# =====================================================================
# THE SCREEN
# =====================================================================
_STATES = {}                     # (timeframe, swing, candles) -> {"version", "found": [(isin, state, bars_ago)], "tally": {...}}
_LOCK = threading.Lock()


def states_for(bars, timeframe, swing, candles):
    """Every stock's latest higher low on these bars (before the size filters): {"found": [(isin, state)], "tally": {...}}. Cached with the bars, so a change of the
    filters costs nothing."""
    key = (timeframe, swing, candles)
    with _LOCK:
        hit = _STATES.get(key)
        if hit and hit["version"] == bars["version"]:
            return hit
    found, tally = [], {"tested": 0, "short": 0, "stale": 0, "unadjusted": 0}
    md = bars["market_date"].toordinal()
    for isin, b in bars["series"].items():
        n = len(b)
        if n < MIN_BARS:
            tally["short"] += 1
            continue
        if md - b.d[-1] > STALE_DAYS:
            tally["stale"] += 1
            continue
        if isin in bars["unadjusted"]:
            tally["unadjusted"] += 1
            continue
        tally["tested"] += 1
        e = scan_ohlc([b.date(i) for i in range(n)], b.h, b.l, b.c, swing, candles)
        if e is not None:
            found.append((isin, e))
    result = {"version": bars["version"], "market_date": bars["market_date"], "found": found, "tally": tally}
    with _LOCK:
        _STATES[key] = result
    return result


def passes(e, recent, pullback, rise, trend):
    """Does this stock's latest higher low meet the reader's settings? (pullback and rise are per cent.)"""
    return (e["bars_ago"] < recent and e["pullback"] * 100 >= pullback - 1e-9 and e["rise"] * 100 >= rise - 1e-9 and (trend != "HH" or e["up_trend"]))


def clean_params(raw):
    """The reader's controls, clamped to the allowed choices; anything unusable becomes the default."""
    return pb.clean_controls(SCREEN["controls"], raw)


def run_screen(cur, key, raw_params, cap, limit, meta_fn, attach_fn, today=None):
    """The whole answer for the screen. meta_fn(cur, isins, when) -> {isin: meta}; attach_fn(cur, rows) adds each row's latest price."""
    params = clean_params(raw_params)
    tf, swing, candles = params["timeframe"], params["swing"], params["basis"] == "CANDLE"
    limit = max(1, min(int(limit), ALL_ROWS))
    bars = pb.get_bars(cur, tf, today)
    base = {"screen": key, "params": params, "timeframe": tf, "basis": params["basis"]}
    if bars is None:
        return {**base, "rows": [], "total": 0, "considered": 0, "as_of": None, "unavailable": "No prices are stored for the period this timeframe reads."}
    res = states_for(bars, tf, swing, candles)
    hits = [(isin, e) for isin, e in res["found"] if passes(e, params["recent"], params["pullback"], params["rise"], params["trend"])]
    meta = meta_fn(cur, [i for i, _ in hits], bars["market_date"])
    rows = []
    for isin, e in hits:
        m = meta.get(isin)
        if not m or not m.get("company_name"):
            continue                                          # not an equity we know
        if cap and m.get("cap_class") != cap:
            continue
        rows.append({
            "isin": isin, "symbol": m.get("symbol"), "name": m.get("company_name"), "sector": m.get("sector"), "cap": m.get("cap_class"), "timeframe": tf,
            "bars_ago": e["bars_ago"], "confirmed_date": str(e["conf_date"]), "forming": tf != "DAILY" and e["bars_ago"] == 0,
            "low": round(e["low"], 2), "low_date": str(e["low_date"]), "prev_low": round(e["prev_low"], 2), "prev_low_date": str(e["prev_low_date"]),
            "high": round(e["high"], 2), "high_date": str(e["high_date"]), "high0": round(e["high0"], 2) if "high0" in e else None,
            "high0_date": str(e["high0_date"]) if "high0_date" in e else None,
            "rise_pct": round(e["rise"] * 100, 1), "pullback_pct": round(e["pullback"] * 100, 1), "held_by_pct": round(e["held_by"] * 100, 1),
            "close": round(e["close"], 2), "up_trend": e["up_trend"],
        })
    # newest first, then the stocks that have since pulled away from the low the most (the low has held, and held well), then by name
    rows.sort(key=lambda r: (r["bars_ago"], -r["rise_pct"], r["name"] or ""))
    total = len(rows)
    rows = rows[:limit]
    attach_fn(cur, rows)
    t = res["tally"]
    return {**base, "rows": rows, "total": total, "considered": t["tested"], "as_of": str(bars["market_date"]),
            "with_a_higher_low": len([1 for _, e in res["found"] if e["bars_ago"] < params["recent"]]),
            "set_aside": {"unadjusted": t["unadjusted"], "stale": t["stale"], "short": t["short"]}}
