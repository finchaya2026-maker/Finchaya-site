"""
scanner_signals.py -- the chart scanners as BACK-TEST SIGNALS: "on this day the scanner newly listed this stock".
---------------------------------------------------------------------------
PURE FUNCTIONS (numpy only). Used by backtest_engine.py as one more kind of condition ("Scanner signal"), so a scanner on the Screener page can be replayed
over ten years of daily prices with the back-tester's own exits, stops, money, groups and statistics.

WHAT A SIGNAL IS
    The stock NEWLY APPEARS on the scanner on that day: the scanner, run with the stock's prices up to that day's close, lists it, and did not list it the day before
    (for an event scanner that is simply the day the event was first visible). It is the same thing the alert on the scanner emails ("a new stock appears"), so a
    back-test of a scanner tells you how the stocks that triggered the alert did afterwards.

    * Change in Dow Theory (turning up / turning down): the bar that completes the turn (`dow_change.scan_ohlc` for the short-term reading, a rolling
      `dow_change.scan_major` for the big-picture one, with the screen's own settings).
    * Higher low: the bar on which a swing low higher than the one before it is CONFIRMED (the screen's own pullback / rise / uptrend filters).
    * Fibonacci retracement: the first bar on which the stock is inside the retracement zone (`fib_retracement.find_setup` / `in_zone`, with the screen's settings).
    * Chart patterns (double top / bottom with a fake breakout, head and shoulders and its inverse, bullish and bearish flag and pole): the bar of the first CLOSE through the
      neckline (or out of the flag), from `chart_patterns.scan`, a single forward pass that uses only the bars up to each one (test_patterns.py proves it).

NO PEEKING. Every signal on a day is what the scanner would have said on THAT day, from the prices up to it. test_signals.py proves it against the live scanner's own functions:
for random price paths, the signal on every day equals "run the scanner on the bars up to that day and see whether it lists the stock for the first time".

WEEKLY AND MONTHLY. A scanner on weekly or monthly bars is read on those candles (the back-tester's own, finished ones only) and its signal is carried to the
week's or month's LAST TRADING DAY: it is never acted on before the candle has closed.

WHAT IS DIFFERENT FROM THE LIVE SCANNER, and the page says so
    * the live scanner reads the last ~13 months of daily bars (2 years of weekly, 4 of monthly); a back-test reads all the history it has, so a trend that began earlier can
      count (a pattern that is visible in more history). "Look back over: all the history read" is 400 bars here.
    * prices are the back-tester's adjusted prices (splits and bonuses applied), where the live scanner sets such a stock aside.
    * the live scanner's "alert made within the last N bars" does not apply: a signal is the first day.
"""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

TF = {"DAILY": "D", "WEEKLY": "W", "MONTHLY": "M"}
NEVER = ("recent", "status")             # a screen's "within the last N bars" and "show the watch list" are about the live list, not about a day in the past
MIN_FIB_BARS = 15                        # fib_retracement.MIN_BARS
PATTERN_KEYS = ("double_top", "double_bottom", "head_shoulders", "inv_head_shoulders", "flag_pole", "flag_pole_down")      # chart_patterns.py
MAX_LOOKBACK = 400


class SignalError(ValueError):
    """A signal that cannot be used, in words the person can act on."""


# =====================================================================
# THE CATALOGUE: which scanners, with their own controls
# =====================================================================
def _modules():
    """{screen key: (catalogue entry, direction)} for the scanners this server has."""
    out = {}
    try:
        import dow_change as dow
        for s in dow.SCREENS:
            out[s["key"]] = (s, "short" if s.get("direction") == "down" else "long")
    except ImportError:
        pass
    try:
        import higher_low as hl
        out[hl.SCREEN["key"]] = (hl.SCREEN, "long")
    except ImportError:
        pass
    try:
        import fib_retracement as fr
        out[fr.SCREEN["key"]] = (fr.SCREEN, "long")
    except ImportError:
        pass
    try:
        import chart_patterns as cp
        for s in cp.SCREENS:
            out[s["key"]] = (s, "short" if s.get("direction") == "down" else "long")
    except ImportError:
        pass
    return out


