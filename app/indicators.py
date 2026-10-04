"""
MACD, RSI, Supertrend and ADX, implemented in pure pandas.

No TA-Lib / pandas_ta dependency -- those are a versioning headache and you do not
want a broken install five minutes before an investor call.

Both functions take a DataFrame indexed by date with columns: open, high, low, close.
Both return the input frame with indicator columns appended.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- MACD

def macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    price_col: str = "close",
) -> pd.DataFrame:
    """
    Standard MACD.

    Adds:
        macd_line   : EMA(fast) - EMA(slow)
        macd_signal : EMA(signal) of macd_line
        macd_hist   : macd_line - macd_signal
        macd_rising : macd_line > previous macd_line   (bool)
        macd_above  : macd_line > macd_signal          (bool)

    Note on warm-up: EMAs are seeded with `adjust=False`, which matches what
    TradingView and Kite charts show. You need roughly slow + signal (35) bars
    before the values are trustworthy. `enough_bars_for_macd` checks this.
    """
    out = df.copy()
    price = out[price_col]

    ema_fast = price.ewm(span=fast, adjust=False).mean()
    ema_slow = price.ewm(span=slow, adjust=False).mean()

    out["macd_line"] = ema_fast - ema_slow
    out["macd_signal"] = out["macd_line"].ewm(span=signal, adjust=False).mean()
    out["macd_hist"] = out["macd_line"] - out["macd_signal"]

    out["macd_rising"] = out["macd_line"] > out["macd_line"].shift(1)
    out["macd_above"] = out["macd_line"] > out["macd_signal"]

    return out


def enough_bars_for_macd(n_bars: int, slow: int = 26, signal: int = 9) -> bool:
    """MACD needs a warm-up. Below this the value is arithmetic, not information."""
    return n_bars >= (slow + signal)


# --------------------------------------------------------------------------- EMA

def ema(
    df: pd.DataFrame,
    periods=(5, 10, 20, 26, 50, 100, 200),
    price_col: str = "close",
) -> pd.DataFrame:
    """
    Exponential moving averages, one column per period: ema_5, ema_10, ...

    SEEDING IS THE WHOLE STORY HERE, so it is worth stating plainly.

    An EMA is a recursion -- each value depends on the one before it -- so
    it has to start somewhere, and where it starts is a choice:

        pandas .ewm(adjust=False)   seeds with the FIRST CLOSE, and emits a
                                    value from bar 1
        every charting platform     seeds with the SMA of the first `period`
                                    bars, and emits nothing before bar
                                    `period`

    For a short span the two converge almost immediately and nobody would
    notice. For a long one they do not. The seed's remaining influence
    after k further bars is (1 - alpha)^k, and at period 200 alpha is
    2/201 -- so 200 bars later the starting guess STILL carries about 13%
    of the answer. A 200-EMA seeded the pandas way would sit visibly off
    the line on his chart, and "it does not match Kite" is the first thing
    anybody checks.

    So this seeds with the SMA, matching the platforms. It is done
    vectorised rather than with a Python loop: overwrite the value at
    bar `period - 1` with the SMA of the first `period` bars, then run
    .ewm(adjust=False) from that point -- which seeds with the first value
    it is given, and that value is now the SMA. Exact, and fast enough to
    run over two thousand stocks.

    NaN before bar `period`, which becomes a real NULL in the database.
    That is deliberate: a 200-EMA on a stock with 60 bars of history is
    not a small number, it is no number, and inventing one would put a
    confident line on a chart that has nothing behind it.

    MACD IS LEFT ALONE. Its internal EMAs keep pandas' own seeding, both
    because the spans are short enough for it not to matter and because
    changing it would move every stock score already written.
    """
    out = df.copy()
    price = out[price_col].astype(float)
    n = len(price)

    for p in periods:
        col = f"ema_{p}"
        if n < p:
            # Not "zero" and not "the last close" -- absent.
            out[col] = np.nan
            continue

        seeded = price.copy()
        seeded.iloc[p - 1] = price.iloc[:p].mean()
        run = seeded.iloc[p - 1:].ewm(span=p, adjust=False).mean()

        full = pd.Series(np.nan, index=price.index, dtype=float)
        full.iloc[p - 1:] = run.to_numpy()
        out[col] = full

    return out


def enough_bars_for_ema(n_bars: int, period: int) -> bool:
    """Whether an EMA of this period is worth READING, not merely defined.

    Charts draw it from bar `period`, and this stores it from there too so
    the numbers match. But the seed is still washing out at that point, so
    anything SCORING off an EMA should ask this instead.

    Two periods is the practical line: the seed's share is (1 - alpha)^k,
    which by k = period is down to roughly 13% and falling. Below that the
    value is still partly a statement about where the history happened to
    be cut.
    """
    return n_bars >= period * 2


# ---------------------------------------------------------------------------- RSI

def rsi(df: pd.DataFrame, period: int = 14, price_col: str = "close") -> pd.DataFrame:
    """
    Relative Strength Index, Wilder's original smoothing.

    Adds:
        rsi : 0-100

    Wilder's RMA (alpha = 1/period), not a simple rolling mean -- the simple-mean
    variant gives visibly different values and will not match what you see on a
    Kite or TradingView chart, which is the first thing anyone will check.

    Edge case: if a window has no down closes at all, average loss is 0 and RS
    is infinite. RSI is then 100 by definition; the guard below produces that
    rather than a NaN.
    """
    out = df.copy()
    delta = out[price_col].diff()

    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out["rsi"] = 100.0 - (100.0 / (1.0 + rs))
    # No losses in the window -> RSI is 100, not undefined.
    out.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100.0
    out.loc[(avg_loss == 0) & (avg_gain == 0), "rsi"] = 50.0

    return out


def enough_bars_for_rsi(n_bars: int, period: int = 14) -> bool:
    """RSI needs a warm-up like any Wilder-smoothed series. Two periods is the
    practical minimum before the value stops drifting from its seed."""
    return n_bars >= period * 2


# ---------------------------------------------------------------------- Supertrend

def _true_range(df: pd.DataFrame) -> pd.Series:
    """True Range: the greatest of today's range, and today's high/low measured
    against yesterday's close. Shared by ATR (Supertrend) and DI (ADX) so the two
    can never drift apart."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    return pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _wilder_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """
    Average True Range using Wilder's smoothing (RMA), which is what Supertrend
    expects. A simple rolling mean here gives visibly different bands.
    """
    # Wilder's RMA == EWM with alpha = 1/period
    return _true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """
    Supertrend with the standard band carry-forward rule.

    Adds:
        atr             : Wilder ATR
        st_upper        : final upper band
        st_lower        : final lower band
        supertrend      : the active band (the line you see on a chart)
        st_bullish      : True when trend is up   (bool)

    The carry-forward logic is the part people get wrong. The raw bands are
    recomputed every bar, but the *final* bands only move in the trend's favour
    until price breaks through -- that ratchet is what makes it a trailing stop
    rather than a volatility envelope.
    """
    out = df.copy()
    out["atr"] = _wilder_atr(out, period)

    hl2 = (out["high"] + out["low"]) / 2.0
    basic_upper = hl2 + multiplier * out["atr"]
    basic_lower = hl2 - multiplier * out["atr"]

    n = len(out)
    close = out["close"].to_numpy(dtype=float)
    bu = basic_upper.to_numpy(dtype=float)
    bl = basic_lower.to_numpy(dtype=float)

    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    trend = np.ones(n, dtype=int)  # 1 = bullish, -1 = bearish

    if n == 0:
        out["st_upper"] = []
        out["st_lower"] = []
        out["supertrend"] = []
        out["st_bullish"] = []
        return out

    final_upper[0] = bu[0]
    final_lower[0] = bl[0]

    for i in range(1, n):
        # Upper band ratchets down while bearish, resets on a close above it.
        if bu[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
            final_upper[i] = bu[i]
        else:
            final_upper[i] = final_upper[i - 1]

        # Lower band ratchets up while bullish, resets on a close below it.
        if bl[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
            final_lower[i] = bl[i]
        else:
            final_lower[i] = final_lower[i - 1]

        # Trend flips only on a decisive close through the opposing final band.
        if trend[i - 1] == 1 and close[i] < final_lower[i - 1]:
            trend[i] = -1
        elif trend[i - 1] == -1 and close[i] > final_upper[i - 1]:
            trend[i] = 1
        else:
            trend[i] = trend[i - 1]

    out["st_upper"] = final_upper
    out["st_lower"] = final_lower
    out["st_bullish"] = trend == 1
    out["supertrend"] = np.where(trend == 1, final_lower, final_upper)

    return out


def enough_bars_for_supertrend(n_bars: int, period: int = 10) -> bool:
    return n_bars >= period * 2


# ---------------------------------------------------------------------------- ADX

def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Average Directional Index, Wilder's original construction.

    Adds:
        plus_di    : +DI, 0-100
        minus_di   : -DI, 0-100
        adx        : 0-100, trend STRENGTH only -- it says nothing about direction
        adx_rising : adx > previous adx   (bool)

    Method:
        +DM = today's high - yesterday's high, kept only if it exceeds -DM and > 0
        -DM = yesterday's low - today's low,   kept only if it exceeds +DM and > 0
        +DI = 100 * RMA(+DM) / ATR
        -DI = 100 * RMA(-DM) / ATR
        DX  = 100 * |+DI - -DI| / (+DI + -DI)
        ADX = RMA(DX)

    The double smoothing (DX is already smoothed, then ADX smooths it again) is
    why ADX lags badly and why it needs a long warm-up: the series is seeded from
    a single raw DX reading and takes roughly 3 x period bars to shake that seed
    off. `enough_bars_for_adx` encodes that, and it is deliberately stricter than
    the MACD/RSI/Supertrend checks.

    Two guards worth knowing about:
      - ATR = 0 (a dead, gapless bar) would divide by zero; DI is left NaN there
        rather than becoming inf.
      - +DI + -DI = 0 (no directional movement at all either way) leaves DX NaN,
        which pandas' EWM skips rather than poisoning every later value.

    NOTE ON `adx_rising`: the comparison is strict. An exactly-flat ADX therefore
    reads as NOT rising and lands in the "falling" bucket. On float-valued data
    an exact tie effectively never happens, but on a synthetic or heavily rounded
    series it can, so the tie-break is stated rather than left to be discovered.
    """
    out = df.copy()

    up_move = out["high"].diff()
    down_move = -out["low"].diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0),
        index=out.index,
        dtype=float,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0),
        index=out.index,
        dtype=float,
    )

    alpha = 1.0 / period
    atr = _true_range(out).ewm(alpha=alpha, adjust=False).mean()
    atr_safe = atr.replace(0.0, np.nan)

    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_safe
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_safe

    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum

    out["plus_di"] = plus_di
    out["minus_di"] = minus_di
    out["adx"] = dx.ewm(alpha=alpha, adjust=False).mean()
    out["adx_rising"] = out["adx"] > out["adx"].shift(1)

    return out


