"""
stockalerts_engine.py -- what a stock alert IS, how it is checked, and how it is put into words. Pure: no database,
no web framework, no mail. stockalerts_api.py (setting and listing alerts) and stockalerts_checker.py (the scheduled
check and the email) both call it, so the page, the check and the tests cannot disagree.
---------------------------------------------------------------------------
THE SIX KINDS (one email each, once, when it first happens; the alert is then finished)

  price         a CLOSE above or below a number                         "Infosys closes above 1,500"
                ... or above/below a REFERENCE that is worked out once, when the alert is set, and shown as a number:
                  the highest high / lowest low of the last N sessions, or the high / low of one chosen date
                  "closes above the highest high of the last 5 sessions (1,612.40)"
  ema_support   price comes DOWN to a moving average (EMA) and it holds: on the daily, weekly or monthly chart
                  near   the close comes within X% above the EMA after being further above it   (an early heads-up)
                  holds  a candle's low reaches within X% of the EMA but the close stays at or above it (support held)
  trend_change  a Dow-theory change of trend (up, or the mirror down) on the daily/weekly/monthly chart, measured by
                the same code as the "Change in Dow Theory" screens (dow_change.py)
  indicator     any one condition of the trade-plan catalogue ("weekly RSI crossed above 50", "daily MACD crossed
                above its signal line", "price crossed above the 200 EMA" ...), tradeplan_engine.py reads it
  results       the next quarterly RESULTS are filed (optionally only if net profit / sales moved a stated way on a
                year ago), or the company ANNOUNCES the date of its results meeting

WHAT IS AND IS NOT HELD (said on the page, not hidden)
    Daily, weekly and monthly candles built from the end-of-day prices we hold. NO INTRADAY prices exist here, so every
    price alert is on a CLOSE -- a stock that touches 1,500 during the day and closes at 1,480 does not fire an alert
    on 1,500. Weekly and monthly alerts are judged on a FINISHED week or month only.

THE RULES THAT KEEP IT HONEST (the same ones the trade-plan alerts follow)
    * An alert fires only on a candle that came AFTER the alert was set (spec["baseline"] is the last candle that
      existed). A level that is already crossed when you set it is REFUSED, in words ("it closed at 1,543.20 on 9 Oct,
      already above 1,500"), because it would either fire at once or never.
    * References are numbers fixed at creation. "Above the high of the last 5 days" means the highest high of the 5
      sessions up to now, written into the alert as a rupee figure, so the person sees exactly what is being watched.
    * Nothing is guessed. Too little history, a missing candle, or a data problem is "no data", never "met".
    * A chart pattern or an indicator describes what the price has done; none of these emails is advice.
"""

import math
import re
from datetime import date

import pandas as pd

import backtest_engine as be
import tradeplan_engine as te

MAX_WALK = 40                   # newest candles looked at for "did it happen since the alert was set"
MAX_BARS_FOR_TREND = 1500       # the trend scan reads at most this many of the newest bars
TIMEFRAMES = (("D", "Daily"), ("W", "Weekly"), ("M", "Monthly"))
TF_LABEL = dict(TIMEFRAMES)
KINDS = ("price", "ema_support", "trend_change", "indicator", "results")
MAX_REF_DAYS = 250
MAX_LEVEL = 10_000_000

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class AlertError(ValueError):
    """The alert cannot be made as asked. The message is for the person, in plain words."""


# =====================================================================
# SMALL HELPERS
# =====================================================================
def dmy(d):
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    if hasattr(d, "date") and callable(d.date):
        d = d.date()
    return "%d %s %d" % (d.day, _MONTHS[d.month - 1], d.year)


def money(v):
    return "₹{:,.2f}".format(float(v))


def crore(v):
    """Rupees to a crore figure with thousands separators ('48,211 crore')."""
    c = float(v) / 1e7
    return "₹" + ("{:,.0f}".format(c) if abs(c) >= 100 else "{:,.2f}".format(c)) + " crore"


def _iso(d):
    if hasattr(d, "date") and callable(d.date):
        d = d.date()
    return d.isoformat()


