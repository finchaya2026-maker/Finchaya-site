"""
screenbuilder_engine.py -- "Build your own screener": the catalogue of things a stock can be filtered on, the rules a person writes with them,
and the evaluation of those rules. PURE (no database, no web): screenbuilder_data.py supplies the readings, screenbuilder_api.py the web.
---------------------------------------------------------------------------------------------------------------------------
A SCREEN is
    universe   which stocks to look at: market-cap classes, sectors, industries (all of them when empty)
    rules      a tree of conditions joined by ALL (and) / ANY (or), nested up to three levels:
                   "chart pattern Change in Dow Theory (daily) AND weekly RSI above 50 AND at least 10 funds newly bought it
                    AND (P/E below the industry average OR P/B below the industry average)"
A CONDITION is one field with its settings (params) and a test:
    a NUMBER  field  (RSI, P/E, funds that bought, % held by foreign institutions ...)  is more than / at least / less than / at most / between
                     a number, or -- for the ratios that have an industry -- compared with the INDUSTRY AVERAGE (x a multiplier)
    a FLAG    field  (a chart pattern, a crossover, MACD above its signal ...)  has happened / has not happened

THE FOUR GROUPS OF FIELDS (each says where its numbers come from; the page shows it)
    technical    RSI, MACD, moving averages, 52-week range, price change, volume -- on daily, weekly or monthly bars
    patterns     the chart-pattern screens the site already has: Dow Theory turns, Fibonacci 62-78%, expanded flat, multi-year breakout
    funds        mutual-fund activity in the latest month (how many funds newly bought, added, trimmed, exited, hold)
    ownership    foreign institutions, mutual funds, promoters, pledged, institutions, individuals: level and change over the quarter
    fundamental  market cap, P/E, P/B, PEG, ROE, debt/equity, interest cover, sales / profit / EPS growth
    events       days until the next results

THREE-VALUED LOGIC. A stock for which a reading cannot be made (no balance sheet, too little history for a 200-day average) is neither
"met" nor "not met": the condition is UNKNOWN for it. ALL is false when any part is false, unknown when none is false but one is unknown; ANY is
true when any part is true, unknown when none is true but one is unknown. Only TRUE stocks are listed, and the answer counts the unknowns, so a
screen can never look cleaner than the data behind it.

"INDUSTRY AVERAGE" is the MEDIAN of the same ratio over the stocks of the same industry that have one (positive, for P/E, P/B and PEG), and
only when at least 5 do: a few extreme companies cannot drag a median the way they drag a mean. The page and the sentences say so. A MULTIPLE of an
average that is zero or negative (profit growth in a shrinking industry) is not a meaningful bar, so "1.5 times the average" is UNKNOWN there.

NOTHING HERE IS A SCORE, A RATING OR A RECOMMENDATION. A screen lists stocks that match what the person asked for.
"""

import json
import math
from datetime import date

MAX_CONDITIONS = 20
MAX_DEPTH = 3
MAX_PATTERN_SETTINGS = 6
MAX_UNIVERSE_LIST = 25
MAX_TEXT = 80
CAPS = ("Large", "Mid", "Small")

TF_OPTIONS = [["DAILY", "Daily"], ["WEEKLY", "Weekly"], ["MONTHLY", "Monthly"]]
TF_WORD = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly"}
WITHIN_OPTIONS = [[1, "1 bar (the latest)"], [2, "2 bars"], [3, "3 bars"], [5, "5 bars"]]
DIR_OPTIONS = [["up", "upward (above)"], ["down", "downward (below)"]]
NUM_OPS = [["gt", "is more than"], ["gte", "is at least"], ["lt", "is less than"], ["lte", "is at most"], ["between", "is between"]]
OP_WORD = {k: v for k, v in NUM_OPS}
INDUSTRY_MULTIPLIERS = [0.5, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 2.0]
EMA_FAST = [5, 10, 20, 26, 50]
EMA_SLOW = [20, 26, 50, 100, 200]
EMA_ALL = [5, 10, 20, 26, 50, 100, 200]
SMA_ALL = [5, 10, 20, 50, 100, 200]


class SpecError(ValueError):
    """A screen that cannot be run, with the reason in words the person can act on."""


# =====================================================================
# THE CATALOGUE
# =====================================================================
GROUPS = [
    {"key": "technical", "name": "Technical", "what": "Indicators read from the daily, weekly or monthly price bars."},
    {"key": "candles", "name": "Candles (open, high, low, close)", "what": "Compare the open, high, low and close of one candle with another, or look at a candle's shape."},
    {"key": "patterns", "name": "Chart patterns", "what": "The pattern screens the site already has, used as a condition."},
    {"key": "funds", "name": "Mutual funds", "what": "What mutual funds did with the stock in their latest monthly portfolios."},
    {"key": "ownership", "name": "Who owns it", "what": "The quarterly shareholding pattern each company files: foreign institutions, mutual funds, promoters."},
    {"key": "fundamental", "name": "Fundamentals", "what": "Ratios and growth from the company's filed results and balance sheet, at the latest price."},
    {"key": "events", "name": "Coming up", "what": "Dates the exchanges have announced."},
]
GROUP_KEYS = {g["key"] for g in GROUPS}

FIELDS = {}


def _choice(param, label, options, default):
    return {"param": param, "label": label, "kind": "choice", "default": default, "options": options}


def _number(param, label, default, lo, hi, step=1):
    return {"param": param, "label": label, "kind": "number", "default": default, "min": lo, "max": hi, "step": step}


TF = _choice("tf", "Time frame", TF_OPTIONS, "DAILY")
TF_DW = _choice("tf", "Time frame", [["DAILY", "Daily"], ["WEEKLY", "Weekly"]], "DAILY")


def _fmt(v, decimals):
    if v is None:
        return ""
    if decimals == 0:
        return "{:,.0f}".format(v)
    s = "{:,.{d}f}".format(v, d=decimals)
    return s.rstrip("0").rstrip(".") if "." in s else s