def enough_bars_for_adx(n_bars: int, period: int = 14) -> bool:
    """ADX is smoothed twice, so two periods is not enough -- the value is still
    visibly walking away from its seed. Three is the practical floor."""
    return n_bars >= period * 3


# ------------------------------------------------------------------ monthly resample

def to_monthly(daily: pd.DataFrame) -> pd.DataFrame:
    """
    Build monthly OHLC candles from daily candles.

    Kite does not serve a monthly interval, so this is the workaround you flagged:
        open  = open of the first trading day of the month
        high  = highest high in the month
        low   = lowest low in the month
        close = close of the last trading day of the month
        volume = sum

    The index is set to the LAST TRADING DAY actually present in the data, not the
    calendar month-end. That matters -- 31 Aug is a Sunday, and labelling the bar
    with a date the market was shut invites off-by-one errors when you later join
    against a portfolio disclosure date.
    """
    if daily.empty:
        return daily.copy()

    d = daily.sort_index()
    grouped = d.resample("MS")  # month start buckets

    monthly = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
        }
    )
    if "volume" in d.columns:
        monthly["volume"] = grouped["volume"].sum()

    # Replace the synthetic month-start label with the real last trading day.
    last_day = d.index.to_series().resample("MS").max()
    monthly["last_trading_day"] = last_day
    monthly = monthly.dropna(subset=["close"])
    monthly.index = pd.DatetimeIndex(monthly["last_trading_day"])
    monthly = monthly.drop(columns=["last_trading_day"])
    monthly.index.name = "date"

    return monthly


