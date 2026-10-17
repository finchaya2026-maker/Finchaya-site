"""
dow_change.py -- the "Change in Dow Theory" stock scanners: a downtrend turning up, and (the mirror image) an
uptrend turning down. Mounted by screener_api.py; the bars come from price_bars.py.
------------------------------------------------------------------------
WHAT THE UP SCANNER LOOKS FOR
    A downtrend turning into an uptrend, the way Dow described it. In a downtrend each rally tops out lower (a
    lower high, LH) and each fall ends lower (a lower low, LL). The trend is turning when price:

        1. goes ABOVE the last lower high of the downtrend       (the first higher high, HH)
        2. pulls back and holds ABOVE the low it rose from       (a higher low, HL)
        3. goes above the top of that first rally again          (the second higher high, HH)

    The alert is the bar that does step 3. Worked example, Axis Bank, 9 Oct 2026 (the chart the owner drew on):

        23 Sep  1,243.20  last lower high of the fall                        <- the level to beat
        24 Sep  1,186.50  lower low                                           (downtrend: LH, LL)
         1 Oct  1,217.10  the low the recovery rose from
         6 Oct  1,248.00  closes above 1,243.20: first higher high             (step 1)
         7 Oct  1,242.50  pulls back, above 1,217.10: higher low              (step 2)
         9 Oct  1,259.10  closes above 1,248.00: second higher high  -> ALERT  (step 3)

    Not on 6 Oct: a close above the previous wiggle's top (1,226) would be a "higher high" of the smallest kind,
    but the downtrend's own lower high at 1,243.20 was still unbroken. Judging every swing only against the one
    before it would alert on 6 Oct, a bar early.

THE MIRROR (turning down)
    An uptrend (a higher high, then a higher low) is turning when price goes BELOW the last higher low of the
    rise (the first lower low), bounces but fails to get back above the high it fell from (a lower high), and
    then goes below the bottom of that first fall (the second lower low). It is computed by running the very
    same scan on the negated prices, so the two scanners cannot disagree about a rule.

TWO WAYS TO MEASURE THE TURN (the "measure" control; the big picture is the default)
    The sequence above judges a turn against the LAST lower high of the fall, and with every zig-zag counted that
    can be a tiny wiggle right at the bottom: a stock that has lost 90% over two years can "turn" because it
    topped a 17 rupee bounce. So there are two readings:

    MAJOR  (default) the high the whole fall began from. Within the look-back window the lowest point is the
           bottom of the fall and the highest point BEFORE it is the high the fall began from. A stock is listed
           when (a) it fell at least `fall` per cent from that high to that bottom, (b) it then rallied and pulled
           back to a higher low (a swing low above the bottom, after a swing high), and (c) the price has now gone
           above the high the fall began from. The alert is the first bar that completes all three. On a line
           that is a close above that high; on candlesticks a candle whose HIGH is above it. A stock still below
           that high, or with no pullback low yet, is not listed. Run on the flipped prices for the mirror: the
           lowest point in the window, a rise of at least `fall` per cent, a lower high, and a break below it.
    SWING  the last lower high before the last low (the sequence above), every zig-zag counted at the chosen
           swing size: a short-term reading that fires much more often.

LINE OR CANDLESTICKS
    On a LINE chart only closing prices exist: swings, breaks and failures are all closes. With CANDLESTICKS all
    four prices are used: a swing high is a bar whose HIGH tops its neighbours, a swing low is a bar whose LOW
    undercuts them, a level is broken when a bar's high (or, turning down, low) goes through it, and the
    pattern fails when a bar's low (high) goes through the low (high) the move started from. Wicks count, so the
    two readings are different charts and can disagree either way: a wick can complete a pattern the closes did
    not, or undercut a low and spoil one the closes held (on 9 Oct 2026, daily, 49 stocks alerted turning up on a
    line and 14 on candlesticks).

HOW SWINGS ARE FOUND
    A swing high is a bar higher than the `swing` bars on each side of it; a swing low is lower than them. Swing 1
    is every zig-zag (what the example chart shows); 2 and 3 ignore the small ones. A swing is only known once
    `swing` more bars have closed after it, and the scan walks the bars in order using only what had closed by
    each bar, so an alert is exactly what would have been visible on that day. On a line, runs of equal closes
    count as one level; with candles a flat top counts at its first bar, and a bar that is both a swing high and
    a swing low (an outside bar) is not a swing.

WHAT IS LEFT OUT, AND SAID
    * a stock whose stored prices show a split or bonus that was not adjusted for (it would fake a fall or a rise);
    * a stock whose newest bar is more than 7 days behind the market (no fresh price);
    * a stock with fewer than 15 bars (no trend to turn).
    The page shows the counts.

NOT ADVICE. It reports a pattern that has already happened, and a pattern can fail.
"""