def _num(v, where, lo, hi, what, integer=False):
    if isinstance(v, bool) or v is None:
        raise AlertError("%s: %s needs a number." % (where, what))
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        raise AlertError("%s: %s needs a number." % (where, what))
    if not math.isfinite(f):
        raise AlertError("%s: %s needs a number." % (where, what))
    if integer:
        if not f.is_integer():
            raise AlertError("%s: %s must be a whole number." % (where, what))
        f = int(f)
    if f < lo or f > hi:
        raise AlertError("%s: %s must be between %s and %s." % (where, what, ("{:,g}".format(lo)), ("{:,g}".format(hi))))
    return f


def _choice(v, allowed, where):
    if not isinstance(v, str) or v not in allowed:
        raise AlertError(where)
    return v


def _frame(frames, tf):
    """The candles of one time frame, or AlertError saying why there are none."""
    f = (frames or {}).get(tf)
    if f is None:
        raise AlertError("We have no %s candles for this stock." % TF_LABEL[tf].lower())
    if isinstance(f, str):
        raise AlertError(f.rstrip(".") + ".")
    if len(f) == 0:
        raise AlertError("We have no %s candles for this stock." % TF_LABEL[tf].lower())
    return f


def _last_date(df):
    return df.index[-1].date()


def _eligible(df, tf, baseline, today):
    """Positions (ints) of the CLOSED candles whose label is after `baseline`, oldest first. A weekly or monthly
    candle that is still being drawn is never eligible; the daily candles we hold are end-of-day."""
    n = len(df)
    out = []
    for i in range(max(0, n - MAX_WALK), n):
        label = df.index[i]
        if label.date() <= baseline:
            continue
        if i == n - 1 and te.candle_forming(tf, label, today):
            continue
        out.append(i)
    return out


def _baseline(spec):
    return date.fromisoformat(spec["baseline"])


# =====================================================================
# THE CATALOGUE (what the page draws)
# =====================================================================
def catalog():
    """Everything the page needs to draw the form, so it never hard-codes what the validator would refuse."""
    base = te.catalog()
    return {
        "kinds": [
            {"key": "price", "label": "Price", "blurb": "The close goes above or below a number, or above a recent high."},
            {"key": "ema_support", "label": "Price at a moving average (support)",
             "blurb": "The price comes down to an EMA and holds."},
            {"key": "trend_change", "label": "Change of trend", "blurb": "A Dow-theory change of trend, on any chart."},
            {"key": "indicator", "label": "Technical indicator", "blurb": "RSI, MACD, Supertrend, moving averages and more."},
            {"key": "results", "label": "Financial results", "blurb": "When results are filed, or the date is announced."},
        ],
        "timeframes": [{"key": k, "label": v} for k, v in TIMEFRAMES],
        "price": {"sources": [
            {"key": "number", "label": "A price I type"},
            {"key": "last_n_high", "label": "The highest high of the last N sessions"},
            {"key": "last_n_low", "label": "The lowest low of the last N sessions"},
            {"key": "date_high", "label": "The high of one date"},
            {"key": "date_low", "label": "The low of one date"}],
            "max_days": MAX_REF_DAYS, "default_days": 5},
        "ema_support": {"modes": [
            {"key": "near", "label": "Comes within X% above the EMA (early heads-up)"},
            {"key": "holds", "label": "Dips to the EMA but closes back above it (support held)"}],
            "period": {"default": 50, "min": 2, "max": 400}, "tolerance": {"default": 1.0, "min": 0.1, "max": 10}},
        "trend_change": {"directions": [{"key": "up", "label": "A downtrend turning up (bullish)"},
                                        {"key": "down", "label": "An uptrend turning down (bearish)"}],
                         "measures": [{"key": "MAJOR", "label": "Big picture: the high the fall began from"},
                                      {"key": "SWING", "label": "Short term: every swing"}],
                         "charts": [{"key": "LINE", "label": "Closing prices"}, {"key": "CANDLE", "label": "Candlesticks (highs and lows)"}],
                         "falls": [10, 20, 30, 50], "default_fall": 20},
        # candle colour is not an alert; and the company-data rules (profit, ratios, shareholding) are read from filings, not charts: the "Financial results" alert is theirs
        "indicators": [i for i in base["indicators"] if i["key"] != "candle" and i.get("data") != "company"],
        "results": {"events": [{"key": "filed", "label": "When the next quarterly results are filed"},
                               {"key": "date", "label": "When the date of the results meeting is announced"}],
                    "metrics": [{"key": "profit", "label": "Net profit"}, {"key": "revenue", "label": "Sales"},
                                {"key": "eps", "label": "EPS"}]},
        "honesty": ("We hold end-of-day prices only, so a price alert is on a CLOSE. Weekly and monthly alerts wait for "
                    "the week or month to finish. An alert is one email, once; set another if you want to be told again."),
    }


