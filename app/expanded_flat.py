"""
expanded_flat.py -- the "Expanded flat (A-B-C)" stock scanner. Mounted by screener_api.py; the bars come from
price_bars.py and the swing points from dow_change.py.
------------------------------------------------------------------------
THE PATTERN (an expanded flat inside an uptrend, as the owner described it)

        P0      the top of an uptrend: a swing high that is the highest point of the bars before it
        A       the pullback from P0: a swing low, still above where the uptrend started
        B       the next swing high, HIGHER than P0, and 1.15 to 1.25 times the 0-to-A move above A
        C       the next fall: 1.6 times the 0-to-A move below B (a trend-based Fibonacci extension drawn from
                0 to A to B), which puts it BELOW A

    "The move" is P0 - A. So B = A + (1.15 to 1.25) x move, a little above P0, and C = B - 1.6 x move, well under A.

    Worked example (the shape in the owner's chart): P0 150, A 130 (move 20), B 154 (A + 1.2 x 20, a higher high),
    C 122 (B - 1.6 x 20, below A). The scan lists a stock when C has JUST formed.

WHAT "FORMED" MEANS
    C is the lowest point since B (the lowest low on candlesticks, the lowest close on a line), made within the last
    `recent` bars, with the ratios inside their bands. The price may still be falling or may already have turned: a
    stock that keeps falling past the band drops off the list, one that has not yet reached it is not on it.
    The whole pattern, P0 to C, must fit inside `span` bars (30, 45 or 60). Nothing after B may go above B.

"AROUND, NOT EXACT"
    B's band (1.15 to 1.25) and the 1.6 are targets, not lines. "How exact" sets the give: tight is B +/-0.03 and
    C +/-0.05, normal +/-0.05 and +/-0.10, loose +/-0.10 and +/-0.20 (in multiples of the move).

THE UPTREND
    Within the 60 bars before P0, P0 must be the highest high, the price must have risen at least `rise` per cent
    from the lowest low of those bars to P0, and A must stay above that lowest low (a pullback, not a reversal). At
    least 5 bars of history are needed before P0.

LINE OR CANDLESTICKS
    On a LINE chart every point is a closing price. With CANDLESTICKS P0 and B are highs, A and C are lows, so wicks
    count. The two charts can disagree.

SWINGS are found as in the Dow screens (a bar higher, or lower, than `swing` bars each side; a swing is only known
once `swing` more bars have closed), so the pattern is what was visible on that day.

HISTORY. Bars per timeframe are what price_bars.py reads: about a year of daily bars, two of weekly, four of monthly.
A pattern cannot be longer than the history, so a monthly one is at most about four years.

THE MIRROR (a downtrend). The same shape upside down: a fall to a bottom (0), a bounce to A (still below where the
downtrend started), a LOWER low at B (1.15 to 1.25 times the 0-to-A move below A), then a rally to C about 1.6 times
the move above B (so above A). It runs the very same scan on the negated prices, so the two cannot disagree about a
rule; only the size of the trend (a fall of at least `rise` per cent from the highest high of the 60 bars before 0) is
judged on the real prices. Worked example: 0 at 100, A at 120 (move 20), B at 96 (A - 1.2 x 20, a lower low),
C at 128 (B + 1.6 x 20, above A).

NOT ADVICE. It reports a shape that has formed. A Fibonacci extension is a reference many traders watch, not
something the market is bound to respect.
"""

import threading

import price_bars as pb
from dow_change import swing_at

MIN_BARS = 15
STALE_DAYS = 7
MAX_ROWS = 200
ALL_ROWS = 100000            # a caller inside the site (the screener builder) may ask for every match; the web route caps at MAX_ROWS first
UPTREND_BARS = 60            # how far back to look for the rise into point 0
MIN_UP_HISTORY = 5           # bars needed before point 0 to speak of an uptrend
RECENT_MAX = 10              # the longest "C made within the last" the page offers
EXACT = {"tight": (0.03, 0.05), "normal": (0.05, 0.10), "loose": (0.10, 0.20)}    # give on B's band, give on C
DEFAULTS = {"b_low": 1.15, "b_high": 1.25, "c_ratio": 1.6}

