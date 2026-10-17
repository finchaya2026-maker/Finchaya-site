"""
backtest_engine.py -- the rules-based back-tester behind /backtest.
---------------------------------------------------------------------------
PURE FUNCTIONS. No database, no web framework, nothing written anywhere. The
caller hands in daily candles and a rule set and gets numbers back, which is
what makes every line here testable on its own (test_backtest_engine.py).

WHAT A RULE SET IS
    A person builds rules out of three things:

      * OPERANDS  -- a price field (open/high/low/close/volume), an indicator
                     (SMA, EMA, RSI, MACD, Bollinger, ATR, ADX/DI, Supertrend,
                     highest-high / lowest-low of the previous N days, % change),
                     or a plain number. Any operand can be read from N days ago
                     ("yesterday's high") and multiplied by a factor
                     ("1.5 x the 20-day average volume").
      * CONDITIONS -- operand, comparison, operand. Comparisons are above /
                     at-or-above / below / at-or-below / crosses above /
                     crosses below. A condition can also be "true at any point
                     in the last N days", which is what lets two crossovers
                     that never land on the same day be combined.
      * GROUPS     -- ALL of (and) or ANY of (or), nested to a few levels. That
                     is the whole "and / or / any combination" requirement.

    ENTRY is a group. EXIT is an optional group, plus stop-loss, target,
    trailing stop and a holding limit. At least one way out is required: a
    position with no exit is not a back-test of anything.

    TIMEFRAMES. Every operand is read on daily, weekly or monthly candles, and one
    rule can mix them ("daily 5 EMA crosses 50 EMA AND monthly MACD is rising").
    Trading is always on daily candles. A weekly or monthly operand is worked out
    on its own candles (the site's indicators.to_weekly_real_days / to_monthly) and
    carried onto the daily days ONLY once its candle has finished: it first shows
    on the week's or month's last trading day and holds until the next candle
    finishes. "N candles ago" counts in the operand's own candles. See _htf_frame.

    MONEY. A test takes an amount per stock (default 1,00,000). Each stock is tested
    alone with all of it, profits staying in that stock, so a trade's rupee profit is
    the stock's balance times the trade's return and the trades add up exactly to the
    stock's total. Across a group the amounts are added up (the same sum in EACH
    stock); no rule splits one pot between stocks.

HONESTY RULES -- the things a back-tester gets wrong, and what is done here
    * NO PEEKING. A rule is judged on a day's CLOSE, so the order it produces
      cannot fill before the NEXT day's open. That is the default
      (fill = "next_open"). "same_close" is offered for people who want to see
      the optimistic version, and the page says which one was used.
      test_backtest_engine.py proves every indicator is causal: the value on
      day t is identical whether or not later days exist.
    * GAPS. A stop that the price gaps through is filled at the OPEN, not at
      the stop level the price never traded at. Same for a target.
    * SAME-DAY STOP AND TARGET. If one candle touched both, the stop is
      assumed to have come first. A daily candle cannot say which happened
      first, and assuming the kinder order would flatter every result.
      A TRAILING stop gets the same treatment: the day's own high counts as
      the best price BEFORE the day's low is tested against the trail, so a
      candle that makes a new high and then falls back through the trail is
      stopped out. (The kind order -- low first, then high -- let tight trails
      turn losers into winners; an independent review measured it.)
      A target that the price OPENS beyond fills at the open, ahead of any
      stop, because a resting order fills before the price can go anywhere.
    * NO INVENTED INDICATORS. Indicators come from indicators.py -- the same
      code that feeds the charts and the stock page, so an EMA here matches
      Kite -- and are left empty (NaN) until they have enough history to mean
      something. A condition that touches an empty value is simply false.
    * COSTS. A charge on each side is taken off every trade. It is an
      assumption and is shown to the reader as one.
    * ONE POSITION PER STOCK AT A TIME, all of the money in it. No pyramiding,
      no position sizing: the point is to see what the RULES did.
    * DATA JUMPS. Stored candles are a mix of adjusted and unadjusted (see
      api.py, price chart). api.py's confirmed split/bonus adjustment is applied
      before anything gets here; what is left is a one-day close-to-close move
      that nothing explains: a FALL of 30% or more (JUMP_DOWN) or a RISE of 60%
      or more (JUMP_UP). The fall threshold is lower because that is where an
      unrecorded 1:2 bonus (-33%) lands; real falls that large are rare on
      stocks with a 20% daily limit. A trade that spans one is SET ASIDE (still
      listed, not counted) because it is more likely a missing split than a
      real result. This does remove the occasional REAL crash, which flatters
      the result, so the count is always shown and the switch is the person's.

WHAT IT DOES NOT MODEL, and the page says so: tax, brokerage slabs, impact
cost on thin stocks, circuit limits, dividends (price returns only), and
stocks that were delisted (the database holds what is listed today).
"""

from __future__ import annotations

import math
import re
import time

import numpy as np
import pandas as pd

import indicators as ind

try:                                   # the Screener's chart scanners as a kind of condition ("Scanner signal"); a server without the file just does not offer them
    import scanner_signals as sg
except ImportError:                    # pragma: no cover
    sg = None


# =====================================================================
# LIMITS -- the spec arrives from a browser, so it is untrusted
# =====================================================================
MAX_STOCKS = 25            # stocks picked one by one
MAX_GROUP_STOCKS = 250     # stocks in a ready-made group (a cap segment, an index)
MAX_PERIOD = 400
MAX_AGO = 250
MAX_WITHIN = 30
MAX_CONDITIONS = 16        # per rule (entry / exit)
MAX_DEPTH = 4
MAX_TRADES_RETURNED = 3000
MIN_BARS = 60              # below this a stock has no history worth testing

JUMP_DOWN = 0.70           # a one-day close at or below 70% of the day before, unexplained
JUMP_UP = 1.60             # ... or at or above 160% of it
HORIZONS = (1, 5, 10, 20, 60, 120, 250)
YEARS_CHOICES = (1, 2, 3, 5, 7, 10)

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


class SpecError(ValueError):
    """The rule set is not usable. The message is for the person, in plain words."""


# =====================================================================
# THE CATALOGUE -- one place the page AND the validator both read
# =====================================================================
def _p(name, label, default, lo, hi, typ="int"):
    return {"name": name, "label": label, "default": default,
            "min": lo, "max": hi, "type": typ}


_SRC = {"name": "src", "label": "of", "default": "close", "type": "choice",
        "choices": ["close", "open", "high", "low", "volume"]}
_PERIOD = lambda d: _p("period", "period", d, 1, MAX_PERIOD)       # in candles of the operand's timeframe

# The candle size an operand is read on. Everything is still TRADED on daily candles; a weekly or
# monthly operand is worked out on weekly or monthly candles and then made available to the daily
# rule only once that week or month has finished (see _htf_frame).
TIMEFRAMES = (("D", "Daily"), ("W", "Weekly"), ("M", "Monthly"))
_TF = dict(TIMEFRAMES)
_TF_UNIT = {"D": "day", "W": "week", "M": "month"}
_TF_WORD = {"D": "", "W": "weekly ", "M": "monthly "}
# For a weekly or monthly operand: the last FINISHED candle (steady), or the one IN PROGRESS as it
# stands at each day's close (fresher, but it can change until the candle ends).
_CANDLES = {"completed": "Last finished candle", "current": "Candle in progress"}

DEFAULT_CAPITAL = 100000.0
MIN_CAPITAL, MAX_CAPITAL = 1000.0, 1e9

OPERANDS = [
    {"kind": "close",  "label": "Close",  "group": "Price", "params": []},
    {"kind": "open",   "label": "Open",   "group": "Price", "params": []},
    {"kind": "high",   "label": "High",   "group": "Price", "params": []},
    {"kind": "low",    "label": "Low",    "group": "Price", "params": []},
    {"kind": "volume", "label": "Volume", "group": "Price", "params": []},

    {"kind": "sma", "label": "Simple moving average (SMA)", "group": "Averages",
     "params": [_PERIOD(20), _SRC]},
    {"kind": "ema", "label": "Exponential moving average (EMA)", "group": "Averages",
     "params": [_PERIOD(20), _SRC]},

    {"kind": "rsi", "label": "RSI", "group": "Momentum", "params": [_PERIOD(14)]},
    {"kind": "macd", "label": "MACD line", "group": "Momentum",
     "params": [_p("fast", "fast", 12, 2, MAX_PERIOD), _p("slow", "slow", 26, 3, MAX_PERIOD),
                _p("signal", "signal", 9, 1, MAX_PERIOD)]},
    {"kind": "macd_signal", "label": "MACD signal line", "group": "Momentum",
     "params": [_p("fast", "fast", 12, 2, MAX_PERIOD), _p("slow", "slow", 26, 3, MAX_PERIOD),
                _p("signal", "signal", 9, 1, MAX_PERIOD)]},
    {"kind": "macd_hist", "label": "MACD histogram", "group": "Momentum",
     "params": [_p("fast", "fast", 12, 2, MAX_PERIOD), _p("slow", "slow", 26, 3, MAX_PERIOD),
                _p("signal", "signal", 9, 1, MAX_PERIOD)]},
    {"kind": "roc", "label": "Percent change over N candles", "group": "Momentum",
     "params": [_PERIOD(1)]},

    {"kind": "bb_upper", "label": "Bollinger upper band", "group": "Bands and range",
     "params": [_PERIOD(20), _p("dev", "std devs", 2, 0.1, 10, "float")]},
    {"kind": "bb_middle", "label": "Bollinger middle band", "group": "Bands and range",
     "params": [_PERIOD(20), _p("dev", "std devs", 2, 0.1, 10, "float")]},
    {"kind": "bb_lower", "label": "Bollinger lower band", "group": "Bands and range",
     "params": [_PERIOD(20), _p("dev", "std devs", 2, 0.1, 10, "float")]},
    {"kind": "atr", "label": "ATR (average true range)", "group": "Bands and range",
     "params": [_PERIOD(14)]},
    {"kind": "highest", "label": "Highest high of the previous N candles", "group": "Bands and range",
     "params": [_PERIOD(20)]},
    {"kind": "lowest", "label": "Lowest low of the previous N candles", "group": "Bands and range",
     "params": [_PERIOD(20)]},

    # Period 1 is meaningless for these (a one-candle +DI is only ever 0 or 100) and is the one setting
    # where the candle-in-progress reading cannot be done in one step, so the minimum is 2.
    {"kind": "adx", "label": "ADX (trend strength)", "group": "Trend",
     "params": [_p("period", "period", 14, 2, MAX_PERIOD)]},
    {"kind": "plus_di", "label": "+DI", "group": "Trend", "params": [_p("period", "period", 14, 2, MAX_PERIOD)]},
    {"kind": "minus_di", "label": "-DI", "group": "Trend", "params": [_p("period", "period", 14, 2, MAX_PERIOD)]},
    {"kind": "supertrend", "label": "Supertrend line", "group": "Trend",
     "params": [_PERIOD(10), _p("factor", "multiplier", 3, 0.1, 20, "float")]},
    {"kind": "supertrend_dir", "label": "Supertrend direction (1 = up, -1 = down)", "group": "Trend",
     "params": [_PERIOD(10), _p("factor", "multiplier", 3, 0.1, 20, "float")]},

    {"kind": "number", "label": "A number", "group": "Number",
     "params": [_p("value", "value", 0, -1e9, 1e9, "float")]},
]
_KIND = {o["kind"]: o for o in OPERANDS}

