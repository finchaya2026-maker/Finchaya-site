"""
chart_patterns.py -- six chart-pattern scanners: double top, double bottom, head and shoulders, inverted head and shoulders, bullish flag and pole, bearish flag and pole.
Mounted by screener_api.py; the bars come from price_bars.py; scanner_signals.py replays them in the back-tester and scanner_alerts.py emails them.
------------------------------------------------------------------------
THE ONE IDEA: every pattern is read in a single forward pass over the bars, using only the bars up to each one, and the pass raises a SIGNAL on the bar on which the pattern
is confirmed (the first CLOSE through the neckline, or out of the flag). So "what the scanner says on a day" and "what the back-test would have acted on that day" are the same
thing, by construction (test_patterns.py proves it against a scan of the bars up to each day), and an alert can be made on any of them.

TOP-SPACE. A bearish pattern (double top, head and shoulders, bearish flag) is a mirror of a bullish one (double bottom, inverted head and shoulders, bullish flag). The code is
written once, for tops, on prices turned upside down for the bullish ones; every percentage is measured on the real price (tops: from the top; bottoms: from the bottom).

SWINGS. A swing high is a bar higher than the `swing` bars on each side of it (a swing low the opposite): it is only known `swing` bars later. The swings alternate high, low, high, ...
(two of a kind in a row keep the more extreme). On a line chart only closes exist (runs of equal closes count as one point); on candlesticks highs and lows are used.
Breaks and breakouts are always judged on the CLOSE.

DOUBLE TOP / DOUBLE BOTTOM, WITH A FAKE BREAKOUT / BREAKDOWN
    T1, then a pullback to the neckline low N, then T2. T2 may be a little below T1 (the classic shape) or ABOVE it by up to `poke`: a fake breakout. In a fake breakout a candle may
    CLOSE above the first top (a wick is not required) as long as the price then closes back inside the range (below T1 again) and goes on to close below the neckline. A close
    above T1 by more than `poke` is a real breakout: the pattern is void. Also required: a pullback of at least `depth` between the tops, the tops at most `span` bars apart,
    and (unless the reader turns it off) a MOVE INTO the pattern: the first top is the highest point of the `span` bars before it and stands at least `rise` above the lowest point
    of those bars (a fall into a double bottom, the other way round). Signal: the first close below N (above it, for a double bottom), within `window` bars of T2.

HEAD AND SHOULDERS / INVERTED
    Left shoulder, head, right shoulder, with the lows between them (N1, N2) forming the neckline (it may slope, by up to 10%). The head is above both shoulders by at least `head`;
    the head has room: at least `gap` bars (default 10) from the end of the left shoulder (N1, where the neckline starts) to the head, and from the head to the right shoulder's extreme;
    the shoulders' heights above the neckline are within `shoulder` of each other and each at least `shoulder_min` of the head's height; the head stands at least `depth` above the neckline; the same move into the pattern as for the double top
    (the head is the highest point of the `span` bars before the left shoulder and stands at least `rise` above their lowest); left to right shoulder at most `span` bars.
    Signal: the first close below the neckline (above it, for the inverted one), within `window` bars of the right shoulder. A close above the head voids the pattern.

FLAG AND POLE
    A pole: a rise of at least `pole` in at most `pole_bars` bars with most closes up. A flag: 5 to `flag_max` bars that drift against the pole (flat or down) inside two parallel
    lines, quiet (smaller bars than the pole's), giving back no more than `retrace` of the pole. Signal: the first close above the flag's upper line (below the lower line, for the bearish one).

NOT ADVICE. These report a shape that has formed; a pattern can fail.
"""

import threading

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

import price_bars as pb

MIN_BARS = 25
STALE_DAYS = 7
MAX_ROWS = 200
ALL_ROWS = 100000
NECK_TILT = 0.10           # the neckline of a head and shoulders may slope by this much between its two lows
POLE_UP_SHARE = 0.60        # most of a pole's closes are in its direction
FLAG_QUIET = 0.80           # a flag's average bar is at most this much of the pole's average bar
FLAG_PARALLEL = 0.35        # the flag's two lines may drift apart by this much of the channel's width over the flag
FLAG_DRIFT = 0.001          # a flag may drift with the pole by at most this much of the price per bar (flat is fine)
FLAG_MIN = 5
FLAG_BREAK = 0.15           # a break out of the flag must clear its upper line by this much of the flag's width (a tick over the line, inside a drift, is noise)