def _controls(direction):
    """The controls of one screen. The two screens take the same parameters; only the wording differs."""
    up = direction == "up"
    return [
        {"param": "timeframe", "label": "Timeframe", "kind": "choice", "default": "DAILY",
         "options": [["DAILY", "Daily bars"], ["WEEKLY", "Weekly bars"], ["MONTHLY", "Monthly bars"]]},
        {"param": "basis", "label": "Chart", "kind": "choice", "default": "LINE",
         "options": [["LINE", "Line (closing prices)"], ["CANDLE", "Candlesticks (highs and lows)"]]},
        {"param": "b_low", "kind": "number", "default": DEFAULTS["b_low"], "min": 1.0, "max": 2.0, "step": 0.01,
         "label": "B is at least (x the first pullback, above A)" if up else "B is at least (x the first bounce, below A)"},
        {"param": "b_high", "label": "and at most", "kind": "number",
         "default": DEFAULTS["b_high"], "min": 1.0, "max": 2.5, "step": 0.01},
        {"param": "c_ratio", "kind": "number", "default": DEFAULTS["c_ratio"], "min": 1.2, "max": 3.0, "step": 0.01,
         "label": "C is about (x the first pullback, below B)" if up else "C is about (x the first bounce, above B)"},
        {"param": "exact", "label": "How exact", "kind": "choice", "default": "normal",
         "options": [["tight", "Tight"], ["normal", "Normal"], ["loose", "Loose"]]},
        {"param": "span", "label": "Forms within", "kind": "choice", "default": 60,
         "options": [[30, "30 bars"], [45, "45 bars"], [60, "60 bars"]]},
        {"param": "rise", "kind": "choice", "default": 10, "options": [[5, "5%"], [10, "10%"], [20, "20%"], [30, "30%"]],
         "label": "Uptrend into point 0 of at least" if up else "Downtrend into point 0 of at least"},
        {"param": "recent", "label": "C made within the last", "kind": "choice", "default": 3,
         "options": [[1, "1 bar (the latest)"], [2, "2 bars"], [3, "3 bars"], [5, "5 bars"], [10, "10 bars"]]},
        {"param": "swing", "label": "Swing size", "kind": "choice", "default": 2,
         "options": [[1, "Every swing"], [2, "2 bars each side"], [3, "3 bars each side"]]},
    ]


SCREEN_UP = {
    "key": "expanded_flat", "group": "chart", "name": "Expanded flat (A-B-C): uptrend", "direction": "up",
    "what": "An uptrend that pulls back to A, makes a higher high at B (about 1.15 to 1.25 times the first pullback above "
            "A), then falls to C, about 1.6 times that pullback below B and below A. Lists stocks whose C has just formed.",
    "why": "Measure the first pullback from the top of the uptrend (0) down to A. B is the next high: that move above A, "
           "so a little higher than 0. C is a trend-based Fibonacci extension from 0 to A to B: 1.6 times the move below "
           "B, which lands under A. The ratios are targets, not lines (“How exact” sets the give), and the "
           "whole shape has to fit in the number of bars chosen. On a line chart the points are closing prices; with "
           "candlesticks they are highs and lows. A Fibonacci level is a reference many traders watch, not a promise: "
           "this reports a shape that has formed and says nothing about what comes next.",
    "controls": _controls("up"),
}
SCREEN_DOWN = {
    "key": "expanded_flat_down", "group": "chart", "name": "Expanded flat (A-B-C): downtrend", "direction": "down",
    "what": "A downtrend that bounces to A, makes a lower low at B (about 1.15 to 1.25 times the first bounce below "
            "A), then rallies to C, about 1.6 times that bounce above B and above A. Lists stocks whose C has just formed.",
    "why": "The mirror image of the uptrend shape, run on the flipped prices. Measure the first bounce from the bottom of "
           "the downtrend (0) up to A. B is the next low: that move below A, so a little lower than 0. C is a "
           "trend-based Fibonacci extension from 0 to A to B: 1.6 times the move above B, which lands over A. The "
           "ratios are targets, not lines (“How exact” sets the give), and the whole shape has to fit in the number of "
           "bars chosen. On a line chart the points are closing prices; with candlesticks they are highs and lows. A "
           "Fibonacci level is a reference many traders watch, not a promise: this reports a shape that has formed and "
           "says nothing about what comes next.",
    "controls": _controls("down"),
}
SCREENS = [SCREEN_UP, SCREEN_DOWN]
SCREEN_KEYS = {s["key"] for s in SCREENS}
_BY_KEY = {s["key"]: s for s in SCREENS}