COMPARISONS = [
    ("gt", "is above"),
    ("gte", "is at or above"),
    ("lt", "is below"),
    ("lte", "is at or below"),
    ("crosses_above", "crosses above"),
    ("crosses_below", "crosses below"),
]
_CMP = dict(COMPARISONS)

REASONS = ("Stop-loss", "Stepped stop", "Trailing stop", "Target", "Exit rule", "Time limit", "Still open")


def catalog():
    """Everything the page needs to draw the builder, so it never hard-codes a
    name the validator would refuse."""
    return {
        "operands": OPERANDS,
        "comparisons": [{"key": k, "label": v} for k, v in COMPARISONS],
        "timeframes": [{"key": k, "label": v, "unit": _TF_UNIT[k]} for k, v in TIMEFRAMES],
        "candles": [{"key": k, "label": v} for k, v in _CANDLES.items()],
        "limits": {"max_stocks": MAX_STOCKS, "max_conditions": MAX_CONDITIONS,
                   "max_depth": MAX_DEPTH, "max_within": MAX_WITHIN,
                   "max_ago": MAX_AGO, "years": list(YEARS_CHOICES),
                   "capital": {"default": DEFAULT_CAPITAL, "min": MIN_CAPITAL, "max": MAX_CAPITAL}},
        # the chart scanners that can be replayed as a condition: {key, label, what, direction, controls}
        "signals": sg.catalog() if sg is not None else [],
    }


# =====================================================================
# VALIDATION -- turns whatever arrived into a clean spec, or says why not
# =====================================================================
def _number(v, pm, where):
    lo, hi = pm["min"], pm["max"]
    if isinstance(v, bool) or v is None:
        raise SpecError("%s: %s needs a number." % (where, pm["label"]))
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):      # 10**400 is a valid JSON integer
        raise SpecError("%s: %s needs a number." % (where, pm["label"]))
    if not math.isfinite(f):
        raise SpecError("%s: %s needs a number." % (where, pm["label"]))
    if pm["type"] == "int":
        if not f.is_integer():
            raise SpecError("%s: %s must be a whole number." % (where, pm["label"]))
        f = int(f)
    if f < lo or f > hi:
        raise SpecError("%s: %s must be between %g and %g." % (where, pm["label"], lo, hi))
    return f


def _clean_operand(o, where):
    if not isinstance(o, dict):
        raise SpecError("%s: pick something to compare." % where)
    kind = o.get("kind")
    meta = _KIND.get(kind) if isinstance(kind, str) else None      # a list or object here is not a name
    if meta is None:
        raise SpecError("%s: pick something to compare." % where)
    out = {"kind": kind}
    named = "%s, %s" % (where, meta["label"])            # so "days" says WHICH days
    for pm in meta["params"]:
        v = o.get(pm["name"], pm["default"])
        if pm["type"] == "choice":
            if v not in pm["choices"]:
                raise SpecError("%s: %s must be one of %s." % (named, pm["label"], ", ".join(pm["choices"])))
            out[pm["name"]] = v
        else:
            out[pm["name"]] = _number(v, pm, named)
    if kind in ("macd", "macd_signal", "macd_hist") and out["fast"] >= out["slow"]:
        raise SpecError("%s: the MACD fast period must be shorter than the slow one." % named)
    out["ago"] = _number(o.get("ago", 0), _p("ago", "candles ago", 0, 0, MAX_AGO), named)
    out["times"] = _number(o.get("times", 1), _p("times", "multiplier", 1, 0.001, 1000, "float"), named)
    tf = o.get("tf", "D")
    if kind == "number":
        tf = "D"                                  # a plain number has no candles
    elif not isinstance(tf, str) or tf not in _TF:
        raise SpecError("%s: choose daily, weekly or monthly candles." % named)
    out["tf"] = tf
    candle = o.get("candle", "completed")
    if not isinstance(candle, str) or candle not in _CANDLES:
        raise SpecError("%s: choose the last finished candle or the candle in progress." % named)
    # A daily candle is always finished by the time a rule looks at it, so the choice only exists
    # for weekly and monthly ones.
    out["candle"] = candle if tf != "D" else "completed"
    return out


def _clean_tree(node, label, counter, depth):
    if depth > MAX_DEPTH:
        raise SpecError("%s: groups are nested too deeply (up to %d levels)." % (label, MAX_DEPTH))
    if not isinstance(node, dict):
        raise SpecError("%s: the rule is not in a form I can read." % label)
    if "items" in node:
        op = str(node.get("op", "and")).lower()
        if op not in ("and", "or"):
            raise SpecError("%s: a group must match ALL or ANY of its conditions." % label)
        items = node.get("items")
        if not isinstance(items, list) or not items:
            raise SpecError("%s: a group has nothing in it. Add a condition or remove the group." % label)
        return {"op": op, "items": [_clean_tree(i, label, counter, depth + 1) for i in items]}
    counter[0] += 1
    if counter[0] > MAX_CONDITIONS:
        raise SpecError("%s: too many conditions (up to %d)." % (label, MAX_CONDITIONS))
    where = "%s, condition %d" % (label, counter[0])
    if "signal" in node:                                  # a chart scanner as a condition: the stock newly appears on it
        if sg is None:
            raise SpecError("%s: scanner signals are not available on this server yet. Upload scanner_signals.py and restart." % where)
        try:
            sig = sg.clean(node.get("signal"), where)
        except sg.SignalError as e:
            raise SpecError(str(e))
        within = _number(node.get("within", 1), _p("within", "days", 1, 1, MAX_WITHIN), where)
        return {"signal": sig, "within": within}
    cmp_ = node.get("cmp")
    if not isinstance(cmp_, str) or cmp_ not in _CMP:
        raise SpecError("%s: choose how the two sides compare." % where)
    left = _clean_operand(node.get("left"), where)
    right = _clean_operand(node.get("right"), where)
    if left["kind"] == "number" and right["kind"] == "number":
        raise SpecError("%s: both sides are plain numbers, so it can never change. "
                        "Put a price or an indicator on one side." % where)
    if left == right:
        # Both sides read the very same number, so ">" "<" and "crosses" can never be true (and ">=" "<=" are
        # always true). The usual cause: asking whether a weekly or monthly value is "rising" without setting
        # "months ago" to 1 on the right-hand side. A run that finds no trade for this reason looks like a fault.
        what = describe_operand(left)
        hint = ""
        if left.get("tf", "D") != "D":
            unit = _TF_UNIT[left["tf"]]
            hint = (" To ask whether it is rising, set \"%ss ago\" to 1 on the right-hand side, so the %s is "
                    "compared with the one before it." % (unit, unit))
        elif left["kind"] != "number":
            hint = " To compare it with an earlier day, set \"days ago\" to 1 on the right-hand side."
        raise SpecError("%s: both sides are the same thing (%s), so this can never be true.%s" % (where, what, hint))
    within = _number(node.get("within", 1), _p("within", "days", 1, 1, MAX_WITHIN), where)
    return {"left": left, "cmp": cmp_, "right": right, "within": within}


def _opt_pct(v, name, hi):
    if v in (None, "", False):
        return None
    f = _number(v, _p(name, name, 1, 0.01, hi, "float"), "Exit settings")
    return f


def clean_spec(raw, need_exit=True):
    """Validated copy of the rule set; see _clean_spec. Anything the checks inside it did not foresee
    (a value of a shape nobody thought of) is still a plain refusal for the person, never a crash."""
    try:
        return _clean_spec(raw, need_exit)
    except SpecError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
        raise SpecError("The rules are not in a form I can read. Rebuild the condition and try again.")