import threading

import price_bars as pb

MIN_BARS = 15                # fewer bars than this and there is no trend to turn
STALE_DAYS = 7               # newest bar further behind the market than this: no fresh price
MAX_ROWS = 200
ALL_ROWS = 100000            # a caller inside the site (the screener builder) may ask for every match; the web route caps at MAX_ROWS first


# =====================================================================
# THE CATALOGUE ENTRIES
# =====================================================================
TIMEFRAME_CONTROL = {"param": "timeframe", "label": "Timeframe", "kind": "choice", "default": "DAILY",
                     "options": [["DAILY", "Daily bars"], ["WEEKLY", "Weekly bars"], ["MONTHLY", "Monthly bars"]]}
BASIS_CONTROL = {"param": "basis", "label": "Chart", "kind": "choice", "default": "LINE",
                 "options": [["LINE", "Line (closing prices)"], ["CANDLE", "Candlesticks (highs and lows)"]]}
DEFAULT_MEASURE = "MAJOR"
_LOOKBACKS = [[400, "All the history read (about 1 year of days, 2 of weeks, 4 of months)"],
              [120, "Last 120 bars"], [60, "Last 60 bars"]]
_SIZES = [[10, "10%"], [20, "20%"], [30, "30%"], [50, "50%"]]


def _controls(direction):
    """The controls of one screen. The two screens take the same parameters; only the wording differs. The
    look-back and the size of the fall belong to the big-picture reading, so the page shows them only then ("when")."""
    up = direction == "up"
    return [
        TIMEFRAME_CONTROL, BASIS_CONTROL,
        {"param": "measure", "kind": "choice", "default": DEFAULT_MEASURE,
         "label": "Trend change when price goes " + ("above" if up else "below"),
         "options": [["MAJOR", "The high the fall began from (big picture)" if up else "The low the rise began from (big picture)"],
                     ["SWING", "The last lower high (every swing, short-term)" if up else "The last higher low (every swing, short-term)"]]},
        {"param": "lookback", "label": "Look back over", "kind": "choice", "default": 400, "options": _LOOKBACKS,
         "when": {"param": "measure", "value": "MAJOR"}},
        {"param": "fall", "label": "The fall must be at least" if up else "The rise must be at least", "kind": "choice",
         "default": 20, "options": _SIZES, "when": {"param": "measure", "value": "MAJOR"}},
        {"param": "recent", "label": "Alert made within the last", "kind": "choice", "default": 1,
         "options": [[1, "1 bar (the latest)"], [2, "2 bars"], [3, "3 bars"], [5, "5 bars"]]},
        {"param": "swing", "label": "Swing size", "kind": "choice", "default": 1,
         "options": [[1, "Every swing (as on the chart)"], [2, "2 bars each side"], [3, "3 bars each side"]]},
    ]