TIMEFRAME_CONTROL = {"param": "timeframe", "label": "Timeframe", "kind": "choice", "default": "DAILY",
                     "options": [["DAILY", "Daily bars"], ["WEEKLY", "Weekly bars"], ["MONTHLY", "Monthly bars"]]}
BASIS_CONTROL = {"param": "basis", "label": "Chart", "kind": "choice", "default": "LINE",
                 "options": [["LINE", "Line (closing prices)"], ["CANDLE", "Candlesticks (highs and lows)"]]}
RECENT_CONTROL = {"param": "recent", "label": "Signal made within the last", "kind": "choice", "default": 3,
                  "options": [[1, "1 bar (the latest)"], [2, "2 bars"], [3, "3 bars"], [5, "5 bars"], [10, "10 bars"]]}
SWING_CONTROL = {"param": "swing", "label": "Swing size", "kind": "choice", "default": 2,
                 "options": [[1, "Every swing (as on the chart)"], [2, "2 bars each side"], [3, "3 bars each side"]]}
STATUS_CONTROL = {"param": "status", "label": "Show", "kind": "choice", "default": "SIGNAL",
                  "options": [["SIGNAL", "Confirmed: the signal has happened"], ["WATCH", "Formed, signal still to come (a watch list)"]]}
WINDOW_CONTROL = {"param": "window", "label": "Signal at most this long after the last point", "kind": "choice", "default": 20,
                  "options": [[10, "10 bars"], [20, "20 bars"], [40, "40 bars"]]}
RISE_CONTROL = {"param": "rise", "label": "Starts at the extreme of a move of at least", "kind": "choice", "default": 10,
                "options": [[0, "Any (no check)"], [5, "5%"], [10, "10%"], [20, "20%"]]}
SPAN_CONTROL = lambda default, opts: {"param": "span", "label": "Pattern at most this many bars long", "kind": "choice", "default": default, "options": [[o, "%d bars" % o] for o in opts]}   # noqa: E731

DOUBLE_CONTROLS = [
    TIMEFRAME_CONTROL, BASIS_CONTROL,
    {"param": "shape", "label": "Shape", "kind": "choice", "default": "ANY",
     "options": [["ANY", "Either: classic or with a fake breakout"], ["CLASSIC", "Classic only (the second point does not go past the first)"],
                 ["FAKE", "Fake breakout / breakdown only (the second point goes past the first, then closes back inside)"]]},
    {"param": "tol", "label": "The two points within", "kind": "choice", "default": 3, "options": [[1, "1%"], [2, "2%"], [3, "3%"], [5, "5%"]]},
    {"param": "poke", "label": "A fake breakout may go past the first point by up to", "kind": "choice", "default": 3,
     "options": [[0, "Not at all"], [1, "1%"], [3, "3%"], [5, "5%"], [10, "10%"]]},
    {"param": "depth", "label": "Pullback between the two points at least", "kind": "choice", "default": 5, "options": [[3, "3%"], [5, "5%"], [8, "8%"], [10, "10%"]]},
    RISE_CONTROL, SPAN_CONTROL(60, (20, 40, 60, 100)), WINDOW_CONTROL, RECENT_CONTROL, SWING_CONTROL, STATUS_CONTROL,
]
HS_CONTROLS = [
    TIMEFRAME_CONTROL, BASIS_CONTROL,
    {"param": "head", "label": "The head beyond the shoulders by at least", "kind": "choice", "default": 3, "options": [[2, "2%"], [3, "3%"], [5, "5%"], [8, "8%"]]},
    {"param": "shoulder", "label": "The shoulders' heights within", "kind": "choice", "default": 15, "options": [[10, "10%"], [15, "15%"], [25, "25%"], [35, "35%"]]},
    {"param": "shoulder_min", "label": "Each shoulder at least this much of the head's height", "kind": "choice", "default": 25,
     "options": [[0, "No minimum"], [25, "A quarter"], [35, "A third"], [50, "Half"]]},
    {"param": "gap", "label": "Bars from the end of the left shoulder to the head, and from the head to the right shoulder, at least", "kind": "choice", "default": 10,
     "options": [[5, "5 bars"], [10, "10 bars"], [15, "15 bars"], [20, "20 bars"]]},
    {"param": "depth", "label": "The head beyond the neckline at least", "kind": "choice", "default": 5, "options": [[3, "3%"], [5, "5%"], [8, "8%"], [10, "10%"]]},
    RISE_CONTROL, SPAN_CONTROL(120, (40, 80, 120, 200)), WINDOW_CONTROL, RECENT_CONTROL, SWING_CONTROL, STATUS_CONTROL,
]
FLAG_CONTROLS = [
    TIMEFRAME_CONTROL, BASIS_CONTROL,
    {"param": "pole", "label": "The pole at least", "kind": "choice", "default": 15, "options": [[10, "10%"], [15, "15%"], [20, "20%"], [30, "30%"]]},
    {"param": "pole_bars", "label": "...in at most", "kind": "choice", "default": 15, "options": [[8, "8 bars"], [10, "10 bars"], [15, "15 bars"], [20, "20 bars"]]},
    {"param": "flag_max", "label": "The flag at most", "kind": "choice", "default": 20, "options": [[10, "10 bars"], [15, "15 bars"], [20, "20 bars"], [30, "30 bars"]]},
    {"param": "retrace", "label": "The flag gives back no more than", "kind": "choice", "default": 50, "options": [[25, "25% of the pole"], [38, "38%"], [50, "50%"], [62, "62%"]]},
    RECENT_CONTROL, STATUS_CONTROL,
]