def to_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Same idea, weekly. Monthly MACD moves about twice a year; weekly is often
    a more useful timeframe for a score you intend to refresh every month."""
    if daily.empty:
        return daily.copy()
    d = daily.sort_index()
    g = d.resample("W-FRI")
    weekly = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
        }
    )
    if "volume" in d.columns:
        weekly["volume"] = g["volume"].sum()
    return weekly.dropna(subset=["close"])


def to_weekly_real_days(daily: pd.DataFrame) -> pd.DataFrame:
    """Weekly bars labelled with the last day that actually TRADED.

    to_weekly above labels each bar with the calendar Friday, so a run on
    a Wednesday stamps the current bar with a Friday that has not happened
    yet -- a date in the future, sitting in a table where every other date
    is real.

    This lived in fetch_technicals.py, which cannot be imported without
    pulling in the Kite client. Anything that wants weekly bars from
    stored candles -- a backfill, a one-off check -- then needed a broker
    session it had no use for. It belongs beside to_weekly and to_monthly.
    """
    weekly = to_weekly(daily)
    if weekly.empty:
        return weekly
    last_day = daily.index.to_series().resample("W-FRI").max()
    weekly = weekly.join(last_day.rename("real_day"))
    weekly = weekly.dropna(subset=["real_day"])
    weekly.index = pd.DatetimeIndex(weekly["real_day"])
    weekly = weekly.drop(columns=["real_day"])
    weekly.index.name = "date"
    return weekly