# =====================================================================
# VALIDATION  (raw input from the page -> a clean spec; AlertError says why not)
# =====================================================================
def clean_spec(raw, frames=None, today=None, ctx=None):
    """Validate `raw` and return the spec to store: clean, with everything that depends on today's data RESOLVED (a
    reference high becomes a rupee figure, the baseline candle is fixed). `frames` is {tf: DataFrame | reason string},
    `ctx` carries rows for the results kinds: {"fin": [...], "events": [...]}."""
    if not isinstance(raw, dict):
        raise AlertError("The alert is not in a form I can read.")
    kind = _choice(raw.get("kind"), KINDS, "Choose what you want to be alerted about.")
    today = today or date.today()
    try:
        return _CLEAN[kind](raw, frames, today, ctx or {})
    except AlertError:
        raise
    except ImportError:
        raise AlertError("This kind of alert is not available on this server yet (part of it has not been installed).")
    except te.SpecError as e:
        raise AlertError(str(e))
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
        raise AlertError("The alert is not in a form I can read.")


def _clean_price(raw, frames, today, ctx):
    direction = _choice(raw.get("direction"), ("above", "below"), "Say whether you want the close above or below.")
    source = _choice(raw.get("source", "number"), ("number", "last_n_high", "last_n_low", "date_high", "date_low"),
                     "Choose what the price is compared with.")
    d = _frame(frames, "D")
    last = _last_date(d)
    spec = {"kind": "price", "direction": direction, "source": source, "baseline": _iso(last)}
    if source == "number":
        spec["level"] = round(_num(raw.get("level"), "Price", 0.01, MAX_LEVEL, "the price"), 2)
    elif source in ("last_n_high", "last_n_low"):
        n = int(_num(raw.get("days"), "Sessions", 1, MAX_REF_DAYS, "the number of sessions", integer=True))
        if len(d) < n:
            raise AlertError("We hold only %d sessions of prices for this stock, fewer than the %d you asked for." % (len(d), n))
        window = d.iloc[-n:]
        if source == "last_n_high":
            spec["level"] = round(float(window["high"].max()), 2)
        else:
            spec["level"] = round(float(window["low"].min()), 2)
        spec["days"] = n
        spec["ref_from"], spec["ref_to"] = _iso(window.index[0]), _iso(window.index[-1])
    else:
        want = raw.get("date")
        try:
            wd = date.fromisoformat(str(want)[:10])
        except ValueError:
            raise AlertError("Choose the date, for example 2026-10-01.")
        if wd > last:
            raise AlertError("That date is after the latest prices we hold (%s)." % dmy(last))
        hit = d.loc[d.index.normalize() == pd.Timestamp(wd)]
        if len(hit) == 0:
            before = d.loc[d.index < pd.Timestamp(wd)]
            after = d.loc[d.index > pd.Timestamp(wd)]
            near = [dmy(x.index[-1]) for x in (before,) if len(x)] + [dmy(x.index[0]) for x in (after,) if len(x)]
            raise AlertError("There was no trading in this stock on %s (a weekend or holiday?)%s" % (
                dmy(wd), ". The nearest sessions are " + " and ".join(near) + "." if near else "."))
        spec["level"] = round(float(hit["high" if source == "date_high" else "low"].iloc[0]), 2)
        spec["ref_date"] = _iso(wd)
    # a "close above the highest high of the last N sessions" can never be already true, but a typed number or an old
    # date's high can be: refuse that in words rather than save an alert that fires at once or never
    close = float(d["close"].iloc[-1])
    if _price_hit(spec, close):
        raise AlertError("It already closed at %s on %s, %s %s. An alert needs a level the price has not reached yet."
                         % (money(close), dmy(last), "above" if direction == "above" else "below", money(spec["level"])))
    return spec


def _price_hit(spec, close):
    return close > spec["level"] if spec["direction"] == "above" else close < spec["level"]