# =====================================================================
# THE DETECTION (pure: highs and lows in, the pattern or None out)
# =====================================================================
def pivots_of(H, L, k):
    """Confirmed swing points, alternating high and low (two of a kind in a row: the more extreme is kept)."""
    piv = []
    for t in range(2 * k, len(H)):
        s = swing_at(H, L, t, k, True)
        if s is None:
            continue
        idx, kind = s
        price = H[idx] if kind == "H" else L[idx]
        if piv and piv[-1][1] == kind:
            if (kind == "H" and price > piv[-1][2]) or (kind == "L" and price < piv[-1][2]):
                piv[-1] = (idx, kind, price)
        else:
            piv.append((idx, kind, price))
    return piv


def find_flat(H, L, k=2, b_lo=1.15, b_hi=1.25, c_target=1.6, give_b=0.05, give_c=0.10, span=60, rise=0.10,
              recent=RECENT_MAX, sign=1.0):
    """The newest expanded flat in the bars whose C is among the last `recent` bars, as a dict of indexes, prices and
    ratios, or None. H = highs, L = lows (the same list on a line chart), oldest first.

    ratio_b = (B - A) / (P0 - A)        ratio_c = (B - C) / (P0 - A)

    This is the uptrend shape. For the mirror (a downtrend) pass the NEGATED prices (highs become -lows, lows become
    -highs) and sign=-1: the ratios do not change when the prices are flipped, and sign puts the prices in the result
    back, and judges the size of the trend on the real prices."""
    n = len(H)
    if n < MIN_BARS:
        return None
    piv = pivots_of(H, L, k)
    best = None
    for j in range(len(piv) - 3, -1, -1):
        (i0, k0, hp0), (ia, ka, la), (ib, kb_, hb) = piv[j], piv[j + 1], piv[j + 2]
        if not (k0 == "H" and ka == "L" and kb_ == "H") or ib >= n - 1:
            continue
        if hb <= hp0:
            continue                                                  # B must be a HIGHER high than point 0
        leg = hp0 - la
        if leg <= 0:
            continue
        rb = (hb - la) / leg
        if not (b_lo - give_b - 1e-9 <= rb <= b_hi + give_b + 1e-9):
            continue
        ic = min(range(ib + 1, n), key=lambda i: (L[i], -i))          # the lowest point since B (the latest, if tied)
        if n - 1 - ic >= recent or L[ic] >= la:
            continue                                                  # not recent, or not below A
        if ic - i0 + 1 > span:
            continue                                                  # the whole shape has to fit
        # the legs are what they are named: P0 the top of its leg, A the bottom of the pullback, B the top after A
        if max(H[i0:ia + 1]) > hp0 or min(L[i0 + 1:ib]) < la or max(H[ia + 1:n]) > hb:
            continue
        rc = (hb - L[ic]) / leg
        if abs(rc - c_target) > give_c + 1e-9:
            continue
        # an uptrend into point 0
        s = max(0, i0 - UPTREND_BARS)
        if i0 - s < MIN_UP_HISTORY:
            continue
        low_min = min(L[s:i0])
        start = sign * low_min                                        # the real price the trend started from
        if start <= 0 or max(H[s:i0]) > hp0 or abs(sign * hp0 - start) / start < rise - 1e-9 or la <= low_min:
            continue
        cand = {"p0": i0, "a": ia, "b": ib, "c": ic, "p0_price": sign * hp0, "a_price": sign * la,
                "b_price": sign * hb, "c_price": sign * L[ic], "ratio_b": rb, "ratio_c": rc, "leg": leg,
                "rise": abs(sign * hp0 - start) / start, "span": ic - i0 + 1, "low_min": start}
        key = (n - 1 - ic, abs(rc - c_target) + abs(rb - (b_lo + b_hi) / 2))      # the newest C, then the closest fit
        if best is None or key < best[0]:
            best = (key, cand)
    return best[1] if best else None


# =====================================================================
# THE SCREEN
# =====================================================================
_FOUND = {}                  # (direction, timeframe, candles, b_lo, b_hi, c_ratio, exact, span, rise, swing) -> {...}
_LOCK = threading.Lock()


def find_in_bars(highs, lows, closes, direction, candles, p):
    """find_flat on real bars with the reader's settings `p` (as clean_params returns them). On a line the closes are
    the highs and the lows; direction="down" is the mirror, run on the negated prices."""
    give_b, give_c = EXACT[p["exact"]]
    H, L = (list(highs), list(lows)) if candles else (list(closes), list(closes))
    sign = 1.0
    if direction == "down":
        H, L, sign = [-x for x in L], [-x for x in H], -1.0               # a fall in the prices is a rise in these
    return find_flat(H, L, p["swing"], p["b_low"], p["b_high"], p["c_ratio"], give_b, give_c, p["span"],
                     p["rise"] / 100.0, RECENT_MAX, sign)