def available():
    return list(_modules())


def controls_of(spec):
    return [c for c in spec.get("controls") or [] if c["param"] not in NEVER]


def catalog():
    """What the page needs to draw the picker: one entry per scanner with its controls (the same shape as the Screener page's own)."""
    out = []
    for key, (spec, direction) in _modules().items():
        out.append({"key": key, "label": spec["name"], "what": spec.get("what", ""), "direction": direction,
                    "controls": [dict(c) for c in controls_of(spec)]})
    return out


def defaults(key):
    spec = _modules()[key][0]
    return {c["param"]: c["default"] for c in controls_of(spec)}


# =====================================================================
# VALIDATION (the spec arrives from a browser, so it is untrusted; a value that is not offered is refused, never quietly changed)
# =====================================================================
def _control(c, v, where):
    label = c.get("label") or c["param"]
    if v is None or v == "":
        return c["default"]
    if isinstance(v, bool):
        raise SignalError("%s: choose one of the offered values for '%s'." % (where, label))
    if c["kind"] == "number":
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError):
            raise SignalError("%s: '%s' must be a number." % (where, label))
        if f != f or not (c["min"] <= f <= c["max"]):
            raise SignalError("%s: '%s' must be between %g and %g." % (where, label, c["min"], c["max"]))
        return int(f) if isinstance(c["default"], int) and f == int(f) else f
    for o in c["options"]:
        if str(o[0]) == str(v):
            return o[0]
    raise SignalError("%s: choose one of the offered values for '%s'." % (where, label))


def clean(raw, where="Scanner signal"):
    """{"screen": key, "params": {...}} checked, with every offered setting present (missing ones take the scanner's defaults)."""
    if not isinstance(raw, dict):
        raise SignalError("%s: choose a scanner." % where)
    key = raw.get("screen")
    mods = _modules()
    if not isinstance(key, str) or key not in mods:
        if not mods:
            raise SignalError("%s: scanner signals are not available on this server yet (scanner_signals.py needs dow_change.py, higher_low.py or fib_retracement.py)." % where)
        raise SignalError("%s: choose one of the scanners offered." % where)
    spec = mods[key][0]
    rp = raw.get("params")
    rp = rp if isinstance(rp, dict) else {}
    params = {c["param"]: _control(c, rp.get(c["param"]), where) for c in controls_of(spec)}
    if key == "fib_retrace" and params["retr_high"] <= params["retr_low"]:
        raise SignalError("%s: the retracement must end higher than it starts (%g to %g)." % (where, params["retr_low"], params["retr_high"]))
    return {"screen": key, "params": params}


def timeframe_of(sig):
    return TF[sig["params"].get("timeframe", "DAILY")]


def _shown(spec, params):
    ctl = controls_of(spec)
    for c in ctl:
        w = c.get("when")
        if w:
            other = next((x for x in ctl if x["param"] == w["param"]), None)
            if other is not None and str(params.get(other["param"], other["default"])) != str(w["value"]):
                continue
        yield c


def describe(sig):
    """The signal in words, for the "Reads as" line and the results."""
    spec, direction = _modules()[sig["screen"]]
    p = sig["params"]
    bits = []
    for c in _shown(spec, p):
        v = p.get(c["param"], c["default"])
        if c["kind"] == "choice":
            lab = next((o[1] for o in c["options"] if str(o[0]) == str(v)), v)
            bits.append("%s: %s" % (c.get("label") or c["param"], lab))
        else:
            bits.append("%s: %g" % (c.get("label") or c["param"], float(v)))
    return "the \"%s\" scanner newly lists the stock%s" % (spec["name"], (" (" + "; ".join(bits) + ")") if bits else "")


# =====================================================================
# THE DETECTIONS, ONE PASS OVER A STOCK'S BARS
# =====================================================================
def _latest_extreme_distance(W, want_min):
    """For each row of a sliding-window matrix (oldest bar first): how many bars back from the row's last bar the lowest (or highest) value is, the LATEST such bar if tied."""
    R = W[:, ::-1]
    return np.argmin(R, axis=1) if want_min else np.argmax(R, axis=1)