def _clean_ema(raw, frames, today, ctx):
    tf = _choice(raw.get("tf", "D"), tuple(k for k, _ in TIMEFRAMES), "Choose daily, weekly or monthly.")
    mode = _choice(raw.get("mode", "holds"), ("near", "holds"), "Choose how the price should come to the average.")
    period = int(_num(raw.get("period", 50), "Moving average", 2, 400, "the EMA length", integer=True))
    tol = round(_num(raw.get("tolerance", 1.0), "Tolerance", 0.1, 10, "the distance (per cent)"), 2)
    df = _frame(frames, tf)
    d = _frame(frames, "D")
    spec = {"kind": "ema_support", "tf": tf, "mode": mode, "period": period, "tolerance": tol, "baseline": _iso(_last_date(d))}
    ema, close, in_zone, _came = _ema_series(df, spec)
    if math.isnan(ema.iloc[-1]):
        raise AlertError("This stock has too little history for a %d-candle EMA on the %s chart (%d candles)."
                         % (period, TF_LABEL[tf].lower(), len(df)))
    px, e = float(close.iloc[-1]), float(ema.iloc[-1])
    if px < e:
        raise AlertError("The %s close (%s) is already below the %s EMA (%s). Support means the price comes DOWN to the "
                         "average from above. To be told when it gets back above, use a Technical indicator alert."
                         % (TF_LABEL[tf].lower(), money(px), period, money(e)))
    if bool(in_zone.iloc[-1]):
        raise AlertError("The price is already there: the %s close %s is %s the %d EMA (%s) and within %g%% of it. "
                         "Choose a smaller distance, or wait until it moves away." % (
                             TF_LABEL[tf].lower(), money(px), "at" if px == e else "just above", period, money(e), tol))
    return spec


def _ema_series(df, spec):
    p, tol = spec["period"], spec["tolerance"]
    ema = be._compute(df, "ema", {"period": p, "src": "close"}).astype(float)
    close, low = df["close"].astype(float), df["low"].astype(float)
    zone_top = ema * (1 + tol / 100.0)
    if spec["mode"] == "near":
        in_zone = (close >= ema) & (close <= zone_top)
        came = close.shift(1) > zone_top.shift(1)
    else:
        in_zone = (low <= zone_top) & (close >= ema)
        came = close.shift(1) > ema.shift(1)
    return ema, close, in_zone.fillna(False), came.fillna(False)


def _clean_trend(raw, frames, today, ctx):
    tf = _choice(raw.get("tf", "D"), tuple(k for k, _ in TIMEFRAMES), "Choose daily, weekly or monthly.")
    direction = _choice(raw.get("direction"), ("up", "down"), "Say whether you want a turn up or a turn down.")
    measure = _choice(raw.get("measure", "MAJOR"), ("MAJOR", "SWING"), "Choose how the turn is measured.")
    chart = _choice(raw.get("chart", "LINE"), ("LINE", "CANDLE"), "Choose closing prices or candlesticks.")
    fall = int(_num(raw.get("fall", 20), "Size of the move", 5, 90, "the size of the fall (per cent)", integer=True))
    df = _frame(frames, tf)
    d = _frame(frames, "D")
    import dow_change
    if len(df) < dow_change.MIN_BARS:
        raise AlertError("This stock has only %d %s candles, too few to read a trend." % (len(df), TF_LABEL[tf].lower()))
    return {"kind": "trend_change", "tf": tf, "direction": direction, "measure": measure, "chart": chart, "fall": fall,
            "baseline": _iso(_last_date(d))}


def _clean_indicator(raw, frames, today, ctx):
    cond_raw = raw.get("condition") if isinstance(raw.get("condition"), dict) else raw
    tf = cond_raw.get("tf", "D")
    if tf in te.INTRADAY:
        raise AlertError("We hold daily, weekly and monthly candles only, so an indicator alert can use those.")
    if tf not in dict(TIMEFRAMES):
        raise AlertError("Choose daily, weekly or monthly.")
    cond = te._clean_condition({k: cond_raw.get(k) for k in ("tf", "indicator", "params", "test", "inputs")}, 1)
    if te._IND[cond["indicator"]].get("data") == "company":
        raise AlertError("Profit, revenue, ratios and shareholding are not chart indicators. Use \"Financial results\" to be told when results are filed, or "
                         "set the rule in My trade plan.")
    cond["join"] = "and"
    df = _frame(frames, tf)
    d = _frame(frames, "D")
    return {"kind": "indicator", "condition": cond, "baseline": _iso(_last_date(d))}