def _clean_spec(raw, need_exit=True):
    """Validated copy of the rule set. Raises SpecError with a plain message.

    need_exit=False is for the page's live preview, which describes a rule
    while it is still being built and has no business complaining that the
    exit is missing yet."""
    if not isinstance(raw, dict):
        raise SpecError("Nothing to test: the request was empty.")

    direction = str(raw.get("direction", "long")).lower()
    if direction not in ("long", "short"):
        raise SpecError("Direction must be long or short.")
    fill = str(raw.get("fill", "next_open")).lower()
    if fill not in ("next_open", "same_close"):
        raise SpecError("Fill must be next_open or same_close.")

    years = _number(raw.get("years", 10), _p("years", "Period", 10, 1, 15), "Settings")
    cost = _number(raw.get("cost_pct", 0.1), _p("cost_pct", "Cost per side", 0.1, 0, 2, "float"), "Settings")
    capital = _number(raw.get("capital", DEFAULT_CAPITAL),
                      _p("capital", "Money per stock", DEFAULT_CAPITAL, MIN_CAPITAL, MAX_CAPITAL, "float"), "Settings")

    entry_raw = raw.get("entry")
    if isinstance(entry_raw, dict) and "items" not in entry_raw and entry_raw:
        entry_raw = {"op": "and", "items": [entry_raw]}
    entry = _clean_tree(entry_raw, "Entry", [0], 1)

    exit_tree = None
    exit_raw = raw.get("exit")
    if exit_raw:
        if isinstance(exit_raw, dict) and "items" not in exit_raw:
            exit_raw = {"op": "and", "items": [exit_raw]}
        if isinstance(exit_raw.get("items") if isinstance(exit_raw, dict) else None, list) \
                and not exit_raw["items"]:
            exit_tree = None                      # an empty group means "no exit rule"
        else:
            exit_tree = _clean_tree(exit_raw, "Exit", [0], 1)

    sl = _opt_pct(raw.get("stop_loss_pct"), "Stop-loss", 90)
    tp = _opt_pct(raw.get("target_pct"), "Target", 1000)
    tr = _opt_pct(raw.get("trailing_pct"), "Trailing stop", 90)
    # A stepped stop: starts at the stop-loss and moves up `step_by` for every `step_every` the price rises.
    step_every = _opt_pct(raw.get("step_every_pct"), "Stepped stop, the rise for each step", 1000)
    step_by = _opt_pct(raw.get("step_by_pct"), "Stepped stop, how far the stop moves up", 90)
    if step_every or step_by:
        if not sl:
            raise SpecError("The stepped stop starts from the stop-loss, so fill in the stop-loss too (or clear "
                            "the stepped stop).")
        if not step_every:
            raise SpecError("Stepped stop: say how far the price must rise for each step.")
        if not step_by:
            step_by = sl                  # "move by the percentage mentioned": by default as far as the stop-loss is
    mh = raw.get("max_hold_days")
    mh = None if mh in (None, "", False) else _number(mh, _p("max_hold_days", "Holding limit", 20, 1, 1000),
                                                      "Exit settings")
    if need_exit and not (exit_tree or sl or tp or tr or mh):
        raise SpecError("Choose at least one way to exit: an exit rule, a stop-loss, a target, "
                        "a trailing stop, or a holding limit in days.")

    exclude = raw.get("exclude_jumps", True)
    if not isinstance(exclude, bool):                 # bool("false") is True; do not guess
        raise SpecError("Settings: exclude_jumps must be true or false.")

    return {"direction": direction, "fill": fill, "years": years, "cost_pct": cost, "capital": capital,
            "entry": entry, "exit": exit_tree, "stop_loss_pct": sl, "target_pct": tp,
            "trailing_pct": tr, "step_every_pct": step_every, "step_by_pct": step_by,
            "max_hold_days": mh, "exclude_jumps": exclude}


# =====================================================================
# DESCRIBING A RULE IN WORDS -- shown back so the person can check it
# =====================================================================
def _g(v):
    """A number for a sentence: 1.5, 20, and 10,000,000 rather than 1e+07."""
    if abs(v) >= 1e6:
        return "{:,.0f}".format(v) if float(v).is_integer() else "{:,.2f}".format(v)
    return "%g" % v


def describe_operand(o):
    """An operand in words. A weekly or monthly one says so, and counts in weeks or
    months: "5-week EMA of close", "monthly MACD line (12, 26, 9)", "1 month ago"."""
    k = o["kind"]
    if k == "number":
        return _g(o["value"])                     # a plain number: no "ago", no multiplier
    tf = o.get("tf", "D")
    unit, word = _TF_UNIT[tf], _TF_WORD[tf]
    if k in ("close", "open", "high", "low", "volume"):
        s = (word + k) if word else k.capitalize()
    elif k in ("sma", "ema"):
        s = "%d-%s %s of %s" % (o["period"], unit, k.upper(), o["src"])
        s = (word + s) if word else s
    elif k == "rsi":
        s = "%sRSI(%d)" % (word, o["period"])
    elif k in ("macd", "macd_signal", "macd_hist"):
        nm = {"macd": "MACD line", "macd_signal": "MACD signal line", "macd_hist": "MACD histogram"}[k]
        s = "%s%s (%d, %d, %d)" % (word, nm, o["fast"], o["slow"], o["signal"])
    elif k == "roc":
        s = "%d-%s %% change" % (o["period"], unit)
        s = (word + s) if word else s
    elif k.startswith("bb_"):
        nm = {"bb_upper": "upper", "bb_middle": "middle", "bb_lower": "lower"}[k]
        s = "%sBollinger %s band (%d, %s)" % (word, nm, o["period"], _g(o["dev"]))
    elif k == "atr":
        s = "%sATR(%d)" % (word, o["period"])
    elif k in ("highest", "lowest"):
        what = "highest high" if k == "highest" else "lowest low"
        if tf == "D":
            s = "highest high of the previous %d days" % o["period"] if k == "highest" \
                else "lowest low of the previous %d days" % o["period"]
        else:
            # The N candles BEFORE the one the operand is read at, so a candle's own close can be tested
            # against the level it is breaking. For a finished reading that candle is the last finished
            # one; for the in-progress reading it is the candle in progress, so the N finished ones.
            ref = ("the week in progress" if tf == "W" else "the month in progress") \
                if o.get("candle") == "current" else ("the last finished week" if tf == "W" else "the last finished month")
            s = "%s%s of the %d %ss before %s" % (word, what, o["period"], unit, ref)
    elif k == "adx":
        s = "%sADX(%d)" % (word, o["period"])
    elif k == "plus_di":
        s = "%s+DI(%d)" % (word, o["period"])
    elif k == "minus_di":
        s = "%s-DI(%d)" % (word, o["period"])
    elif k == "supertrend":
        s = "%sSupertrend line (%d, %s)" % (word, o["period"], _g(o["factor"]))
    elif k == "supertrend_dir":
        s = "%sSupertrend direction (%d, %s)" % (word, o["period"], _g(o["factor"]))
    else:
        raise SpecError("Unknown indicator %r." % k)
    if tf != "D" and o.get("candle") == "current":
        # `ago` counts back from the candle in progress: 0 is that candle, 1 the last finished one
        if o.get("ago"):
            s += " (%d %s%s before the one in progress)" % (o["ago"], unit, "" if o["ago"] == 1 else "s")
        elif k not in ("highest", "lowest"):          # those already say "before the week in progress"
            s += " (%s in progress)" % unit
    elif o.get("ago"):
        s += (" yesterday" if o["ago"] == 1 and tf == "D"
              else " 1 %s ago" % unit if o["ago"] == 1 else " %d %ss ago" % (o["ago"], unit))
    if o.get("times", 1) != 1:
        s = "%s x %s" % (_g(o["times"]), s)
    return s


def _describe_node(node, top):
    if "items" in node:
        parts = [_describe_node(i, False) for i in node["items"]]
        if len(parts) == 1:
            return parts[0]
        txt = (" AND " if node["op"] == "and" else " OR ").join(parts)
        return txt if top else "(" + txt + ")"
    if "signal" in node:
        txt = sg.describe(node["signal"])
    else:
        txt = "%s %s %s" % (describe_operand(node["left"]), _CMP[node["cmp"]], describe_operand(node["right"]))
    if node.get("within", 1) > 1:
        txt += " (at any point in the last %d days)" % node["within"]
    return txt


def describe_tree(node):
    """{"join": "AND"/"OR", "lines": [...]} for the top level of a rule."""
    if "items" not in node:
        return {"join": "AND", "lines": [_describe_node(node, True)]}
    return {"join": "AND" if node["op"] == "and" else "OR",
            "lines": [_describe_node(i, False) for i in node["items"]]}


# =====================================================================
# OPERANDS AND CONDITIONS -> series
# =====================================================================
def _mask(s, n):
    """Blank the first n values: an indicator inside its warm-up is arithmetic,
    not information (the same stance indicators.py takes)."""
    s = s.copy()
    s.iloc[:n] = np.nan
    return s


def _compute(df, kind, p):
    close = df["close"]
    if kind in ("close", "open", "high", "low", "volume"):
        return df[kind].astype(float)
    if kind == "sma":
        return df[p["src"]].astype(float).rolling(p["period"]).mean()
    if kind == "ema":
        return ind.ema(df, periods=(p["period"],), price_col=p["src"])["ema_%d" % p["period"]]
    if kind == "rsi":
        return _mask(ind.rsi(df, p["period"])["rsi"], 2 * p["period"])
    if kind in ("macd", "macd_signal", "macd_hist"):
        m = ind.macd(df, p["fast"], p["slow"], p["signal"])
        col = {"macd": "macd_line", "macd_signal": "macd_signal", "macd_hist": "macd_hist"}[kind]
        return _mask(m[col], p["slow"] + p["signal"])
    if kind == "roc":
        return (close / close.shift(p["period"]) - 1.0) * 100.0
    if kind.startswith("bb_"):
        mid = close.rolling(p["period"]).mean()
        if kind == "bb_middle":
            return mid
        sd = close.rolling(p["period"]).std(ddof=0)        # population, as the chart does
        return mid + p["dev"] * sd if kind == "bb_upper" else mid - p["dev"] * sd
    if kind == "atr":
        return _mask(ind._wilder_atr(df, p["period"]), p["period"])
    if kind == "highest":
        return df["high"].rolling(p["period"]).max().shift(1)
    if kind == "lowest":
        return df["low"].rolling(p["period"]).min().shift(1)
    if kind in ("adx", "plus_di", "minus_di"):
        a = ind.adx(df, p["period"])
        return _mask(a[kind], (3 if kind == "adx" else 2) * p["period"])
    if kind in ("supertrend", "supertrend_dir"):
        st = ind.supertrend(df, p["period"], p["factor"])
        if kind == "supertrend":
            return _mask(st["supertrend"], 2 * p["period"])
        d = pd.Series(np.where(st["st_bullish"], 1.0, -1.0), index=df.index)
        return _mask(d, 2 * p["period"])
    raise SpecError("Unknown indicator %r." % kind)