SCREEN_UP = {
    "key": "dow_change", "group": "chart", "name": "Change in Dow Theory: turning up", "direction": "up",
    "what": "A downtrend turning into an uptrend. By default the turn is measured against the high the whole fall "
            "began from: the stock has fallen a long way, rallied and pulled back to a higher low, and the price has "
            "now gone above that high. The short-term setting measures it against the fall's last lower high "
            "instead (a first higher high, a higher low, then a second higher high).",
    "why": "A small bounce at the bottom of a long fall is not a change of trend, so the big-picture reading waits "
           "for the high the fall began from to be taken out, after a higher low. The short-term reading follows "
           "Dow's sequence swing by swing and fires far more often. On a line chart only closing prices count; with "
           "candlesticks the highs and lows do, so the two charts can give different answers. It reports a pattern "
           "that has already happened, and a pattern can fail.",
    "controls": _controls("up"),
}
SCREEN_DOWN = {
    "key": "dow_change_down", "group": "chart", "name": "Change in Dow Theory: turning down", "direction": "down",
    "what": "An uptrend turning into a downtrend. By default the turn is measured against the low the whole rise "
            "began from: the stock has risen a long way, bounced and failed at a lower high, and the price has now "
            "gone below that low. The short-term setting measures it against the rise's last higher low instead "
            "(a first lower low, a lower high, then a second lower low).",
    "why": "The mirror image of the turning-up scan, built from the same rules run on the flipped prices: a small dip "
           "at the top of a long rise is not a change of trend, so the big-picture reading waits for the low the rise "
           "began from to be taken out, after a lower high. The short-term reading follows the sequence swing by swing "
           "and fires far more often. On a line chart only closing prices count; with candlesticks the highs and lows "
           "do. It reports a pattern that has already happened, and a pattern can fail.",
    "controls": _controls("down"),
}
SCREENS = [SCREEN_UP, SCREEN_DOWN]
SCREEN_KEYS = {s["key"] for s in SCREENS}
_BY_KEY = {s["key"]: s for s in SCREENS}


# =====================================================================
# THE DETECTION (pure: prices in, alerts out)
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
    """The swing that becomes known at bar t, as (index, 'H'|'L'), or None. It is the bar k before t: a high
    above the highs of the k bars each side (a low below their lows). With `ties` a flat top counts at its first
    bar (the bars to the right may equal it). A bar that is both is not a swing."""
    i = t - k
    if i < k:
        return None
    hi, lo = H[i], L[i]
    left_h, right_h = H[i - k:i], H[i + 1:i + k + 1]
    left_l, right_l = L[i - k:i], L[i + 1:i + k + 1]
    is_high = all(hi > x for x in left_h) and all((hi >= x) if ties else (hi > x) for x in right_h)
    is_low = all(lo < x for x in left_l) and all((lo <= x) if ties else (lo < x) for x in right_l)
    if is_high == is_low:
        return None                                          # neither, or an outside bar
    return (i, "H") if is_high else (i, "L")


def scan_values(H, L, k=1, ties=False):
    """Every alert in the bars (H = highs, L = lows; the same list on a line chart), as dicts of INDEXES.
    Walks forward using only what had closed by each bar.

    State: none -> down (a lower high then a lower low is on record; `lh` is the high to beat)
                -> broke (a high above `lh`: the first higher high; `peak` is its running top)
                -> hl (the pullback after that top held above `low0`, the low the rise started from)
                -> alert (a high above `peak`), then none until a new downtrend appears."""
    events, pivots = [], []
    state = "none"
    lh = low0 = peak = hl = None             # (index, price) each
    brk = None
    n = len(H)

    for t in range(n):
        s = swing_at(H, L, t, k, ties) if t >= 2 * k else None
        if s is not None:
            idx, kind = s
            price = H[idx] if kind == "H" else L[idx]
            fresh = True
            if pivots and pivots[-1][1] == kind:                     # two of a kind in a row: keep the more extreme
                fresh = False
                if (kind == "H" and price > pivots[-1][2]) or (kind == "L" and price < pivots[-1][2]):
                    pivots[-1] = (idx, kind, price)
            else:
                pivots.append((idx, kind, price))
            if fresh and kind == "L":
                new_down = False
                if len(pivots) >= 4:
                    h_b, l_a, h_a = pivots[-2], pivots[-3], pivots[-4]
                    # a downtrend: the high before this low was LOWER than the high before the previous low,
                    # and this low is lower than the previous low
                    new_down = (h_b[1] == "H" and l_a[1] == "L" and h_a[1] == "H"
                                and h_b[2] < h_a[2] and price < l_a[2])
                if new_down:
                    state, lh, low0, peak, hl, brk = "down", (pivots[-2][0], pivots[-2][2]), (idx, price), None, None, None
                elif state == "down":
                    low0 = (idx, price)                              # the latest low before any break
                elif state in ("broke", "hl") and idx > peak[0]:
                    if price > low0[1]:
                        hl, state = (idx, price), "hl"               # a pullback that holds above where the rise began
                    else:
                        state, low0, peak, hl = "down", (idx, price), None, None

        hi, lo = H[t], L[t]
        if state == "down":
            if t > lh[0] and hi > lh[1]:
                state, peak, brk, hl = "broke", (t, hi), t, None
        elif state == "broke":
            if lo < low0[1]:
                state, peak, hl = "down", None, None
            elif hi > peak[1]:
                peak = (t, hi)
        elif state == "hl":
            if lo < low0[1]:
                state, peak, hl = "down", None, None
            elif hi > peak[1]:
                events.append({"bar": t, "level_broken": lh[1], "level_broken_bar": lh[0], "break_bar": brk,
                               "first_extreme": peak[1], "first_extreme_bar": peak[0], "pullback": hl[1],
                               "pullback_bar": hl[0], "prior_extreme": low0[1], "prior_extreme_bar": low0[0]})
                state, lh, low0, peak, hl, brk = "none", None, None, None, None, None
    return events