def _clean_results(raw, frames, today, ctx):
    event = _choice(raw.get("event", "filed"), ("filed", "date"), "Choose results filed, or the date announced.")
    fin, events = ctx.get("fin") or [], ctx.get("events") or []
    spec = {"kind": "results", "event": event}
    if event == "date":
        upcoming = [e for e in events if e["event_date"] >= today]
        if upcoming:
            e = min(upcoming, key=lambda x: x["event_date"])
            raise AlertError("The date is already out: a board meeting to consider results is on %s. Choose \"when the "
                             "results are filed\" instead." % dmy(e["event_date"]))
        spec["baseline"] = _iso(today)
        return spec
    flt = raw.get("filter")
    if flt not in (None, {}, False):
        if not isinstance(flt, dict):
            raise AlertError("The results condition is not in a form I can read.")
        metric = _choice(flt.get("metric"), ("profit", "revenue", "eps"), "Choose net profit, sales or EPS.")
        direction = _choice(flt.get("direction"), ("up", "down"), "Choose up or down on a year ago.")
        pct = round(_num(flt.get("min_pct", 0), "Change", 0, 10000, "the change (per cent)"), 1)
        spec["filter"] = {"metric": metric, "direction": direction, "min_pct": pct}
    quarterly = [r for r in fin if r.get("period_type", "quarterly") == "quarterly"]
    spec["baseline_period"] = _iso(max(r["period_end"] for r in quarterly)) if quarterly else None
    spec["baseline"] = _iso(today)
    return spec


_CLEAN = {"price": _clean_price, "ema_support": _clean_ema, "trend_change": _clean_trend,
          "indicator": _clean_indicator, "results": _clean_results}


# =====================================================================
# WORDS
# =====================================================================
METRIC_LABEL = {"profit": "net profit", "revenue": "sales", "eps": "EPS"}


def describe(spec):
    """The alert as one sentence, without the company's name (the page and the email put that beside it)."""
    k = spec["kind"]
    if k == "price":
        side = "above" if spec["direction"] == "above" else "below"
        src = spec.get("source", "number")
        lv = money(spec["level"])
        if src == "number":
            return "closes %s %s" % (side, lv)
        if src in ("last_n_high", "last_n_low"):
            return "closes %s the %s of the last %d sessions (%s, up to %s)" % (
                side, "highest high" if src == "last_n_high" else "lowest low", spec["days"], lv, dmy(spec["ref_to"]))
        return "closes %s the %s of %s (%s)" % (side, "high" if src == "date_high" else "low", dmy(spec["ref_date"]), lv)
    if k == "ema_support":
        tf = TF_LABEL[spec["tf"]].lower()
        if spec["mode"] == "near":
            return "comes down to within %g%% above its %d EMA on the %s chart (a %s close)" % (
                spec["tolerance"], spec["period"], tf, tf)
        return "dips to its %d EMA (within %g%%) and closes back above it, on the %s chart" % (
            spec["period"], spec["tolerance"], tf)
    if k == "trend_change":
        way = "up" if spec["direction"] == "up" else "down"
        ends = "a downtrend ends" if spec["direction"] == "up" else "an uptrend ends"
        how = "big picture, a move of at least %d%%" % spec["fall"] if spec["measure"] == "MAJOR" else "short-term swings"
        chart = "closing prices" if spec["chart"] == "LINE" else "candlesticks"
        return "shows a change of trend turning %s on the %s chart (%s; Dow theory, %s, %s)" % (way, TF_LABEL[spec["tf"]].lower(), ends, how, chart)
    if k == "indicator":
        return te.describe_condition(spec["condition"])
    if k == "results":
        if spec["event"] == "date":
            return "announces the date of its next results meeting"
        f = spec.get("filter")
        if not f:
            return "files its next quarterly results"
        word = "up" if f["direction"] == "up" else "down"
        by = " by at least %g%%" % f["min_pct"] if f["min_pct"] else ""
        return "files its next quarterly results and %s is %s%s on a year ago" % (METRIC_LABEL[f["metric"]], word, by)
    raise AlertError("Unknown alert kind.")


KIND_TITLE = {"price": "Price", "ema_support": "Moving average", "trend_change": "Change of trend",
              "indicator": "Indicator", "results": "Results"}


# =====================================================================
# CHECKING
# =====================================================================
def _res(state, reading=None, as_of=None, reason=None):
    return {"state": state, "reading": reading, "as_of": as_of, "reason": reason}


