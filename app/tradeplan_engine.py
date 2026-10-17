"""
tradeplan_engine.py -- the maths behind /tradeplan ("My trade plan").
---------------------------------------------------------------------------
PURE FUNCTIONS. No database, no web framework. The caller hands in candles and
a plan and gets plain data back, which is what makes every line here testable on
its own (test_tradeplan_engine.py).

WHAT A PLAN IS
    A trader or investor writes down, once, what has to be true before they act:

      * INSTRUMENT   Nifty 50, Bank Nifty, Sensex, stocks, natural gas, gold,
                     silver, crude oil, or "other" (named in their own words).
      * TIME FRAME   the one they trade on (5 minutes ... monthly).
      * CONDITIONS   any number of them, each on ITS OWN time frame: "hourly MACD
                     is above its signal line", "daily RSI is above 50". Each is
                     an indicator, a time frame and a test, and a test may take
                     numbers ("above 50", "within the last 3 candles").
      * AND / OR     each condition after the first says how it joins the one
                     before it. AND binds tighter than OR, so
                         A and B or C    means    (A and B) or C
                     which is the same as "either set works; inside a set every
                     condition must hold". The page shows the sets, so nobody has
                     to remember the rule.
      * DIRECTION    the view the plan is written for (bullish or bearish).

    A plan is then CHECKED against one instrument and a view. If the view is the
    one the plan was written for, every condition is judged as written. If it is
    the opposite one, every condition is judged as its MIRROR IMAGE: "RSI above
    50" becomes "RSI below 50", "MACD crossed above its signal line" becomes
    "crossed below", "price above the 50 EMA" becomes "below". Tests that do not
    take a side (trend strength, volume, volatility) stay as they are. The mirror
    is defined once, in the catalogue, and test_tradeplan_engine.py proves it:
    a condition on a chart gives the same answer as its mirror on the chart
    turned upside down.

WHAT COMES BACK
    For every condition: MET, NOT MET or NO DATA, the reading behind it (the
    actual MACD and signal values, the actual RSI), and the candle it was read
    on. For the plan as a whole: met, not met, or unknown. UNKNOWN IS REAL: a
    condition we have no candles for is not "not met". With AND, one false
    condition settles it whatever else is missing; with OR, one true one does.
    Otherwise a missing condition leaves the answer unknown, and the page says
    so rather than rounding it to a yes or a no.

WHAT IT IS NOT
    Not advice, and not a score. These are the person's own rules, checked
    against the latest candles. Nothing here says buy or sell, and the wording is
    "met / not met". FinChaya is not a SEBI-registered Research Analyst or
    Investment Adviser.

AFTER THE CONDITIONS ARE MET
    The page offers a link to the person's own broker terminal (Zerodha, Fyers, Groww, Dhan, or any other broker
    they name). It is a plain outbound link: no order is placed or prefilled, no login is handled, no referral
    code is added, and nothing records which broker was opened. See BROKERS below, and clean_terminal_link for
    the broker a person names themselves.

INDICATORS
    Worked out by backtest_engine._compute, which uses indicators.py -- the same
    code behind the charts, the stock page and the back-tester -- so an RSI here
    is the RSI there, and matches Kite. A value inside an indicator's warm-up is
    empty, and a condition that needs it is NO DATA, not false.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta

import pandas as pd

import backtest_engine as be

SpecError = be.SpecError

# =====================================================================
# LIMITS -- a plan arrives from a browser, so it is untrusted
# =====================================================================
MAX_CONDITIONS = 20                  # a trader who writes down every rule they use (charts, results, ratios, ownership) needs more than a dozen
MAX_NAME = 80
MAX_LABEL = 60
MAX_NOTES = 500
MAX_WITHIN = 30

# =====================================================================
# INSTRUMENTS AND TIME FRAMES
# =====================================================================
INSTRUMENTS = [
    {"key": "nifty50",   "label": "Nifty 50",                  "kind": "index"},
    {"key": "banknifty", "label": "Bank Nifty",                "kind": "index"},
    {"key": "sensex",    "label": "Sensex",                    "kind": "index"},
    {"key": "stock",     "label": "Stocks (pick one when you check)", "kind": "stock"},
    {"key": "natgas",    "label": "Natural gas",               "kind": "commodity"},
    {"key": "gold",      "label": "Gold",                      "kind": "commodity"},
    {"key": "silver",    "label": "Silver",                    "kind": "commodity"},
    {"key": "crude",     "label": "Crude oil",                 "kind": "commodity"},
    {"key": "other",     "label": "Other (name it yourself)",  "kind": "other"},
]
_INSTR = {i["key"]: i for i in INSTRUMENTS}

TIMEFRAMES = [
    ("5m", "5-minute"), ("15m", "15-minute"), ("30m", "30-minute"), ("1h", "Hourly"),
    ("4h", "4-hour"), ("D", "Daily"), ("W", "Weekly"), ("M", "Monthly"),
    ("R", "Company data"),                    # not candles: what the company reports (results, ratios, shareholding). Set automatically for those conditions.
]
TF_LABEL = dict(TIMEFRAMES)
CANDLE_TIMEFRAMES = [(k, v) for k, v in TIMEFRAMES if k != "R"]
INTRADAY = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}      # minutes in a candle
DIRECTIONS = ("bullish", "bearish")


def coverage_for(instrument):
    """What we can actually check for an instrument TODAY: {timeframe: reason or None}.
    None means candles are held. Kept next to the catalogue so the page, the
    check and the tests all read one answer. When a source for another
    instrument or for intraday candles is added, this is the one place to say so."""
    kind = _INSTR[instrument]["kind"]
    out = {}
    for tf, _ in TIMEFRAMES:
        if tf == COMPANY_TF:
            out[tf] = None if kind == "stock" else "Company results, ratios and shareholding are held for stocks only."
        elif kind == "stock" and tf in ("D", "W", "M"):
            out[tf] = None
        elif tf in INTRADAY:
            out[tf] = "We do not hold %s candles yet." % TF_LABEL[tf].lower()
        elif kind == "stock":
            out[tf] = None
        else:
            out[tf] = ("We do not hold price candles for %s yet."
                       % ("this instrument" if kind == "other" else _INSTR[instrument]["label"]))
    return out


# =====================================================================
# THE CATALOGUE -- one place the page AND the validator both read
# =====================================================================
def _p(name, label, default, lo, hi, typ="int"):
    return {"name": name, "label": label, "default": default, "min": lo, "max": hi, "type": typ}


_PERIOD = lambda d, lo=1: _p("period", "period", d, lo, be.MAX_PERIOD)
_BARS = _p("bars", "within the last (candles)", 3, 1, MAX_WITHIN)
_CONFIRMED = _p("bars", "confirmed within the last (candles)", 5, 1, MAX_WITHIN)
_LEVEL100 = lambda d: _p("level", "level", d, 0, 100, "float")

# "flip" = 100 - x, "neg" = -x, "keep" = x. Used only for mirroring a number.
_FLIP = {"keep": lambda v: v, "flip": lambda v: 100.0 - v, "neg": lambda v: -v}


def _t(key, predicate, inputs=(), mirror=None, subject=None):
    """A test. `predicate` is what is said about the subject ("is above its signal line"); the sentence is
    "{tf} <subject> <predicate>" and the short label in the page's drop-down is the predicate (with the subject in
    front only when the test has one of its own). `mirror` = (other test key, {input: (transform, source input)}),
    or None for a test that takes no side (it mirrors to itself)."""
    return {"key": key, "predicate": predicate, "inputs": list(inputs), "mirror": mirror, "subject": subject}


def _m(to, **inputs):
    return (to, {k: v for k, v in inputs.items()})


_MACD_P = [_p("fast", "fast", 12, 2, be.MAX_PERIOD), _p("slow", "slow", 26, 3, be.MAX_PERIOD),
           _p("signal", "signal", 9, 1, be.MAX_PERIOD)]

_AVG_TESTS = lambda name: [
    _t("price_above", "is above the %s ({period})" % name, mirror=_m("price_below")),
    _t("price_below", "is below the %s ({period})" % name, mirror=_m("price_above")),
    _t("cross_up", "crossed above the %s ({period}) within the last {bars} candles" % name, [_BARS], _m("cross_down")),
    _t("cross_down", "crossed below the %s ({period}) within the last {bars} candles" % name, [_BARS], _m("cross_up")),
    _t("rising", "is rising", mirror=_m("falling"), subject="%s ({period})" % name),
    _t("falling", "is falling", mirror=_m("rising"), subject="%s ({period})" % name),
]
_PAIR_TESTS = lambda name: [
    _t("fast_above", "is above %s ({slow})" % name, mirror=_m("fast_below")),
    _t("fast_below", "is below %s ({slow})" % name, mirror=_m("fast_above")),
    _t("cross_up", "crossed above %s ({slow}) within the last {bars} candles" % name, [_BARS], _m("cross_down")),
    _t("cross_down", "crossed below %s ({slow}) within the last {bars} candles" % name, [_BARS], _m("cross_up")),
]

INDICATORS = [
    {"key": "macd", "label": "MACD", "group": "Momentum", "params": _MACD_P,
     "subject": "MACD ({fast}, {slow}, {signal})",
     "aliases": ["macd", "movingaverageconvergencedivergence"],
     "tests": [
         _t("above_signal", "is above its signal line", mirror=_m("below_signal")),
         _t("below_signal", "is below its signal line", mirror=_m("above_signal")),
         _t("cross_up", "crossed above its signal line within the last {bars} candles", [_BARS], _m("cross_down")),
         _t("cross_down", "crossed below its signal line within the last {bars} candles", [_BARS], _m("cross_up")),
         _t("rising", "is rising (higher than on the previous candle)", mirror=_m("falling")),
         _t("falling", "is falling (lower than on the previous candle)", mirror=_m("rising")),
         _t("hist_rising", "histogram is rising", mirror=_m("hist_falling")),
         _t("hist_falling", "histogram is falling", mirror=_m("hist_rising")),
         _t("above_zero", "is above zero", mirror=_m("below_zero")),
         _t("below_zero", "is below zero", mirror=_m("above_zero")),
     ]},
    {"key": "rsi", "label": "RSI", "group": "Momentum", "params": [_PERIOD(14, 2)],
     "subject": "RSI ({period})",
     "aliases": ["rsi", "relativestrengthindex"],
     "tests": [
         _t("above", "is above {level:g}", [_LEVEL100(50)], _m("below", level=("flip", "level"))),
         _t("below", "is below {level:g}", [_LEVEL100(50)], _m("above", level=("flip", "level"))),
         _t("between", "is between {low:g} and {high:g}",
            [_p("low", "from", 40, 0, 100, "float"), _p("high", "to", 60, 0, 100, "float")],
            _m("between", low=("flip", "high"), high=("flip", "low"))),
         _t("cross_up", "crossed above {level:g} within the last {bars} candles",
            [_LEVEL100(50), _BARS], _m("cross_down", level=("flip", "level"))),
         _t("cross_down", "crossed below {level:g} within the last {bars} candles",
            [_LEVEL100(50), _BARS], _m("cross_up", level=("flip", "level"))),
         _t("rising", "is rising (higher than on the previous candle)", mirror=_m("falling")),
         _t("falling", "is falling (lower than on the previous candle)", mirror=_m("rising")),
     ]},
    {"key": "adx", "label": "ADX / DI (trend)", "group": "Trend", "params": [_PERIOD(14, 2)],
     "subject": "ADX ({period})",
     "aliases": ["adx", "dmi", "di", "plusdi", "minusdi", "averagedirectionalindex"],
     "tests": [
         _t("above", "is above {level:g} (a trend is in place)", [_LEVEL100(20)]),
         _t("below", "is below {level:g} (no clear trend)", [_LEVEL100(20)]),
         _t("rising", "is rising (the trend is getting stronger)"),
         _t("falling", "is falling (the trend is weakening)"),
         _t("di_bull", "is above -DI (buyers are leading)", mirror=_m("di_bear"), subject="+DI ({period})"),
         _t("di_bear", "is above +DI (sellers are leading)", mirror=_m("di_bull"), subject="-DI ({period})"),
     ]},
    {"key": "supertrend", "label": "Supertrend", "group": "Trend",
     "params": [_PERIOD(10), _p("factor", "multiplier", 3, 0.1, 20, "float")],
     "subject": "Supertrend ({period}, {factor:g})",
     "aliases": ["supertrend"],
     "tests": [
         _t("up", "is pointing up", mirror=_m("down")),
         _t("down", "is pointing down", mirror=_m("up")),
         _t("flip_up", "turned up within the last {bars} candles", [_BARS], _m("flip_down")),
         _t("flip_down", "turned down within the last {bars} candles", [_BARS], _m("flip_up")),
     ]},
    {"key": "ema", "label": "EMA (exponential moving average)", "group": "Averages", "params": [_PERIOD(20)],
     "subject": "price",
     "aliases": ["ema", "exponentialmovingaverage", "movingaverage"],
     "tests": _AVG_TESTS("EMA")},
    {"key": "sma", "label": "SMA (simple moving average)", "group": "Averages", "params": [_PERIOD(50)],
     "subject": "price",
     "aliases": ["sma", "simplemovingaverage"],
     "tests": _AVG_TESTS("SMA")},
    {"key": "ema_cross", "label": "Two EMAs (fast and slow)", "group": "Averages",
     "params": [_p("fast", "fast", 5, 1, be.MAX_PERIOD), _p("slow", "slow", 50, 2, be.MAX_PERIOD)],
     "subject": "EMA ({fast})",
     "aliases": ["emacross", "emacrossover", "goldencross", "deathcross"],
     "tests": _PAIR_TESTS("EMA")},
    {"key": "sma_cross", "label": "Two SMAs (fast and slow)", "group": "Averages",
     "params": [_p("fast", "fast", 20, 1, be.MAX_PERIOD), _p("slow", "slow", 50, 2, be.MAX_PERIOD)],
     "subject": "SMA ({fast})",
     "aliases": ["smacross", "smacrossover"],
     "tests": _PAIR_TESTS("SMA")},
    {"key": "bb", "label": "Bollinger Bands", "group": "Bands and range",
     "params": [_PERIOD(20, 2), _p("dev", "std devs", 2, 0.1, 10, "float")],
     "subject": "price",
     "aliases": ["bollinger", "bollingerbands", "bb", "bbands"],
     "tests": [
         _t("above_upper", "is above the upper Bollinger band ({period}, {dev:g})", mirror=_m("below_lower")),
         _t("below_lower", "is below the lower Bollinger band ({period}, {dev:g})", mirror=_m("above_upper")),
         _t("above_middle", "is above the middle Bollinger band ({period}, {dev:g})", mirror=_m("below_middle")),
         _t("below_middle", "is below the middle Bollinger band ({period}, {dev:g})", mirror=_m("above_middle")),
     ]},
    {"key": "roc", "label": "Percent change (ROC)", "group": "Momentum", "params": [_PERIOD(10)],
     "subject": "price change over {period} candles",
     "aliases": ["roc", "rateofchange", "percentchange", "momentum"],
     "tests": [
         _t("above", "is above {level:g}%", [_p("level", "per cent", 5, -100, 1000, "float")],
            _m("below", level=("neg", "level"))),
         _t("below", "is below {level:g}%", [_p("level", "per cent", -5, -100, 1000, "float")],
            _m("above", level=("neg", "level"))),
     ]},
    {"key": "atr", "label": "ATR (volatility)", "group": "Bands and range", "params": [_PERIOD(14)],
     "subject": "ATR ({period})",
     "aliases": ["atr", "averagetruerange", "volatility"],
     "tests": [
         _t("rising", "is rising (price swings are getting wider)"),
         _t("falling", "is falling (price swings are narrowing)"),
     ]},
    {"key": "breakout", "label": "Breakout (N-candle high / low)", "group": "Bands and range", "params": [_PERIOD(20)],
     "subject": "close",
     "aliases": ["breakout", "donchian", "donchianchannel", "highestlow", "newhigh", "newlow"],
     "tests": [
         _t("above_high", "is above the highest high of the previous {period} candles", mirror=_m("below_low")),
         _t("below_low", "is below the lowest low of the previous {period} candles", mirror=_m("above_high")),
     ]},
    {"key": "volume", "label": "Volume", "group": "Volume", "params": [_PERIOD(20)],
     "subject": "volume",
     "aliases": ["volume", "volumespike"],
     "tests": [
         _t("above_avg", "is above {times:g} times its {period}-candle average",
            [_p("times", "times", 1.5, 0.1, 100, "float")]),
     ]},
    {"key": "candle", "label": "Candle colour", "group": "Price", "params": [],
     "subject": "candle",
     "aliases": ["candle", "candlecolour", "candlecolor", "greencandle", "redcandle"],
     "tests": [
         _t("green", "closed above its open (green)", mirror=_m("red")),
         _t("red", "closed below its open (red)", mirror=_m("green")),
     ]},
    {"key": "range", "label": "Distance from the high / low of the last N candles", "group": "Bands and range", "params": [_PERIOD(250, 5)],
     "subject": "close",
     "aliases": ["range", "52weekhigh", "52weeklow", "yearhigh", "yearlow", "weekhigh52", "distancefromhigh", "distancefromlow", "nearhigh", "nearlow", "alltimehigh"],
     "tests": [
         _t("near_high", "is within {pct:g}% of the highest high of the last {period} candles", [_p("pct", "per cent", 5, 0, 100, "float")], _m("near_low")),
         _t("near_low", "is within {pct:g}% of the lowest low of the last {period} candles", [_p("pct", "per cent", 5, 0, 100, "float")], _m("near_high")),
         _t("below_high", "is at least {pct:g}% below the highest high of the last {period} candles", [_p("pct", "per cent", 20, 0, 100, "float")], _m("above_low")),
         _t("above_low", "is at least {pct:g}% above the lowest low of the last {period} candles", [_p("pct", "per cent", 20, 0, 1000, "float")], _m("below_high")),
     ]},
    # Dow theory: higher highs and higher lows (an uptrend), lower highs and lower lows (a downtrend), read from the candles' own highs and lows.
    # A swing high is a candle whose high is above the highs of the `swing` candles on each side of it (a swing low: below their lows); it is only known
    # `swing` candles later, so "confirmed within the last N candles" counts from the candle on which it could first be seen.
    {"key": "dow", "label": "Dow theory (higher highs and higher lows)", "group": "Price",
     "params": [_p("swing", "swing size (candles each side)", 2, 1, 5)],
     "subject": "price",
     "aliases": ["dow", "dowtheory", "swing", "swings", "swinghighlow", "higherhigh", "higherlow", "lowerhigh", "lowerlow", "hhhl", "trendstructure"],
     "tests": [
         _t("higher_low", "has made a higher low: the latest swing low is above the one before it and has not been undercut, confirmed within the last {bars} candles "
            "(swings of {swing} candles each side)", [_CONFIRMED], _m("lower_high")),
         _t("lower_high", "has made a lower high: the latest swing high is below the one before it and has not been exceeded, confirmed within the last {bars} candles "
            "(swings of {swing} candles each side)", [_CONFIRMED], _m("higher_low")),
         _t("higher_high", "has made a higher high: the latest swing high is above the one before it, confirmed within the last {bars} candles "
            "(swings of {swing} candles each side)", [_CONFIRMED], _m("lower_low")),
         _t("lower_low", "has made a lower low: the latest swing low is below the one before it, confirmed within the last {bars} candles "
            "(swings of {swing} candles each side)", [_CONFIRMED], _m("higher_high")),
         _t("uptrend", "is in a Dow uptrend: the latest swing high and the latest swing low are each above the ones before them, and the low has not been undercut "
            "(swings of {swing} candles each side)", mirror=_m("downtrend")),
         _t("downtrend", "is in a Dow downtrend: the latest swing high and the latest swing low are each below the ones before them, and the high has not been exceeded "
            "(swings of {swing} candles each side)", mirror=_m("uptrend")),
     ]},
]


# ---------------------------------------------------------------------
# COMPANY DATA: what the company itself reports, not a chart. These conditions have no time frame of their own (their "time frame" is the
# pseudo time frame "R", Company data, set for them automatically) and are read from the company's filings, its ratios and its shareholding
# pattern, not from candles. They are written the same way, joined with AND / OR the same way, and mirrored the same way (profit up <-> profit
# down, a ratio above a level <-> below it). `part` says which of the three readings a condition needs, so only those are loaded.
# ---------------------------------------------------------------------
COMPANY_TF = "R"
_PCT = _p("pct", "by more than (%)", 0, 0, 1000, "float")
_NQ = _p("n", "in each of the last (quarters)", 4, 2, 8)
_FUNDS_N = _p("n", "funds", 3, 1, 1000)
_FUNDS_HOLD = _p("n", "funds", 50, 0, 5000)


def _fin_tests(profit=False):
    """The tests every one of revenue / profit / EPS has: against a year ago, the previous quarter, the year before, and a run of quarters."""
    out = [
        _t("up_yoy", "in the latest quarter is higher than in the same quarter a year ago, by more than {pct:g}%", [_PCT], _m("down_yoy")),
        _t("down_yoy", "in the latest quarter is lower than in the same quarter a year ago, by more than {pct:g}%", [_PCT], _m("up_yoy")),
        _t("up_qoq", "in the latest quarter is higher than in the previous quarter, by more than {pct:g}%", [_PCT], _m("down_qoq")),
        _t("down_qoq", "in the latest quarter is lower than in the previous quarter, by more than {pct:g}%", [_PCT], _m("up_qoq")),
        _t("up_fy", "in the latest financial year is higher than in the year before, by more than {pct:g}%", [_PCT], _m("down_fy")),
        _t("down_fy", "in the latest financial year is lower than in the year before, by more than {pct:g}%", [_PCT], _m("up_fy")),
        _t("up_streak", "was higher than a year earlier in each of the last {n} quarters", [_NQ], _m("down_streak")),
        _t("down_streak", "was lower than a year earlier in each of the last {n} quarters", [_NQ], _m("up_streak")),
    ]
    if profit:
        out = [
            _t("profit_q", "in the latest quarter was positive (a profit, not a loss)", mirror=_m("loss_q")),
            _t("loss_q", "in the latest quarter was negative (a loss)", mirror=_m("profit_q")),
            _t("profit_fy", "in the latest financial year was positive (a profit, not a loss)", mirror=_m("loss_fy")),
            _t("loss_fy", "in the latest financial year was negative (a loss)", mirror=_m("profit_fy")),
        ] + out
    return out


def _level_tests(unit, default, lo=0, hi=1000, what="level"):
    return [_t("above", "is above %s" % unit, [_p("level", what, default, lo, hi, "float")], _m("below")),
            _t("below", "is below %s" % unit, [_p("level", what, default, lo, hi, "float")], _m("above"))]


def _own_tests(unit):
    return _level_tests(unit, 50, 0, 100, "per cent") + [
        _t("rising", "has risen since the previous quarter", mirror=_m("falling")),
        _t("falling", "has fallen since the previous quarter", mirror=_m("rising"))]


def _company(key, label, group, part, subject, tests, aliases):
    return {"key": key, "label": label, "group": group, "params": [], "subject": subject, "aliases": aliases, "tests": tests, "data": "company", "part": part}


INDICATORS += [
    _company("profit", "Profit (net profit)", "Company results", "results", "The company's profit", _fin_tests(profit=True),
             ["profit", "netprofit", "profitable", "profitability", "pat", "profitaftertax"]),
    _company("revenue", "Revenue (sales)", "Company results", "results", "The company's revenue", _fin_tests(),
             ["revenue", "sales", "turnover", "topline", "revenuegrowth", "salesgrowth"]),
    _company("eps", "EPS (earnings per share)", "Company results", "results", "The company's EPS", _fin_tests(),
             ["eps", "earningspershare", "earnings"]),
    _company("pe", "P/E (price to earnings)", "Valuation and strength", "ratios", "The P/E ratio", _level_tests("{level:g}", 25),
             ["pe", "peratio", "priceearnings", "pricetoearnings"]),
    _company("peg", "PEG", "Valuation and strength", "ratios", "The PEG ratio", _level_tests("{level:g}", 1, 0, 100), ["peg", "pegratio"]),
    _company("pb", "P/B (price to book)", "Valuation and strength", "ratios", "The price-to-book ratio", _level_tests("{level:g}", 3, 0, 1000),
             ["pb", "pbratio", "pricetobook", "pricebook", "bookvalue"]),
    _company("roe", "Return on equity", "Valuation and strength", "ratios", "The return on equity", _level_tests("{level:g}%", 15, -1000, 100000, "per cent"),
             ["roe", "returnonequity"]),
    _company("de", "Debt to equity", "Valuation and strength", "ratios", "The debt-to-equity ratio", _level_tests("{level:g}", 1, 0, 1000),
             ["de", "debttoequity", "debtequity", "debt", "leverage", "gearing"]),
    _company("icr", "Interest cover", "Valuation and strength", "ratios", "The interest cover", _level_tests("{level:g} times", 3, 0, 100000, "times"),
             ["interestcover", "interestcoverage", "icr"]),
    _company("mcap", "Market capitalisation", "Valuation and strength", "ratios", "The market capitalisation",
             _level_tests("₹{level:g} crore", 5000, 0, 100000000, "₹ crore"), ["marketcap", "marketcapitalisation", "marketcapitalization", "mcap", "size", "largecap", "smallcap"]),
    _company("mf_flow", "Mutual fund buying and selling", "Mutual funds", "funds", "Mutual funds",
             [_t("new_atleast", "newly bought it last month: at least {n} funds", [_FUNDS_N], _m("exited_atleast")),
              _t("exited_atleast", "sold out of it last month: at least {n} funds", [_FUNDS_N], _m("new_atleast")),
              _t("bought_atleast", "added to their holding last month: at least {n} funds", [_FUNDS_N], _m("sold_atleast")),
              _t("sold_atleast", "trimmed their holding last month: at least {n} funds", [_FUNDS_N], _m("bought_atleast")),
              _t("net_buying", "added or newly bought it in more funds than trimmed or sold out of it last month", mirror=_m("net_selling")),
              _t("net_selling", "trimmed or sold out of it in more funds than added or newly bought it last month", mirror=_m("net_buying")),
              _t("holders_above", "hold it: more than {n} funds", [_FUNDS_HOLD], _m("holders_below")),
              _t("holders_below", "hold it: fewer than {n} funds", [_FUNDS_HOLD], _m("holders_above"))],
             ["mffunds", "fundbuying", "fundselling", "fundflows", "fundflow", "funds", "amcbuying", "institutionalbuying"]),
    _company("results_date", "Results date", "Company events", "events", "The company's next results",
             [_t("due_within", "are due within the next {days} days", [_p("days", "days", 14, 1, 90)]),
              _t("not_due_within", "are not due within the next {days} days", [_p("days", "days", 14, 1, 90)])],
             ["resultsdate", "earningsdate", "boardmeeting", "quarterlyresults", "resultsdue"]),
    _company("promoter", "Promoter holding", "Who owns it", "ownership", "The promoters' holding", _own_tests("{level:g}% of the company"),
             ["promoter", "promoters", "promoterholding"]),
    _company("pledged", "Promoter shares pledged", "Who owns it", "ownership", "The part of the promoters' holding that is pledged",
             _level_tests("{level:g}%", 10, 0, 100, "per cent") + [
                 _t("rising", "has risen since the previous quarter", mirror=_m("falling")), _t("falling", "has fallen since the previous quarter", mirror=_m("rising"))],
             ["pledge", "pledged", "promoterpledge", "pledging"]),
    _company("fii", "Foreign institutions' holding", "Who owns it", "ownership", "Foreign institutions' holding", _own_tests("{level:g}% of the company"),
             ["fii", "fpi", "foreigninstitutions", "foreigninvestors"]),
    _company("mf", "Mutual funds' holding", "Who owns it", "ownership", "Mutual funds' holding", _own_tests("{level:g}% of the company"),
             ["mutualfunds", "mfholding", "fundholding"]),
]
_IND = {i["key"]: i for i in INDICATORS}
for _i in INDICATORS:
    for _x in _i["tests"]:
        if _i.get("data") == "company":
            _x["text"] = "%s %s" % (_i["subject"], _x["predicate"])         # no time frame: these are read from the company's filings, not from candles
            _x["short"] = _x["predicate"]
        else:
            _x["text"] = "{tf} %s %s" % (_x["subject"] or _i["subject"], _x["predicate"])
            _x["short"] = ("%s %s" % (_x["subject"], _x["predicate"])) if _x["subject"] else _x["predicate"]
    _i["_tests"] = {t["key"]: t for t in _i["tests"]}


# =====================================================================
# BROKER TERMINALS -- where "Open your broker's terminal" goes
# =====================================================================
# OUTBOUND LINKS, NOTHING ELSE. The page opens the broker's own website in a new tab and that is the whole of it:
#
#   * Nothing here places an order, prefills one, holds a login, or talks to a broker's API. Whether the broker's
#     page then shows the terminal or its own sign-in depends on whether this browser already holds that broker's
#     session; that is the broker's business and cannot be seen from here. (Most brokers end every session each day.)
#   * NO REFERRAL CODES, NO TRACKING, NOT ONE. Every address is the broker's own public address. The one query
#     string below (Fyers) is Fyers' OWN login button -- `cb` is the address Fyers returns the person to after
#     signing in, copied from fyers.in, not a partner id. test_tradeplan_engine.py fails if any other address ever
#     carries a parameter. The same compliance boundary as brokers.py (SEBI RIA FAQ 34): FinChaya earns nothing
#     from a click and does not record which broker anybody opens.
#   * Each address was read from the broker's own site on `checked`. They are the sort of thing that moves, so
#     they are data here rather than scattered through the page, and one line is all it takes to change.
BROKERS = [
    {"key": "zerodha", "name": "Zerodha Kite", "url": "https://kite.zerodha.com/", "checked": "2026-10-11",
     "source": "Zerodha support: \"Login to Kite web\" -- visit kite.zerodha.com"},
    {"key": "fyers", "name": "Fyers", "url": "https://login.fyers.in/?cb=https://fyers.in/web", "checked": "2026-10-11",
     "source": "the Login button on fyers.in; Fyers' own sign-in, which returns to its web terminal"},
    {"key": "groww", "name": "Groww", "url": "https://groww.in/charts", "checked": "2026-10-11",
     "source": "\"Groww Terminal\" in the footer and F&O menu of groww.in (charts, orders, positions, watchlists)"},
    {"key": "dhan", "name": "Dhan", "url": "https://web.dhan.co/", "checked": "2026-10-11",
     "source": "dhan.co web trading platform page and the DhanHQ docs: sign in at web.dhan.co"},
]
BROKER_DISCLOSURE = ("These are plain links to each broker's own website. FinChaya is paid nothing by any of them, adds "
                     "no referral code, does not place or prefill an order, never sees your broker login, and does "
                     "not record which broker you open.")
MAX_BROKER_NAME = 40
MAX_TERMINAL_URL = 300
_HOST = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")
_PATH_OK = re.compile(r"^[A-Za-z0-9\-._~/%:!$&'()*+,;=]*$")      # no @: it looks like a login in a link and is never needed


def clean_terminal_link(name, url):
    """The broker a person types in under "Other", made safe to use as a link.

    The address is typed on the person's own device and opened by the person's own click, but a link is still
    a place a page can send somebody, so it is held to the plainest form:
      * https only (a trading login over plain http is refused, not silently upgraded: the address may not
        work upgraded, and a person pasting http:// should know);
      * no javascript:, data:, file:, ftp: or any other scheme;
      * a real host name -- letters, digits, hyphens and dots, at least one dot, not an IP address, no port, no
        user name or password in it (`https://me:secret@broker.com` would put a password in a link and in history);
      * ONLY THE ADDRESS IS KEPT. Anything after a ? or # is dropped. Pasted from a browser's address bar a terminal
        link can carry a session or request token, and a token should never be stored, even in the person's own
        browser. The answer says when something was dropped.
    Returns {"name", "url", "notes"} or raises SpecError in plain words."""
    nm = _text(name, "Broker", 1, MAX_BROKER_NAME, "the broker's name")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in nm):
        raise SpecError("Broker: the name has characters I cannot use.")
    if not isinstance(url, str):
        raise SpecError("Paste the address of your broker's trading terminal.")
    raw = url.strip()
    if not raw:
        raise SpecError("Paste the address of your broker's trading terminal.")
    if len(raw) > MAX_TERMINAL_URL:
        raise SpecError("That address is too long (up to %d characters)." % MAX_TERMINAL_URL)
    if re.search(r"\s", raw) or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise SpecError("The address cannot contain spaces.")
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
    elif raw.startswith("//"):
        scheme, rest = "https", raw[2:]
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:(?!\d+(/|$))", raw):
        raise SpecError("That is not a web address. Use the one that starts with https://")
    else:
        scheme, rest = "https", raw
    if scheme.lower() != "https":
        raise SpecError("Use the secure address that starts with https:// (a trading login should never be on plain http).")
    cut = re.search(r"[/?#]", rest)
    authority, remainder = (rest[:cut.start()], rest[cut.start():]) if cut else (rest, "")
    if "@" in authority:
        raise SpecError("The address must not contain a user name or password. Paste only the address of the page.")
    if ":" in authority:
        raise SpecError("The address must not include a port number.")
    host = authority.lower()
    if not host or not host.isascii() or not _HOST.match(host) or re.fullmatch(r"[\d.]+", host) \
            or not re.search(r"[a-z]{2,}$", host):
        raise SpecError("That does not look like a broker's web address, for example https://kite.zerodha.com/")
    notes = []
    path = remainder
    q = re.search(r"[?#]", path)
    if q:
        path = path[:q.start()]
        notes.append("Only the address itself was kept: anything after a ? or # was removed, so a session token "
                     "in the link you pasted is not stored.")
    if not path:
        path = "/"
    if not path.startswith("/") or not _PATH_OK.match(path):
        raise SpecError("That address has characters I cannot use. Paste the plain address of the page.")
    return {"name": nm, "url": "https://" + host + path, "notes": notes}


def catalog():
    """Everything the page needs to draw the builder, so it never hard-codes a name the validator
    would refuse. (The private "_tests" lookup is left out.)"""
    inds = [{k: v for k, v in i.items() if k != "_tests"} for i in INDICATORS]
    return {
        "instruments": [dict(i, coverage=coverage_for(i["key"])) for i in INSTRUMENTS],
        "timeframes": [{"key": k, "label": v, "intraday": k in INTRADAY} for k, v in CANDLE_TIMEFRAMES],
        "company_timeframe": {"key": COMPANY_TF, "label": TF_LABEL[COMPANY_TF]},
        "directions": list(DIRECTIONS),
        "indicators": inds,
        "brokers": [{"key": b["key"], "name": b["name"], "url": b["url"]} for b in BROKERS],
        "broker_disclosure": BROKER_DISCLOSURE,
        "limits": {"max_conditions": MAX_CONDITIONS, "max_name": MAX_NAME, "max_label": MAX_LABEL,
                   "max_notes": MAX_NOTES, "max_broker_name": MAX_BROKER_NAME},
    }


def known_indicator(name):
    """If `name` (however it is spelled) is an indicator we already have, its key; else None.
    Used so a request for "RSI" or "Bollinger bands" says "it is already there" instead of adding
    to the list of things we are asked to build."""
    k = name_key(name)
    if not k:
        return None
    for i in INDICATORS:
        if k == name_key(i["label"]) or k == i["key"] or k in i["aliases"]:
            return i["key"]
    return None


def name_key(text):
    """'Ichimoku Cloud', 'ichimoku-cloud' and 'ICHIMOKU  cloud' are one request."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


# =====================================================================
# VALIDATION -- turns whatever arrived into a clean plan, or says why not
# =====================================================================
def _text(v, where, lo, hi, what):
    if not isinstance(v, str):
        raise SpecError("%s: %s must be text." % (where, what))
    v = re.sub(r"\s+", " ", v).strip()
    if len(v) < lo:
        raise SpecError("%s: %s is needed." % (where, what))
    if len(v) > hi:
        raise SpecError("%s: %s can be up to %d characters." % (where, what, hi))
    return v


def _clean_condition(raw, n):
    where = "Condition %d" % n
    if not isinstance(raw, dict):
        raise SpecError("%s: it is not in a form I can read." % where)
    tf = raw.get("tf", "D")
    if not isinstance(tf, str) or tf not in TF_LABEL:
        raise SpecError("%s: choose a time frame." % where)
    ik = raw.get("indicator")
    ind = _IND.get(ik) if isinstance(ik, str) else None
    if ind is None:
        raise SpecError("%s: choose an indicator." % where)
    if ind.get("data") == "company":
        tf = COMPANY_TF                          # a company's profit has no time frame of its own: it is always read from its latest filings
    elif tf == COMPANY_TF:
        raise SpecError("%s: choose a time frame for %s." % (where, ind["label"]))
    named = "%s, %s" % (where, ind["label"])
    params_in = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    params = {}
    for pm in ind["params"]:
        params[pm["name"]] = be._number(params_in.get(pm["name"], pm["default"]), pm, named)
    if "fast" in params and "slow" in params and params["fast"] >= params["slow"]:
        raise SpecError("%s: the fast period must be shorter than the slow one." % named)
    tk = raw.get("test")
    test = ind["_tests"].get(tk) if isinstance(tk, str) else None
    if test is None:
        raise SpecError("%s: choose what to check." % named)
    inputs_in = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
    inputs = {}
    for pm in test["inputs"]:
        inputs[pm["name"]] = be._number(inputs_in.get(pm["name"], pm["default"]), pm, named)
    if "low" in inputs and "high" in inputs and inputs["low"] > inputs["high"]:
        raise SpecError("%s: the lower end of the range must not be above the upper end." % named)
    join = str(raw.get("join", "and")).lower() if n > 1 else "and"
    if join not in ("and", "or"):
        raise SpecError("%s: join it to the one before with AND or OR." % where)
    return {"tf": tf, "indicator": ik, "params": params, "test": tk, "inputs": inputs, "join": join}


def clean_plan(raw):
    """Validated copy of a plan; see _clean_plan. Anything the checks did not foresee (a value of a
    shape nobody thought of) is still a plain refusal for the person, never a crash."""
    try:
        return _clean_plan(raw)
    except SpecError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
        raise SpecError("The plan is not in a form I can read.")


def _clean_plan(raw):
    if not isinstance(raw, dict):
        raise SpecError("The plan is not in a form I can read.")
    name = _text(raw.get("name"), "Plan", 1, MAX_NAME, "a name")
    ik = raw.get("instrument")
    if not isinstance(ik, str) or ik not in _INSTR:
        raise SpecError("Choose an instrument.")
    label = None
    if ik == "other":
        label = _text(raw.get("instrument_label"), "Instrument", 1, MAX_LABEL, "a name for the instrument")
    direction = raw.get("direction", "bullish")
    if not isinstance(direction, str) or direction not in DIRECTIONS:
        raise SpecError("Say whether the plan is written for a bullish or a bearish view.")
    trade_tf = raw.get("trade_tf", "D")
    if not isinstance(trade_tf, str) or trade_tf not in TF_LABEL or trade_tf == COMPANY_TF:
        raise SpecError("Choose the time frame you trade on.")
    closed_only = raw.get("closed_only", False)
    if not isinstance(closed_only, bool):
        raise SpecError("\"Use closed candles only\" must be on or off.")
    notes = raw.get("notes")
    notes = "" if notes in (None, "") else _text(notes, "Notes", 0, MAX_NOTES, "notes")
    conds = raw.get("conditions")
    if not isinstance(conds, list) or not conds:
        raise SpecError("Add at least one condition.")
    if len(conds) > MAX_CONDITIONS:
        raise SpecError("A plan can have up to %d conditions." % MAX_CONDITIONS)
    return {"name": name, "instrument": ik, "instrument_label": label, "direction": direction,
            "trade_tf": trade_tf, "closed_only": closed_only, "notes": notes,
            "conditions": [_clean_condition(c, i + 1) for i, c in enumerate(conds)]}


# =====================================================================
# WORDS
# =====================================================================
def describe_condition(cond):
    ind = _IND[cond["indicator"]]
    test = ind["_tests"][cond["test"]]
    vals = dict(cond["params"])
    vals.update(cond["inputs"])
    text = test["text"].format(tf=TF_LABEL[cond["tf"]], **vals)
    if text.endswith(", by more than 0%"):                  # "any rise" reads as a plain "higher than", not "higher by more than 0%"
        text = text[:-len(", by more than 0%")]
    return text[:1].upper() + text[1:]


def groups(conds):
    """The sets the conditions form: a new set starts at every OR. [[0, 1], [2]] is (c0 and c1) or c2."""
    out = []
    for i, c in enumerate(conds):
        if i == 0 or c["join"] == "or":
            out.append([i])
        else:
            out[-1].append(i)
    return out


def describe_plan(plan):
    conds = plan["conditions"]
    gs = groups(conds)
    sets = [{"indices": g, "text": " and ".join(describe_condition(conds[i]) for i in g)} for g in gs]
    if len(gs) == 1:
        sentence = ("All of these must hold: " if len(gs[0]) > 1 else "") + sets[0]["text"] + "."
    else:
        sentence = "Any one of these sets will do: " + "; or ".join("(%s)" % s["text"] for s in sets) + "."
    tfs = sorted({c["tf"] for c in conds}, key=[k for k, _ in TIMEFRAMES].index)
    return {"sets": sets, "sentence": sentence,
            "timeframes": [TF_LABEL[t] for t in tfs],
            "conditions": [describe_condition(c) for c in conds]}


# =====================================================================
# MIRROR -- the same plan, seen from the other side
# =====================================================================
def mirror_condition(cond):
    ind = _IND[cond["indicator"]]
    test = ind["_tests"][cond["test"]]
    if test["mirror"] is None:
        return dict(cond, params=dict(cond["params"]), inputs=dict(cond["inputs"]))
    to, imap = test["mirror"]
    target = ind["_tests"][to]
    inputs = {}
    for pm in target["inputs"]:
        name = pm["name"]
        tr, src = imap.get(name, ("keep", name))
        inputs[name] = _FIX(_FLIP[tr](cond["inputs"][src]))
    return dict(cond, test=to, params=dict(cond["params"]), inputs=inputs)


def _FIX(v):
    """100 - 60.0 is 40.0; keep whole numbers whole so the sentence reads 'below 40', not 'below 40.0'."""
    return int(v) if float(v).is_integer() and abs(v) < 1e9 else v


def mirror_plan(plan):
    out = dict(plan)
    out["conditions"] = [mirror_condition(c) for c in plan["conditions"]]
    return out


def view_note(plan, view):
    """The sentence that tells the person their plan is being read from the other side, or None."""
    if view == plan["direction"]:
        return None
    return ("Your plan is written for a %s view. For a %s view each condition is turned round "
            "(for example RSI above 50 becomes RSI below 50, a crossing up becomes a crossing down), "
            "and the ones that do not take a side, such as trend strength or volume, stay as they are."
            % (plan["direction"], view))


# =====================================================================
# CANDLES: which one is the latest, and is it still forming?
# =====================================================================
def _as_date(ts):
    return ts.date() if hasattr(ts, "date") and callable(ts.date) else ts


def candle_forming(tf, label, today, now=None):
    """Is the newest candle (stamped `label`) still being drawn?

    Daily: never -- the daily candles we hold are end-of-day. Weekly and monthly: the candle is forming
    until its period has ended (the Friday, or the month's last weekday); a week that ended early on a
    holiday is read as forming until that Friday has passed, which is the cautious way to be wrong.
    Intraday: forming until its start plus its length has passed."""
    if tf == "D":
        return False
    if tf in INTRADAY:
        now = now or datetime.now()
        start = pd.Timestamp(label).to_pydatetime().replace(tzinfo=None)
        return start + timedelta(minutes=INTRADAY[tf]) > now
    label = pd.Timestamp(label).normalize()
    end = be._period_end(label, tf)
    return bool(label < end and pd.Timestamp(today) <= end)


def _stamp(tf, label):
    ts = pd.Timestamp(label)
    if tf in INTRADAY:
        return "%d %s %d, %02d:%02d" % (ts.day, be._MONTHS[ts.month - 1], ts.year, ts.hour, ts.minute)
    return be._dmy(ts.date().isoformat())


# =====================================================================
# EVALUATING ONE CONDITION
# =====================================================================
class _NoData(Exception):
    """A condition that cannot be judged yet. The message says why, for the person."""


class _Ctx:
    """The candles of one time frame plus a cache of indicator series."""

    def __init__(self, df, cond):
        self.df, self.p, self.i = df, cond["params"], cond["inputs"]
        self.tf = cond["tf"]
        self._cache = {}
        self.n = len(df)

    def s(self, kind, **params):
        key = (kind, tuple(sorted(params.items())))
        if key not in self._cache:
            self._cache[key] = be._compute(self.df, kind, params)
        return self._cache[key]

    def v(self, series, ago=0, what="this reading"):
        """The value `ago` candles back, or _NoData if it does not exist yet."""
        if len(series) <= ago:
            raise _NoData("there are only %d candles, too few for %s" % (self.n, what))
        x = series.iloc[-1 - ago]
        if x is None or (isinstance(x, float) and math.isnan(x)):
            raise _NoData("%d candles are not enough yet for %s" % (self.n, what))
        return float(x)

    def crossed(self, a, b, kind, within, what):
        """Did a cross above (or below) b in any of the last `within` candles? Same definition as the
        back-tester: above = now above and the candle before at or below. Returns the number of candles
        ago it happened (0 = on the latest candle) or None."""
        self.v(a, 0, what)                       # the latest value must exist
        bb = b if isinstance(b, pd.Series) else pd.Series(float(b), index=a.index)
        if kind == "up":
            x = (a > bb) & (a.shift(1) <= bb.shift(1))
        else:
            x = (a < bb) & (a.shift(1) >= bb.shift(1))
        tail = x.iloc[-within:]
        hits = [k for k, flag in enumerate(reversed(tail.tolist())) if flag]
        return hits[0] if hits else None


def _num(v):
    return ("%.2f" % v).rstrip("0").rstrip(".") if abs(v) < 1000 else "{:,.2f}".format(v)


def _ago_text(k):
    return "on the latest candle" if k == 0 else "%d candle%s ago" % (k, "" if k == 1 else "s")


def swing_pivots(H, L, k):
    """The alternating swing pivots of these candles, oldest first, each (index, 'H'|'L', price). A swing high is a candle whose high is above the highs of the k candles
    before it and at least the highs of the k after it (a swing low: below the lows before, at most the lows after), so a flat top counts at its first candle; a candle that
    is both is not a swing. Two of a kind in a row keep the more extreme. A swing is only known k candles after it, so the answer is what had been confirmed by the last
    candle: nothing here looks ahead."""
    pv = []
    for t in range(2 * k, len(H)):
        i = t - k
        hi, lo = H[i], L[i]
        is_h = all(hi > H[j] for j in range(i - k, i)) and all(hi >= H[j] for j in range(i + 1, i + k + 1))
        is_l = all(lo < L[j] for j in range(i - k, i)) and all(lo <= L[j] for j in range(i + 1, i + k + 1))
        if is_h == is_l:
            continue
        kind, price = ("H", hi) if is_h else ("L", lo)
        if pv and pv[-1][1] == kind:
            if (kind == "H" and price > pv[-1][2]) or (kind == "L" and price < pv[-1][2]):
                pv[-1] = (i, kind, price)
        else:
            pv.append((i, kind, price))
    return pv


def _eval_dow(test, c):
    """Dow theory on the candles' own highs and lows. -> (met, reading). Raises _NoData when there are not two swings of the kind to compare."""
    k = int(c.p["swing"])
    H, L = c.df["high"].astype(float).tolist(), c.df["low"].astype(float).tolist()
    n = len(H)
    if n < 2 * k + 3:
        raise _NoData("there are only %d candles, too few to find swings" % n)
    pv = swing_pivots(H, L, k)
    highs, lows = [x for x in pv if x[1] == "H"], [x for x in pv if x[1] == "L"]
    day = lambda x: _stamp(c.tf, c.df.index[x[0]])          # noqa: E731
    look = {"higher_low": ("low", lows, "above"), "lower_high": ("high", highs, "below"), "higher_high": ("high", highs, "above"), "lower_low": ("low", lows, "below")}

    if test in look:
        name, side, way = look[test]
        if len(side) < 2:
            raise _NoData("only %d swing %s%s has formed so far, and a comparison needs two" % (len(side), name, "" if len(side) == 1 else "s"))
        last, prev = side[-1], side[-2]
        better = last[2] > prev[2] if way == "above" else last[2] < prev[2]
        held = True
        if test == "higher_low":
            held = not min(L[last[0] + 1:], default=float("inf")) < last[2]
        elif test == "lower_high":
            held = not max(H[last[0] + 1:], default=float("-inf")) > last[2]
        ago = n - 1 - (last[0] + k)
        within = ago < int(c.i["bars"])
        base = "Latest swing %s %s (%s), the one before it %s (%s)" % (name, _num(last[2]), day(last), _num(prev[2]), day(prev))
        if not better:
            return False, base + "; it is not %s it" % way
        if not held:
            return False, base + "; a later candle went %s it (%s)" % ("below" if test == "higher_low" else "above", "undercut" if test == "higher_low" else "exceeded")
        if not within:
            return False, base + "; it was confirmed %s, outside the last %d candles" % (_ago_text(ago), int(c.i["bars"]))
        return True, base + "; confirmed %s" % _ago_text(ago)

    if len(highs) < 2 or len(lows) < 2:
        raise _NoData("only %d swing high%s and %d swing low%s have formed so far, and a trend needs two of each"
                      % (len(highs), "" if len(highs) == 1 else "s", len(lows), "" if len(lows) == 1 else "s"))
    (h1, h0), (l1, l0) = (highs[-1], highs[-2]), (lows[-1], lows[-2])
    up = test == "uptrend"
    hh = h1[2] > h0[2] if up else h1[2] < h0[2]
    ll = l1[2] > l0[2] if up else l1[2] < l0[2]
    if up:
        held = not min(L[l1[0] + 1:], default=float("inf")) < l1[2]
    else:
        held = not max(H[h1[0] + 1:], default=float("-inf")) > h1[2]
    reading = ("Latest swing high %s (%s) %s the one before it, %s (%s); latest swing low %s (%s) %s the one before it, %s (%s)"
               % (_num(h1[2]), day(h1), "above" if hh and up else "below" if hh else "not " + ("above" if up else "below"), _num(h0[2]), day(h0),
                  _num(l1[2]), day(l1), "above" if ll and up else "below" if ll else "not " + ("above" if up else "below"), _num(l0[2]), day(l0)))
    if hh and ll and not held:
        reading += "; but a later candle went %s the latest swing %s" % ("below" if up else "above", "low" if up else "high")
    return hh and ll and held, reading


def _eval(ind, test, c):
    """-> (met, reading). Raises _NoData."""
    p, i = c.p, c.i
    close = c.df["close"].astype(float)

    if ind == "dow":
        return _eval_dow(test, c)

    if ind == "range":
        n = int(p["period"])
        if len(c.df) < n:
            raise _NoData("there are only %d candles, too few for the last %d" % (len(c.df), n))
        px = c.v(close, 0, "the price")
        hi = float(c.df["high"].astype(float).iloc[-n:].max())
        lo = float(c.df["low"].astype(float).iloc[-n:].min())
        pct = float(i["pct"])
        if test in ("near_high", "below_high"):
            gap = (hi - px) / hi * 100 if hi > 0 else 0.0
            base = "Close %s, highest high of the last %d candles %s (%.1f%% below it)" % (_num(px), n, _num(hi), gap)
            return (gap <= pct if test == "near_high" else gap >= pct), base
        gap = (px - lo) / lo * 100 if lo > 0 else 0.0
        base = "Close %s, lowest low of the last %d candles %s (%.1f%% above it)" % (_num(px), n, _num(lo), gap)
        return (gap <= pct if test == "near_low" else gap >= pct), base

    if ind == "macd":
        kw = dict(fast=p["fast"], slow=p["slow"], signal=p["signal"])
        line, sig, hist = c.s("macd", **kw), c.s("macd_signal", **kw), c.s("macd_hist", **kw)
        m, s = c.v(line, 0, "MACD"), c.v(sig, 0, "the MACD signal line")
        base = "MACD %s, signal line %s" % (_num(m), _num(s))
        if test == "above_signal":
            return m > s, base
        if test == "below_signal":
            return m < s, base
        if test in ("cross_up", "cross_down"):
            k = c.crossed(line, sig, "up" if test == "cross_up" else "down", int(i["bars"]), "MACD")
            return k is not None, base + ("; crossed %s" % _ago_text(k) if k is not None else "; no crossing in that time")
        if test in ("rising", "falling"):
            prev = c.v(line, 1, "MACD on the previous candle")
            ok = m > prev if test == "rising" else m < prev
            return ok, "MACD went from %s to %s" % (_num(prev), _num(m))
        if test in ("hist_rising", "hist_falling"):
            h0, h1 = c.v(hist, 0, "the MACD histogram"), c.v(hist, 1, "the MACD histogram on the previous candle")
            ok = h0 > h1 if test == "hist_rising" else h0 < h1
            return ok, "MACD histogram went from %s to %s" % (_num(h1), _num(h0))
        if test == "above_zero":
            return m > 0, "MACD is %s" % _num(m)
        return m < 0, "MACD is %s" % _num(m)

    if ind == "rsi":
        r = c.s("rsi", period=p["period"])
        v = c.v(r, 0, "RSI (%d)" % p["period"])
        base = "RSI is %.1f" % v
        if test == "above":
            return v > i["level"], base
        if test == "below":
            return v < i["level"], base
        if test == "between":
            return i["low"] <= v <= i["high"], base
        if test in ("cross_up", "cross_down"):
            k = c.crossed(r, i["level"], "up" if test == "cross_up" else "down", int(i["bars"]), "RSI")
            return k is not None, base + ("; crossed %s" % _ago_text(k) if k is not None else "; no crossing in that time")
        prev = c.v(r, 1, "RSI on the previous candle")
        ok = v > prev if test == "rising" else v < prev
        return ok, "RSI went from %.1f to %.1f" % (prev, v)

    if ind == "adx":
        a = c.s("adx", period=p["period"])
        if test in ("above", "below", "rising", "falling"):
            v = c.v(a, 0, "ADX (%d)" % p["period"])
            if test == "above":
                return v > i["level"], "ADX is %.1f" % v
            if test == "below":
                return v < i["level"], "ADX is %.1f" % v
            prev = c.v(a, 1, "ADX on the previous candle")
            ok = v > prev if test == "rising" else v < prev
            return ok, "ADX went from %.1f to %.1f" % (prev, v)
        pdi = c.v(c.s("plus_di", period=p["period"]), 0, "+DI")
        mdi = c.v(c.s("minus_di", period=p["period"]), 0, "-DI")
        ok = pdi > mdi if test == "di_bull" else mdi > pdi
        return ok, "+DI is %.1f, -DI is %.1f" % (pdi, mdi)

    if ind == "supertrend":
        d = c.s("supertrend_dir", period=p["period"], factor=p["factor"])
        now_ = c.v(d, 0, "Supertrend")
        word = "up" if now_ > 0 else "down"
        if test in ("up", "down"):
            return (now_ > 0) == (test == "up"), "Supertrend is pointing %s" % word
        up = (d > 0) & (d.shift(1) < 0) if test == "flip_up" else (d < 0) & (d.shift(1) > 0)
        hits = [k for k, f in enumerate(reversed(up.iloc[-int(i["bars"]):].tolist())) if f]
        return bool(hits), "Supertrend is pointing %s" % word + (
            "; it turned %s" % _ago_text(hits[0]) if hits else "; no turn in that time")

    if ind in ("ema", "sma"):
        ma = c.s(ind, period=p["period"], src="close")
        m = c.v(ma, 0, "the %s (%d)" % (ind.upper(), p["period"]))
        px = c.v(close, 0, "the price")
        if test in ("price_above", "price_below"):
            ok = px > m if test == "price_above" else px < m
            return ok, "Price %s, %s (%d) %s" % (_num(px), ind.upper(), p["period"], _num(m))
        if test in ("cross_up", "cross_down"):
            k = c.crossed(close, ma, "up" if test == "cross_up" else "down", int(i["bars"]), ind.upper())
            return k is not None, "Price %s, %s (%d) %s" % (_num(px), ind.upper(), p["period"], _num(m)) + (
                "; crossed %s" % _ago_text(k) if k is not None else "; no crossing in that time")
        prev = c.v(ma, 1, "the %s on the previous candle" % ind.upper())
        ok = m > prev if test == "rising" else m < prev
        return ok, "%s (%d) went from %s to %s" % (ind.upper(), p["period"], _num(prev), _num(m))

    if ind in ("ema_cross", "sma_cross"):
        k_ = ind[:3]
        f = c.s(k_, period=p["fast"], src="close")
        sl = c.s(k_, period=p["slow"], src="close")
        fv, sv = c.v(f, 0, "the fast %s (%d)" % (k_.upper(), p["fast"])), c.v(sl, 0, "the slow %s (%d)" % (k_.upper(), p["slow"]))
        base = "%s (%d) %s, %s (%d) %s" % (k_.upper(), p["fast"], _num(fv), k_.upper(), p["slow"], _num(sv))
        if test == "fast_above":
            return fv > sv, base
        if test == "fast_below":
            return fv < sv, base
        k = c.crossed(f, sl, "up" if test == "cross_up" else "down", int(i["bars"]), k_.upper())
        return k is not None, base + ("; crossed %s" % _ago_text(k) if k is not None else "; no crossing in that time")

    if ind == "bb":
        kw = dict(period=p["period"], dev=p["dev"])
        px = c.v(close, 0, "the price")
        if test in ("above_upper", "below_lower"):
            band = c.v(c.s("bb_upper" if test == "above_upper" else "bb_lower", **kw), 0, "the Bollinger band")
            ok = px > band if test == "above_upper" else px < band
            return ok, "Price %s, %s band %s" % (_num(px), "upper" if test == "above_upper" else "lower", _num(band))
        band = c.v(c.s("bb_middle", **kw), 0, "the Bollinger middle band")
        ok = px > band if test == "above_middle" else px < band
        return ok, "Price %s, middle band %s" % (_num(px), _num(band))

    if ind == "roc":
        v = c.v(c.s("roc", period=p["period"]), 0, "the %d-candle change" % p["period"])
        ok = v > i["level"] if test == "above" else v < i["level"]
        return ok, "Price change over %d candles is %.1f%%" % (p["period"], v)

    if ind == "atr":
        a = c.s("atr", period=p["period"])
        v, prev = c.v(a, 0, "ATR (%d)" % p["period"]), c.v(a, 1, "ATR on the previous candle")
        ok = v > prev if test == "rising" else v < prev
        return ok, "ATR went from %s to %s" % (_num(prev), _num(v))

    if ind == "breakout":
        px = c.v(close, 0, "the price")
        if test == "above_high":
            lvl = c.v(c.s("highest", period=p["period"]), 0, "the %d-candle high" % p["period"])
            return px > lvl, "Close %s, highest high of the previous %d candles %s" % (_num(px), p["period"], _num(lvl))
        lvl = c.v(c.s("lowest", period=p["period"]), 0, "the %d-candle low" % p["period"])
        return px < lvl, "Close %s, lowest low of the previous %d candles %s" % (_num(px), p["period"], _num(lvl))

    if ind == "volume":
        vol = c.v(c.df["volume"].astype(float), 0, "volume")
        avg = c.v(c.s("sma", period=p["period"], src="volume"), 0, "the %d-candle average volume" % p["period"])
        if avg <= 0:
            raise _NoData("the average volume is zero")
        return vol > i["times"] * avg, "Volume %s is %.2f times its %d-candle average" % ("{:,.0f}".format(vol), vol / avg, p["period"])

    if ind == "candle":
        o, cl = c.v(c.df["open"].astype(float), 0, "the open"), c.v(close, 0, "the close")
        ok = cl > o if test == "green" else cl < o
        return ok, "Open %s, close %s" % (_num(o), _num(cl))

    raise SpecError("Unknown indicator %r." % ind)


# =====================================================================
# COMPANY DATA: results, ratios and shareholding (no candles)
# =====================================================================
_SHARE_COUNT_TOLERANCE = 0.15            # the same line api._fin_eps_comparable draws: a share count that moved more is a bonus or split


def _default_neighbour(rows, r, lo_days, hi_days):
    """The row of the same kind and basis whose period ended between lo and hi days before r's (as api._fin_neighbour)."""
    for o in rows:
        if o is r or o["period_type"] != r["period_type"] or o["consolidated"] != r["consolidated"]:
            continue
        if lo_days <= (r["period_end"] - o["period_end"]).days <= hi_days:
            return o
    return None


def _default_eps_like(now, then):
    """Is EPS like-for-like between two periods? Profit over EPS is the share count; if it moved by more than the tolerance (a bonus issue or split) do not
    compare. None = cannot tell (a figure is missing)."""
    try:
        a, b = abs(now["profit"] / now["eps"]), abs(then["profit"] / then["eps"])
        return abs(a / b - 1) <= _SHARE_COUNT_TOLERANCE
    except (TypeError, ZeroDivisionError):
        return None


def results_part(rows, neighbour=None, eps_like=None):
    """The company's results as the rules read them: its latest eight quarters and three financial years, each with the period a year earlier (and, for a
    quarter, the one before it) on the SAME basis (consolidated against consolidated), and whether EPS is like-for-like with it. `rows` are stock_financials rows,
    newest first, with revenue / profit / eps as plain numbers in rupees. `neighbour` and `eps_like` are the Financials tab's own helpers (api._fin_neighbour and
    api._fin_eps_comparable) when the site calls this, so a rule and that tab cannot disagree; the defaults here are the same two functions. None when there are none."""
    neighbour, eps_like = neighbour or _default_neighbour, eps_like or _default_eps_like
    rows = list(rows or [])
    qs = [r for r in rows if r["period_type"] == "quarterly"]
    ys = [r for r in rows if r["period_type"] == "yearly"]
    if not qs and not ys:
        return None

    def q_item(r):
        ya, pv = neighbour(rows, r, 350, 380), neighbour(rows, r, 80, 100)
        return {"row": r, "year_ago": ya, "prev": pv, "eps_year": eps_like(r, ya) if ya else None, "eps_prev": eps_like(r, pv) if pv else None}

    def y_item(r):
        pr = neighbour(rows, r, 350, 380)
        return {"row": r, "prior": pr, "eps_prior": eps_like(r, pr) if pr else None}
    return {"quarters": [q_item(r) for r in qs[:8]], "years": [y_item(r) for r in ys[:3]]}


_METRIC_WORD = {"profit": "profit", "revenue": "revenue", "eps": "EPS"}


def _indian(n):
    """A whole number with Indian digit grouping: 4,14,272."""
    s = "%d" % round(n)
    neg, s = s.startswith("-"), s.lstrip("-")
    if len(s) > 3:
        head, tail, parts = s[:-3], s[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if neg else "") + s


def _cr(x):
    """A rupee-crore figure, as the site writes them: whole numbers from a thousand up (₹4,14,272 crore), two decimals below (₹120.5 crore)."""
    return _indian(x) if abs(x) >= 1000 else _num(x)


def _money(metric, v):
    if v is None:
        return "no figure"
    return ("-" if v < 0 else "") + (("₹%.2f" % abs(v)) if metric == "eps" else "₹%s crore" % _cr(abs(v) / 1e7))


def _basis(r):
    return "%s, %s" % (r["period_label"], "consolidated" if r["consolidated"] else "standalone")


def _compare(metric, now, then, pct, up, then_word, now_word):
    """(met, reading) for a figure against an earlier one. pct 0 means any rise (or fall); above that the change must be MORE than pct per cent, which only
    means something against a positive earlier figure (against a loss or zero it is no data)."""
    if now is None:
        raise _NoData("the company has no %s figure for that period" % _METRIC_WORD[metric])
    if then is None:
        raise _NoData("there is no %s figure to compare it with" % _METRIC_WORD[metric])
    base = "%s was %s %s, against %s %s" % (_METRIC_WORD[metric].capitalize() if metric != "eps" else "EPS", _money(metric, now), now_word, _money(metric, then), then_word)
    if pct == 0:
        return (now > then if up else now < then), base
    if then <= 0:
        raise _NoData("the earlier %s was zero or a loss, so a percentage change means nothing" % _METRIC_WORD[metric])
    change = (now - then) / then * 100
    return (change > pct if up else change < -pct), "%s (%+.1f%%)" % (base, change)


def _eval_results(metric, test, part, inputs):
    """-> (met, reading, as_of) for a profit / revenue / EPS condition. Raises _NoData."""
    qs, ys = part["quarters"], part["years"]
    if test in ("profit_q", "loss_q"):
        if not qs:
            raise _NoData("no quarterly results are on file")
        r = qs[0]["row"]
        if r["profit"] is None:
            raise _NoData("the latest quarter has no profit figure")
        return (r["profit"] > 0 if test == "profit_q" else r["profit"] < 0), "Profit was %s in %s" % (_money("profit", r["profit"]), _basis(r)), _basis(r)
    if test in ("profit_fy", "loss_fy"):
        if not ys:
            raise _NoData("no full-year results are on file")
        r = ys[0]["row"]
        if r["profit"] is None:
            raise _NoData("the latest financial year has no profit figure")
        return (r["profit"] > 0 if test == "profit_fy" else r["profit"] < 0), "Profit was %s in %s" % (_money("profit", r["profit"]), _basis(r)), _basis(r)

    up = test.startswith("up_")
    span = test[test.index("_") + 1:]
    pct = float(inputs.get("pct", 0))
    guard = "EPS is not compared across a bonus issue or split, where the share count changed"
    if span in ("yoy", "qoq"):
        if not qs:
            raise _NoData("no quarterly results are on file")
        q = qs[0]
        then_row = q["year_ago"] if span == "yoy" else q["prev"]
        if then_row is None:
            raise _NoData("there is no %s on the same basis to compare the latest quarter with" % ("quarter a year earlier" if span == "yoy" else "quarter before it"))
        if metric == "eps" and (q["eps_year"] if span == "yoy" else q["eps_prev"]) is not True:
            raise _NoData(guard if (q["eps_year"] if span == "yoy" else q["eps_prev"]) is False else "it cannot be told whether the share count changed, so EPS is not compared")
        met, reading = _compare(metric, q["row"][metric], then_row[metric], pct, up, "a year earlier" if span == "yoy" else "in the previous quarter", "in " + _basis(q["row"]))
        return met, reading, _basis(q["row"])
    if span == "fy":
        if not ys:
            raise _NoData("no full-year results are on file")
        y = ys[0]
        if y["prior"] is None:
            raise _NoData("there is no earlier financial year on the same basis to compare the latest one with")
        if metric == "eps" and y["eps_prior"] is not True:
            raise _NoData(guard if y["eps_prior"] is False else "it cannot be told whether the share count changed, so EPS is not compared")
        met, reading = _compare(metric, y["row"][metric], y["prior"][metric], pct, up, "in the year before", "in " + _basis(y["row"]))
        return met, reading, _basis(y["row"])
    # a run of quarters, each against the same quarter a year earlier
    n = int(inputs["n"])
    if len(qs) < n:
        raise _NoData("only %d quarters of results are on file, and %d are needed" % (len(qs), n))
    verdicts, why = [], None
    for q in qs[:n]:
        if q["year_ago"] is None:
            verdicts.append(None)
            why = why or "%s has no quarter a year earlier on the same basis" % q["row"]["period_label"]
            continue
        if metric == "eps" and q["eps_year"] is not True:
            verdicts.append(None)
            why = why or "%s: %s" % (q["row"]["period_label"], guard if q["eps_year"] is False else "it cannot be told whether the share count changed")
            continue
        a, b = q["row"][metric], q["year_ago"][metric]
        verdicts.append(None if a is None or b is None else (a > b if up else a < b))
        if a is None or b is None:
            why = why or "%s has no %s figure" % (q["row"]["period_label"], _METRIC_WORD[metric])
    held = sum(v is True for v in verdicts)
    reading = "%s was %s than a year earlier in %d of the last %d quarters" % (_METRIC_WORD[metric].capitalize() if metric != "eps" else "EPS", "higher" if up else "lower", held, n)
    if any(v is False for v in verdicts):
        return False, reading, _basis(qs[0]["row"])
    if any(v is None for v in verdicts):
        raise _NoData(why or "some of those quarters cannot be compared")
    return True, reading, _basis(qs[0]["row"])


_RATIO_NAME = {"pe": "P/E", "peg": "PEG", "pb": "P/B", "roe": "Return on equity", "de": "Debt to equity", "icr": "Interest cover", "mcap": "Market capitalisation"}
_RATIO_KEY = {"icr": "interest_cover", "mcap": "market_cap"}


def _eval_ratio(ind, test, part, inputs):
    """-> (met, reading, as_of) for a ratio condition. `part` is api.build_ratios's answer: each ratio {value, reason, detail}."""
    r = part.get(_RATIO_KEY.get(ind, ind))
    if not isinstance(r, dict) or r.get("value") is None:
        raise _NoData((r or {}).get("reason") if isinstance(r, dict) and r.get("reason") else "this figure is not available for the stock")
    v, lvl = float(r["value"]), float(inputs["level"])
    shown = {"roe": "%s%%" % _num(v), "mcap": "₹%s crore" % _cr(v), "icr": "%s times" % _num(v)}.get(ind, _num(v))
    return (v > lvl if test == "above" else v < lvl), "%s is %s" % (_RATIO_NAME[ind], shown), "latest close and latest filings"


_OWN = {"promoter": ("promoter", "Promoter holding"), "pledged": ("pledged", "The pledged part of the promoters' holding"),
        "fii": ("foreign_institutions", "Foreign institutions' holding"), "mf": ("mutual_funds", "Mutual funds' holding")}


def _eval_own(ind, test, part, inputs):
    """-> (met, reading, as_of) for a shareholding condition. `part` is api.build_shareholding's answer (quarters newest first, each with its change)."""
    qs = (part or {}).get("quarters") or []
    if not qs:
        raise _NoData("no shareholding pattern is on file")
    key, name = _OWN[ind]
    q = qs[0]
    v = q.get(key)
    if v is None:
        raise _NoData("the detailed shareholding for %s has not been read yet" % q["label"])
    label = q["label"]
    if test in ("above", "below"):
        lvl = float(inputs["level"])
        return (v > lvl if test == "above" else v < lvl), "%s is %s%% (%s)" % (name, _num(v), label), label
    ch = (q.get("change") or {}).get(key)
    if ch is None:
        raise _NoData("there is no quarter immediately before %s to compare it with" % label)
    moved = "up %s points" % _num(ch) if ch > 0 else "down %s points" % _num(-ch) if ch < 0 else "unchanged"
    return (ch > 0 if test == "rising" else ch < 0), "%s is %s%% (%s), %s from the quarter before" % (name, _num(v), label, moved), label


def _eval_funds(test, part, inputs):
    """-> (met, reading, as_of) for a mutual-fund condition. `part` is {"as_of", "compared_with", "counts": {kind: funds}, "holders"}: what the funds' two latest monthly
    portfolios say about this stock (the stock page's own counts, a bonus or split not counted as buying)."""
    c = (part or {}).get("counts")
    if not isinstance(c, dict):
        raise _NoData("there is no comparison of two monthly portfolios yet")
    new, bought, sold, exited = (int(c.get(k, 0)) for k in ("new", "bought", "sold", "exited"))
    when = "portfolios of %s against %s" % (_stamp("D", date.fromisoformat(part["as_of"])), _stamp("D", date.fromisoformat(part["compared_with"])))
    tally = "Last month %d fund%s newly bought it, %d added, %d trimmed and %d sold out; %d hold it" % (new, "" if new == 1 else "s", bought, sold, exited, int(part.get("holders", 0)))
    n = int(inputs.get("n", 0))
    met = {"new_atleast": new >= n, "exited_atleast": exited >= n, "bought_atleast": bought >= n, "sold_atleast": sold >= n,
           "net_buying": new + bought > sold + exited, "net_selling": sold + exited > new + bought,
           "holders_above": int(part.get("holders", 0)) > n, "holders_below": int(part.get("holders", 0)) < n}[test]
    return met, tally, when

def _eval_events(test, part, inputs, today):
    """-> (met, reading, as_of) for "results are due within N days". `part` is {"results": [dates of announced results meetings, soonest first]}. With no date
    announced it is NO DATA, not "not due": a company that has not announced a date may still report soon."""
    ahead = sorted(d for d in (part or {}).get("results", []) if d >= today)
    if not ahead:
        raise _NoData("no results date has been announced yet")
    d = ahead[0]
    days = (d - today).days
    due = days <= int(inputs["days"])
    reading = "Results are due on %s, %s" % (_stamp("D", d), "today" if days == 0 else "in %d day%s" % (days, "" if days == 1 else "s"))
    return (due if test == "due_within" else not due), reading, "announced date"

def _evaluate_company(cond, frame, today=None):
    """One company-data condition against the company's own figures. `frame` is {"results": part, "ratios": part, "ownership": part} (a part may be a string: why
    it could not be read) or a string. Returns the same dict evaluate_condition does."""
    out = {"state": "no_data", "reading": None, "reason": None, "as_of": None, "forming": False}
    ind = _IND[cond["indicator"]]
    if isinstance(frame, str):
        out["reason"] = frame
        return out
    part = (frame or {}).get(ind["part"])
    if part is None or isinstance(part, str):
        out["reason"] = part or {"results": "No results are on file for this stock yet.", "ratios": "The ratios could not be worked out for this stock.",
                                 "ownership": "No shareholding pattern is on file for this stock yet.", "events": "No results dates could be read for this stock.", "funds": "No mutual-fund portfolios could be compared for this stock."}[ind["part"]]
        return out
    try:
        if ind["part"] == "results":
            met, reading, as_of = _eval_results(ind["key"], cond["test"], part, cond["inputs"])
        elif ind["part"] == "ratios":
            met, reading, as_of = _eval_ratio(ind["key"], cond["test"], part, cond["inputs"])
        elif ind["part"] == "events":
            met, reading, as_of = _eval_events(cond["test"], part, cond["inputs"], today or date.today())
        elif ind["part"] == "funds":
            met, reading, as_of = _eval_funds(cond["test"], part, cond["inputs"])
        else:
            met, reading, as_of = _eval_own(ind["key"], cond["test"], part, cond["inputs"])
    except _NoData as e:
        msg = str(e).rstrip(". ")                          # a reason the stock page gives already ends in a full stop
        out["reason"] = msg[:1].upper() + msg[1:] + "."
        return out
    out.update(state="met" if met else "not_met", reading=reading, as_of=as_of)
    return out


def evaluate_condition(cond, frame, today=None, now=None, closed_only=False):
    """One condition against the candles of its time frame (or, for a company-data condition, against the company's own figures).

    `frame` is a DataFrame (open/high/low/close/volume, ascending) or a string: the reason there are no
    candles. Returns {state: met | not_met | no_data, reading, reason, as_of, forming, closed_only}."""
    today = today or date.today()
    if cond["tf"] == COMPANY_TF:
        return _evaluate_company(cond, frame, today)
    out = {"state": "no_data", "reading": None, "reason": None, "as_of": None, "forming": False}
    if isinstance(frame, str):
        out["reason"] = frame
        return out
    if frame is None or len(frame) == 0:
        out["reason"] = "There are no candles for this time frame."
        return out
    df = frame
    forming = candle_forming(cond["tf"], df.index[-1], today, now)
    if closed_only and forming:
        df = df.iloc[:-1]
        forming = False
        if len(df) == 0:
            out["reason"] = "Only the candle still forming is held, and the plan uses closed candles only."
            return out
    out["as_of"] = _stamp(cond["tf"], df.index[-1])
    out["forming"] = forming
    try:
        met, reading = _eval(cond["indicator"], cond["test"], _Ctx(df, cond))
    except _NoData as e:
        out["reason"] = str(e)[:1].upper() + str(e)[1:] + "."
        return out
    out["state"] = "met" if met else "not_met"
    out["reading"] = reading
    return out


# =====================================================================
# THE WHOLE PLAN
# =====================================================================
def _and(states):
    if any(s is False for s in states):
        return False
    return None if any(s is None for s in states) else True


def _or(states):
    if any(s is True for s in states):
        return True
    return None if any(s is None for s in states) else False


_TRI = {"met": True, "not_met": False, "no_data": None}
_NAME = {True: "met", False: "not_met", None: "unknown"}


def evaluate_plan(plan, frames, view, today=None, now=None):
    """Check a CLEAN plan against candles for a view.

    frames -- {timeframe: DataFrame | reason string}; a time frame the plan uses and the dict lacks
              reads as "no candles".
    Returns the conditions with their results, the sets they form, and the plan's overall answer."""
    if view not in DIRECTIONS:
        raise SpecError("The view must be bullish or bearish.")
    today = today or date.today()
    mirrored = view != plan["direction"]
    used = mirror_plan(plan) if mirrored else plan
    conds = used["conditions"]

    rows = []
    for idx, c in enumerate(conds):
        r = evaluate_condition(c, frames.get(c["tf"], "No company figures were read for this stock." if c["tf"] == COMPANY_TF else "There are no candles for this time frame."),
                               today=today, now=now, closed_only=plan["closed_only"])
        r.update(index=idx, tf=c["tf"], tf_label=TF_LABEL[c["tf"]], join=c["join"],
                 text=describe_condition(c))
        if mirrored:
            r["written"] = describe_condition(plan["conditions"][idx])
        rows.append(r)

    gs = []
    for g in groups(conds):
        st = _and([_TRI[rows[i]["state"]] for i in g])
        gs.append({"indices": g, "state": _NAME[st]})
    overall = _or([{"met": True, "not_met": False, "unknown": None}[g["state"]] for g in gs])
    met = sum(r["state"] == "met" for r in rows)
    unknown = sum(r["state"] == "no_data" for r in rows)

    notes = []
    vn = view_note(plan, view)
    if vn:
        notes.append(vn)
    if plan["closed_only"]:
        notes.append("This plan uses closed candles only, so a weekly or monthly candle that is still forming is "
                     "left out and the one before it is read.")
    elif any(r["forming"] for r in rows):
        notes.append("A candle that is still forming can change before it closes, so a reading on it may be "
                     "different by the close. Turn on \"closed candles only\" in the plan to read only finished ones.")
    return {
        "view": view, "plan_direction": plan["direction"], "mirrored": mirrored,
        "conditions": rows, "sets": gs,
        "overall": {"state": _NAME[overall], "met": met, "total": len(rows), "unknown": unknown},
        "notes": notes,
    }