def _windows(H, L, lookback, chunk=256):
    """Yield (a, b, WH, WL): sliding windows of the last `lookback` bars for bars a..b-1 (a bar's window is the `lookback` bars up to and including it, shorter at the start)."""
    n = len(H)
    Hp = np.concatenate([np.full(lookback - 1, -np.inf), H])
    Lp = np.concatenate([np.full(lookback - 1, np.inf), L])
    WH, WL = sliding_window_view(Hp, lookback), sliding_window_view(Lp, lookback)
    for a in range(0, n, chunk):
        b = min(n, a + chunk)
        yield a, b, WH[a:b], WL[a:b]


# ----- Dow theory, short-term reading: dow_change.scan_ohlc is a single causal pass ------------------------------------
def dow_swing_positions(h, l, c, swing, candles, down):
    import dow_change as dow
    out = np.zeros(len(c), dtype=bool)
    n = len(c)
    for e in dow.scan_ohlc(list(range(n)), h, l, c, swing, "down" if down else "up", candles):
        out[e["bar"]] = True
    return out


# ----- Dow theory, big-picture reading: dow_change.scan_major on the window of `lookback` bars up to each bar ------------
def dow_major_positions(H, L, lookback, move_min, k, sign):
    """True on every bar t on which dow_change.scan_major(H[window], L[window]) reports an alert whose bar IS t (the alert is new on that day). H, L are read as for a downtrend
    turning up (for the mirror pass the negated prices and sign=-1). Exactly the live reading, but each bar's trough and peak come from one sliding-window pass and the walk
    after the trough is carried forward bar by bar while the trough and peak stay the same, instead of being redone for every bar."""
    import dow_change as dow
    n = len(H)
    sig = np.zeros(n, dtype=bool)
    if n < 3:
        return sig
    lookback = int(lookback)
    trough = np.zeros(n, dtype=np.int64)
    top = np.zeros(n, dtype=np.int64)
    pos = np.arange(lookback)
    for a, b, WH, WL in _windows(H, L, lookback):
        r = _latest_extreme_distance(WL, True)                       # bars back from t to the window's lowest low (the latest, if tied)
        ts = np.arange(a, b)
        trough[a:b] = ts - r
        tpos = lookback - 1 - r                                      # the trough's place in the window
        masked = np.where(pos[None, :] < tpos[:, None], WH, -np.inf)
        top[a:b] = ts - _latest_extreme_distance(masked, False)      # the highest high BEFORE the trough (the latest, if tied)
    ts = np.arange(n)
    start = np.maximum(0, ts + 1 - lookback)
    ok = (trough > start) & (trough < ts) & (top >= start)
    peak = H[np.clip(top, 0, n - 1)]
    low = L[np.clip(trough, 0, n - 1)]
    a_, b_ = sign * peak, sign * low
    with np.errstate(divide="ignore", invalid="ignore"):
        move = np.abs(b_ - a_) / a_
    cand = ok & (a_ > 0) & (b_ > 0) & ~(move < move_min)
    Hl, Ll = H.tolist(), L.tolist()
    state = None
    for t in range(n):
        if not cand[t]:
            state = None
            continue
        tr, tp, st = int(trough[t]), int(top[t]), int(start[t])
        if tr - st < 2 * k + 1:
            # the trough is next to the window's start, where the window's own start changes which bars a swing can use: take the exact reading
            e = dow.scan_major(Hl[st:t + 1], Ll[st:t + 1], lookback, move_min, k, sign)
            sig[t] = bool(e) and st + e["bar"] == t
            state = None
            continue
        if state is None or state["key"] != (tr, tp):
            state = {"key": (tr, tp), "peak": Hl[tp], "low": Ll[tr], "tr": tr, "high": None, "rally": None, "hl": None, "last": None, "fired": False}
            for tt in range(tr + 1, t + 1):
                if _major_step(state, Hl, Ll, tt, k):
                    state["fired"] = True
                    sig[t] = (tt == t)
                    break
        elif not state["fired"]:
            if _major_step(state, Hl, Ll, t, k):
                state["fired"] = True
                sig[t] = True
    return sig