def _fmt_exact(v):
    """A number as it was typed: thousands separators, no trailing zeros, up to six decimals."""
    s = "{:,.6f}".format(v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def number_field(key, group, label, subject, unit="", lo=-1e9, hi=1e9, decimals=1, params=None, industry=False, needs=None, help="",
                 default=None, validate=None, source=""):
    FIELDS[key] = {"key": key, "group": group, "label": label, "kind": "number", "unit": unit, "min": lo, "max": hi, "decimals": decimals,
                   "params": params or [], "industry": industry, "needs": needs or group, "help": help, "subject": subject,
                   "default": default, "validate": validate, "source": source}


def flag_field(key, group, label, subject, params=None, needs=None, help="", yes=None, no=None, validate=None, source=""):
    FIELDS[key] = {"key": key, "group": group, "label": label, "kind": "flag", "unit": "", "params": params or [], "industry": False,
                   "needs": needs or group, "help": help, "subject": subject, "yes": yes or subject, "no": no, "validate": validate,
                   "source": source}


def _bars(n):
    return "%d bar%s" % (n, "" if n == 1 else "s")


# The longest moving average each bar size has the history for (an EMA is read only with 2 x its period in bars, a SMA with its period): about
# 440 daily, 270 weekly and 65 monthly bars are held. Asking for more would be "no reading" for every stock, so it is refused in words instead.
EMA_MAX = {"DAILY": 200, "WEEKLY": 100, "MONTHLY": 26}
SMA_MAX = {"DAILY": 200, "WEEKLY": 200, "MONTHLY": 50}


def _too_long(kind, per, tf):
    return SpecError("We do not hold enough %s history for a %d-bar %s average (the longest is %d bars). Choose a shorter average or a shorter time frame." % (
        TF_WORD[tf], per, kind, (EMA_MAX if kind == "EMA" else SMA_MAX)[tf]))


def _check_fast_slow(p):
    if int(p["fast"]) >= int(p["slow"]):
        raise SpecError("The fast moving average must be shorter than the slow one (you chose %s and %s)." % (p["fast"], p["slow"]))
    if int(p["slow"]) > EMA_MAX[p["tf"]]:
        raise _too_long("EMA", int(p["slow"]), p["tf"])


def _check_ma(p):
    per = int(p["period"])
    if p["kind"] == "SMA" and per not in SMA_ALL:
        raise SpecError("There is no simple %s-bar average here; choose 5, 10, 20, 50, 100 or 200." % p["period"])
    if per > (EMA_MAX if p["kind"] == "EMA" else SMA_MAX)[p["tf"]]:
        raise _too_long(p["kind"], per, p["tf"])


def _check_fib_band(p):
    if float(p["retr_high"]) < float(p["retr_low"]):
        raise SpecError("Fibonacci retracement: the second percentage must not be smaller than the first (you chose %s and %s)." % (_fmt(p["retr_low"], 1), _fmt(p["retr_high"], 1)))


def _check_flat_band(p):
    if float(p["b_high"]) < float(p["b_low"]):
        raise SpecError("Expanded flat: the upper limit of B must not be smaller than the lower limit (you chose %s and %s)." % (_fmt(p["b_low"], 2), _fmt(p["b_high"], 2)))


# ---- technical ---------------------------------------------------------------------------------------------------
_TECH_SOURCE = "Daily prices read from the exchange end-of-day files; weekly and monthly bars are built from them (the week or month in progress counts as it stands)."
number_field("rsi", "technical", "RSI (14)", lambda p: "RSI (14) on %s bars" % TF_WORD[p["tf"]], lo=0, hi=100, decimals=1, params=[TF],
             needs="tech", help="Wilder's Relative Strength Index, 14 bars. Above 50: gains have outweighed losses lately; above 70 is often called overbought.",
             default=50, source=_TECH_SOURCE)
flag_field("rsi_cross", "technical", "RSI crossed a level",
           lambda p: "RSI (14) on %s bars crossed %s %s within the last %s" % (TF_WORD[p["tf"]], "up through" if p["direction"] == "up" else "down through",
                                                                              _fmt(float(p["level"]), 1), _bars(int(p["within"]))),
           params=[TF, _choice("direction", "Direction", [["up", "up through"], ["down", "down through"]], "up"),
                   _number("level", "Level", 50, 5, 95, 1), _choice("within", "Within the last", WITHIN_OPTIONS, 1)],
           needs="tech", help="A crossing happened in the last few bars (as the RSI screen reads it: the bar before was below, this one at or above).",
           source=_TECH_SOURCE)
flag_field("macd_state", "technical", "MACD position",
           lambda p: {"above_signal": "The MACD line is above its signal line on %s bars", "below_signal": "The MACD line is below its signal line on %s bars",
                      "above_zero": "The MACD line is above zero on %s bars", "below_zero": "The MACD line is below zero on %s bars"}[p["state"]]
           % TF_WORD[p["tf"]],
           params=[TF, _choice("state", "State", [["above_signal", "above its signal line"], ["below_signal", "below its signal line"],
                                                  ["above_zero", "above zero"], ["below_zero", "below zero"]], "above_signal")],
           needs="tech", help="MACD is the 12-bar EMA minus the 26-bar EMA; its signal line is the 9-bar EMA of that.", source=_TECH_SOURCE)
flag_field("macd_cross", "technical", "MACD crossover",
           lambda p: "The MACD line crossed %s its signal line on %s bars within the last %s" % ("above" if p["direction"] == "up" else "below",
                                                                                               TF_WORD[p["tf"]], _bars(int(p["within"]))),
           params=[TF, _choice("direction", "Direction", [["up", "above (positive crossover)"], ["down", "below (negative crossover)"]], "up"),
                   _choice("within", "Within the last", WITHIN_OPTIONS, 1)],
           needs="tech", help="The MACD histogram turned positive (or negative) in the last few bars.", source=_TECH_SOURCE)
number_field("ma_dist", "technical", "Price vs a moving average", lambda p: "The price's distance above its %s %d (%s bars)" % (p["kind"], int(p["period"]), TF_WORD[p["tf"]]),
             unit="%", lo=-100, hi=1000, decimals=1,
             params=[TF, _choice("kind", "Average", [["EMA", "Exponential (EMA)"], ["SMA", "Simple (SMA)"]], "EMA"),
                     _choice("period", "Period", [[p, str(p)] for p in EMA_ALL], 50)],
             needs="tech", default=0, help="Positive: the price is above the average; negative: below. An average needs enough bars (an EMA needs twice its period).",
             validate=_check_ma, source=_TECH_SOURCE)
number_field("ma_gap", "technical", "Fast EMA vs slow EMA", lambda p: "The %d EMA's distance above the %d EMA (%s bars)" % (int(p["fast"]), int(p["slow"]), TF_WORD[p["tf"]]),
             unit="%", lo=-100, hi=1000, decimals=2,
             params=[TF, _choice("fast", "Fast EMA", [[x, str(x)] for x in EMA_FAST], 5), _choice("slow", "Slow EMA", [[x, str(x)] for x in EMA_SLOW], 50)],
             needs="tech", default=0, help="Positive: the fast average is above the slow one (an uptrend by that measure).", validate=_check_fast_slow, source=_TECH_SOURCE)
flag_field("ema_cross", "technical", "Moving-average crossover",
           lambda p: "The %d EMA crossed %s the %d EMA on %s bars within the last %s" % (int(p["fast"]), "above" if p["direction"] == "up" else "below", int(p["slow"]),
                                                                                       TF_WORD[p["tf"]], _bars(int(p["within"]))),
           params=[TF, _choice("fast", "Fast EMA", [[x, str(x)] for x in EMA_FAST], 5), _choice("slow", "Slow EMA", [[x, str(x)] for x in EMA_SLOW], 50),
                   _choice("direction", "Direction", [["up", "above (golden-style cross)"], ["down", "below (death-style cross)"]], "up"),
                   _choice("within", "Within the last", WITHIN_OPTIONS, 3)],
           needs="tech", help="A crossing in the last few bars, read as the Crossovers screen reads it.", validate=_check_fast_slow, source=_TECH_SOURCE)
number_field("from_high", "technical", "Below the 52-week high", lambda p: "The price's distance below its 52-week high",
             unit="%", lo=0, hi=100, decimals=1, params=[], needs="tech", default=10,
             help="0 means at the high. The 52-week high is the highest HIGH of the last 365 days (needs a year of history).", source=_TECH_SOURCE)
number_field("from_low", "technical", "Above the 52-week low", lambda p: "The price's distance above its 52-week low",
             unit="%", lo=0, hi=100000, decimals=1, params=[], needs="tech", default=50,
             help="The 52-week low is the lowest LOW of the last 365 days (needs a year of history).", source=_TECH_SOURCE)
number_field("ret", "technical", "Price change", lambda p: "The price change over %s" % {"5": "a week", "21": "a month", "63": "3 months", "126": "6 months", "252": "a year"}[str(p["bars"])],
             unit="%", lo=-100, hi=100000, decimals=1,
             params=[_choice("bars", "Over", [["5", "1 week (5 sessions)"], ["21", "1 month"], ["63", "3 months"], ["126", "6 months"], ["252", "1 year"]], "63")],
             needs="tech", default=0, help="Close to close, in sessions, on closes adjusted for splits and bonuses where the stored prices show them; a stock with a jump the "
             "records cannot explain has no value.", source=_TECH_SOURCE)
number_field("vol_ratio", "technical", "Volume vs its average", lambda p: "The latest day's volume, as a multiple of its %s-day average" % p["days"], unit="x", lo=0, hi=1000,
             decimals=1, params=[_choice("days", "Average of", [["20", "20 days"], ["50", "50 days"]], "20")], needs="tech", default=2,
             help="2 means twice the usual volume. The average is of the days before the latest one.", source=_TECH_SOURCE)


# ---- candles: open, high, low, close --------------------------------------------------------------------------------
# One candle against another ("the close of the latest bar is above the high of the bar before it"), a candle's shape, a run of higher closes, a candle's own
# price. A bar is counted back from the newest: 0 is the latest, 1 the one before it, up to 5. The bars come from the daily candles (adjusted for splits and bonuses);
# a weekly bar is the week's first open, highest high, lowest low and last close, a monthly bar the month's. A week or month still in progress counts as it stands
# unless "finished bars only" is chosen (a week is treated as finished from its Friday, a month from its last weekday).
PRICE_OPTIONS = [["O", "Open"], ["H", "High"], ["L", "Low"], ["C", "Close"]]
PRICE_WORD = {"O": "open", "H": "high", "L": "low", "C": "close"}
BAR_OPTIONS = [[0, "the latest bar"], [1, "1 bar back (the previous bar)"], [2, "2 bars back"], [3, "3 bars back"], [4, "4 bars back"], [5, "5 bars back"]]
BARS_MODE = _choice("bars", "Bars", [["latest", "Latest bar as it stands"], ["finished", "Finished bars only"]], "latest")
RIGHT_TF = _choice("right_tf", "Right side on", [["SAME", "The same time frame"], ["DAILY", "Daily bars"], ["WEEKLY", "Weekly bars"], ["MONTHLY", "Monthly bars"]], "SAME")
MAX_BAR = 5
SHAPES = [["bullish", "a bullish candle (closed above its open)"], ["bearish", "a bearish candle (closed below its open)"], ["doji", "a doji (open and close within 10% of the range)"],
          ["inside", "an inside bar (high and low within the bar before)"], ["outside", "an outside bar (high above and low below the bar before)"],
          ["gap_up", "a gap up (opened above the bar before's high)"], ["gap_down", "a gap down (opened below the bar before's low)"],
          ["close_top", "closed in the top quarter of its range"], ["close_bottom", "closed in the bottom quarter of its range"],
          ["higher_hl", "a higher high and a higher low than the bar before"], ["lower_hl", "a lower high and a lower low than the bar before"]]
SHAPE_WORD = {k: v for k, v in SHAPES}


def _tf_of(p, side):
    return p["tf"] if side == "left" or p.get("right_tf", "SAME") == "SAME" else p["right_tf"]


def _bar_phrase(tf, bar, mode):
    w = TF_WORD[tf]
    if bar == 0:
        return "the latest %s%s bar" % ("finished " if mode == "finished" else "", w)
    if bar == 1:
        return "the previous %s%s bar" % ("finished " if mode == "finished" else "", w)
    return "the %s%s bar %d bars back" % ("finished " if mode == "finished" else "", w, bar)


def _side(p, side):
    pre = "left_" if side == "left" else "right_"
    return "the %s of %s" % (PRICE_WORD[p[pre + "price"]], _bar_phrase(_tf_of(p, side), int(p[pre + "bar"]), p["bars"]))


def _check_candle_sides(p):
    if _tf_of(p, "left") == _tf_of(p, "right") and p["left_price"] == p["right_price"] and int(p["left_bar"]) == int(p["right_bar"]):
        raise SpecError("Both sides are the same thing (the %s of the same bar). Change the bar, the price or the time frame on one side." % PRICE_WORD[p["left_price"]])


_CANDLE_SIDES = [_choice("left_price", "Left side: price", PRICE_OPTIONS, "C"), _choice("left_bar", "Left side: bar", BAR_OPTIONS, 0)]
_CANDLE_RIGHT = [RIGHT_TF, _choice("right_price", "Right side: price", PRICE_OPTIONS, "H"), _choice("right_bar", "Right side: bar", BAR_OPTIONS, 1)]
_CANDLE_SRC = "Daily candles read from the exchange end-of-day files, adjusted for splits and bonuses where the stored prices show them; weekly and monthly candles are built from them."

flag_field("candle_compare", "candles", "Candle price vs candle price",
           lambda p: "%s is %s %s" % (_side(p, "left").capitalize(), ("%s" % p["relation"]) if not float(p["margin"]) else "more than %s%% %s" % (_fmt_exact(float(p["margin"])), p["relation"]), _side(p, "right")),
           params=[TF, BARS_MODE] + _CANDLE_SIDES + [_choice("relation", "Is", [["above", "above"], ["below", "below"]], "above"), _number("margin", "By more than (%)", 0, 0, 100, 0.1)] + _CANDLE_RIGHT,
           needs="tech", help="For example: the close of the latest bar is above the high of the bar before it (a breakout of the previous candle), or today's low is below yesterday's low. "
           "Add a margin to ask for a clear move rather than a tick.", validate=_check_candle_sides, source=_CANDLE_SRC)
number_field("candle_diff", "candles", "Candle price distance", lambda p: "The percentage by which %s is %s %s" % (_side(p, "left"), p["relation"], _side(p, "right")), unit="%", lo=-100, hi=100000, decimals=2,
             params=[TF, BARS_MODE] + _CANDLE_SIDES + [_choice("relation", "Measured", [["above", "above"], ["below", "below"]], "above")] + _CANDLE_RIGHT,
             needs="tech", default=1, help="The same comparison as a percentage: how much higher the left price is than the right one (or, measured 'below', how much lower). Negative when it is the other way.",
             validate=_check_candle_sides, source=_CANDLE_SRC)
number_field("candle_price", "candles", "A candle's price", lambda p: "The %s of %s" % (PRICE_WORD[p["price"]], _bar_phrase(p["tf"], int(p["bar"]), p["bars"])), unit="₹", lo=0, hi=10000000, decimals=2,
             params=[TF, BARS_MODE, _choice("price", "Price", PRICE_OPTIONS, "C"), _choice("bar", "Bar", BAR_OPTIONS, 0)], needs="tech", default=100,
             help="The open, high, low or close of one candle, in rupees.", source=_CANDLE_SRC)
flag_field("candle_shape", "candles", "A candle's shape",
           lambda p: "%s was %s" % (_bar_phrase(p["tf"], int(p["bar"]), p["bars"]).capitalize(), SHAPE_WORD[p["shape"]]),
           params=[TF, BARS_MODE, _choice("bar", "Bar", BAR_OPTIONS, 0), _choice("shape", "Shape", SHAPES, "bullish")], needs="tech",
           help="Bullish or bearish candle, doji, inside or outside bar, gap up or down, where it closed in its range, higher highs and lows. The shapes that compare with the bar before need that bar.",
           source=_CANDLE_SRC)
flag_field("candle_streak", "candles", "A run of higher or lower closes",
           lambda p: "The last %d %s bars each closed %s the bar before it" % (int(p["count"]), TF_WORD[p["tf"]], "higher than" if p["direction"] == "up" else "lower than") + (
               " (finished bars only)" if p["bars"] == "finished" else ""),
           params=[TF, BARS_MODE, _choice("direction", "Direction", [["up", "higher"], ["down", "lower"]], "up"), _choice("count", "Times in a row", [[n, str(n)] for n in (2, 3, 4, 5)], 3)],
           needs="tech", help="For example three daily bars in a row that each closed higher than the one before.", source=_CANDLE_SRC)


# ---- chart patterns (their settings are the screens' own, filled in by register_patterns) ---------------------------
def _sessions(n):
    return "%d session%s" % (int(n), "" if int(n) == 1 else "s")


def _basis_word(p):
    return "candlesticks (highs and lows)" if p.get("basis") == "CANDLE" else "closing prices"


def _swing_word(p):
    n = int(p.get("swing", 1))
    return "every swing counts" if n == 1 else "a swing is a bar higher or lower than %d bars each side" % n


# Each of these says EVERY setting that changes the answer, so that two conditions that read the same ARE the same.
def _dow_words(spec):
    up = spec.get("direction") == "up"

    def say(p):
        if p.get("measure") == "MAJOR":
            look = "all the history read" if int(p["lookback"]) >= 400 else "the last %d bars" % int(p["lookback"])
            how = "big-picture reading: the price has gone %s the %s a %s of at least %s%% began from (looking at %s)" % (
                "above" if up else "below", "high" if up else "low", "fall" if up else "rise", _fmt(float(p["fall"]), 1), look)
        else:
            how = "short-term reading, swing by swing (%s)" % _swing_word(p)
        return "Change in Dow Theory: turning %s on %s bars, within the last %s; %s; %s" % ("up" if up else "down", TF_WORD[p["timeframe"]], _bars(int(p["recent"])), how, _basis_word(p))
    return say


def _fib_words(spec):
    return lambda p: ("Price retraced %s%% to %s%% (give or take %s points) of a rise of at least %s%% from the bottom of the last %d bars, on %s bars; %s" % (
        _fmt(float(p["retr_low"]), 1), _fmt(float(p["retr_high"]), 1), _fmt(float(p["leeway"]), 1), _fmt(float(p["rise"]), 0), int(p["lookback"]), TF_WORD[p["timeframe"]],
        _basis_word(p)))


def _flat_words(spec):
    up = spec.get("direction") == "up"

    def say(p):
        move = "pullback" if up else "bounce"
        return ("Expanded flat (A-B-C) %s shape on %s bars: B is %s to %s times the first %s %s A, C about %s times that %s %s B (tolerance: %s), formed within %d bars "
                "after a %s of at least %s%%, with C made within the last %s; %s; %s" % (
                    "uptrend" if up else "downtrend", TF_WORD[p["timeframe"]], _fmt(float(p["b_low"]), 2), _fmt(float(p["b_high"]), 2), move, "above" if up else "below",
                    _fmt(float(p["c_ratio"]), 2), move, "below" if up else "above", p["exact"], int(p["span"]), "rise" if up else "fall", _fmt(float(p["rise"]), 0),
                    _bars(int(p["recent"])), _swing_word(p), _basis_word(p)))
    return say


def _ipo_trend_words(spec):
    def say(p):
        which = {"HOLD": "scenario 1 (the price rose after listing, pulled back to a higher low, and has gone above the high before the pullback)",
                 "DOWN": "scenario 2 (the price kept falling after listing, formed a higher low, and has gone above the last lower high)"}.get(
                     p.get("scenario"), "scenario 1 (higher low, then a new high) or scenario 2 (downtrend, higher low, then the last lower high broken)")
        move = float(p.get("move") or 0)
        return ("IPO trend after listing, on %s bars: %s, for stocks listed in the last %d days, signal made within the last %s%s; %s; %s" % (
            TF_WORD[p["timeframe"]], which, int(p["listed_within"]), _bars(int(p["recent"])),
            ", the first move being at least %s%%" % _fmt(move, 1) if move else "", _swing_word(p), _basis_word(p)))
    return say


def _higher_low_words(spec):
    def say(p):
        rise, pull = float(p.get("rise") or 0), float(p.get("pullback") or 0)
        return ("Higher low on %s bars: the latest swing low is higher than the one before it%s, after a pullback of %s from the high, confirmed within the last %s and not "
                "undercut since%s; %s; %s" % (
                    TF_WORD[p["timeframe"]], " by at least %s%%" % _fmt(rise, 1) if rise else "", "at least %s%%" % _fmt(pull, 1) if pull else "any size", _bars(int(p["recent"])),
                    ", with a higher high before it too" if p.get("trend") == "HH" else "", _swing_word(p), _basis_word(p)))
    return say


def _breakout_words(spec):
    def say(p):
        s = "Multi-year breakout: went above the highest %s of the last %s year%s within the last %s" % ("high" if p.get("basis") == "CANDLE" else "close", p["years"],
                                                                                              "" if int(p["years"]) == 1 else "s", _sessions(p["recent"]))
        if p.get("hold") == "above":
            s += ", and is still above it"
        if int(p.get("age") or 0):
            s += ", that high being at least %s days old" % p["age"]
        if float(p.get("volume") or 0):
            s += ", on volume of at least %sx the 50-day average" % _fmt(float(p["volume"]), 1)
        return s + "; " + _basis_word(p)
    return say


_PATTERN_WORDS = {"dow_change": _dow_words, "dow_change_down": _dow_words, "fib_retrace": _fib_words, "expanded_flat": _flat_words,
                  "expanded_flat_down": _flat_words, "multiyear_breakout": _breakout_words, "ipo_trend": _ipo_trend_words, "higher_low": _higher_low_words}
_PATTERN_VALIDATE = {"fib_retrace": _check_fib_band, "expanded_flat": _check_flat_band, "expanded_flat_down": _check_flat_band}


def register_patterns(modules):
    """modules: [(screen_key, catalogue_entry)] of the pattern screens present on this server. Their own controls become the condition's settings."""
    for key, spec in modules:
        params = [dict(c) for c in spec["controls"]]

        def subject(p, spec=spec, params=params):
            bits = []
            for c in params:
                if c.get("when"):
                    other = p.get(c["when"]["param"])
                    if str(other) != str(c["when"]["value"]):
                        continue
                v = p.get(c["param"])
                # the time frame is always said; any other setting only when it is not the screen's own default
                if c["param"] != "timeframe" and str(v) == str(c["default"]):
                    continue
                if c["kind"] == "choice":
                    lab = next((o[1] for o in c["options"] if str(o[0]) == str(v)), v)
                    if c["param"] == "timeframe":
                        bits.append(str(lab).split(" ")[0].lower())
                    elif c["param"] == "basis":
                        bits.append(str(lab).split(" (")[0].lower())
                    else:
                        bits.append("%s: %s" % (c["label"].rstrip(":").lower(), str(lab).lower()))
                else:
                    bits.append("%s %s" % (c["label"].rstrip(":").lower(), _fmt(float(v), 2)))
            return "%s (%s)" % (spec["name"], "; ".join(bits)) if bits else spec["name"]
        words = _PATTERN_WORDS.get(key)
        flag_field(key, "patterns", spec["name"], words(spec) if words else subject, params=params, needs="pattern", help=spec["what"],
                   yes=None, validate=_PATTERN_VALIDATE.get(key), source="Computed from the daily price bars exactly as on the Screener page (same rules, same settings).")


# ---- mutual funds ------------------------------------------------------------------------------------------------
_FUND_SRC = "The funds' monthly portfolio disclosures: latest month against the month before, for funds that filed both (the Screener page's Fund flows)."
for _k, _l, _s, _h in (
        ("funds_new", "Funds that newly bought it", "The number of funds that newly bought the stock last month", "Funds that held none a month earlier and hold some now."),
        ("funds_buying", "Funds that added to it", "The number of funds that added to their holding last month", "Funds that raised their share count by more than the trading band, bonus and split effects removed."),
        ("funds_selling", "Funds that trimmed it", "The number of funds that trimmed their holding last month", "Funds that cut their share count, bonus and split effects removed."),
        ("funds_exited", "Funds that exited it", "The number of funds that sold out of the stock last month", "Funds that held it a month earlier and hold none now."),
        ("funds_holding", "Funds holding it", "The number of funds that hold the stock", "Funds with a position in the latest monthly portfolio."),
        ("funds_net", "Funds adding minus leaving", "Funds that bought or added, minus funds that trimmed or exited,", "(new + added) minus (trimmed + exited), last month.")):
    number_field(_k, "funds", _l, (lambda s: (lambda p: s))(_s), unit="funds", lo=-100000, hi=100000, decimals=0, needs="funds", help=_h, default=10, source=_FUND_SRC)

# ---- who owns it -------------------------------------------------------------------------------------------------
_OWN_SRC = "The shareholding pattern each company files with the exchange every quarter (the newest quarter most companies have filed). A change that looks like holders being re-labelled, not trading, has no value."
for _k, _l, _s, _unit, _lo, _hi, _h in (
        ("fii_pct", "Foreign institutions hold", "The share of the company held by foreign institutions", "%", 0, 100, "FIIs/FPIs, % of all shares."),
        ("fii_change", "Foreign institutions: change", "The change in the foreign institutions' holding over the quarter", "pts", -100, 100, "Percentage points, latest quarter against the one before."),
        ("mf_pct", "Mutual funds hold", "The share of the company held by mutual funds", "%", 0, 100, "All mutual funds together, % of all shares."),
        ("mf_change", "Mutual funds: change", "The change in the mutual funds' holding over the quarter", "pts", -100, 100, "Percentage points, latest quarter against the one before."),
        ("inst_pct", "All institutions hold", "The share of the company held by all institutions", "%", 0, 100, "Foreign + mutual funds + insurers + banks + other domestic institutions."),
        ("inst_change", "All institutions: change", "The change in all institutions' holding over the quarter", "pts", -100, 100, "Percentage points, latest quarter against the one before."),
        ("promoter_pct", "Promoters hold", "The share of the company held by its promoters", "%", 0, 100, "Promoters and promoter group, % of all shares."),
        ("promoter_change", "Promoters: change", "The change in the promoters' holding over the quarter", "pts", -100, 100, "Percentage points; a fall can be a sale or new shares issued."),
        ("pledged_pct", "Promoter shares pledged", "The share of the promoters' holding that is pledged", "%", 0, 100, "Pledged or encumbered, % of the promoters' own shares as filed."),
        ("retail_pct", "Individuals hold", "The share of the company held by individual investors", "%", 0, 100, "Resident individuals, % of all shares.")):
    number_field(_k, "ownership", _l, (lambda s: (lambda p: s))(_s), unit=_unit, lo=_lo, hi=_hi, decimals=2, needs="own", help=_h,
                 default=0.5 if _unit == "pts" else 10, source=_OWN_SRC)

# ---- fundamentals ------------------------------------------------------------------------------------------------
_FUND_SRC2 = "The company's filed quarterly results and balance sheet, at the latest closing price: the same working as the stock page's Valuation table."
number_field("mcap", "fundamental", "Market cap", lambda p: "The market capitalisation", unit="₹ Cr", lo=0, hi=1e9, decimals=0, needs="fund", default=5000,
             help="Latest close times the share count (in rupees crore).", source=_FUND_SRC2)
number_field("pe", "fundamental", "P/E (last 12 months)", lambda p: "The P/E ratio", unit="x", lo=0, hi=100000, decimals=1, needs="fund", industry=True, default=20,
             help="Price over the last four quarters' earnings per share. A company that made a loss has no P/E.", source=_FUND_SRC2)
number_field("pb", "fundamental", "P/B", lambda p: "The price-to-book ratio", unit="x", lo=0, hi=100000, decimals=2, needs="fund", industry=True, default=3,
             help="Price over book value per share (net worth over the share count). A negative net worth has none.", source=_FUND_SRC2)
number_field("peg", "fundamental", "PEG", lambda p: "The PEG ratio", unit="x", lo=0, hi=100000, decimals=2, needs="fund", industry=True, default=1,
             help="P/E over the growth of the last 12 months' EPS against the year before (reported growth, not forecasts). Needs eight quarters of results.", source=_FUND_SRC2)
number_field("roe", "fundamental", "Return on equity", lambda p: "The return on equity", unit="%", lo=-1000, hi=100000, decimals=1, needs="fund", industry=True, default=15,
             help="The latest full year's profit over average shareholders' equity.", source=_FUND_SRC2)
number_field("de", "fundamental", "Debt to equity", lambda p: "The debt-to-equity ratio", unit="x", lo=0, hi=100000, decimals=2, needs="fund", industry=True, default=1,
             help="Borrowings over total equity. Not used for banks and insurers.", source=_FUND_SRC2)
number_field("interest_cover", "fundamental", "Interest cover", lambda p: "The interest cover", unit="x", lo=-100000, hi=100000, decimals=1, needs="fund", industry=True, default=3,
             help="(Profit before tax + finance costs) over finance costs, latest full year. Not used for banks, insurers and finance companies.", source=_FUND_SRC2)
number_field("sales_growth", "fundamental", "Sales growth (year on year)", lambda p: "The growth in sales, latest quarter against the same quarter a year earlier", unit="%",
             lo=-100, hi=100000, decimals=1, needs="fund", industry=True, default=10,
             help="Revenue from operations, same basis (consolidated with consolidated). No value when the earlier figure was zero or a loss.", source=_FUND_SRC2)
number_field("profit_growth", "fundamental", "Profit growth (year on year)", lambda p: "The growth in net profit, latest quarter against the same quarter a year earlier", unit="%",
             lo=-100000, hi=100000, decimals=1, needs="fund", industry=True, default=10,
             help="No value when the earlier quarter's profit was zero or a loss (a percentage of a loss is not meaningful).", source=_FUND_SRC2)
number_field("price", "fundamental", "Share price", lambda p: "The latest closing price", unit="₹", lo=0, hi=10000000, decimals=2, needs="profile", default=100,
             help="The latest stored close.", source="The newest end-of-day price held.")

# ---- events ------------------------------------------------------------------------------------------------------
number_field("result_days", "events", "Days to the next results", lambda p: "The number of days until the company's next announced results", unit="days", lo=0, hi=365, decimals=0,
             needs="events", default=14, help="From the exchanges' board-meeting notices; a company can move its date. Stocks with no announced date have no value.",
             source="Board-meeting notices recorded from the exchanges.")


# Starting points the page offers ("Try an example"). Written in the same form a person's screen is saved in, so the tests can check every one of them
# cleans, and the page turns them into the form without any special case.
TEMPLATES = [
    {"name": "Trend turning up, funds buying, cheap for its industry",
     "what": "A change in Dow Theory on the daily chart in the last 5 bars (the short-term, swing-by-swing reading), weekly RSI above 50, at least 10 funds newly "
             "bought it, and P/E or P/B below the industry average. Few stocks pass all four at once; the table under the results shows how many pass each.",
     "spec": {"universe": {}, "rules": {"op": "all", "items": [
         {"field": "dow_change", "params": {"timeframe": "DAILY", "measure": "SWING", "recent": 5}, "value": True},
         {"field": "rsi", "params": {"tf": "WEEKLY"}, "op": "gt", "value": 50},
         {"field": "funds_new", "op": "gte", "value": 10},
         {"op": "any", "items": [{"field": "pe", "op": "lt", "ref": "industry", "mult": 1.0},
                                 {"field": "pb", "op": "lt", "ref": "industry", "mult": 1.0}]}]}}},
    {"name": "Quality at a fair price",
     "what": "Return on equity of at least 15%, debt to equity of at most 1, P/E below the industry average, and profit growing at least 10% year on year.",
     "spec": {"universe": {}, "rules": {"op": "all", "items": [
         {"field": "roe", "op": "gte", "value": 15},
         {"field": "de", "op": "lte", "value": 1},
         {"field": "pe", "op": "lt", "ref": "industry", "mult": 1.0},
         {"field": "profit_growth", "op": "gte", "value": 10}]}}},
    {"name": "Strong momentum near the high",
     "what": "Daily RSI between 55 and 75, the price within 10% of its 52-week high, and today's volume at least 1.5 times the usual.",
     "spec": {"universe": {}, "rules": {"op": "all", "items": [
         {"field": "rsi", "params": {"tf": "DAILY"}, "op": "between", "value": [55, 75]},
         {"field": "from_high", "op": "lte", "value": 10},
         {"field": "vol_ratio", "params": {"days": "20"}, "op": "gte", "value": 1.5}]}}},
    {"name": "Multi-year breakout on heavy volume",
     "what": "A stock that broke its 5-year high in the last 10 sessions, with volume at least twice the usual.",
     "spec": {"universe": {}, "rules": {"op": "all", "items": [
         {"field": "multiyear_breakout", "params": {"years": 5, "recent": 10}, "value": True},
         {"field": "vol_ratio", "params": {"days": "50"}, "op": "gte", "value": 2}]}}},
    {"name": "Beaten down, but institutions are adding",
     "what": "At least 40% below the 52-week high, at least 3 funds newly bought it, and foreign institutions raised their holding over the quarter.",
     "spec": {"universe": {}, "rules": {"op": "all", "items": [
         {"field": "from_high", "op": "gte", "value": 40},
         {"field": "funds_new", "op": "gte", "value": 3},
         {"field": "fii_change", "op": "gt", "value": 0}]}}},
    {"name": "Close above the previous candle's high, on volume",
     "what": "The latest daily candle closed above the high of the candle before it, with today's volume at least 1.5 times the 20-day average. Change the bars, the prices "
             "or the time frame (a weekly close above last week's high) to build your own candle rule.",
     "spec": {"universe": {}, "rules": {"op": "all", "items": [
         {"field": "candle_compare", "params": {"tf": "DAILY", "left_price": "C", "left_bar": 0, "right_price": "H", "right_bar": 1}, "value": True},
         {"field": "vol_ratio", "params": {"days": "20"}, "op": "gte", "value": 1.5}]}}},
    {"name": "New listings: a higher low, then a new high",
     "what": "Stocks that listed as an IPO in the last 180 days, rose after listing, pulled back to a higher low, and have now gone above the high before the pullback "
             "(scenario 1 of the IPO trend). Choose scenario 2 for IPOs that kept falling and have now broken the last lower high.",
     "spec": {"universe": {"ipo": {"within": 180}}, "rules": {"op": "all", "items": [
         {"field": "ipo_trend", "params": {"timeframe": "DAILY", "scenario": "HOLD", "listed_within": 180}, "value": True}]}}},
    {"name": "Golden cross in large and mid caps",
     "what": "The 50-bar EMA crossed above the 200-bar EMA on the daily chart in the last 5 bars, among large and mid caps.",
     "spec": {"universe": {"caps": ["Large", "Mid"]}, "rules": {"op": "all", "items": [
         {"field": "ema_cross", "params": {"tf": "DAILY", "fast": 50, "slow": 200, "direction": "up", "within": 5}, "value": True}]}}},
]


def fields_in_order():
    order = {g["key"]: i for i, g in enumerate(GROUPS)}
    return sorted(FIELDS.values(), key=lambda f: (order[f["group"]], 0))


def public_catalog(availability=None):
    """The catalogue as JSON for the page. availability: {field_key: reason it cannot be used right now (or absent)}."""
    availability = availability or {}
    fields = []
    for f in fields_in_order():
        item = {k: f[k] for k in ("key", "group", "label", "kind", "unit", "industry", "help", "source")}
        item["params"] = f["params"]
        if f["kind"] == "number":
            item.update(min=f["min"], max=f["max"], decimals=f["decimals"], ops=NUM_OPS, default=f.get("default"))
        if f["key"] in availability:
            item["unavailable"] = availability[f["key"]]
        fields.append(item)
    templates = []
    for t in TEMPLATES:
        try:                                                      # an example that uses a pattern this server lacks is simply not offered
            spec = clean_spec(t["spec"])
        except SpecError:
            continue
        templates.append({"name": t["name"], "what": t["what"], "spec": spec})
    return {"groups": GROUPS, "fields": fields, "limits": {"max_conditions": MAX_CONDITIONS, "max_depth": MAX_DEPTH, "max_pattern_settings": MAX_PATTERN_SETTINGS},
            "industry_multipliers": INDUSTRY_MULTIPLIERS, "caps": list(CAPS), "templates": templates}


# =====================================================================
# CLEANING A SCREEN
# =====================================================================
def _finite(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise SpecError("%s must be a number." % what)
    if isinstance(v, str) and len(v) > 40:
        raise SpecError("%s must be a number." % what)
    try:
        x = float(v)
    except (ValueError, OverflowError):                          # "abc"; or an integer of hundreds of digits
        raise SpecError("%s must be a number." % what)
    if not math.isfinite(x):
        raise SpecError("%s must be a number." % what)
    return x


def _same(option, v):
    """Is `v` the option? Compared as text, or as numbers when both are numbers: a browser sends 0 for the option 0.0 and 2 for 2.0."""
    if str(option) == str(v):
        return True
    if isinstance(option, bool) or isinstance(v, bool) or not isinstance(option, (int, float)):
        return False
    if isinstance(v, str) and len(v) > 40:
        return False
    try:
        return float(option) == float(v)
    except (TypeError, ValueError, OverflowError):
        return False


def clean_params(field, raw):
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for ps in field["params"]:
        v = raw.get(ps["param"], ps["default"])
        if ps["kind"] == "choice":
            allowed = [o[0] for o in ps["options"]]
            hit = next((o for o in allowed if _same(o, v)), None)
            if hit is None:
                raise SpecError("%s: choose one of the listed values for \"%s\"." % (field["label"], ps["label"]))
            out[ps["param"]] = hit
        else:
            x = _finite(v, "%s: %s" % (field["label"], ps["label"]))
            if x < ps["min"] or x > ps["max"]:
                raise SpecError("%s: \"%s\" must be between %s and %s." % (field["label"], ps["label"], _fmt(ps["min"], 2), _fmt(ps["max"], 2)))
            out[ps["param"]] = x
    if field.get("validate"):
        field["validate"](out)
    return out


def _clean_condition(raw):
    key = raw.get("field")
    f = FIELDS.get(key) if isinstance(key, str) else None
    if f is None:
        raise SpecError("One of the conditions asks for something this server does not have (%s)." % (str(key)[:40] if key else "no field chosen"))
    cond = {"field": key, "params": clean_params(f, raw.get("params"))}
    if f["kind"] == "flag":
        v = raw.get("value", True)
        if not isinstance(v, bool):
            raise SpecError("%s: choose whether it has happened or has not." % f["label"])
        cond["op"], cond["value"] = "is", v
        return cond
    op = raw.get("op")
    if not isinstance(op, str) or op not in OP_WORD:
        raise SpecError("%s: choose how to compare (more than, at least, less than, at most, between)." % f["label"])
    cond["op"] = op
    if raw.get("ref") == "industry":
        if not f["industry"]:
            raise SpecError("%s cannot be compared with an industry average." % f["label"])
        if op == "between":
            raise SpecError("%s: \"between\" cannot be used against the industry average." % f["label"])
        mult = _finite(raw.get("mult", 1.0), "%s: the multiple of the industry average" % f["label"])
        if mult not in INDUSTRY_MULTIPLIERS:                      # (a number, so the membership test cannot meet an unhashable value)
            raise SpecError("%s: choose one of the listed multiples of the industry average." % f["label"])
        cond["ref"], cond["mult"] = "industry", mult
        return cond
    if op == "between":
        v = raw.get("value")
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise SpecError("%s: give the two numbers it must lie between." % f["label"])
        lo, hi = _finite(v[0], "%s: the lower number" % f["label"]), _finite(v[1], "%s: the upper number" % f["label"])
        if lo > hi:
            raise SpecError("%s: the first number must not be larger than the second." % f["label"])
        cond["value"] = [lo, hi]
    else:
        cond["value"] = _finite(raw.get("value"), "%s: the number to compare with" % f["label"])
    vals = cond["value"] if isinstance(cond["value"], list) else [cond["value"]]
    for x in vals:
        if x < f["min"] or x > f["max"]:
            raise SpecError("%s: the number must be between %s and %s." % (f["label"], _fmt(f["min"], 2), _fmt(f["max"], 2)))
    return cond


def _clean_node(raw, depth, state):
    if not isinstance(raw, dict):
        raise SpecError("A rule is not in a form I can read.")
    if "items" in raw:
        if depth > MAX_DEPTH:
            raise SpecError("Groups can be nested up to %d levels. Simplify the rules." % MAX_DEPTH)
        if raw.get("op") not in ("all", "any"):
            raise SpecError("A group must be \"all of\" or \"any of\".")
        items = raw["items"]
        if not isinstance(items, list) or not items:
            raise SpecError("A group has nothing in it. Add a condition or remove the group.")
        if len(items) > MAX_CONDITIONS:
            raise SpecError("Too many conditions in one group.")
        return {"op": raw["op"], "items": [_clean_node(i, depth + 1, state) for i in items]}
    state["n"] += 1
    if state["n"] > MAX_CONDITIONS:
        raise SpecError("A screen can have up to %d conditions." % MAX_CONDITIONS)
    c = _clean_condition(raw)
    c["n"] = state["n"] - 1
    return c


def _clean_names(v, what, lo_ok=None):
    if v in (None, ""):
        return []
    if not isinstance(v, list):
        raise SpecError("%s must be a list." % what)
    if len(v) > 4 * MAX_UNIVERSE_LIST:                            # refuse before looping: a list of a hundred thousand names must not cost any time
        raise SpecError("Choose up to %d %s." % (MAX_UNIVERSE_LIST, what.lower()))
    out, seen = [], set()
    for x in v:
        if not isinstance(x, str) or not x.strip() or len(x) > MAX_TEXT:
            raise SpecError("%s has an entry that is not valid." % what)
        x = " ".join(x.split())
        if x not in seen:
            seen.add(x)
            out.append(x)
    if len(out) > MAX_UNIVERSE_LIST:
        raise SpecError("Choose up to %d %s." % (MAX_UNIVERSE_LIST, what.lower()))
    return out


MAX_IPO_DAYS = 3660                                               # ten years: more than any history we hold


def _clean_ipo(v):
    """The 'only IPOs' choice of a screen's universe: {"within": days} and/or {"from": date, "to": date} (the exchange's listing date), or None for no such choice.
    Nothing is added to a screen that does not use it, so screens saved before this existed read exactly as they did."""
    if v in (None, "", False, {}):
        return None
    if not isinstance(v, dict):
        raise SpecError("The IPO choice is not in a form I can read.")
    out = {}
    w = v.get("within")
    if w not in (None, "", 0, "0"):
        if isinstance(w, bool):
            raise SpecError("'Listed in the last' must be a number of days.")
        try:
            w = int(float(w))
        except (TypeError, ValueError, OverflowError):
            raise SpecError("'Listed in the last' must be a number of days.")
        if not 1 <= w <= MAX_IPO_DAYS:
            raise SpecError("'Listed in the last' must be between 1 and %d days." % MAX_IPO_DAYS)
        out["within"] = w
    for k, word in (("from", "from"), ("to", "to")):
        d = v.get(k)
        if d not in (None, ""):
            try:
                out[k] = date.fromisoformat(str(d).strip()).isoformat()
            except ValueError:
                raise SpecError("The 'listed %s' date must be a date like 2026-09-30." % word)
    if "from" in out and "to" in out and out["from"] > out["to"]:
        raise SpecError("The 'listed from' date is after the 'listed to' date.")
    return out or None


def ipo_words(ipo):
    """The IPO choice in words: 'IPOs listed in the last 90 days', 'IPOs listed between 1 Sep 2026 and 30 Sep 2026' ..."""
    bits = []
    if ipo.get("within"):
        bits.append("in the last %d day%s" % (ipo["within"], "" if ipo["within"] == 1 else "s"))
    if ipo.get("from") and ipo.get("to"):
        bits.append("between %s and %s" % (_dmy(ipo["from"]), _dmy(ipo["to"])))
    elif ipo.get("from"):
        bits.append("on or after %s" % _dmy(ipo["from"]))
    elif ipo.get("to"):
        bits.append("on or before %s" % _dmy(ipo["to"]))
    return "IPOs listed " + " and ".join(bits)


def _dmy(iso):
    d = date.fromisoformat(iso)
    return "%d %s %d" % (d.day, d.strftime("%b"), d.year)


def clean_spec(raw):
    """A person's screen, checked and normalised: {"universe": {...}, "rules": {"op", "items"}} with every condition numbered "n" in reading
    order. Raises SpecError. A bare condition or a list of conditions is accepted as ALL of them."""
    if not isinstance(raw, dict):
        raise SpecError("The screen is not in a form I can read.")
    uni = raw.get("universe") if isinstance(raw.get("universe"), dict) else {}
    caps = _clean_names(uni.get("caps"), "Market-cap classes")
    for c in caps:
        if c not in CAPS:
            raise SpecError("Market-cap class must be Large, Mid or Small.")
    universe = {"caps": caps, "sectors": _clean_names(uni.get("sectors"), "Sectors"), "industries": _clean_names(uni.get("industries"), "Industries")}
    ipo = _clean_ipo(uni.get("ipo"))
    if ipo:
        universe["ipo"] = ipo
    rules = raw.get("rules")
    if isinstance(rules, list):
        rules = {"op": "all", "items": rules}
    elif isinstance(rules, dict) and "items" not in rules and "field" in rules:
        rules = {"op": "all", "items": [rules]}
    if not isinstance(rules, dict) or not rules.get("items"):
        raise SpecError("Add at least one condition.")
    state = {"n": 0}
    tree = _clean_node(rules, 1, state)
    if state["n"] == 0:
        raise SpecError("Add at least one condition.")
    # every different chart-pattern setting is a scan of the whole market's bars: a handful is plenty for a screen
    scans = {(c["field"], json.dumps(c["params"], sort_keys=True)) for c in conditions(tree) if FIELDS[c["field"]]["needs"] == "pattern"}
    if len(scans) > MAX_PATTERN_SETTINGS:
        raise SpecError("A screen can use up to %d different chart-pattern settings; this one uses %d." % (MAX_PATTERN_SETTINGS, len(scans)))
    return {"universe": universe, "rules": tree, "count": state["n"]}


def conditions(tree):
    """The conditions of a cleaned tree, in reading order."""
    out = []

    def walk(n):
        if "items" in n:
            for i in n["items"]:
                walk(i)
        else:
            out.append(n)
    walk(tree)
    return out


# =====================================================================
# WORDS
# =====================================================================
def value_text(f, v):
    if v is None:
        return "no value"
    if isinstance(v, bool):
        return "yes" if v else "no"
    unit = f.get("unit", "")
    text = _fmt_exact(v)                                          # exactly what the person typed: 2.5 funds is not "2 funds", 5000.6 is not "5,001"
    if unit in ("%", "x"):
        return text + unit
    if unit == "₹":
        return "₹" + text
    if unit == "₹ Cr":
        return "₹" + text + " crore"
    if unit == "pts":
        return text + " points"
    if unit == "days":
        return text + (" day" if v == 1 else " days")
    if unit == "funds":
        return text + (" fund" if v == 1 else " funds")
    return text


def sentence(cond):
    f = FIELDS[cond["field"]]
    subject = f["subject"](cond["params"])
    if f["kind"] == "flag":
        if cond["value"]:
            return subject
        return "NOT: " + subject
    word = OP_WORD[cond["op"]]
    if cond.get("ref") == "industry":
        m = cond["mult"]
        target = "the industry average" if m == 1.0 else "%s times the industry average" % _fmt(m, 2)
        return "%s %s %s" % (subject, word, target)
    if cond["op"] == "between":
        lo, hi = cond["value"]
        return "%s %s %s and %s" % (subject, word, value_text(f, lo), value_text(f, hi))
    return "%s %s %s" % (subject, word, value_text(f, cond["value"]))


def describe(spec):
    """The screen in plain English: a nested outline, each condition numbered as in the results."""
    def walk(n):
        if "items" in n:
            return {"op": n["op"], "items": [walk(i) for i in n["items"]]}
        return {"n": n["n"], "text": sentence(n)}
    lead = []
    u = spec["universe"]
    if u["caps"]:
        lead.append("%s caps" % " and ".join(c.lower() for c in u["caps"]))
    if u["sectors"]:
        lead.append("in the %s sector%s" % (" or ".join(u["sectors"]), "" if len(u["sectors"]) == 1 else "s"))
    if u["industries"]:
        lead.append("in the %s industr%s" % (" or ".join(u["industries"]), "y" if len(u["industries"]) == 1 else "ies"))
    rest = " ".join(lead)
    if u.get("ipo"):
        head = ipo_words(u["ipo"])
        return {"universe": head + (", " + rest if rest else ""), "outline": walk(spec["rules"])}
    return {"universe": rest if lead else "all stocks", "outline": walk(spec["rules"])}


def outline_text(outline, indent=0):
    """The outline as lines of text (for emails and exports)."""
    pad = "  " * indent
    if "items" in outline:
        head = {"all": "ALL of these:", "any": "ANY of these:"}[outline["op"]]
        lines = [pad + head]
        for i in outline["items"]:
            lines += outline_text(i, indent + 1)
        return lines
    return [pad + "%d. %s" % (outline["n"] + 1, outline["text"])]


# =====================================================================
# EVALUATING
# =====================================================================
def test_condition(cond, value, ref):
    """True / False / None (unknown) for one condition on one stock. value: the stock's reading (number, bool or None); ref: the industry
    average where the condition asks for it."""
    if value is None:
        return None
    if cond["op"] == "is":
        return bool(value) == cond["value"]
    if cond.get("ref") == "industry":
        if ref is None:
            return None
        if cond["mult"] != 1.0 and ref <= 0:
            return None                         # "1.5 times" a zero or negative average is not a meaningful bar: unknown, not a guess
        target = ref * cond["mult"]
    elif cond["op"] == "between":
        return cond["value"][0] <= value <= cond["value"][1]
    else:
        target = cond["value"]
    op = cond["op"]
    if op == "gt":
        return value > target
    if op == "gte":
        return value >= target
    if op == "lt":
        return value < target
    return value <= target


def combine(op, results):
    """Kleene AND / OR over True / False / None."""
    if op == "all":
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True
    if any(r is True for r in results):
        return True
    return None if any(r is None for r in results) else False


def evaluate(spec, isins, read):
    """Run a cleaned screen over `isins`. read(cond, isin) -> (value, ref). Returns
        {"matches": [(isin, cells)], "unknown": n, "counts": {n: {"true", "false", "unknown"}}, "tested": len(isins)}
    where cells[n] = {"value", "ref", "ok"} for every condition (ok: True / False / None)."""
    conds = conditions(spec["rules"])
    counts = {c["n"]: {"true": 0, "false": 0, "unknown": 0} for c in conds}
    matches, unknown = [], 0

    def walk(node, cells):
        if "items" in node:
            return combine(node["op"], [walk(i, cells) for i in node["items"]])
        return cells[node["n"]]["ok"]

    for isin in isins:
        cells = {}
        for c in conds:
            value, ref = read(c, isin)
            ok = test_condition(c, value, ref)
            cells[c["n"]] = {"value": value, "ref": ref, "ok": ok}
            counts[c["n"]]["true" if ok else "false" if ok is False else "unknown"] += 1
        r = walk(spec["rules"], cells)
        if r is True:
            matches.append((isin, cells))
        elif r is None:
            unknown += 1
    return {"matches": matches, "unknown": unknown, "counts": counts, "tested": len(isins)}