_WHY_COMMON = ("A pattern is read from the swings of the chart (or, for the flag, from the bars themselves) using only the bars up to each day, and is confirmed by a CLOSE, "
               "not a wick. On a line chart only closing prices count; with candlesticks the highs and lows are the points, so the two charts can disagree either way. "
               "It reports a shape that has already formed; a pattern can fail, and this is not a forecast or advice.")

SCREENS = [
    {"key": "double_top", "group": "chart", "name": "Double top (with fake breakout)", "direction": "down", "kind": "double",
     "what": "A stock that rose to a top, pulled back, rallied to the same area again (or a little past it: a fake breakout, where a candle can even close above the first top and the price then closes "
             "back inside the range) and has now closed below the pullback low.",
     "why": "Two failures to hold the same area, then the support under the pullback gives way. " + _WHY_COMMON, "controls": DOUBLE_CONTROLS},
    {"key": "double_bottom", "group": "chart", "name": "Double bottom (with fake breakdown)", "direction": "up", "kind": "double",
     "what": "A stock that fell to a bottom, bounced, fell to the same area again (or a little below it: a fake breakdown, where a candle can even close below the first bottom and the price then "
             "closes back inside the range) and has now closed above the bounce high.",
     "why": "Two failures to break lower from the same area, then the resistance over the bounce gives way. " + _WHY_COMMON, "controls": DOUBLE_CONTROLS},
    {"key": "inv_head_shoulders", "group": "chart", "name": "Inverted head and shoulders", "direction": "up", "kind": "hs",
     "what": "Three lows, the middle one (the head) lowest and a right shoulder higher than the head, with the neckline joining the two highs between them, and a close above the neckline.",
     "why": "A fall that makes a lower low and then fails to make another, with the price now back over the highs in between. " + _WHY_COMMON, "controls": HS_CONTROLS},
    {"key": "head_shoulders", "group": "chart", "name": "Head and shoulders", "direction": "down", "kind": "hs",
     "what": "Three highs, the middle one (the head) highest and a right shoulder lower than the head, with the neckline joining the two lows between them, and a close below the neckline.",
     "why": "A rise that makes a higher high and then fails to make another, with the price now back under the lows in between. " + _WHY_COMMON, "controls": HS_CONTROLS},
    {"key": "flag_pole", "group": "chart", "name": "Flag and pole: bullish", "direction": "up", "kind": "flag",
     "what": "A sharp rise (the pole), then a short, quiet drift against it between two parallel lines (the flag) that gave back only part of the pole, and a close above the flag's upper line.",
     "why": "A pause after a strong move, resolved in the direction of the move. " + _WHY_COMMON, "controls": FLAG_CONTROLS},
    {"key": "flag_pole_down", "group": "chart", "name": "Flag and pole: bearish", "direction": "down", "kind": "flag",
     "what": "A sharp fall (the pole), then a short, quiet drift against it between two parallel lines (the flag) that gave back only part of the pole, and a close below the flag's lower line.",
     "why": "A pause after a strong fall, resolved in the direction of the fall. " + _WHY_COMMON, "controls": FLAG_CONTROLS},
]
_BY_KEY = {s["key"]: s for s in SCREENS}
SCREEN_KEYS = set(_BY_KEY)