def _candles(df, tf):
    """The site's own weekly or monthly candles for a daily frame, or a plain refusal if this server's
    indicators.py is too old to have them (to_weekly_real_days was added to it recently)."""
    f = getattr(ind, "to_weekly_real_days" if tf == "W" else "to_monthly", None)
    if f is None:
        raise SpecError("Weekly and monthly candles are not available on this server yet: its indicators.py "
                        "is older than the back-test needs. Upload the current indicators.py and restart.")
    return f(df)


def _period_end(label, tf):
    """The earliest day the week or month containing `label` can be over: the Friday
    of that week, or the last weekday of that month."""
    if tf == "W":
        return label.to_period("W-FRI").end_time.normalize()
    end = label.to_period("M").end_time.normalize()
    while end.weekday() > 4:
        end -= pd.Timedelta(days=1)
    return end


def _htf_frame(df, tf, cache):
    """The stock's weekly or monthly candles, built by the site's own indicators.to_weekly_real_days
    and indicators.to_monthly (so they are the candles Kite and the stock page show), each labelled
    with the LAST TRADING DAY of its week or month.

    ONLY FINISHED CANDLES. The newest week or month in the data is dropped if it has not reached its
    end (a Friday, or the month's last weekday). A candle still forming is a number nobody has yet:
    using it would hand a daily rule next Friday's close on Wednesday. Every earlier candle is
    finished by definition, and the exchange's holidays are published in advance, so labelling a
    week that ended on Thursday with Thursday is not hindsight.
    """
    key = ("frame", tf)
    if key not in cache:
        f = _candles(df, tf)
        if len(f) and f.index[-1] < _period_end(f.index[-1], tf):
            f = f.iloc[:-1]
        cache[key] = f
    return cache[key]


# ---------------------------------------------------------------------------------------------
# THE CANDLE IN PROGRESS
# ---------------------------------------------------------------------------------------------
# A weekly or monthly operand can be read two ways, and the person chooses:
#   completed  (default)  the last FINISHED week or month: stable, but up to a month stale;
#   current               the week or month IN PROGRESS as it stands at each day's close: fresher, but it
#                         can change from day to day and flip back before the candle ends.
# "Current" is not look-ahead: on day t it uses only the days up to t (the candle's open so far, its high
# and low so far, today's close, volume so far). The definition, and what test_backtest_engine.py checks
# every kind against, is: cut the daily data at t, build the weekly or monthly candles from it with the
# site's own functions (the newest candle is then the one in progress), work the indicator out, and take
# its last value. That is what a chart shows at that day's close.
#
# Doing that literally for every day of ten years is thousands of indicator runs per stock, so for the
# indicators people actually use on a higher timeframe the last value is computed in ONE STEP from the
# state at the last finished candle (an EMA is alpha * today + (1 - alpha) * yesterday's EMA, and so on).
# Each one-step version is verified against the literal definition above. ADX and +/-DI carry a smoothing
# state that pandas treats specially around a candle with no movement at all; where a FINISHED candle
# has one (never seen on a real week or month) the one-step update would not be exact, so that stock's
# operand is recomputed literally instead: correct, and slower, which the time budget covers.
_ONE_STEP = frozenset(("close", "open", "high", "low", "volume", "sma", "ema", "rsi", "macd", "macd_signal",
                       "macd_hist", "roc", "bb_upper", "bb_middle", "bb_lower", "atr", "highest", "lowest",
                       "supertrend", "supertrend_dir", "adx", "plus_di", "minus_di"))


def _periods(df, tf, cache):
    """(all weekly/monthly candles INCLUDING the newest unfinished one, the period number of each daily day).

    Day t belongs to the first candle whose label (its last trading day) is on or after t."""
    key = ("periods", tf)
    if key not in cache:
        full = _candles(df, tf)
        kk = np.searchsorted(full.index.values, df.index.values, side="left")
        cache[key] = (full, kk)
    return cache[key]


def _partial(df, kk, cache):
    """Open / high / low / close / volume of the candle in progress, as of the close of each daily day."""
    key = ("partial", id(kk))
    if key not in cache:
        cache[key] = {
            "open": df["open"].groupby(kk).transform("first").to_numpy(float),
            "high": df["high"].groupby(kk).cummax().to_numpy(float),
            "low": df["low"].groupby(kk).cummin().to_numpy(float),
            "close": df["close"].to_numpy(float),
            "volume": df["volume"].groupby(kk).cumsum().to_numpy(float),
        }
    return cache[key]