def patterns_for(bars, timeframe, candles, p, direction="up"):
    """Every stock's newest pattern (C within the last RECENT_MAX bars) on these bars: cached with the bars, so a
    change of the "within the last" control costs nothing. direction="down" runs the same scan on the negated prices."""
    key = (direction, timeframe, candles, p["b_low"], p["b_high"], p["c_ratio"], p["exact"], p["span"], p["rise"],
           p["swing"])
    with _LOCK:
        hit = _FOUND.get(key)
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
        f = find_in_bars(b.h, b.l, b.c, direction, candles, p)
        if f is not None:
            f["bars_ago"] = n - 1 - f["c"]
            for name in ("p0", "a", "b", "c"):
                f[name + "_date"] = b.date(f[name])
            f["close"] = b.c[-1]
            f["newest_bar"] = b.date(n - 1)
            out.append((isin, f))
    result = {"version": bars["version"], "market_date": bars["market_date"], "found": out, "tally": tally}
    with _LOCK:
        _FOUND[key] = result
    return result


def clean_params(raw):
    p = pb.clean_controls(SCREEN_UP["controls"], raw)
    if p["b_high"] < p["b_low"]:                                    # an inverted band: back to the two ratios
        p["b_low"], p["b_high"] = DEFAULTS["b_low"], DEFAULTS["b_high"]
    p["b_low"], p["b_high"], p["c_ratio"] = round(p["b_low"], 2), round(p["b_high"], 2), round(p["c_ratio"], 2)
    if p["exact"] not in EXACT:
        p["exact"] = "normal"
    return p


def run_screen(cur, key, raw_params, cap, limit, meta_fn, attach_fn, today=None):
    """The whole answer for one of the two screens. meta_fn(cur, isins, when) -> {isin: meta}; attach_fn(cur, rows)
    adds each row's price."""
    direction = _BY_KEY[key]["direction"]
    p = clean_params(raw_params)
    tf, candles, recent = p["timeframe"], p["basis"] == "CANDLE", p["recent"]
    limit = max(1, min(int(limit), ALL_ROWS))
    bars = pb.get_bars(cur, tf, today)
    base = {"screen": key, "params": p, "timeframe": tf, "basis": p["basis"], "direction": direction}
    if bars is None:
        return {**base, "rows": [], "total": 0, "considered": 0, "as_of": None,
                "unavailable": "No prices are stored for the period this timeframe reads."}
    res = patterns_for(bars, tf, candles, p, direction)
    hits = [(isin, f) for isin, f in res["found"] if f["bars_ago"] < recent]
    meta = meta_fn(cur, [i for i, _ in hits], bars["market_date"])
    rows = []
    for isin, f in hits:
        m = meta.get(isin)
        if not m or not m.get("company_name"):
            continue
        if cap and m.get("cap_class") != cap:
            continue
        rows.append({
            "isin": isin, "symbol": m.get("symbol"), "name": m.get("company_name"), "sector": m.get("sector"),
            "cap": m.get("cap_class"), "timeframe": tf, "basis": p["basis"], "direction": direction,
            "bars_ago": f["bars_ago"],
            "forming": tf != "DAILY" and f["bars_ago"] == 0,
            "p0": round(f["p0_price"], 2), "p0_date": str(f["p0_date"]),
            "a": round(f["a_price"], 2), "a_date": str(f["a_date"]),
            "b": round(f["b_price"], 2), "b_date": str(f["b_date"]), "ratio_b": round(f["ratio_b"], 2),
            "c": round(f["c_price"], 2), "c_date": str(f["c_date"]), "ratio_c": round(f["ratio_c"], 2),
            "move": round(f["leg"], 2), "move_pct": round(f["leg"] / f["p0_price"] * 100, 1),
            "rise_pct": round(f["rise"] * 100, 1), "span_bars": f["span"], "close": round(f["close"], 2),
            "newest_bar": str(f["newest_bar"]),
        })
    rows.sort(key=lambda r: (r["bars_ago"], abs(r["ratio_c"] - p["c_ratio"]), r["name"] or ""))
    total = len(rows)
    rows = rows[:limit]
    attach_fn(cur, rows)
    t = res["tally"]
    give_b, give_c = EXACT[p["exact"]]
    return {**base, "rows": rows, "total": total, "considered": t["tested"], "as_of": str(bars["market_date"]),
            "give": {"b": give_b, "c": give_c},
            "set_aside": {"unadjusted": t["unadjusted"], "stale": t["stale"], "short": t["short"]}}