# =====================================================================
# THE SWINGS (walked forward, bar by bar)
# =====================================================================
def swing_at(H, L, t, k, ties):
    """The swing that becomes known at point t, as (index, 'H'|'L'), or None: the point k before t, a high above the highs of the k points each side (a low below their lows).
    With `ties` a flat top (or bottom) counts at its first point. A point that is both is not a swing."""
    i = t - k
    if i < k:
        return None
    hi, lo = H[i], L[i]
    is_high = all(hi > x for x in H[i - k:i]) and all((hi >= x) if ties else (hi > x) for x in H[i + 1:i + k + 1])
    is_low = all(lo < x for x in L[i - k:i]) and all((lo <= x) if ties else (lo < x) for x in L[i + 1:i + k + 1])
    if is_high == is_low:
        return None
    return (i, "H") if is_high else (i, "L")


class Pivots:
    """The alternating swing pivots of a stock's bars as they are confirmed, in TOP-SPACE (a high is a top). Feed one bar at a time with add()."""

    def __init__(self, k, candles):
        self.k, self.candles = k, candles
        self.H, self.L, self.where = [], [], []
        self.pv = []                                   # (index, 'H'|'L', price, bar of the extreme)

    def add(self, bar, ht, lt, ct):
        """Returns the pivot that was created or replaced by this bar (a tuple), or None."""
        if self.candles:
            self.H.append(ht)
            self.L.append(lt)
        else:
            if self.H and ct == self.H[-1]:
                return None                            # a run of equal closes is one point
            self.H.append(ct)
            self.L.append(ct)
        self.where.append(bar)
        m = len(self.H) - 1
        sw = swing_at(self.H, self.L, m, self.k, self.candles)
        if sw is None:
            return None
        idx, kind = sw
        price = self.H[idx] if kind == "H" else self.L[idx]
        p = (idx, kind, price, self.where[idx])
        if self.pv and self.pv[-1][1] == kind:
            if (kind == "H" and price > self.pv[-1][2]) or (kind == "L" and price < self.pv[-1][2]):
                self.pv[-1] = p
                return p
            return None
        self.pv.append(p)
        return p


def _iso(dates, i):
    d = dates[i]
    return str(d)[:10]


# =====================================================================
# DOUBLE TOP / BOTTOM
# =====================================================================
def _rise_ok(look, first_bar, top_value, p):
    """A move into the pattern, when the reader asks for one: the top (in top-space) stands at least `rise` above the lowest low of the `span` bars up to the pattern's first point,
    AND no point of those bars is above the pattern's first top (it forms at the top of a rise, not as a bump inside a fall). Needs no earlier swing, so a pattern near the start
    of the history is judged the same way as any other. look = (the highs, the lows) in top-space."""
    if p["rise"] <= 0:
        return True
    hi, lo = look
    a = max(0, first_bar - p["span"])
    if float(np.max(hi[a:first_bar + 1])) > top_value + 1e-12 * abs(top_value):
        return False
    v0 = float(np.min(lo[a:first_bar + 1]))
    return (top_value - v0) / abs(v0) >= p["rise"] - 1e-12


def _double_candidate(pv, sign, p, look):
    """The double top (in top-space) ending at the last pivot, or None. pv: [..., T1, N, T2]."""
    if len(pv) < 3 or pv[-1][1] != "H" or pv[-2][1] != "L" or pv[-3][1] != "H":
        return None
    t2, n, t1 = pv[-1], pv[-2], pv[-3]
    v1, v2, vn = t1[2], t2[2], n[2]
    if v1 == 0:
        return None
    e = (v2 - v1) / abs(v1)                            # how far the second point went past the first (negative: short of it)
    if not (-p["tol"] <= e <= p["poke"] + 1e-12):
        return None
    if p["shape"] == "CLASSIC" and e > 0:
        return None
    if p["shape"] == "FAKE" and not e > 0:
        return None
    vmin = min(v1, v2)
    if not (vmin - vn) / abs(vmin) >= p["depth"] - 1e-12:
        return None
    if not _rise_ok(look, t1[3], v1, p):
        return None
    if t2[3] - t1[3] > p["span"]:
        return None
    return {"kind": "double", "t1": t1, "n": n, "t2": t2, "neck": vn, "invalid": v1 + p["poke"] * abs(v1) + 1e-12 * abs(v1), "e": e,
            "depth": (vmin - vn) / abs(vmin), "anchor": t2[3], "sign": sign}