def check(spec, frames, today=None, ctx=None):
    """Has it happened on a candle AFTER the alert was set? -> {state: met | not_met | no_data, reading, as_of, reason}.
    Never raises for a data problem: that is "no_data" with the reason."""
    today = today or date.today()
    try:
        return _CHECK[spec["kind"]](spec, frames, today, ctx or {})
    except AlertError as e:
        return _res("no_data", reason=str(e))
    except ImportError:
        return _res("no_data", reason="Part of the alert checker is not installed on this server.")


def status_now(spec, frames, today=None, ctx=None):
    """Is the condition true RIGHT NOW, on the newest candle (forming or not)? Used only when an alert is being made,
    to say 'already' instead of saving something that cannot behave. Same result shape as check()."""
    today = today or date.today()
    k = spec["kind"]
    try:
        if k == "price":
            d = _frame(frames, "D")
            close = float(d["close"].iloc[-1])
            return _res("met" if _price_hit(spec, close) else "not_met", "Closed at %s on %s" % (money(close), dmy(_last_date(d))),
                        _iso(_last_date(d)))
        if k == "indicator":
            df = _frame(frames, spec["condition"]["tf"])
            r = te.evaluate_condition(spec["condition"], df, today, closed_only=False)
            return _res(r["state"], r.get("reading"), None, r.get("reason"))
        if k == "ema_support":
            df = _frame(frames, spec["tf"])
            ema, close, in_zone, _c = _ema_series(df, spec)
            if math.isnan(ema.iloc[-1]):
                return _res("no_data", reason="Too little history for this average.")
            return _res("met" if bool(in_zone.iloc[-1]) else "not_met")
        if k == "results" and spec["event"] == "date":
            up = [e for e in (ctx or {}).get("events", []) if e["event_date"] >= today]
            return _res("met" if up else "not_met")
        return _res("not_met")
    except AlertError as e:
        return _res("no_data", reason=str(e))


def _chk_price(spec, frames, today, ctx):
    d = _frame(frames, "D")
    base = _baseline(spec)
    for i in _eligible(d, "D", base, today):
        close = float(d["close"].iloc[i])
        if _price_hit(spec, close):
            when = d.index[i].date()
            return _res("met", "Closed at %s on %s, %s %s" % (money(close), dmy(when), spec["direction"], money(spec["level"])),
                        _iso(when))
    return _res("not_met", "Last close %s on %s" % (money(float(d["close"].iloc[-1])), dmy(_last_date(d))), _iso(_last_date(d)))


def _chk_ema(spec, frames, today, ctx):
    df = _frame(frames, spec["tf"])
    base = _baseline(spec)
    ema, close, in_zone, came = _ema_series(df, spec)
    low = df["low"].astype(float)
    for i in _eligible(df, spec["tf"], base, today):
        if math.isnan(ema.iloc[i]) or not (bool(in_zone.iloc[i]) and bool(came.iloc[i])):
            continue
        when = df.index[i].date()
        if spec["mode"] == "near":
            reading = "%s close %s, %d EMA %s: %.1f%% above it" % (
                TF_LABEL[spec["tf"]], money(close.iloc[i]), spec["period"], money(ema.iloc[i]),
                (close.iloc[i] / ema.iloc[i] - 1) * 100)
        else:
            reading = "%s candle of %s: low %s reached the %d EMA (%s) and the close %s held above it" % (
                TF_LABEL[spec["tf"]], dmy(when), money(low.iloc[i]), spec["period"], money(ema.iloc[i]), money(close.iloc[i]))
        return _res("met", reading, _iso(when))
    return _res("not_met")


def _trend_events(spec, frames, today):
    """The change-of-trend events on the chart, as dicts with a date, for the closed candles only."""
    import dow_change
    df = _frame(frames, spec["tf"])
    if len(df) and te.candle_forming(spec["tf"], df.index[-1], today):
        df = df.iloc[:-1]
    df = df.iloc[-MAX_BARS_FOR_TREND:]
    if len(df) < dow_change.MIN_BARS:
        raise AlertError("Too few candles to read a trend.")
    dates = [x.date() for x in df.index]
    h, l, c = (df[x].astype(float).tolist() for x in ("high", "low", "close"))
    candles = spec["chart"] == "CANDLE"
    if spec["measure"] == "MAJOR":
        return dow_change.scan_major_ohlc(dates, h, l, c, spec["direction"], candles, 400, spec["fall"], 1)
    return dow_change.scan_ohlc(dates, h, l, c, 1, spec["direction"], candles)