def _current_values(df, kind, p, tf, cache):
    """The value of the operand on the candle in progress, for every daily day (numpy array)."""
    full, kk = _periods(df, tf, cache)
    part = _partial(df, kk, cache)
    n = len(df)
    nan = np.full(n, np.nan)
    kp = kk - 1                                        # the last FINISHED candle as of each day

    def at(arr, idx):                                  # arr[idx] where idx >= 0, else NaN
        arr = np.asarray(arr, float)
        return np.where(idx >= 0, arr[np.clip(idx, 0, max(len(arr) - 1, 0))], np.nan) if len(arr) else nan

    C = full["close"].to_numpy(float)
    x_c, x_h, x_l = part["close"], part["high"], part["low"]

    if kind in ("close", "open", "high", "low", "volume"):
        return part[kind].copy()

    if kind in ("sma", "ema", "bb_upper", "bb_middle", "bb_lower"):
        src = p.get("src", "close")
        S = full[src].to_numpy(float)
        x = part[src]
        P = p["period"]
        R = pd.Series(S).rolling(P - 1).sum().to_numpy() if P > 1 else None
        if kind == "sma":
            return x.copy() if P == 1 else (at(R, kp) + x) / P
        if kind == "ema":
            if P == 1:
                return x.copy()
            E = ind.ema(full, periods=(P,), price_col=src)["ema_%d" % P].to_numpy(float)
            alpha = 2.0 / (P + 1.0)
            step = alpha * x + (1 - alpha) * at(E, kp)
            seed = (at(R, kp) + x) / P                 # the candle that completes the first P: SMA-seeded
            return np.where(kk >= P, step, np.where(kk == P - 1, seed, np.nan))
        # Bollinger, on the close, population standard deviation
        if P == 1:
            mean, sd = x_c.copy(), np.zeros(n)
        else:
            # Add the candle in progress to the P-1 finished ones with Welford's update. (The shortcut
            # E[x^2] - mean^2 cancels badly and gives a non-zero spread for a run of identical closes,
            # which flips "close at or above the upper band" on a flat stock.)
            sC = pd.Series(C)
            m1 = at(sC.rolling(P - 1).mean().to_numpy(), kp)
            v1 = at(sC.rolling(P - 1).var(ddof=0).to_numpy(), kp)
            mean = (m1 * (P - 1) + x_c) / P
            sd = np.sqrt(np.maximum(((P - 1) * v1 + (x_c - m1) * (x_c - mean)) / P, 0.0))
        if kind == "bb_middle":
            return mean
        return mean + p["dev"] * sd if kind == "bb_upper" else mean - p["dev"] * sd

    if kind == "rsi":
        P = p["period"]
        alpha = 1.0 / P
        delta = np.diff(C, prepend=np.nan)
        AG = pd.Series(np.clip(delta, 0, None)).ewm(alpha=alpha, adjust=False).mean().to_numpy()
        AL = pd.Series(np.clip(-delta, 0, None)).ewm(alpha=alpha, adjust=False).mean().to_numpy()
        d = x_c - at(C, kp)
        g, l = np.clip(d, 0, None), np.clip(-d, 0, None)
        ag = np.where(kk >= 2, (1 - alpha) * at(AG, kp) + alpha * g, np.where(kk == 1, g, np.nan))
        al = np.where(kk >= 2, (1 - alpha) * at(AL, kp) + alpha * l, np.where(kk == 1, l, np.nan))
        with np.errstate(divide="ignore", invalid="ignore"):
            rs = ag / np.where(al == 0, np.nan, al)
            rsi = 100.0 - 100.0 / (1.0 + rs)
        rsi = np.where((al == 0) & (ag > 0), 100.0, rsi)
        rsi = np.where((al == 0) & (ag == 0), 50.0, rsi)
        return np.where(kk >= 2 * P, rsi, np.nan)

    if kind in ("macd", "macd_signal", "macd_hist"):
        fast, slow, sig = p["fast"], p["slow"], p["signal"]
        sC = pd.Series(C)
        EF = sC.ewm(span=fast, adjust=False).mean().to_numpy()
        ES = sC.ewm(span=slow, adjust=False).mean().to_numpy()
        SG = pd.Series(EF - ES).ewm(span=sig, adjust=False).mean().to_numpy()
        af, as_, ag_ = 2.0 / (fast + 1), 2.0 / (slow + 1), 2.0 / (sig + 1)
        ef = (1 - af) * at(EF, kp) + af * x_c
        es = (1 - as_) * at(ES, kp) + as_ * x_c
        m = ef - es
        sg = (1 - ag_) * at(SG, kp) + ag_ * m
        out = {"macd": m, "macd_signal": sg, "macd_hist": m - sg}[kind]
        return np.where(kk >= slow + sig, out, np.nan)

    if kind == "roc":
        return (x_c / at(C, kk - p["period"]) - 1.0) * 100.0

    if kind in ("highest", "lowest"):
        col, fn = ("high", "max") if kind == "highest" else ("low", "min")
        R = getattr(full[col].rolling(p["period"]), fn)().to_numpy(float)
        return at(R, kp)                                # the previous N FINISHED candles, as for a finished one

    if kind in ("adx", "plus_di", "minus_di"):
        P = p["period"]
        alpha = 1.0 / P
        H, L = full["high"].to_numpy(float), full["low"].to_numpy(float)
        a = ind.adx(full, P)
        pdi_c, mdi_c, ADXc = (a[k].to_numpy(float) for k in ("plus_di", "minus_di", "adx"))
        with np.errstate(divide="ignore", invalid="ignore"):
            s_c = pdi_c + mdi_c
            dx_c = 100.0 * np.abs(pdi_c - mdi_c) / np.where(s_c == 0, np.nan, s_c)
        ok = np.flatnonzero(~np.isnan(dx_c))
        if len(ok) and np.isnan(dx_c[ok[0]:]).any():
            # A finished candle with no directional movement at all leaves a gap in the series that
            # pandas' smoothing treats with weights a one-step update does not reproduce. Never seen
            # on a real week or month, but not guessed at either: the caller recomputes literally.
            return None
        upc, dnc = np.diff(H, prepend=np.nan), -np.diff(L, prepend=np.nan)
        PS = pd.Series(np.where((upc > dnc) & (upc > 0), upc, 0.0)).ewm(alpha=alpha, adjust=False).mean().to_numpy()
        MS = pd.Series(np.where((dnc > upc) & (dnc > 0), dnc, 0.0)).ewm(alpha=alpha, adjust=False).mean().to_numpy()
        A = ind._wilder_atr(full, P).to_numpy(float)
        prev_c = at(C, kp)
        tr = np.maximum.reduce([x_h - x_l, np.abs(x_h - prev_c), np.abs(x_l - prev_c)])
        tr = np.where(np.isnan(prev_c), x_h - x_l, tr)
        atr = (1 - alpha) * at(A, kp) + alpha * tr
        up, dn = x_h - at(H, kp), at(L, kp) - x_l
        ps = (1 - alpha) * at(PS, kp) + alpha * np.where((up > dn) & (up > 0), up, 0.0)
        ms = (1 - alpha) * at(MS, kp) + alpha * np.where((dn > up) & (dn > 0), dn, 0.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            atr_s = np.where(atr == 0, np.nan, atr)
            pdi, mdi = 100.0 * ps / atr_s, 100.0 * ms / atr_s
            s = pdi + mdi
            dx = 100.0 * np.abs(pdi - mdi) / np.where(s == 0, np.nan, s)
        prev_adx = at(ADXc, kp)
        adx = np.where(np.isnan(dx), prev_adx, np.where(np.isnan(prev_adx), dx, (1 - alpha) * prev_adx + alpha * dx))
        out, need = {"adx": (adx, 3 * P), "plus_di": (pdi, 2 * P), "minus_di": (mdi, 2 * P)}[kind]
        return np.where(kk >= need, out, np.nan)

    if kind in ("atr", "supertrend", "supertrend_dir"):
        P = p["period"]
        A = ind._wilder_atr(full, P).to_numpy(float)
        prev_c = at(C, kp)
        tr = np.maximum.reduce([x_h - x_l, np.abs(x_h - prev_c), np.abs(x_l - prev_c)])
        tr = np.where(np.isnan(prev_c), x_h - x_l, tr)
        atr = (1 - 1.0 / P) * at(A, kp) + (1.0 / P) * tr
        if kind == "atr":
            return np.where(kk >= P, atr, np.nan)
        st = ind.supertrend(full, P, p["factor"])
        FU, FL = st["st_upper"].to_numpy(float), st["st_lower"].to_numpy(float)
        T = np.where(st["st_bullish"].to_numpy(bool), 1.0, -1.0)
        fu_p, fl_p, t_p = at(FU, kp), at(FL, kp), at(T, kp)
        hl2 = (x_h + x_l) / 2.0
        bu, bl = hl2 + p["factor"] * atr, hl2 - p["factor"] * atr
        fu = np.where((bu < fu_p) | (prev_c > fu_p), bu, fu_p)
        fl = np.where((bl > fl_p) | (prev_c < fl_p), bl, fl_p)
        trend = np.where((t_p == 1) & (x_c < fl_p), -1.0, np.where((t_p == -1) & (x_c > fu_p), 1.0, t_p))
        out = np.where(trend == 1, fl, fu) if kind == "supertrend" else trend
        return np.where(kk >= 2 * P, out, np.nan)

    raise SpecError("Unknown indicator %r." % kind)


LITERAL_SECONDS = 6.0          # the most one operand may spend on the slow, literal reading


def _current_literal(df, kind, p, tf, cache):
    """The same thing, done the slow, obvious way: for each day, the finished candles plus the candle in
    progress, and the indicator's last value. Used where there is no safe one-step version. It is the
    only code in the engine whose cost a rule can inflate (about 15 s per operand on ten years of days),
    and the time budget is only looked at between stocks, so it carries its own limit."""
    full, kk = _periods(df, tf, cache)
    part = _partial(df, kk, cache)
    cols = ("open", "high", "low", "close", "volume")
    base = full[list(cols)].to_numpy(float)
    out = np.full(len(df), np.nan)
    started = time.monotonic()
    for t in range(len(df)):
        if t % 100 == 0 and time.monotonic() - started > LITERAL_SECONDS:
            raise SpecError("Reading this indicator on the candle in progress would take too long. Use the last "
                            "finished candle for it, or a different indicator.")
        k = int(kk[t])
        arr = np.empty((k + 1, 5))
        arr[:k] = base[:k]
        arr[k] = [part[c][t] for c in cols]
        v = _compute(pd.DataFrame(arr, columns=list(cols)), kind, p)
        out[t] = v.iloc[-1] if len(v) else np.nan
    return out


def _current_series(df, o, params, cache):
    """A weekly/monthly operand read on the candle IN PROGRESS, as a daily Series.
    `ago` counts back from the candle in progress: ago 0 is the candle in progress itself, ago 1 the
    last FINISHED one, ago 2 the one before that."""
    tf, kind, ago = o["tf"], o["kind"], o.get("ago", 0)
    full, kk = _periods(df, tf, cache)
    if ago == 0:
        key = ("cur", kind, tf, tuple(sorted(params.items())))
        if key not in cache:
            v = _current_values(df, kind, params, tf, cache) if kind in _ONE_STEP else None
            cache[key] = v if v is not None else _current_literal(df, kind, params, tf, cache)
        v = cache[key]
    else:
        key = ("done", kind, tf, tuple(sorted(params.items())))
        if key not in cache:
            cache[key] = _compute(full, kind, params).to_numpy(float) if len(full) else np.array([])
        arr, idx = cache[key], kk - ago
        v = np.where(idx >= 0, arr[np.clip(idx, 0, max(len(arr) - 1, 0))], np.nan) if len(arr) else np.full(len(df), np.nan)
    return pd.Series(v, index=df.index)


def operand_series(df, o, cache):
    """The operand as a float Series aligned to the DAILY index df.

    Worked out on the operand's own candles (daily, weekly or monthly), shifted by 'candles ago'
    IN THOSE CANDLES (1 month ago on a monthly operand is the previous finished month), scaled
    by its multiplier, and -- for a weekly or monthly one -- carried onto the daily days: on each
    day it shows the value of the latest candle that has FINISHED by that day, unchanged until the
    next one finishes. So a weekly value first appears on that week's last trading day (judged,
    like every rule, at the close), and never earlier. The indicator itself is computed once per
    distinct timeframe and set of parameters.

    With candle="current" a weekly or monthly operand is instead read on the week or month IN
    PROGRESS as it stands at each day's close (see the note above _current_values)."""
    if o["kind"] == "number":
        return pd.Series(float(o["value"]), index=df.index)
    tf = o.get("tf", "D")
    params = {k: v for k, v in o.items() if k not in ("kind", "ago", "times", "tf", "candle")}
    if tf != "D" and o.get("candle") == "current":
        s = _current_series(df, dict(o, tf=tf), params, cache)
        return s * o["times"] if o.get("times", 1) != 1 else s
    key = (o["kind"], tf, tuple(sorted(params.items())))
    if key not in cache:
        frame = df if tf == "D" else _htf_frame(df, tf, cache)
        cache[key] = _compute(frame, o["kind"], params) if len(frame) else pd.Series(dtype=float)
    s = cache[key]
    if o.get("ago"):
        s = s.shift(o["ago"])
    if o.get("times", 1) != 1:
        s = s * o["times"]
    if tf != "D":
        s = s.reindex(df.index, method="ffill") if len(s) else pd.Series(np.nan, index=df.index)
    return s


def tree_candles(node):
    """{"completed"}, {"completed", "current"} ... for the weekly/monthly operands of a rule."""
    if "items" in node:
        out = set()
        for i in node["items"]:
            out |= tree_candles(i)
        return out
    if "signal" in node:                                  # a weekly or monthly scanner is read on finished candles
        return {"completed"} if sg.timeframe_of(node["signal"]) != "D" else set()
    return {o.get("candle", "completed") for o in (node["left"], node["right"])
            if o["kind"] != "number" and o.get("tf", "D") != "D"}


def tree_timeframes(node):
    """The set of candle sizes a rule reads: {"D"}, {"D", "M"} ..."""
    if "items" in node:
        out = set()
        for i in node["items"]:
            out |= tree_timeframes(i)
        return out
    if "signal" in node:
        return {sg.timeframe_of(node["signal"])}
    return {o.get("tf", "D") for o in (node["left"], node["right"]) if o["kind"] != "number"}


def tree_signals(node):
    """The scanner signals a rule uses, in the order they appear."""
    if "items" in node:
        out = []
        for i in node["items"]:
            out += tree_signals(i)
        return out
    return [node["signal"]] if "signal" in node else []


def signal_condition_series(df, node, cache):
    """A scanner signal as a daily boolean Series: True on the day the scanner newly lists the stock (see scanner_signals.py)."""
    sig = node["signal"]
    key = ("signal", sig["screen"], tuple(sorted(sig["params"].items())))
    if key not in cache:
        cache[key] = sg.series(df, sig, lambda tf: _htf_frame(df, tf, cache))
    r = cache[key]
    w = node.get("within", 1)
    if w > 1:
        r = r.astype(float).rolling(w, min_periods=1).max() > 0
    return r


def condition_series(df, node, cache):
    if "signal" in node:
        return signal_condition_series(df, node, cache)
    a = operand_series(df, node["left"], cache)
    b = operand_series(df, node["right"], cache)
    c = node["cmp"]
    if c == "gt":
        r = a > b
    elif c == "gte":
        r = a >= b
    elif c == "lt":
        r = a < b
    elif c == "lte":
        r = a <= b
    elif c == "crosses_above":
        r = (a > b) & (a.shift(1) <= b.shift(1))
    else:
        r = (a < b) & (a.shift(1) >= b.shift(1))
    r = r.fillna(False).astype(bool)          # a comparison with an empty value is false
    w = node.get("within", 1)
    if w > 1:
        r = r.astype(float).rolling(w, min_periods=1).max() > 0
    return r


def tree_series(df, node, cache):
    if "items" in node:
        out = None
        for it in node["items"]:
            s = tree_series(df, it, cache)
            out = s if out is None else ((out & s) if node["op"] == "and" else (out | s))
        return out
    return condition_series(df, node, cache)


# =====================================================================
# THE SIMULATION
# =====================================================================
def simulate(o, h, l, c, entry, exit_, first, side, next_open, sl, tp, tr, mh, st_every=None, st_by=None):
    """Walk the candles once. Returns [(entry_idx, exit_idx, entry_px, exit_px, reason)].

    side +1 long, -1 short. Every comparison is written once, multiplied by
    `side`, so the short side is the same code reflected rather than a second
    copy that can drift from the first.

    The STOPS, all measured against the ENTRY price except the trailing one:
      sl        stop-loss: this % against the trade, from entry
      st_every, st_by   a STEPPED stop, which needs sl to start from. Each time the best price has
                moved another st_every % in the trade's favour (measured from entry), the stop moves up
                by st_by % of the entry price: with sl 5, every 10, by 5 it starts 5% below entry, is
                at the entry price once the trade is up 10%, 5% above it at +20%, and so on. It never
                moves down.
      tr        trailing stop: follows tr % behind the best price (a different thing from the stepped one)
    Where more than one is set, the one closest to the price is the stop in force.

    Order inside one candle, earliest first:
      1. an exit RULE judged on yesterday's close, filled at today's open
      2. a stop (fixed, stepped or trailing) -- gapped through, it fills at the open
      3. a target -- same
      4. the holding limit, at the close
      5. (same_close fills only) an exit rule judged on today's close
    """
    n = len(c)
    out = []
    t = first
    while t < n:
        if not entry[t]:
            t += 1
            continue
        if next_open:
            e = t + 1
            if e >= n:
                break
            px, start = o[e], e
        else:
            e = t
            px, start = c[e], e + 1
            if start >= n:
                break
        if not (px > 0):
            t += 1
            continue

        stop_fixed = px * (1 - side * sl / 100.0) if sl else None
        target = px * (1 + side * tp / 100.0) if tp else None

        def fixed(best_price, _lvl=stop_fixed, _px=px):
            """The stop-loss for a given best price: constant, or raised a step at a time."""
            if not (st_every and sl):
                return _lvl, "Stop-loss"
            gain = side * (best_price / _px - 1.0) * 100.0         # how far the best price is in our favour
            steps = int(gain / st_every + 1e-9) if gain > 0 else 0
            return _px * (1 - side * (sl - steps * st_by) / 100.0), ("Stepped stop" if steps else "Stop-loss")

        best = px                          # best price in our favour since entry
        x = xpx = why = None
        i = start
        while i < n:
            if next_open and i > e and exit_[i - 1]:
                x, xpx, why = i, o[i], "Exit rule"
                break
            if target is not None and side * (o[i] - target) >= 0:
                # Opened at or beyond the target: a resting order fills at the open,
                # before the day can go on to touch the stop.
                x, xpx, why = i, o[i], "Target"
                break
            # (a) The stop in force when the day OPENS. If the price opens through it, that is
            #     a gap and the trade happens at the open, not at a level that never traded.
            s_open, why_open = fixed(best)
            if tr:
                t_open = best * (1 - side * tr / 100.0)
                if s_open is None or side * (t_open - s_open) > 0:
                    s_open, why_open = t_open, "Trailing stop"
            if s_open is not None and side * (s_open - o[i]) >= 0:
                x, xpx, why = i, o[i], why_open
                break
            # (b) During the day. The day's own best price counts BEFORE its worst is
            #     tested: a candle that makes a new high and then drops back through the
            #     trail is stopped, at the trail's level (it did trade there).
            ref = max(best, h[i]) if side == 1 else min(best, l[i])    # the day's own best counts first
            stop, stop_why = fixed(ref)
            if tr:
                trail = ref * (1 - side * tr / 100.0)
                if stop is None or side * (trail - stop) > 0:      # the trail is the tighter one
                    stop, stop_why = trail, "Trailing stop"
            adverse = l[i] if side == 1 else h[i]
            favour = h[i] if side == 1 else l[i]
            if stop is not None and side * (stop - adverse) >= 0:
                x, xpx, why = i, stop, stop_why
                break
            if target is not None and side * (favour - target) >= 0:
                x, xpx, why = i, target, "Target"      # a gap past it was handled above
                break
            best = max(best, h[i]) if side == 1 else min(best, l[i])
            if mh and (i - start + 1) >= mh:
                x, xpx, why = i, c[i], "Time limit"
                break
            if (not next_open) and exit_[i]:
                x, xpx, why = i, c[i], "Exit rule"
                break
            i += 1
        else:
            x, xpx, why = n - 1, c[n - 1], "Still open"
        out.append((e, x, px, xpx, why))
        t = x if next_open else x + 1
    return out


def trade_return(px, xpx, side, cost):
    """Net return of one trade as a fraction. Long: money spent is price plus
    cost, money received is price less cost. Short: measured on the value
    sold, so a fall of 10% earns about 10%. Never below -100%."""
    if side == 1:
        r = (xpx * (1 - cost)) / (px * (1 + cost)) - 1.0
    else:
        r = (px * (1 - cost) - xpx * (1 + cost)) / px
    return max(r, -1.0)


def _equity(c, first, trades, side, cost):
    """Daily equity (starting at 1.0 on the window's first day), marked to the
    close while a position is open, from the trades that are being counted.
    Returns (equity array, bars in market)."""
    n = len(c)
    eq = np.empty(n - first)
    base, pos, in_mkt = 1.0, first, 0
    for (e, x, px, xpx, ret) in trades:
        lo = max(e, first)
        eq[pos - first:lo - first] = base
        for j in range(lo, x):
            if side == 1:
                m = c[j] / (px * (1 + cost))
            else:
                m = 1.0 + (px * (1 - cost) - c[j]) / px
            eq[j - first] = base * max(m, 0.0)
        base *= (1.0 + ret)
        eq[x - first] = base
        pos = x + 1
        in_mkt += x - lo + 1
    eq[pos - first:] = base
    return eq, in_mkt


def _max_drawdown(curve):
    peak = np.maximum.accumulate(curve)
    return float((curve / peak - 1.0).min()) if len(curve) else 0.0


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _dmy(iso):
    """'2022-06-15' -> '15 Jun 2022'. Written out rather than strftime, whose
    month names follow the server's locale."""
    y, m, d = (int(x) for x in str(iso)[:10].split("-"))
    return "%d %s %d" % (d, _MONTHS[m - 1], y)


def _r(x, nd=2):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def run_stock(df, spec, window_start, with_dates=True):
    """One stock. `df`: DatetimeIndex, float columns open/high/low/close/volume,
    already adjusted. Returns plain dicts and lists only."""
    n = len(df)
    if not (df.index.is_monotonic_increasing and df.index.is_unique):
        # The database cannot produce this (ORDER BY, and a primary key on stock and date), but every
        # weekly and monthly calculation silently assumes it, so it is checked rather than assumed.
        raise SpecError("The price history is out of order or has a date twice.")
    cache = {}
    entry = tree_series(df, spec["entry"], cache).to_numpy(dtype=bool)
    exit_ = (tree_series(df, spec["exit"], cache).to_numpy(dtype=bool)
             if spec["exit"] else np.zeros(n, dtype=bool))
    first = int(df.index.searchsorted(window_start))
    if first >= n - 2:
        return {"bars": 0}

    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    side = 1 if spec["direction"] == "long" else -1
    next_open = spec["fill"] == "next_open"
    cost = spec["cost_pct"] / 100.0

    ratio = np.ones(n)
    ratio[1:] = c[1:] / c[:-1]
    jump_flag = (ratio >= JUMP_UP) | (ratio <= JUMP_DOWN)
    jump_flag[:first + 1] = False                     # only moves inside the window matter
    cj = np.cumsum(jump_flag)

    raw = simulate(o, h, l, c, entry, exit_, first, side, next_open,
                   spec["stop_loss_pct"], spec["target_pct"], spec["trailing_pct"], spec["max_hold_days"],
                   spec.get("step_every_pct"), spec.get("step_by_pct"))

    idx = df.index
    trades, counted = [], []
    for (e, x, px, xpx, why) in raw:
        ret = trade_return(px, xpx, side, cost)
        jumped = bool(cj[x] - cj[e] > 0)
        aside = jumped and spec["exclude_jumps"]
        trades.append({
            "entry_date": idx[e].date().isoformat(), "exit_date": idx[x].date().isoformat(),
            "entry_price": _r(px, 2), "exit_price": _r(xpx, 2),
            "days": int((idx[x] - idx[e]).days), "ret_pct": _r(ret * 100.0, 2),
            "reason": why, "set_aside": aside, "jump": jumped,
        })
        if not aside:
            counted.append((e, x, px, xpx, ret))

    # Rupees. All of the stock's money is in each trade and profits stay in the stock, which is how
    # the equity curve works too, so a trade's profit is the balance it started with times its
    # return, and the trades add up to exactly the stock's total profit. A set-aside trade is not
    # counted, so it has no amount.
    # Each amount is the DIFFERENCE between the balance in whole rupees after the trade and before it, so
    # the amounts add up to exactly the final balance less the money put in (rounding each amount on its
    # own would drift by a rupee or more over a long run).
    balance, k, shown = spec["capital"], 0, int(round(spec["capital"]))
    for t in trades:
        if t["set_aside"]:
            t["pnl"] = None
            continue
        balance += balance * counted[k][4]
        k += 1
        t["pnl"] = int(round(balance)) - shown
        shown = int(round(balance))

    eq, in_mkt = _equity(c, first, counted, side, cost)
    bh = c[first:] / c[first]
    window_days = max((idx[-1] - idx[first]).days, 1)
    years = window_days / 365.25

    # What happened after each fresh signal, whatever the exit rules were doing.
    edge = entry.copy()
    edge[1:] = entry[1:] & ~entry[:-1]
    edge[:first] = False
    edge[first] = entry[first]            # a rule already true on day one is traded on day one, so it counts
    fwd = {hh: [] for hh in HORIZONS}
    n_signals = 0
    for t in np.flatnonzero(edge):
        n_signals += 1
        if next_open:
            if t + 1 >= n:
                continue
            base, e0 = o[t + 1], t + 1
        else:
            base, e0 = c[t], t
        if not base > 0:
            continue
        for hh in HORIZONS:
            j = t + hh
            if j >= n:
                continue
            if spec["exclude_jumps"] and cj[j] - cj[e0] > 0:
                continue
            fwd[hh].append(side * (c[j] / base - 1.0) * 100.0)

    jumps = [{"date": idx[j].date().isoformat(), "move_pct": _r((ratio[j] - 1) * 100, 0)}
             for j in np.flatnonzero(jump_flag)][:5]

    yearly = {}
    dates_w = idx[first:]
    for y in sorted(set(dates_w.year)):
        yearly[int(y)] = np.flatnonzero(dates_w.year == y)

    return {
        "bars": int(n - first), "first_date": idx[first].date().isoformat(),
        "last_date": idx[-1].date().isoformat(), "years": years,
        "trades": trades, "counted": counted, "n_signals": n_signals, "fwd": fwd,
        "equity": eq, "bh": bh, "final_balance": balance,
        # per-day date strings are only worth building for the one chart a single stock gets
        "dates": [d.date().isoformat() for d in dates_w] if with_dates else None,
        "in_market_pct": in_mkt / max(len(eq), 1) * 100.0,
        "jumps": jumps, "yearly_idx": yearly, "side": side,
    }


# =====================================================================
# STATISTICS
# =====================================================================
def trade_stats(rets_pct, days):
    """Numbers about a list of trade returns (in per cent)."""
    r = np.asarray(rets_pct, dtype=float)
    n = len(r)
    if n == 0:
        return {"trades": 0}
    wins, losses = r[r > 0], r[r < 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    return {
        "trades": n, "winners": int(len(wins)), "losers": int(len(losses)),
        "win_rate": _r(len(wins) / n * 100.0, 1),
        "avg_trade": _r(r.mean()), "median_trade": _r(np.median(r)),
        "avg_win": _r(wins.mean()) if len(wins) else None,
        "avg_loss": _r(losses.mean()) if len(losses) else None,
        "payoff": _r(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
        "profit_factor": _r(gross_win / gross_loss) if gross_loss > 0 else None,
        "best": _r(r.max()), "worst": _r(r.min()),
        "avg_days": _r(np.mean(days), 1),
    }


def _nice_step(x):
    if not x > 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if x <= m * mag:
            return m * mag
    return 10 * mag


MAX_BINS = 40


def histogram(rets_pct):
    """Counts of trades by return. The middle 96% sets the range and the two outer
    bars also hold everything beyond them, so one freak trade cannot squash the rest.

    The width is derived from that range, so a range of (nearly) nothing -- every
    trade leaving at the same target, say -- must not be allowed to make the bars
    (nearly) nothing wide. It once did, and a plain target-only rule produced tens
    of millions of bars. The range is therefore never narrower than one percentage
    point, and the number of bars never exceeds MAX_BINS whatever comes in.
    """
    r = np.asarray(rets_pct, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return None
    lo_p, hi_p = (float(v) for v in np.percentile(r, [2, 98]))
    if hi_p - lo_p < 1.0:
        mid = (lo_p + hi_p) / 2.0
        lo_p, hi_p = mid - 0.5, mid + 0.5
    step = _nice_step((hi_p - lo_p) / 18.0)
    lo = math.floor(lo_p / step) * step
    hi = math.ceil(hi_p / step) * step
    while (hi - lo) / step > MAX_BINS:                  # belt and braces: never an unbounded loop of bars
        step = _nice_step(step * 1.5)
        lo = math.floor(lo_p / step) * step
        hi = math.ceil(hi_p / step) * step
    nb = max(int(round((hi - lo) / step)), 1)
    edges = lo + step * np.arange(nb + 1)
    counts, _ = np.histogram(np.clip(r, edges[0], edges[-1] - step * 1e-6), bins=edges)
    return {"edges": [_r(e, 4) for e in edges], "counts": [int(k) for k in counts],
            "step": _r(step, 4)}


def _fwd_table(pooled):
    rows = []
    for hh in HORIZONS:
        v = np.asarray(pooled[hh], dtype=float)
        if len(v) == 0:
            continue
        rows.append({"days": hh, "n": int(len(v)), "pct_up": _r((v > 0).mean() * 100.0, 1),
                     "avg": _r(v.mean()), "median": _r(np.median(v)),
                     "best": _r(v.max()), "worst": _r(v.min())})
    return rows


def run_backtest(frames, spec, names, window_start, time_budget=None, keep_curve=None):
    """Run every stock and pool the answers.

    frames: {isin: DataFrame}, or any iterable of (isin, DataFrame) pairs. A group of
    250 stocks is fed in as a stream, one stock at a time, so only one stock's candles
    are ever held at once. `names` is read as each stock arrives, so a producer may
    add to a stock's notes before yielding it.
    names: {isin: {"symbol","name","notes":[...]}}.
    keep_curve: build the day-by-day equity curve and buy-and-hold line. That is only
    for a test of ONE stock (several have no honest single curve), so it defaults to
    True for a dict of one and False otherwise; a stream must say.
    time_budget: seconds of calculation allowed. A test of 25 stocks with dozens of
    Supertrend variants can take far longer than anyone is waiting, and holds one of
    the few places the server has for back-tests while it does; past the budget it
    is stopped with a plain message instead.
    """
    per_stock, all_trades, notes = [], [], []
    pooled_fwd = {hh: [] for hh in HORIZONS}
    n_signals = 0
    tested = []                       # (run_stock result, its row), kept only when a curve is wanted
    started = time.monotonic()
    if keep_curve is None:
        keep_curve = hasattr(frames, "__len__") and len(frames) == 1
    pairs = frames.items() if hasattr(frames, "items") else frames

    for isin, df in pairs:
        if time_budget and time.monotonic() - started > time_budget:
            raise SpecError("This test is too heavy to finish in reasonable time. Try fewer stocks, "
                            "fewer conditions, or fewer different indicators.")
        meta = names.get(isin, {})
        row = {"isin": isin, "symbol": meta.get("symbol"), "name": meta.get("name"),
               "notes": list(meta.get("notes", []))}
        if len(df) < MIN_BARS:
            # A stock that was not tested gets its reason and nothing else: a note
            # about how it was tested would contradict it.
            row.update(skipped="Not enough price history to test (%d days)." % len(df), notes=[])
            per_stock.append(row)
            continue
        r = run_stock(df, spec, window_start, with_dates=keep_curve)
        if not r["bars"]:
            row.update(skipped="No price history inside the chosen period.", notes=[])
            per_stock.append(row)
            continue

        counted_rets = [t["ret_pct"] for t in r["trades"] if not t["set_aside"]]
        days = [t["days"] for t in r["trades"] if not t["set_aside"]]
        eq = r["equity"]
        capital = spec["capital"]
        total = float(eq[-1] - 1.0) * 100.0
        bh_total = float(r["bh"][-1] - 1.0) * 100.0
        row.update({
            "data_from": r["first_date"], "data_to": r["last_date"],
            "trades": len(counted_rets), "set_aside": sum(1 for t in r["trades"] if t["set_aside"]),
            "win_rate": _r(np.mean(np.asarray(counted_rets) > 0) * 100.0, 1) if counted_rets else None,
            "avg_trade": _r(np.mean(counted_rets)) if counted_rets else None,
            "total_return": _r(total, 1), "bh_return": _r(bh_total, 1),
            # the same results in rupees, for `capital` put into this stock (whole rupees, from the same
            # ledger the trades' amounts come from, so they add up exactly)
            "final_value": int(round(r["final_balance"])),
            "profit": int(round(r["final_balance"])) - int(round(capital)),
            "bh_final": int(round(capital * float(r["bh"][-1]))),
            "bh_profit": int(round(capital * float(r["bh"][-1]))) - int(round(capital)),
            "max_drawdown": _r(_max_drawdown(eq) * 100.0, 1),
            "in_market": _r(r["in_market_pct"], 0),
            "signals": r["n_signals"],
        })
        if r["jumps"]:
            row["notes"].append(
                "A one-day price move that no split or bonus explains (a fall of 30%% or more, or a rise "
                "of 60%% or more): %s. %s The holding figure still includes it." % (
                    ", ".join("%s (%+g%%)" % (_dmy(j["date"]), j["move_pct"]) for j in r["jumps"][:3]),
                    "Trades that span one are set aside." if spec["exclude_jumps"]
                    else "Trades that span one are counted, because that option is switched off."))
        per_stock.append(row)
        n_signals += r["n_signals"]
        for hh in HORIZONS:
            pooled_fwd[hh].extend(r["fwd"][hh])
        for t in r["trades"]:
            all_trades.append(dict(t, isin=isin, symbol=meta.get("symbol") or isin))
        if keep_curve:
            tested.append((r, row))

    ran = [p for p in per_stock if "skipped" not in p]
    counted = [t for t in all_trades if not t["set_aside"]]
    stats = trade_stats([t["ret_pct"] for t in counted], [t["days"] for t in counted])
    stats["set_aside"] = sum(1 for t in all_trades if t["set_aside"])
    stats["open_at_end"] = sum(1 for t in counted if t["reason"] == "Still open")
    stats["signals"] = n_signals
    stats["capital"] = spec["capital"]
    pnls = [t["pnl"] for t in counted if t.get("pnl") is not None]
    stats["avg_pnl"] = int(round(float(np.mean(pnls)))) if pnls else None
    stats["best_pnl"] = int(max(pnls)) if pnls else None
    stats["worst_pnl"] = int(min(pnls)) if pnls else None

    out = {"stocks": per_stock, "summary": stats, "notes": notes,
           "forward": _fwd_table(pooled_fwd),
           "histogram": histogram([t["ret_pct"] for t in counted]),
           "exits": {k: sum(1 for t in counted if t["reason"] == k) for k in REASONS
                     if any(t["reason"] == k for t in counted)}}

    # Calendar-year table. Strategy and buy-and-hold year returns exist only
    # for a single stock (there is no honest way to add up equity curves
    # without inventing a position-sizing rule).
    by_year, pnl_year = {}, {}
    for t in counted:
        y = int(t["exit_date"][:4])
        by_year.setdefault(y, []).append(t["ret_pct"])
        pnl_year[y] = pnl_year.get(y, 0.0) + (t.get("pnl") or 0.0)
    year_rows = {y: {"year": y, "trades": len(v), "win_rate": _r(np.mean(np.asarray(v) > 0) * 100.0, 1),
                     "avg_trade": _r(np.mean(v)), "pnl": int(pnl_year[y])} for y, v in sorted(by_year.items())}

    if keep_curve and len(tested) == 1:
        r = tested[0][0]
        eq, bh, dates = r["equity"], r["bh"], r["dates"]
        out["equity"] = {"dates": dates, "strategy": [_r(v, 4) for v in eq], "buy_hold": [_r(v, 4) for v in bh]}
        prev_s = prev_b = 1.0
        for y, ix in r["yearly_idx"].items():
            last = ix[-1]
            s_end, b_end = float(eq[last]), float(bh[last])
            yr = year_rows.setdefault(y, {"year": y, "trades": 0, "win_rate": None, "avg_trade": None, "pnl": 0})
            # A short that has lost everything leaves a year-end balance of exactly 0, and the next year's
            # return from nothing is not a number; it is left blank rather than divided by zero.
            yr["strategy_pct"] = _r((s_end / prev_s - 1) * 100.0, 1) if prev_s > 0 else None
            yr["buy_hold_pct"] = _r((b_end / prev_b - 1) * 100.0, 1) if prev_b > 0 else None
            prev_s, prev_b = s_end, b_end
        yrs = max(r["years"], 1e-9)
        total, bh_total = float(eq[-1] - 1), float(bh[-1] - 1)
        stats.update({
            "total_return": _r(total * 100.0, 1), "bh_return": _r(bh_total * 100.0, 1),
            "cagr": _r(((1 + total) ** (1 / yrs) - 1) * 100.0, 1) if yrs >= 1 and 1 + total > 0 else None,
            "bh_cagr": _r(((1 + bh_total) ** (1 / yrs) - 1) * 100.0, 1) if yrs >= 1 and 1 + bh_total > 0 else None,
            "max_drawdown": _r(_max_drawdown(eq) * 100.0, 1),
            "bh_max_drawdown": _r(_max_drawdown(bh) * 100.0, 1),
            "in_market": _r(r["in_market_pct"], 0), "years": _r(r["years"], 1),
            "final_value": tested[0][1]["final_value"], "profit": tested[0][1]["profit"],
            "bh_final_value": tested[0][1]["bh_final"], "bh_profit": tested[0][1]["bh_profit"],
        })
    elif ran:
        tots = [p["total_return"] for p in ran if p.get("total_return") is not None]
        bhs = [(p["total_return"], p["bh_return"]) for p in ran
               if p.get("total_return") is not None and p.get("bh_return") is not None]
        # Rupees across the group: the same amount put into EACH stock, each stock tested alone with
        # all of it. Adding the stocks' results up is honest because every stock gets the same sum;
        # nothing here decides how a single pot of money would have been split between them.
        put_in = int(round(spec["capital"])) * len(ran)
        final = sum(p["final_value"] for p in ran if p.get("final_value") is not None)
        bh_final = sum(p["bh_final"] for p in ran if p.get("bh_final") is not None)
        stats.update({
            "put_in": put_in, "final_value": final, "profit": final - put_in,
            "profit_pct": _r((final / put_in - 1.0) * 100.0, 1) if put_in else None,
            "bh_final_value": bh_final, "bh_profit": bh_final - put_in,
            "bh_profit_pct": _r((bh_final / put_in - 1.0) * 100.0, 1) if put_in else None,
            "stocks_tested": len(ran),
            "stocks_positive": sum(1 for v in tots if v > 0),
            "stocks_beat_bh": sum(1 for a, b in bhs if a > b),
            "median_stock_return": _r(np.median(tots), 1) if tots else None,
            "median_bh_return": _r(np.median([b for _, b in bhs]), 1) if bhs else None,
        })
    out["by_year"] = sorted(year_rows.values(), key=lambda r: r["year"])

    all_trades.sort(key=lambda t: (t["exit_date"], t["symbol"]))
    out["trades_total"] = len(all_trades)
    out["trades"] = all_trades[-MAX_TRADES_RETURNED:]
    out["trades_truncated"] = len(all_trades) > MAX_TRADES_RETURNED
    return out


def _inr(v):
    """₹1,00,000 -- Indian digit grouping, written out (Python's format() only knows thousands)."""
    s = "%d" % round(v)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return "₹" + s


def assumptions(spec):
    """The rules of the test, in the reader's words."""
    fill = ("A rule is judged on a day's close, and the trade is made at the NEXT day's open."
            if spec["fill"] == "next_open" else
            "A rule is judged on a day's close and the trade is made at that same close. "
            "This is the optimistic way to test: a real order could only be placed after the close.")
    out = [fill,
           "One position at a time in each stock, with all of the money in it (%s for each stock tested). "
           "Profits and losses stay in that stock, so each trade is sized on what that stock's money has "
           "grown or shrunk to. When a trade ends, the next signal can start another." % _inr(spec["capital"]),
           "A cost of %g%% is taken off on each side of every trade. It is an assumption, not what any "
           "broker charges." % spec["cost_pct"],
           "If a stop or target is passed through overnight, the trade happens at the open, not at the "
           "level that never traded. If one day touches both a stop and a target, the stop is assumed to "
           "have come first.",
           "Daily candles cannot say what happened inside a day, so the unkind order is assumed. A trailing "
           "stop is measured from the best price reached, counting that same day, and if the day also fell "
           "back through it the trade is stopped.",
           "Price returns only: dividends, tax and impact cost are not included. Indicators are worked "
           "out on adjusted daily candles and left blank until they have enough history."]
    if spec["direction"] == "short":
        out.append("Short trades profit when the price falls. A return is measured on the value sold. In the "
                   "Indian cash market a share can only be sold short within the day (or through futures), "
                   "so a short held for days is a test of the idea, not of something you could do in shares.")
    if spec["max_hold_days"]:
        out.append("Holding limit: the trade ends at the close of the %d-th trading day it is held."
                   % spec["max_hold_days"])
    if spec.get("step_every_pct"):
        out.append("Stepped stop: the stop starts %g%% against the trade, measured from the entry price, and each time the "
                   "best price has moved another %g%% in the trade's favour (from entry) it moves up by %g%% of the entry "
                   "price. It never moves down. The rise is measured on the best price reached, counting the same day, "
                   "and if that day also fell back through the new stop level the trade is stopped, the same unkind "
                   "order as for the trailing stop."
                   % (spec["stop_loss_pct"], spec["step_every_pct"], spec["step_by_pct"]))
    sigs = tree_signals(spec["entry"]) + (tree_signals(spec["exit"]) if spec["exit"] else [])
    if sigs:
        out.append("A SCANNER SIGNAL is the day the Screener's scanner newly lists the stock, worked out from the prices up to that day's close "
                   "(the same reading the alert on the scanner uses), so it is judged like any other rule and acted on at the next day's open. "
                   "It is read on all the history held, where the live scanner reads the last 13 months of daily bars, so a pattern that began "
                   "earlier can count; prices are adjusted for splits and bonuses, where the live scanner sets such a stock aside; and 'alert made "
                   "within the last N bars' does not apply (a signal is the first day).")
    tfs = tree_timeframes(spec["entry"]) | (tree_timeframes(spec["exit"]) if spec["exit"] else set())
    if tfs - {"D"}:
        kinds = tree_candles(spec["entry"]) | (tree_candles(spec["exit"]) if spec["exit"] else set())
        out.append("Weekly and monthly conditions are worked out on weekly and monthly candles, and a rule can mix "
                   "them with daily ones. Trades are still made on daily candles: a signal on a weekly or monthly "
                   "reading is acted on at the next day's open, and stops, targets and holding limits work on "
                   "daily candles.")
        if "completed" in kinds:
            out.append("Where a condition uses the LAST FINISHED candle, a week or month only counts once it has "
                       "finished: its value appears on that week's or month's last trading day and stays the same "
                       "until the next one finishes, so such a condition never sees a candle that is still forming.")
        if "current" in kinds:
            out.append("Where a condition uses the candle IN PROGRESS, it reads the week or month as it stands at "
                       "each day's close (its open so far, its high and low so far, that day's close), so the value "
                       "can change from day to day and flip back before the candle ends. It uses nothing from "
                       "later days. Going back from it, 1 candle is the last finished one.")
        out.append("Weekly and monthly indicators need a long history before they show a value (a monthly MACD, "
                   "about three years), so a rule that uses them can only trade in the later part of the period.")
    return out