# =====================================================================
# HEAD AND SHOULDERS
# =====================================================================
def _hs_candidate(pv, sign, p, look):
    """The head and shoulders (in top-space) ending at the last pivot, or None. pv: [..., LS, N1, H, N2, RS]."""
    if len(pv) < 5:
        return None
    kinds = [x[1] for x in pv[-5:]]
    if kinds != ["H", "L", "H", "L", "H"]:
        return None
    ls, n1, hd, n2, rs = pv[-5:]
    if n2[3] == n1[3]:
        return None
    if hd[3] - n1[3] < p["gap"] or rs[3] - hd[3] < p["gap"]:
        return None                                                  # a head needs room: `gap` bars from the end of the left shoulder to it, and from it to the right shoulder's extreme
    slope = (n2[2] - n1[2]) / (n2[3] - n1[3])

    def neck(b):
        return n1[2] + slope * (b - n1[3])
    h_ls, h_hd, h_rs = ls[2] - neck(ls[3]), hd[2] - neck(hd[3]), rs[2] - neck(rs[3])
    if not (h_ls > 0 and h_hd > 0 and h_rs > 0):
        return None
    sh = max(ls[2], rs[2])
    if not (hd[2] - sh) / abs(sh) >= p["head"] - 1e-12:
        return None
    if not abs(h_ls - h_rs) / max(h_ls, h_rs) <= p["shoulder"] + 1e-12:
        return None
    if min(h_ls, h_rs) < p["shoulder_min"] * h_hd - 1e-12:
        return None                                                  # a shoulder is a real swell, not a bump on the neckline
    nk = neck(hd[3])
    if not h_hd / abs(nk) >= p["depth"] - 1e-12:
        return None
    if abs(n2[2] - n1[2]) / min(abs(n1[2]), abs(n2[2])) > NECK_TILT:
        return None
    if not _rise_ok(look, ls[3], hd[2], p):
        return None
    if rs[3] - ls[3] > p["span"]:
        return None
    return {"kind": "hs", "ls": ls, "n1": n1, "hd": hd, "n2": n2, "rs": rs, "slope": slope, "n1_bar": n1[3], "n1_v": n1[2], "invalid": hd[2],
            "heights": (h_ls, h_hd, h_rs), "anchor": rs[3], "sign": sign, "depth": h_hd / abs(nk)}


def _neck_at(c, b):
    if c["kind"] == "double":
        return c["neck"]
    return c["n1_v"] + c["slope"] * (b - c["n1_bar"])


def scan_pivot_pattern(kind, h, l, c, params, sign, candles):
    """Walk the bars once. Returns (events, active): events = [candidate dict + 'bar' (the signal bar), 'close'], active = the candidate still open at the last bar (or None).
    h, l, c are REAL prices; sign = +1 for a top pattern, -1 for a bottom pattern (the bars are turned upside down inside)."""
    n = len(c)
    mk = _double_candidate if kind == "double" else _hs_candidate
    ht = (h if sign > 0 else -l)
    lt = (l if sign > 0 else -h)
    ct = sign * c
    look = (ht, lt) if candles else (ct, ct)           # the highs and lows the move into the pattern is measured on
    tr = Pivots(params["swing"], candles)
    events, active = [], None
    window = params["window"]
    for t in range(n):
        pv = tr.add(t, float(ht[t]), float(lt[t]), float(ct[t]))
        if pv is not None and pv[1] == "H":
            active = mk(tr.pv, sign, params, look)
        if active is not None:
            if t - active["anchor"] > window:
                active = None
            elif ct[t] > active["invalid"]:
                active = None
            elif ct[t] < _neck_at(active, t):
                e = dict(active)
                e["bar"], e["close"] = t, float(c[t])
                events.append(e)
                active = None
    return events, active