def _chk_trend(spec, frames, today, ctx):
    base = _baseline(spec)
    events = [e for e in _trend_events(spec, frames, today) if e["date"] > base]
    if not events:
        return _res("not_met")
    e = events[0]                      # the first change of trend since the alert was set
    up = spec["direction"] == "up"
    reading = ("%s chart: the trend turned %s on %s. The price went %s %s (the %s of %s), after %s to a %s of %s on %s." % (
        TF_LABEL[spec["tf"]], "UP" if up else "DOWN", dmy(e["date"]), "above" if up else "below", money(e["level_broken"]),
        "high" if up else "low", dmy(e["level_broken_date"]), "pulling back" if up else "bouncing",
        "higher low" if up else "lower high", money(e["pullback"]), dmy(e["pullback_date"])))
    return _res("met", reading, _iso(e["date"]))


def trend_info(spec, frames, today=None):
    """For the confirmation shown when a trend alert is made: the date of the latest change of trend of this kind on
    the chart, or None. Informational only; it never stops an alert from being made."""
    try:
        ev = _trend_events(spec, frames, today or date.today())
    except AlertError:
        return None
    return ev[-1]["date"] if ev else None


def _chk_indicator(spec, frames, today, ctx):
    cond = spec["condition"]
    df = _frame(frames, cond["tf"])
    base = _baseline(spec)
    for i in _eligible(df, cond["tf"], base, today):
        r = te.evaluate_condition(cond, df.iloc[:i + 1], today, closed_only=False)
        if r["state"] == "met":
            when = df.index[i].date()
            return _res("met", "%s (%s candle of %s)" % (r.get("reading") or te.describe_condition(cond), TF_LABEL[cond["tf"]].lower(),
                                                         dmy(when)), _iso(when))
    return _res("not_met")


def _growth(now, base):
    """(pct or None, 'up' | 'down' | 'flat' | None). A percentage only where the earlier figure was positive."""
    if now is None or base is None:
        return None, None
    if base > 0:
        pct = round((now - base) / base * 100, 1)
        return pct, ("flat" if abs(pct) < 0.05 else "up" if pct > 0 else "down")
    if base < 0:
        return None, ("up" if now > base else "down" if now < base else "flat")
    return None, ("up" if now > 0 else "down" if now < 0 else "flat")


def _year_ago(rows, r):
    for o in rows:
        if o is r or o["consolidated"] != r["consolidated"]:
            continue
        if 350 <= (r["period_end"] - o["period_end"]).days <= 380:
            return o
    return None


def _prefer_consolidated(rows):
    """One row per quarter: the consolidated one where both exist (the same choice the results page makes)."""
    best = {}
    for r in rows:
        k = r["period_end"]
        if k not in best or (r.get("consolidated") and not best[k].get("consolidated")):
            best[k] = r
    return sorted(best.values(), key=lambda r: r["period_end"])


def _chk_results(spec, frames, today, ctx):
    if spec["event"] == "date":
        up = sorted((e for e in ctx.get("events", []) if e["event_date"] >= today), key=lambda x: x["event_date"])
        if not up:
            return _res("not_met")
        e = up[0]
        detail = (e.get("detail") or "").strip()
        return _res("met", "A board meeting to consider results is on %s%s" % (
            dmy(e["event_date"]), (": " + detail[:200]) if detail else ""), _iso(e["event_date"]))
    rows = [r for r in (ctx.get("fin") or []) if r.get("period_type", "quarterly") == "quarterly"]
    bp = spec.get("baseline_period")
    bp = date.fromisoformat(bp) if bp else None
    fresh = [r for r in _prefer_consolidated(rows) if bp is None or r["period_end"] > bp]
    if not fresh:
        return _res("not_met")
    r = fresh[-1]                      # the latest quarter filed since the alert was set
    ya = _year_ago(rows, r)
    parts = []
    for key, label in (("revenue", "Sales"), ("profit", "Net profit"), ("eps", "EPS")):
        v = r.get(key)
        if v is None:
            continue
        v = float(v)
        pct, _d = _growth(v, float(ya[key]) if ya and ya.get(key) is not None else None)
        txt = "%s %s" % (label, ("₹%.2f" % v) if key == "eps" else crore(v))
        if pct is not None:
            txt += " (%+.1f%% on a year ago)" % pct
        parts.append(txt)
    quarter = (r.get("period_label") or "").strip()
    head = "%sresults for the quarter ended %s, filed %s" % (quarter + " " if quarter else "", dmy(r["period_end"]),
                                                              dmy(r["filed_on"]) if r.get("filed_on") else "recently")
    reading = head + ": " + "; ".join(parts) if parts else head
    when = _iso(r.get("filed_on") or r["period_end"])
    f = spec.get("filter")
    if not f:
        return _res("met", reading, when)
    v, b = r.get(f["metric"]), (ya.get(f["metric"]) if ya else None)
    pct, d = _growth(float(v) if v is not None else None, float(b) if b is not None else None)
    ok = d == f["direction"] and (not f["min_pct"] or (pct is not None and abs(pct) >= f["min_pct"]))
    if ok:
        return _res("met", reading, when)
    out = _res("not_met", "%s did not match your condition (%s)" % (quarter or "The latest results", ("%+.1f%% on a year ago" % pct)
                                                                    if pct is not None else "no comparable year-ago figure"), when)
    out["new_baseline_period"] = _iso(r["period_end"])      # wait for the NEXT quarter, not this one again
    return out