def _major_step(s, H, L, t, k):
    """One bar of dow_change.scan_major's walk after the trough. Returns True when this bar is the alert."""
    import dow_change as dow
    sw = dow.swing_at(H, L, t, k, True) if t >= 2 * k else None
    if sw is not None and sw[0] > s["tr"]:
        idx, kind = sw
        if kind == "H":
            if s["last"] != "H" or H[idx] > s["high"][1]:
                s["high"] = (idx, H[idx])
            s["last"] = "H"
        elif s["high"] is not None and idx > s["high"][0] and L[idx] > s["low"]:
            if s["last"] != "L" or s["hl"] is None or L[idx] < s["hl"][1]:
                s["hl"], s["rally"] = (idx, L[idx]), s["high"]
            s["last"] = "L"
    return s["hl"] is not None and H[t] > s["peak"]


def dow_positions(h, l, c, params, down):
    candles = params["basis"] == "CANDLE"
    if params["measure"] == "SWING":
        return dow_swing_positions(h, l, c, params["swing"], candles, down)
    if candles:
        H, L = np.asarray(h, float), np.asarray(l, float)
    else:
        H = L = np.asarray(c, float)
    sign = 1.0
    if down:
        H, L, sign = -L, -H, -1.0
    return dow_major_positions(H, L, min(int(params["lookback"]), MAX_LOOKBACK), params["fall"] / 100.0, params["swing"], sign)


# ----- Higher low: the bar on which a swing low higher than the one before is confirmed ------------------------------------
def higher_low_positions(h, l, c, params):
    """True on the bar on which higher_low.latest_higher_low would first report the higher low (bars_ago 0) and the screen's filters pass. The swings are walked forward one
    bar at a time with higher_low's own swing_at and pivot rule, so a pivot is exactly what had been confirmed by that bar."""
    import higher_low as hl
    n = len(c)
    out = np.zeros(n, dtype=bool)
    k, candles = params["swing"], params["basis"] == "CANDLE"
    if candles:
        H, L, where, ties = list(map(float, h)), list(map(float, l)), None, True
    else:
        H, where = hl.collapse([float(x) for x in c])
        L, ties = H, False
    pv = []
    for t in range(len(H)):
        sw = hl.swing_at(H, L, t, k, ties)
        if sw is None:
            continue
        idx, kind = sw
        price = H[idx] if kind == "H" else L[idx]
        moved = False
        if pv and pv[-1][1] == kind:
            if (kind == "H" and price > pv[-1][2]) or (kind == "L" and price < pv[-1][2]):
                pv[-1] = (idx, kind, price)
                moved = kind == "L"
        else:
            pv.append((idx, kind, price))
            moved = kind == "L"
        if not moved:
            continue
        j = len(pv) - 1
        if j < 2 or pv[j - 1][1] != "H" or pv[j - 2][1] != "L":
            continue
        l2, hi, l1 = pv[j], pv[j - 1], pv[j - 2]
        if not l2[2] > l1[2]:
            continue
        up_trend = j >= 3 and pv[j - 3][1] == "H" and hi[2] > pv[j - 3][2]
        rise = l2[2] / l1[2] - 1
        pullback = (hi[2] - l2[2]) / hi[2] if hi[2] > 0 else 0.0
        if pullback * 100 >= params["pullback"] - 1e-9 and rise * 100 >= params["rise"] - 1e-9 and (params["trend"] != "HH" or up_trend):
            out[t if where is None else where[t]] = True
    return out