def scan_ohlc(dates, highs, lows, closes, swing=1, direction="up", candles=False):
    """The alerts on real bars, as dicts with dates. dates/highs/lows/closes: equal-length sequences, oldest first.
    candles=False reads closing prices only. direction='down' is the mirror: the same scan on the negated prices.
    Each alert: bar (index), date, close, trigger (the price that made the second extreme: the bar's high, or
    low when turning down, on candles; the close on a line), and the four levels of the story with their dates:
    level_broken, first_extreme, pullback, prior_extreme."""
    if candles:
        h, l, where, ties = list(highs), list(lows), None, True
    else:
        h, where = collapse(closes)
        l, ties = h, False
    if direction == "down":
        h, l = [-x for x in l], [-x for x in h]               # a fall in the prices is a rise in these
    sign = -1.0 if direction == "down" else 1.0
    out = []
    for e in scan_values(h, l, swing, ties):
        def at(i):
            return i if where is None else where[i]

        def d(key):
            return dates[at(e[key])]
        out.append({
            "bar": at(e["bar"]), "date": d("bar"), "close": closes[at(e["bar"])], "trigger": sign * h[e["bar"]],
            "level_broken": sign * e["level_broken"], "level_broken_date": d("level_broken_bar"),
            "break_date": d("break_bar"),
            "first_extreme": sign * e["first_extreme"], "first_extreme_date": d("first_extreme_bar"),
            "pullback": sign * e["pullback"], "pullback_date": d("pullback_bar"),
            "prior_extreme": sign * e["prior_extreme"], "prior_extreme_date": d("prior_extreme_bar"),
        })
    return out


def scan(dates, closes, swing=1, direction="up"):
    """The alerts on a line chart (closing prices only)."""
    return scan_ohlc(dates, closes, closes, closes, swing, direction, candles=False)