# =====================================================================
# FLAG AND POLE
# =====================================================================
def _flag_candidate(Ht, Lt, Ct, t, pole_ok, p):
    """The flag that ends just before bar t (top-space: a bullish flag), or None. Returns a dict with the pole, the flag and the upper line's value at bar t."""
    fmax = p["flag_max"]
    lo = max(0, t - 1 - fmax)
    hi = t - 1
    if hi - lo + 1 < FLAG_MIN + 1:
        return None
    seg = Ht[lo:hi + 1]
    P = lo + int(len(seg) - 1 - np.argmax(seg[::-1]))               # the highest high in the window (the latest, if tied)
    f = t - 1 - P
    if f < FLAG_MIN or f > fmax or not pole_ok[P]:
        return None
    pb = p["pole_bars"]
    b0 = max(0, P - pb)
    wseg = Lt[b0:P + 1]
    B = b0 + int(len(wseg) - 1 - np.argmin(wseg[::-1]))             # the lowest low before the top (the latest, if tied)
    height = Ht[P] - Lt[B]
    if height <= 0 or P - B < 3:
        return None
    base = abs(Lt[B])
    if not height / base >= p["pole"] - 1e-12:
        return None
    ups = int(np.sum(Ct[B + 1:P + 1] >= Ct[B:P]))
    if ups < POLE_UP_SHARE * (P - B) - 1e-12:
        return None
    fl = slice(P + 1, t)
    fh, fl_, fc = Ht[fl], Lt[fl], Ct[fl]
    x = np.arange(len(fh), dtype=float)
    xm = x.mean()
    den = float(np.sum((x - xm) ** 2))
    if den <= 0:
        return None
    su = float(np.sum((x - xm) * (fh - fh.mean())) / den)
    sl = float(np.sum((x - xm) * (fl_ - fl_.mean())) / den)
    sc = float(np.sum((x - xm) * (fc - fc.mean())) / den)
    if sc > FLAG_DRIFT * abs(float(fc.mean())):
        return None                                                  # drifting WITH the pole is not a flag
    s = (su + sl) / 2.0
    au = float(np.max(fh - s * x))
    al = float(np.min(fl_ - s * x))
    width = au - al
    if width < 0 or abs(su - sl) * (len(fh) - 1) > FLAG_PARALLEL * width + 1e-12:
        return None
    pole_move = float(np.mean(np.abs(np.diff(Ct[B:P + 1]))))         # how far a bar moves, close to close, in the pole and in the flag
    flag_move = float(np.mean(np.abs(np.diff(Ct[P:t]))))
    if flag_move > FLAG_QUIET * pole_move + 1e-12:
        return None
    give = (Ht[P] - float(np.min(fl_))) / height
    if give > p["retrace"] + 1e-12:
        return None
    upper_t = au + s * (t - (P + 1))
    lower_t = al + s * (t - (P + 1))
    return {"kind": "flag", "P": P, "B": B, "pole_pct": height / base, "flag_bars": f, "retrace": give, "upper": upper_t, "lower": lower_t, "pole_top": float(Ht[P]),
            "pole_base": float(Lt[B]), "anchor": P, "margin": FLAG_BREAK * width}


def scan_flag(h, l, c, params, sign, candles, want_active=False):
    """Every flag breakout (the first close out of the flag's upper line, in top-space) and, at the last bar, the flag still open. h, l, c are real prices; sign +1 bullish, -1 bearish."""
    n = len(c)
    Ct = (sign * c).astype(float)
    if candles:
        Ht = (h if sign > 0 else -l).astype(float)
        Lt = (l if sign > 0 else -h).astype(float)
    else:
        Ht = Lt = Ct                                                 # a line chart: only the closes exist
    events = []
    if n < FLAG_MIN + 5:
        return events, None
    pb = params["pole_bars"]
    # a bar can be a pole top only if the rise into it is big enough: one pass for all bars
    padL = np.concatenate([np.full(pb, np.inf), Lt])
    wmin = sliding_window_view(padL, pb + 1).min(axis=1)           # the lowest low of the pole_bars before each bar, and the bar itself
    padH = np.concatenate([np.full(pb, -np.inf), Ht])
    wmax = sliding_window_view(padH, pb + 1).max(axis=1)           # a pole's top is the highest high of the bars of its own rise
    with np.errstate(divide="ignore", invalid="ignore"):
        pole_ok = ((Ht - wmin) / np.abs(wmin) >= params["pole"] - 1e-12) & (Ht >= wmax)
    pole_ok = np.nan_to_num(pole_ok, nan=False).astype(bool)
    last_p = None
    # a signal at t needs a pole top within the flag window before it
    csum = np.concatenate([[0], np.cumsum(pole_ok.astype(int))])
    fmax = params["flag_max"]
    for t in range(FLAG_MIN + 1, n):
        a = max(0, t - 1 - fmax)
        b = t - 1 - FLAG_MIN
        if b < a or csum[b + 1] - csum[a] == 0:
            continue
        cand = _flag_candidate(Ht, Lt, Ct, t, pole_ok, params)
        if cand is None:
            continue
        if Ct[t] > cand["upper"] + cand["margin"] and cand["P"] != last_p:
            e = dict(cand)
            e["bar"], e["close"] = t, float(c[t])
            events.append(e)
            last_p = cand["P"]
    active = None
    if want_active and n > FLAG_MIN + 1:
        cand = _flag_candidate(Ht, Lt, Ct, n, pole_ok, params)
        if cand is not None and cand["P"] != last_p and Ct[n - 1] <= cand["upper"] + cand["margin"] + 1e-12:
            active = cand
    return events, active


