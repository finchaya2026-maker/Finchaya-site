"""
ipo_trend.py -- "IPO trend after listing": the two things an IPO's chart does after it lists, as a screen. Mounted by screener_api.py and offered as a pattern in the
screener builder; the bars come from price_bars.py and the listing dates from ipo_universe.py.
------------------------------------------------------------------------
Only stocks that listed as an IPO are looked at (the exchange's listing date), and every swing is read from the LISTING BAR onwards: nothing before the stock traded.

SCENARIO 1 -- "the pullback held" (HOLD)
    The price rose after listing (or after any low), pulled back, and the pullback low was HIGHER than the low the rise started from. The signal is the bar that goes
    above the high before the pullback again: a higher high after a higher low.

        L0  the low the rise started from (often the listing day's low)      -- a swing low
        H1  the top of the first rise                                         -- a swing high, at least `move` per cent above L0
        L1  the pullback low, above L0 (a higher low)                         -- a swing low
        signal: a bar whose high (a close, on a line) goes above H1, with no bar since L1 having gone below L0

    If the pullback goes BELOW L0 instead (a lower low) there is no scenario-1 signal: the rise has failed, and the stock is on the way to scenario 2.

SCENARIO 2 -- "the downtrend broke" (DOWN)
    The price kept falling after listing: lower highs and lower lows. The last lower high of that fall is marked. The signal is the bar that goes above it, AFTER a
    higher low had formed:

        Ha, La   a high and the low after it                                  -- the start of the fall
        Hb       the last lower high (below Ha)                               -- the level to beat
        Lb       the lower low after it (below La)                            -- the bottom, at least `move` per cent under Ha
        R        the bounce from Lb, which stayed BELOW Hb
        HL       the pullback low after R, above Lb (a higher low)
        signal: a bar whose high (a close, on a line) goes above Hb, with no bar since HL having gone below Lb

    If the bounce R had already gone above Hb the downtrend was broken then, and what follows is scenario 1 (the higher low after it, then a new high above R).

A swing high is a bar higher than the `swing` bars on each side of it (a swing low, lower), and is only known once `swing` more bars have closed. The scan walks the bars in order using only what
had closed by each bar, so a signal is exactly what would have been visible that day. Near the listing bar, which has no bars to its left, a swing is judged on the bars it has.
On a LINE chart only closes exist (runs of equal closes count as one point); on CANDLESTICKS highs and lows are used, so wicks count and the two readings can disagree.

TIME FRAMES: daily, weekly and monthly bars (the same bars as every chart screen). Hourly will be added when hourly prices are stored; the scan itself does not care what a bar is.

LEFT OUT, AND SAID: a stock whose stored prices show a split or bonus that was not adjusted for; one with no recent price; one with too few bars since listing; one with no bars near its
listing date (it listed before the prices we read). The page shows the counts.

NOT ADVICE. It reports a pattern that has already happened, and a pattern can fail.
"""

import bisect

import ipo_universe
import price_bars as pb

MIN_BARS = {"DAILY": 6, "WEEKLY": 6, "MONTHLY": 5}     # bars from the listing bar: fewer than this and there is no sequence of swings to read
NEAR_LISTING_DAYS = {"DAILY": 10, "WEEKLY": 14, "MONTHLY": 40}   # the first bar we hold must be this close to the listing date
STALE_DAYS = 7
MAX_ROWS = 200
ALL_ROWS = 100000            # the screener builder may ask for every match; the web route caps at MAX_ROWS first

TIMEFRAME_CONTROL = {"param": "timeframe", "label": "Timeframe", "kind": "choice", "default": "DAILY",
                     "options": [["DAILY", "Daily bars"], ["WEEKLY", "Weekly bars"], ["MONTHLY", "Monthly bars"]]}
BASIS_CONTROL = {"param": "basis", "label": "Chart", "kind": "choice", "default": "LINE",
                 "options": [["LINE", "Line (closing prices)"], ["CANDLE", "Candlesticks (highs and lows)"]]}
SCENARIOS = [["EITHER", "Either scenario"], ["HOLD", "1: price rose, pulled back to a higher low, new high"],
             ["DOWN", "2: downtrend, higher low, last lower high broken"]]
SCENARIO_LABEL = {"HOLD": "Pullback held, new high", "DOWN": "Downtrend broken"}