# ----- Fibonacci retracement: the first bar inside the zone ------------------------------------------------------------------
def fib_in_zone(h, l, c, params):
    """True on every bar on which fib_retracement.find_setup / in_zone, run on the bars up to it, lists the stock (the stock is inside the zone)."""
    n = len(c)
    out = np.zeros(n, dtype=bool)
    if n < MIN_FIB_BARS:
        return out
    candles = params["basis"] == "CANDLE"
    lookback = int(params["lookback"])
    H = np.asarray(h if candles else c, float)
    L = np.asarray(l if candles else c, float)
    C = np.asarray(c, float)
    Hh, Ll = np.asarray(h, float), np.asarray(l, float)
    lo, hi, lw = params["retr_low"] / 100.0, params["retr_high"] / 100.0, params["leeway"] / 100.0
    min_rise = params["rise"] / 100.0
    zl, zh = lo - lw, hi + lw
    ts = np.arange(n)
    bottom = np.zeros(n, dtype=np.int64)
    peak = np.zeros(n, dtype=np.int64)
    pos = np.arange(lookback)
    for a, b, WH, WL in _windows(H, L, lookback):
        r = _latest_extreme_distance(WL, True)
        t = np.arange(a, b)
        bottom[a:b] = t - r
        bpos = lookback - 1 - r
        masked = np.where(pos[None, :] > bpos[:, None], WH, -np.inf)            # the highest point AFTER the bottom
        peak[a:b] = t - _latest_extreme_distance(masked, False)
    ok = (bottom < ts - 1) & (peak < ts) & (peak > bottom) & (ts + 1 >= MIN_FIB_BARS)
    bi, pi = np.clip(bottom, 0, n - 1), np.clip(peak, 0, n - 1)
    bt, pk = L[bi], H[pi]
    with np.errstate(divide="ignore", invalid="ignore"):
        ok &= (bt > 0) & (pk > bt)
        rise = pk / bt - 1
        ok &= ~(rise < min_rise)
        span = pk - bt
        retr = (pk - C) / span
        deep = (pk - Ll) / span if candles else retr
        shallow = (pk - Hh) / span if candles else retr
    inside = (deep >= zl) & (shallow <= zh) if candles else ((retr >= zl) & (retr <= zh))
    return ok & inside


def fib_positions(h, l, c, params):
    inside = fib_in_zone(h, l, c, params)
    prev = np.concatenate([[False], inside[:-1]])
    return inside & ~prev                                                       # the first bar inside the zone (and again each time it comes back)


# =====================================================================
# THE SERIES THE ENGINE USES
# =====================================================================
def positions(frame, sig):
    """Boolean array over `frame`'s bars: the bars on which this signal fires. `frame`: a DataFrame with open/high/low/close columns (daily, or weekly / monthly candles)."""
    key, p = sig["screen"], sig["params"]
    h, l, c = frame["high"].to_numpy(float), frame["low"].to_numpy(float), frame["close"].to_numpy(float)
    if len(c) < 3:
        return np.zeros(len(c), dtype=bool)
    if key.startswith("dow_change"):
        return dow_positions(h, l, c, p, key.endswith("_down"))
    if key == "higher_low":
        return higher_low_positions(h, l, c, p)
    if key == "fib_retrace":
        return fib_positions(h, l, c, p)
    if key in PATTERN_KEYS:
        import chart_patterns as cp
        return cp.positions(key, h, l, c, p)
    raise SignalError("Unknown scanner %r." % key)


def series(df, sig, htf_frame):
    """The signal as a boolean pandas Series on the stock's DAILY index. htf_frame(tf) -> the stock's weekly ('W') or monthly ('M') candles, finished ones only, each labelled
    with its last trading day. A weekly or monthly signal is carried to that label day, so it is never acted on before the candle has closed."""
    import pandas as pd
    tf = timeframe_of(sig)
    out = pd.Series(False, index=df.index)
    frame = df if tf == "D" else htf_frame(tf)
    if len(frame) < 3:
        return out
    pos = positions(frame, sig)
    if tf == "D":
        out.iloc[np.flatnonzero(pos)] = True
        return out
    labels = frame.index[np.flatnonzero(pos)]
    idx = df.index.get_indexer(labels)
    out.iloc[idx[idx >= 0]] = True
    return out