def scan_major(H, L, lookback, move_min, k=1, sign=1.0):
    """The big-picture trend change in the last `lookback` bars, as a dict of INDEXES, or None (see the module
    docstring). H = highs, L = lows (the same list on a line chart), read as for a downtrend turning up: for the
    mirror pass the negated prices and sign=-1 (so the size of the move is judged on the real prices).

        trough  the lowest low in the window (the latest, if tied)
        peak    the highest high BEFORE the trough: the high the fall began from
        the fall from peak to trough must be at least `move_min` (a fraction of the peak)
        then, after the trough, a swing high and a LATER swing low above the trough (a higher low); the one
        reported is the most recent such pullback low before the break, not an early wiggle at the bottom
        the alert is the first bar from the one that confirms a higher low on whose high is above the peak.

    Only bars up to each bar are used to decide that bar, so the alert is what was visible on its day."""
    n = len(H)
    if n < 3:
        return None
    start = max(0, n - int(lookback))
    trough = min(range(start, n), key=lambda i: (L[i], -i))
    if trough <= start or trough >= n - 1:
        return None                                      # nothing before it to fall from, or nothing after it
    top = max(range(start, trough), key=lambda i: (H[i], i))
    peak, low = H[top], L[trough]
    a, b = sign * peak, sign * low                       # the real prices: where the move began, where it ended
    if a <= 0 or b <= 0:
        return None
    move = abs(b - a) / a
    if move < move_min:
        return None
    high = rally = hl = None                             # (index, price): the latest swing high; the rally that the
    last = None                                          # higher low pulled back from; the higher low; last swing kind
    for t in range(trough + 1, n):
        s = swing_at(H, L, t, k, True) if t >= 2 * k else None
        if s is not None and s[0] > trough:
            idx, kind = s
            if kind == "H":
                if last != "H" or H[idx] > high[1]:      # two highs in a row: keep the higher
                    high = (idx, H[idx])
                last = "H"
            elif high is not None and idx > high[0] and L[idx] > low:
                if last != "L" or hl is None or L[idx] < hl[1]:       # two lows in a row: keep the lower
                    hl, rally = (idx, L[idx]), high
                last = "L"
        # the higher low is the MOST RECENT pullback low before the break (not an early wiggle at the bottom)
        if hl is not None and H[t] > peak:
            return {"bar": t, "level_broken": peak, "level_broken_bar": top, "break_bar": t,
                    "first_extreme": rally[1], "first_extreme_bar": rally[0],
                    "pullback": hl[1], "pullback_bar": hl[0],
                    "prior_extreme": low, "prior_extreme_bar": trough, "move": move}
    return None


def scan_major_ohlc(dates, highs, lows, closes, direction="up", candles=False, lookback=400, move_pct=20, swing=1):
    """scan_major on real bars: a list of zero or one alert, in the same shape as scan_ohlc (plus "move", the size
    of the fall, or of the rise when turning down, as a fraction). On a line the closes are the highs and the lows;
    direction='down' runs the same scan on the negated prices."""
    if candles:
        h, l = list(highs), list(lows)
    else:
        h = l = list(closes)
    sign = 1.0
    if direction == "down":
        h, l, sign = [-x for x in l], [-x for x in h], -1.0
    e = scan_major(h, l, lookback, move_pct / 100.0, swing, sign)
    if e is None:
        return []

    def d(key):
        return dates[e[key]]
    return [{
        "bar": e["bar"], "date": d("bar"), "close": closes[e["bar"]], "trigger": sign * h[e["bar"]],
        "level_broken": sign * e["level_broken"], "level_broken_date": d("level_broken_bar"),
        "break_date": d("break_bar"),
        "first_extreme": sign * e["first_extreme"], "first_extreme_date": d("first_extreme_bar"),
        "pullback": sign * e["pullback"], "pullback_date": d("pullback_bar"),
        "prior_extreme": sign * e["prior_extreme"], "prior_extreme_date": d("prior_extreme_bar"),
        "move": e["move"],
    }]


# =====================================================================
# THE SCREEN
# =====================================================================
_ALERTS = {}                     # (direction, timeframe, swing, candles, measure, lookback, fall) -> {"version", "market_date", "alerts", "tally"}
_LOCK = threading.Lock()