SCREEN = {
    "key": "ipo_trend", "group": "chart", "name": "IPO trend after listing", "ipo_only": True,
    "what": "IPO stocks only (by the exchange's listing date). Scenario 1: the price rose after listing and pulled back, the pullback made a higher low, and the price has now "
            "gone above the high before the pullback. Scenario 2: the price kept falling after listing; the last lower high is marked, a higher low formed, and the price "
            "has now gone above that last lower high.",
    "why": "Newly listed stocks have no history, so the first swings after listing are the whole chart. A pullback that holds above the low the rise started from, then a new "
           "high, is the way an uptrend confirms itself; a stock that kept falling turns when it makes a higher low and then takes out the last lower high. A pullback below the "
           "starting low is not scenario 1: the rise failed. On a line chart only closing prices count; with candlesticks the highs and lows do, so the two charts can disagree. "
           "It reports a pattern that has already happened, and a pattern can fail.",
    "controls": [
        TIMEFRAME_CONTROL, BASIS_CONTROL,
        {"param": "scenario", "label": "Look for", "kind": "choice", "default": "EITHER", "options": SCENARIOS},
        {"param": "listed_within", "label": "Listed in the last", "kind": "choice", "default": 180,
         "options": [[30, "30 days"], [60, "60 days"], [90, "90 days"], [180, "180 days"], [365, "1 year"], [730, "2 years"]]},
        {"param": "move", "label": "First move at least", "kind": "choice", "default": 5,
         "options": [[0, "Any size"], [3, "3%"], [5, "5%"], [10, "10%"], [20, "20%"]]},
        {"param": "recent", "label": "Signal made within the last", "kind": "choice", "default": 3,
         "options": [[1, "1 bar (the latest)"], [2, "2 bars"], [3, "3 bars"], [5, "5 bars"], [10, "10 bars"]]},
        {"param": "swing", "label": "Swing size", "kind": "choice", "default": 1,
         "options": [[1, "Every swing (as on the chart)"], [2, "2 bars each side"], [3, "3 bars each side"]]},
    ],
}
SCREEN_KEYS = {SCREEN["key"]}


# =====================================================================
# THE DETECTION (pure: prices in, signals out)
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
    Bars before the first one do not exist, so the bars near the start are judged on the bars they have. With `ties` a flat top counts at its first bar. A bar that is both a
    swing high and a swing low (an outside bar) is not a swing."""
    i = t - k
    if i < 0:
        return None
    hi, lo = H[i], L[i]
    left_h, right_h = H[max(0, i - k):i], H[i + 1:i + k + 1]
    left_l, right_l = L[max(0, i - k):i], L[i + 1:i + k + 1]
    is_high = all(hi > x for x in left_h) and all((hi >= x) if ties else (hi > x) for x in right_h)
    is_low = all(lo < x for x in left_l) and all((lo <= x) if ties else (lo < x) for x in right_l)
    if is_high == is_low:
        return None
    return (i, "H") if is_high else (i, "L")


def _first_above(H, level, a, b):
    """The first bar in [a, b] whose high is above `level`, else None."""
    for j in range(a, b + 1):
        if H[j] > level:
            return j
    return None


def scan_values(H, L, k=1, ties=False, move=0.05, want="EITHER"):
    """Every signal in these bars (H = highs, L = lows; the same list on a line chart; index 0 is the listing bar), as dicts of INDEXES. Walks forward using only what had
    closed by each bar. See the module docstring for the two scenarios."""
    events, pivots, fired = [], [], set()
    n = len(H)
    for t in range(n):
        s = swing_at(H, L, t, k, ties)
        if s is not None:
            idx, kind = s
            price = H[idx] if kind == "H" else L[idx]
            if pivots and pivots[-1][1] == kind:                       # two of a kind in a row: keep the more extreme
                if (kind == "H" and price > pivots[-1][2]) or (kind == "L" and price < pivots[-1][2]):
                    pivots[-1] = (idx, kind, price)
            else:
                pivots.append((idx, kind, price))
        if len(pivots) < 3:
            continue
        l0, h1, l1 = pivots[-3], pivots[-2], pivots[-1]
        if not (l0[1] == "L" and h1[1] == "H" and l1[1] == "L" and l1[2] > l0[2] and l0[2] > 0 and l1[0] < t):
            continue
        # was the rise from l0 following a downtrend whose last lower high it has not yet cleared?  (pivots before l0: Ha, La, Hb)
        down_ctx = False
        if len(pivots) >= 6:
            ha, la, hb = pivots[-6], pivots[-5], pivots[-4]
            down_ctx = (ha[1] == "H" and la[1] == "L" and hb[1] == "H" and hb[2] < ha[2] and l0[2] < la[2] and h1[2] < hb[2])
        if down_ctx:
            if want not in ("EITHER", "DOWN") or (ha[2] - l0[2]) / ha[2] < move:
                continue
            key, level, scen = ("DOWN", hb[0], l1[0]), hb, "DOWN"
        else:
            if want not in ("EITHER", "HOLD") or (h1[2] / l0[2] - 1) < move:
                continue
            key, level, scen = ("HOLD", h1[0], l1[0]), h1, "HOLD"
        if key in fired:
            continue
        b = _first_above(H, level[2], l1[0] + 1, t)
        if b is None or min(L[l1[0] + 1:b + 1]) <= l0[2]:               # nothing above the level yet, or the higher low was undercut first
            continue
        fired.add(key)
        ev = {"scenario": scen, "bar": b, "found": t, "trigger": H[b], "level": level[2], "level_bar": level[0], "higher_low": l1[2], "higher_low_bar": l1[0],
              "base": l0[2], "base_bar": l0[0], "first_high": h1[2], "first_high_bar": h1[0]}
        if scen == "DOWN":
            ev.update(top=ha[2], top_bar=ha[0], move=(ha[2] - l0[2]) / ha[2])
        else:
            ev.update(move=h1[2] / l0[2] - 1)
        events.append(ev)
    return events


def scan_ohlc(dates, highs, lows, closes, swing=1, candles=False, move=0.05, want="EITHER"):
    """The signals on real bars, as dicts with dates. dates/highs/lows/closes: equal-length sequences from the LISTING bar on, oldest first. Each signal: scenario, bar (an index
    into these lists), date, close, trigger (the bar's high on candles, its close on a line), level (the price that was gone through) and its date, higher_low and its date, base (the
    low the move started from: L0, or the bottom Lb of the fall) and its date, first_high, move (the size of the first move as a fraction)."""
    if candles:
        h, l, where, ties = list(highs), list(lows), None, True
    else:
        h, where = collapse(closes)
        l, ties = h, False

    def at(i):
        return i if where is None else where[i]
    out = []
    for e in scan_values(h, l, swing, ties, move, want):
        row = {"scenario": e["scenario"], "bar": at(e["bar"]), "date": dates[at(e["bar"])], "close": closes[at(e["bar"])], "trigger": e["trigger"],
               "level": e["level"], "level_date": dates[at(e["level_bar"])], "higher_low": e["higher_low"], "higher_low_date": dates[at(e["higher_low_bar"])],
               "base": e["base"], "base_date": dates[at(e["base_bar"])], "first_high": e["first_high"], "first_high_date": dates[at(e["first_high_bar"])], "move": e["move"]}
        if "top" in e:
            row["top"], row["top_date"] = e["top"], dates[at(e["top_bar"])]
        out.append(row)
    return out


# =====================================================================
# THE SCREEN
# =====================================================================
def clean_params(raw):
    """The reader's controls, clamped to the allowed choices; anything unusable becomes the default."""
    return pb.clean_controls(SCREEN["controls"], raw)