# =====================================================================
# ONE STOCK, ONE SCREEN
# =====================================================================
def clean_params(key, raw):
    return pb.clean_controls(_BY_KEY[key]["controls"], raw)


def _pct(p):
    out = dict(p)
    for k in ("tol", "poke", "depth", "rise", "head", "shoulder", "shoulder_min", "pole", "retrace"):
        if k in out:
            out[k] = out[k] / 100.0
    return out


def space_sign(key):
    """real price = sign x top-space value. A top pattern (double top, head and shoulders) is bearish and is read as it is (+1); a bottom pattern is read turned upside down (-1).
    A bullish flag is read as it is (+1), a bearish flag upside down (-1)."""
    spec = _BY_KEY[key]
    if spec["kind"] == "flag":
        return 1 if spec["direction"] == "up" else -1
    return 1 if spec["direction"] == "down" else -1


def scan(key, h, l, c, params, want_active=False):
    """(events, active) for one stock. h, l, c: real-price arrays, oldest first. params: cleaned controls (percent as in the page)."""
    spec = _BY_KEY[key]
    p = _pct(params)
    h, l, c = np.asarray(h, float), np.asarray(l, float), np.asarray(c, float)
    if spec["kind"] == "flag":
        return scan_flag(h, l, c, p, space_sign(key), params["basis"] == "CANDLE", want_active)
    return scan_pivot_pattern(spec["kind"], h, l, c, p, space_sign(key), params["basis"] == "CANDLE")


def positions(key, h, l, c, params):
    """Boolean array: the bars on which the signal is raised (for the back-tester)."""
    ev, _ = scan(key, h, l, c, params)
    out = np.zeros(len(c), dtype=bool)
    for e in ev:
        out[e["bar"]] = True
    return out


def describe_event(key, e, dates):
    """An event as plain data for a row: its points (label, date, price in REAL prices), its figures, and the neckline (or the flag's breakout line) at the signal bar."""
    spec = _BY_KEY[key]
    s = space_sign(key)
    top = s > 0
    pts, facts = [], {}

    def pt(label, bar, v):
        return {"label": label, "date": _iso(dates, bar), "price": round(float(s * v), 2)}
    if spec["kind"] == "double":
        word = "top" if top else "bottom"
        pts = [pt("First " + word, e["t1"][3], e["t1"][2]), pt("Neckline", e["n"][3], e["n"][2]), pt("Second " + word, e["t2"][3], e["t2"][2])]
        facts = {"shape": "fake" if e["e"] > 0 else "classic", "past_first_pct": round(float(e["e"]) * 100, 1), "depth_pct": round(float(e["depth"]) * 100, 1)}
        neck = s * e["neck"]
    elif spec["kind"] == "hs":
        nl = "Neckline low" if top else "Neckline high"
        pts = [pt("Left shoulder", e["ls"][3], e["ls"][2]), pt(nl + " 1", e["n1"][3], e["n1"][2]), pt("Head", e["hd"][3], e["hd"][2]), pt(nl + " 2", e["n2"][3], e["n2"][2]),
               pt("Right shoulder", e["rs"][3], e["rs"][2])]
        hl_, _hh, hr = e["heights"]
        sh = max(e["ls"][2], e["rs"][2])
        facts = {"head_pct": round(float((e["hd"][2] - sh) / abs(sh)) * 100, 1), "depth_pct": round(float(e["depth"]) * 100, 1),
                 "shoulder_diff_pct": round(float(abs(hl_ - hr) / max(hl_, hr)) * 100, 1), "left_bars": int(e["hd"][3] - e["n1"][3]), "right_bars": int(e["rs"][3] - e["hd"][3])}
        neck = s * _neck_at(e, e["bar"])
    else:
        pts = [pt("Pole starts", e["B"], e["pole_base"]), pt("Pole top" if top else "Pole bottom", e["P"], e["pole_top"])]
        facts = {"pole_pct": round(float(e["pole_pct"]) * 100, 1), "flag_bars": int(e["flag_bars"]), "retrace_pct": round(float(e["retrace"]) * 100, 1)}
        neck = s * e["upper"]
    return pts, facts, round(float(neck), 2)


