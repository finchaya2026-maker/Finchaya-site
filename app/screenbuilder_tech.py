"""
screenbuilder_tech.py -- the technical readings of ONE stock for the screener builder, in plain Python (no pandas), so that
2,500 stocks on three time frames take seconds. The definitions are the site's own (indicators.py), so a reading here is the same
number the chart, the back-test and the trade plan show:

    RSI     Wilder: average gain and loss with alpha = 1/14 (pandas ewm adjust=False, seeded by the first change). 100 when there
            are no losses, 50 when nothing moved.
    MACD    EMA12 - EMA26 and its 9-bar EMA, the EMAs seeded by the first close (pandas ewm adjust=False), as indicators.macd.
    EMA     SEEDED WITH THE SMA of the first `period` bars, then the usual recursion (what charting platforms draw; indicators.ema).
            An EMA is only read when there are at least 2 x period bars (indicators.enough_bars_for_ema): earlier its seed still
            carries too much of the answer.
    SMA     the mean of the last `period` closes.
A test (screenbuilder_demo/test_tech.py) compares every one of these with indicators.py on random and real series.

compute(d, h, l, c, v, tf) takes the bars oldest-first (d: date ordinals) and returns a dict of readings, or None for a stock with too
few bars. Anything that cannot be read (a moving average that needs more bars than there are) is None, never a guess.
"""

TAIL = 6                          # how many of the newest values of a series are kept, for "crossed within the last k bars"
RSI_PERIOD = 14
MIN_BARS = {"DAILY": 40, "WEEKLY": 30, "MONTHLY": 24}
EMA_PERIODS = (5, 10, 20, 26, 50, 100, 200)
SMA_PERIODS = (5, 10, 20, 50, 100, 200)
RETURN_BARS = {"5": 5, "21": 21, "63": 63, "126": 126, "252": 252}       # a week, a month, 3 months, 6 months, a year of sessions
YEAR_DAYS = 365


def rsi_series(c, period=RSI_PERIOD):
    """RSI at every bar (None for the first, which has no change)."""
    n = len(c)
    out = [None] * n
    if n < 2:
        return out
    alpha = 1.0 / period
    keep = 1.0 - alpha
    ag = al = None
    for i in range(1, n):
        ch = c[i] - c[i - 1]
        g, ls = (ch, 0.0) if ch > 0 else (0.0, -ch)
        if ag is None:
            ag, al = g, ls                                   # pandas seeds with the first value it is given
        else:
            ag = keep * ag + alpha * g
            al = keep * al + alpha * ls
        if al == 0:
            out[i] = 100.0 if ag > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + ag / al)
    return out


def ema_plain(x, span):
    """pandas ewm(span, adjust=False): seeded with the first value."""
    alpha = 2.0 / (span + 1.0)
    keep = 1.0 - alpha
    out = []
    y = None
    for v in x:
        y = v if y is None else keep * y + alpha * v
        out.append(y)
    return out


def macd_series(c, fast=12, slow=26, signal=9):
    """(macd line, signal line, histogram) at every bar."""
    ef, es = ema_plain(c, fast), ema_plain(c, slow)
    line = [a - b for a, b in zip(ef, es)]
    sig = ema_plain(line, signal)
    return line, sig, [a - b for a, b in zip(line, sig)]


def ema_sma_seeded(c, period):
    """The site's EMA: None before bar `period`, the SMA of the first `period` bars at bar period-1, then the recursion."""
    n = len(c)
    out = [None] * n
    if n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    keep = 1.0 - alpha
    y = sum(c[:period]) / period
    out[period - 1] = y
    for i in range(period, n):
        y = keep * y + alpha * c[i]
        out[i] = y
    return out


def _tail(series, k=TAIL):
    return list(series[-k:])


def compute(d, h, l, c, v, tf):
    """The readings of one stock on one time frame. d: date ordinals, h/l/c: highs, lows, closes, v: volumes (or None), oldest first."""
    n = len(c)
    if n < MIN_BARS.get(tf, 30):
        return None
    out = {"n": n, "date": d[-1], "close": c[-1]}
    rs = rsi_series(c)
    out["rsi"] = rs[-1] if n >= 2 * RSI_PERIOD else None
    out["rsi_tail"] = _tail(rs) if n >= 2 * RSI_PERIOD else None
    line, sig, hist = macd_series(c)
    ok = n >= 35                                              # indicators.enough_bars_for_macd
    out["macd"] = line[-1] if ok else None
    out["macd_signal"] = sig[-1] if ok else None
    out["hist_tail"] = _tail(hist) if ok else None
    ema, ema_tail = {}, {}
    for p in EMA_PERIODS:
        if n >= 2 * p:                                        # indicators.enough_bars_for_ema
            s = ema_sma_seeded(c, p)
            ema[p] = s[-1]
            ema_tail[p] = _tail(s)
        else:
            ema[p], ema_tail[p] = None, None
    out["ema"], out["ema_tail"] = ema, ema_tail
    out["sma"] = {p: (sum(c[-p:]) / p if n >= p else None) for p in SMA_PERIODS}
    if tf in ("DAILY", "WEEKLY"):
        start = d[-1] - YEAR_DAYS
        if d[0] <= start + 10:
            i0 = 0
            while d[i0] < start:
                i0 += 1
            out["high52"] = max(h[i0:])
            out["low52"] = min(l[i0:])
        else:
            out["high52"], out["low52"] = None, None
    if tf == "DAILY":
        out["ret"] = {k: ((c[-1] / c[-1 - b] - 1.0) * 100.0 if n > b and c[-1 - b] > 0 else None) for k, b in RETURN_BARS.items()}
        vr = {}
        for days in (20, 50):
            if v is not None and n > days and v[-1] and v[-1] > 0:
                avg = sum(v[-1 - days:-1]) / days
                vr[str(days)] = v[-1] / avg if avg > 0 else None
            else:
                vr[str(days)] = None
        out["vol_ratio"] = vr
    return out


# ---------------------------------------------------------------------------------------------------------
# "crossed within the last k bars", on the kept tails (newest value last)
# ---------------------------------------------------------------------------------------------------------
def crossed(tail, k, up, level=0.0):
    """Did the series cross `level` upward (up=True: the bar before was below it and this bar is at or above it, for a LEVEL; for a
    gap series a cross is from <= 0 to > 0) or downward within the last k bars? None when the tail is too short or has holes."""
    if tail is None or len(tail) < k + 1 or any(x is None for x in tail[-(k + 1):]):
        return None
    for j in range(1, k + 1):
        before, now = tail[-j - 1], tail[-j]
        if up and before < level <= now:
            return True
        if not up and before >= level > now:
            return True
    return False


def crossed_gap(tail, k, up):
    """A gap series (fast minus slow, or MACD histogram) turning positive (up) or negative within the last k bars, as the site's
    crossover screens read it: from <= 0 to > 0, or from >= 0 to < 0."""
    if tail is None or len(tail) < k + 1 or any(x is None for x in tail[-(k + 1):]):
        return None
    for j in range(1, k + 1):
        before, now = tail[-j - 1], tail[-j]
        if up and before <= 0 < now:
            return True
        if not up and before >= 0 > now:
            return True
    return False