def eligible(bars, listings, timeframe):
    """The IPO stocks these bars can actually be read for: ([(isin, Bars, start)], tally). `start` is the index of the listing bar (the first bar on or after the listing date).
    The tally counts those set aside and why."""
    ok = []
    tally = {"tested": 0, "short": 0, "stale": 0, "unadjusted": 0, "nobars": 0, "older": 0}
    md = bars["market_date"].toordinal()
    min_bars, near = MIN_BARS[timeframe], NEAR_LISTING_DAYS[timeframe]
    for isin, info in listings.items():
        b = bars["series"].get(isin)
        if b is None or len(b) == 0:
            tally["nobars"] += 1
            continue
        lo = info["listing_date"].toordinal()
        start = bisect.bisect_left(b.d, lo)
        if start >= len(b):
            tally["nobars"] += 1
            continue
        if start == 0 and b.d[0] - lo > near:                       # the first bar we hold is long after the stock listed: we cannot see its first swings
            tally["older"] += 1
            continue
        if len(b) - start < min_bars:
            tally["short"] += 1
            continue
        if md - b.d[-1] > STALE_DAYS:
            tally["stale"] += 1
            continue
        if isin in bars["unadjusted"]:
            tally["unadjusted"] += 1
            continue
        tally["tested"] += 1
        ok.append((isin, b, start))
    return ok, tally


def signals_for(bars, listings, timeframe, swing, candles, move, want):
    """Every IPO stock's signals on these bars: {"signals": [(isin, event)], "tally": {...}}. `listings` is {isin: info} (ipo_universe)."""
    out = []
    ok, tally = eligible(bars, listings, timeframe)
    for isin, b, start in ok:
        n = len(b)
        dates = [b.date(i) for i in range(start, n)]
        for e in scan_ohlc(dates, list(b.h[start:]), list(b.l[start:]), list(b.c[start:]), swing, candles, move, want):
            e["bars_ago"] = (n - start) - 1 - e["bar"]
            e["bar_index"] = start + e["bar"]
            out.append((isin, e))
    return {"signals": out, "tally": tally}