# =====================================================================
# THE SCREEN
# =====================================================================
_CACHE = {}
_LOCK = threading.Lock()
_DETECT_KEYS = ("timeframe", "basis", "shape", "tol", "poke", "depth", "rise", "span", "window", "swing", "head", "shoulder", "shoulder_min", "gap", "pole", "pole_bars", "flag_max", "retrace")


def found_for(bars, key, params):
    """Every stock's signals (and, for a watch list, its open pattern) on these bars. Cached with the bars, so changing 'within the last' or 'show' costs nothing."""
    ck = (key,) + tuple((k, params[k]) for k in _DETECT_KEYS if k in params)
    with _LOCK:
        hit = _CACHE.get(ck)
        if hit and hit["version"] == bars["version"]:
            return hit
    out, tally = [], {"tested": 0, "short": 0, "stale": 0, "unadjusted": 0}
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
        ev, act = scan(key, np.frombuffer(b.h, dtype=float), np.frombuffer(b.l, dtype=float), np.frombuffer(b.c, dtype=float), params, want_active=True)
        dates = [b.date(i) for i in range(n)]
        for e in ev:
            pts, facts, neck = describe_event(key, e, dates)
            out.append((isin, {"status": "SIGNAL", "bar": e["bar"], "bars_ago": n - 1 - e["bar"], "date": str(dates[e["bar"]]), "close": round(e["close"], 2), "neck": neck,
                               "points": pts, "facts": facts, "newest": str(dates[-1])}))
        if act is not None:
            e = dict(act)
            e["bar"], e["close"] = n - 1, float(b.c[n - 1])
            pts, facts, neck = describe_event(key, e, dates)
            out.append((isin, {"status": "WATCH", "bar": n - 1, "bars_ago": 0, "date": str(dates[-1]), "close": round(e["close"], 2), "neck": neck, "points": pts, "facts": facts,
                               "newest": str(dates[-1])}))
    res = {"version": bars["version"], "market_date": bars["market_date"], "found": out, "tally": tally}
    with _LOCK:
        _CACHE[ck] = res
    return res


def run_screen(cur, key, raw_params, cap, limit, meta_fn, attach_fn, today=None):
    """The whole answer for one of the six screens. meta_fn(cur, isins, when) -> {isin: meta}; attach_fn(cur, rows) adds each row's latest price."""
    params = clean_params(key, raw_params)
    tf = params["timeframe"]
    limit = max(1, min(int(limit), ALL_ROWS))
    bars = pb.get_bars(cur, tf, today)
    base = {"screen": key, "params": params, "timeframe": tf, "basis": params["basis"], "direction": _BY_KEY[key]["direction"], "kind": _BY_KEY[key]["kind"]}
    if bars is None:
        return {**base, "rows": [], "total": 0, "considered": 0, "as_of": None, "unavailable": "No prices are stored for the period this timeframe reads."}
    res = found_for(bars, key, params)
    if params["status"] == "WATCH":
        hits = [(isin, e) for isin, e in res["found"] if e["status"] == "WATCH"]
    else:
        hits = [(isin, e) for isin, e in res["found"] if e["status"] == "SIGNAL" and e["bars_ago"] < params["recent"]]
    meta = meta_fn(cur, [i for i, _ in hits], bars["market_date"])
    rows = []
    for isin, e in hits:
        m = meta.get(isin)
        if not m or not m.get("company_name"):
            continue
        if cap and m.get("cap_class") != cap:
            continue
        rows.append({"isin": isin, "symbol": m.get("symbol"), "name": m.get("company_name"), "sector": m.get("sector"), "cap": m.get("cap_class"), "timeframe": tf,
                     "status": e["status"], "bars_ago": e["bars_ago"], "signal_date": e["date"], "forming": tf != "DAILY" and e["bars_ago"] == 0 and e["status"] == "SIGNAL",
                     "close": e["close"], "neck": e["neck"], "points": e["points"], "facts": e["facts"], "direction": base["direction"], "kind": base["kind"], "newest_bar": e["newest"]})
    rows.sort(key=lambda r: (r["bars_ago"], r["name"] or ""))
    total = len(rows)
    rows = rows[:limit]
    attach_fn(cur, rows)
    t = res["tally"]
    return {**base, "rows": rows, "total": total, "considered": t["tested"], "as_of": str(bars["market_date"]),
            "set_aside": {"unadjusted": t["unadjusted"], "stale": t["stale"], "short": t["short"]}}