def alerts_for(bars, direction, timeframe, swing, candles, measure="SWING", lookback=400, fall=20):
    """Every stock's alerts on these bars: {"alerts": [(isin, event, bars_ago)], "tally": {...}}. Cached with the
    bars, so a change of the "within the last" control costs nothing. `measure` is "MAJOR" (the high the fall began
    from, using `lookback` bars and a fall of at least `fall` per cent) or "SWING" (the last lower high; the
    look-back and the fall do not apply)."""
    major = measure == "MAJOR"
    key = (direction, timeframe, swing, candles, measure, lookback if major else None, fall if major else None)
    with _LOCK:
        hit = _ALERTS.get(key)
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
        dates = [b.date(i) for i in range(n)]
        found = (scan_major_ohlc(dates, b.h, b.l, b.c, direction, candles, lookback, fall, swing) if major
                 else scan_ohlc(dates, b.h, b.l, b.c, swing, direction, candles))
        for e in found:
            e["bars_ago"] = n - 1 - e["bar"]                  # bars of this timeframe after the alert bar
            out.append((isin, e))
    result = {"version": bars["version"], "market_date": bars["market_date"], "alerts": out, "tally": tally}
    with _LOCK:
        _ALERTS[key] = result
    return result


def clean_params(raw):
    """The reader's controls, clamped to the allowed choices; anything unusable becomes the default."""
    return pb.clean_controls(SCREEN_UP["controls"], raw)


def run_screen(cur, key, raw_params, cap, limit, meta_fn, attach_fn, today=None):
    """The whole answer for one of the two scans. meta_fn(cur, isins, when) -> {isin: meta};
    attach_fn(cur, rows) adds each row's latest price."""
    spec = _BY_KEY[key]
    direction = spec["direction"]
    params = clean_params(raw_params)
    tf, recent, swing, candles = params["timeframe"], params["recent"], params["swing"], params["basis"] == "CANDLE"
    limit = max(1, min(int(limit), ALL_ROWS))
    bars = pb.get_bars(cur, tf, today)
    base = {"screen": key, "params": params, "timeframe": tf, "direction": direction, "basis": params["basis"]}
    if bars is None:
        return {**base, "rows": [], "total": 0, "considered": 0, "as_of": None,
                "unavailable": "No prices are stored for the period this timeframe reads."}
    major = params["measure"] == "MAJOR"
    res = alerts_for(bars, direction, tf, swing, candles, params["measure"], params["lookback"], params["fall"])
    hits = [(isin, e) for isin, e in res["alerts"] if e["bars_ago"] < recent]
    meta = meta_fn(cur, [i for i, _ in hits], bars["market_date"])
    rows = []
    for isin, e in hits:
        m = meta.get(isin)
        if not m or not m.get("company_name"):
            continue                                          # not an equity we know
        if cap and m.get("cap_class") != cap:
            continue
        rows.append({
            "isin": isin, "symbol": m.get("symbol"), "name": m.get("company_name"), "sector": m.get("sector"),
            "cap": m.get("cap_class"), "timeframe": tf, "direction": direction, "bars_ago": e["bars_ago"],
            "alert_date": str(e["date"]), "close": round(e["close"], 2), "trigger": round(e["trigger"], 2),
            "forming": tf != "DAILY" and e["bars_ago"] == 0,
            "level_broken": round(e["level_broken"], 2), "level_broken_date": str(e["level_broken_date"]),
            "break_date": str(e["break_date"]),
            "first_extreme": round(e["first_extreme"], 2), "first_extreme_date": str(e["first_extreme_date"]),
            "pullback": round(e["pullback"], 2), "pullback_date": str(e["pullback_date"]),
            "prior_extreme": round(e["prior_extreme"], 2), "prior_extreme_date": str(e["prior_extreme_date"]),
            "measure": params["measure"], "fall_pct": round(e["move"] * 100, 1) if major else None,
        })
    # newest first, then by how far the alert bar went through the level it had to pass (the more decisive first):
    # the high the fall began from on the big-picture reading, the first higher high on the short-term one
    sgn = 1 if direction == "up" else -1
    ref = "level_broken" if major else "first_extreme"
    rows.sort(key=lambda r: (r["bars_ago"], -sgn * (r["trigger"] / r[ref] - 1), r["name"] or ""))
    total = len(rows)
    rows = rows[:limit]
    attach_fn(cur, rows)
    t = res["tally"]
    return {**base, "rows": rows, "total": total, "considered": t["tested"], "as_of": str(bars["market_date"]),
            "set_aside": {"unadjusted": t["unadjusted"], "stale": t["stale"], "short": t["short"]}}