def testable(cur, raw_params, today=None):
    """The stocks this screen could TEST with these settings (an IPO inside the window with enough bars, a recent price and no unadjusted split), or None when that cannot be
    told. A stock set aside is neither 'has the pattern' nor 'has not'. The screener builder uses this for 'has NOT happened'."""
    try:
        params = clean_params(raw_params)
        within, lf, lt = ipo_universe.window_from_params({"listed_within": params["listed_within"], "listed_from": (raw_params or {}).get("listed_from"),
                                                          "listed_to": (raw_params or {}).get("listed_to")})
        listed = ipo_universe.select(ipo_universe.listings(cur, today), within, lf, lt, today)
        bars = pb.get_bars(cur, params["timeframe"], today)
        if bars is None:
            return set()
        return {isin for isin, _, _ in eligible(bars, listed, params["timeframe"])[0]}
    except Exception:                                            # noqa: BLE001  not knowing is allowed; guessing is not
        return None


def run_screen(cur, key, raw_params, cap, limit, meta_fn, attach_fn, today=None):
    """The whole answer for the screen. meta_fn(cur, isins, when) -> {isin: meta}; attach_fn(cur, rows) adds each row's latest price."""
    params = clean_params(raw_params)
    tf, recent, swing, candles = params["timeframe"], params["recent"], params["swing"], params["basis"] == "CANDLE"
    limit = max(1, min(int(limit), ALL_ROWS))
    within, lf, lt = ipo_universe.window_from_params({"listed_within": params["listed_within"], "listed_from": (raw_params or {}).get("listed_from"),
                                                      "listed_to": (raw_params or {}).get("listed_to")})
    base = {"screen": key, "params": params, "timeframe": tf, "basis": params["basis"], "scenario": params["scenario"]}
    try:
        everyone = ipo_universe.listings(cur, today)
    except ipo_universe.NotSetUp as e:
        return {**base, "rows": [], "total": 0, "considered": 0, "as_of": None, "unavailable": str(e)}
    listed = ipo_universe.select(everyone, within, lf, lt, today)
    bars = pb.get_bars(cur, tf, today)
    ipo = {"universe": len(listed), "window": ipo_universe.describe(within, lf, lt)}
    if bars is None:
        return {**base, "rows": [], "total": 0, "considered": 0, "as_of": None, "ipo": ipo, "unavailable": "No prices are stored for the period this timeframe reads."}
    res = signals_for(bars, listed, tf, swing, candles, params["move"] / 100.0, params["scenario"])
    hits = [(isin, e) for isin, e in res["signals"] if e["bars_ago"] < recent]
    meta = meta_fn(cur, [i for i, _ in hits], bars["market_date"])
    rows = []
    for isin, e in hits:
        m = meta.get(isin)
        if not m or not m.get("company_name"):
            continue
        if cap and m.get("cap_class") != cap:
            continue
        info = listed[isin]
        rows.append({
            "isin": isin, "symbol": m.get("symbol"), "name": m.get("company_name"), "sector": m.get("sector"), "cap": m.get("cap_class"),
            "timeframe": tf, "bars_ago": e["bars_ago"], "alert_date": str(e["date"]), "forming": tf != "DAILY" and e["bars_ago"] == 0,
            "scenario": e["scenario"], "scenario_label": SCENARIO_LABEL[e["scenario"]],
            "close": round(e["close"], 2), "trigger": round(e["trigger"], 2),
            "level": round(e["level"], 2), "level_date": str(e["level_date"]),
            "higher_low": round(e["higher_low"], 2), "higher_low_date": str(e["higher_low_date"]),
            "base": round(e["base"], 2), "base_date": str(e["base_date"]),
            "first_high": round(e["first_high"], 2), "first_high_date": str(e["first_high_date"]),
            "top": round(e["top"], 2) if "top" in e else None, "top_date": str(e["top_date"]) if "top_date" in e else None,
            "move_pct": round(e["move"] * 100, 1),
            "listing_date": str(info["listing_date"]), "days_listed": (e["date"] - info["listing_date"]).days, "issue_price": info["issue_price"],
            "slug": info["slug"],
        })
    rows.sort(key=lambda r: (r["bars_ago"], r["scenario"], -(r["trigger"] / r["level"] - 1), r["name"] or ""))
    total = len(rows)
    rows = rows[:limit]
    attach_fn(cur, rows)
    t = res["tally"]
    return {**base, "rows": rows, "total": total, "considered": t["tested"], "as_of": str(bars["market_date"]), "ipo": ipo,
            "set_aside": {"unadjusted": t["unadjusted"], "stale": t["stale"], "short": t["short"], "older": t["older"], "nobars": t["nobars"]}}