_CHECK = {"price": _chk_price, "ema_support": _chk_ema, "trend_change": _chk_trend, "indicator": _chk_indicator,
          "results": _chk_results}


def assess_new(spec, frames, today=None, ctx=None):
    """Called when an alert is being made, after clean_spec: refuse (AlertError) an alert that is already true, or that
    cannot be read at all and so could never fire. Returns the status_now answer for the confirmation."""
    r = status_now(spec, frames, today, ctx)
    if r["state"] == "met":
        raise AlertError("That is already true: %s. An alert waits for something that is not true yet."
                         % (r.get("reading") or describe(spec)).rstrip("."))
    if r["state"] == "no_data":
        raise AlertError("We cannot read this one yet (%s), so an alert on it could never fire." % (r.get("reason") or "no data").rstrip("."))
    return r


# =====================================================================
# THE EMAIL
# =====================================================================
DISCLAIMER = ("This is a condition you chose, checked against the latest end-of-day prices and filings we hold. It is not "
              "a recommendation to buy, sell or hold anything, and a chart pattern or indicator describes what the price "
              "has already done; it does not predict what comes next. Educational content only, not investment advice. "
              "FinChaya is not currently registered with SEBI as an Investment Adviser or Research Analyst.")


def build_email(alert, reading, site):
    """(subject, text body, html body) for a met alert. Nothing the person typed is placed anywhere it could be read as
    markup or a header: the sentence is built here from validated numbers, and the html escapes everything."""
    import html as _html
    who = alert.get("symbol") or alert.get("company_name") or alert["isin"]
    name = alert.get("company_name")
    target = "%s (%s)" % (name, who) if name and alert.get("symbol") else who
    sentence = alert["sentence"]
    subject = _one_line("Alert met: %s %s" % (who, sentence))
    if len(subject) > 150:
        subject = subject[:147] + "..."
    link = "%s/stock-alerts" % site.rstrip("/")
    lines = ["Hello,", "", "An alert you set on %s has happened." % target, "", "  %s %s" % (who, sentence)]
    if reading:
        lines += ["", "  " + reading]
    lines += ["", "This was a one-time alert, so you will not get another email for it. To set another, or to see or remove "
                  "your alerts: " + link, "", DISCLAIMER, ""]
    text = "\n".join(lines)
    e = _html.escape
    page = ("<html><body style=\"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#1b2b24;font-size:15px;line-height:1.5\">"
            "<p>Hello,</p><p>An alert you set on <b>%s</b> has happened.</p>"
            "<p style='font-size:16px'><b>%s</b> %s</p>%s"
            "<p>This was a one-time alert, so you will not get another email for it. <a href=\"%s\">See or set alerts</a>.</p>"
            "<p style='color:#6b7c76;font-size:12.5px'>%s</p></body></html>"
            % (e(target), e(who), e(sentence), ("<p style='color:#51615a'>%s</p>" % e(reading)) if reading else "", e(link), e(DISCLAIMER)))
    return subject, text, page


def _one_line(s):
    return re.sub(r"[\r\n\x00]+", " ", str(s or "")).strip()
