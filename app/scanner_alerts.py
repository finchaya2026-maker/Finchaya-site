"""
scanner_alerts.py -- "email me when a NEW stock appears on this scanner": the settings, the check, the email, and the command line.
---------------------------------------------------------------------------
    cd /opt/mfapi && venv/bin/python3 scanner_alerts.py              # one pass, quietly (this is what cron runs, every 30 minutes)
    venv/bin/python3 scanner_alerts.py --dry-run                     # what WOULD be emailed; nothing saved, nothing sent
    venv/bin/python3 scanner_alerts.py --status                      # is email set up, when did it last run, how many alerts
    venv/bin/python3 scanner_alerts.py --sample you@example.com      # ONE real email built the way an alert's is, from made-up figures: proves delivery

WHAT IT IS
    Every scanner on the Screener page (Change in Dow Theory, Fibonacci retracement, Higher low, Expanded flat, multi-year breakouts, RSI and MACD signals, crossovers,
    ownership and fund-flow screens, results / dividends due ...) has a box: "Email me when a new stock appears". It saves the screen and its settings. From then on, each
    time a day's prices have arrived, this checker runs that scanner on its own, with those settings, and emails the member.

TWO WAYS TO BE TOLD (the member chooses)
    new    (the default) one email each time a stock that was not on the scanner at the last look appears on it. Nothing on a day with nothing new.
    daily  one email every trading day with the whole list, the new stocks marked, even when nothing matched ("nothing matched today" is an answer).

WHAT COUNTS AS "NEW"
    A stock is new when it is on the scanner now and was not at the last look. When the alert is made, the stocks already on it are noted as "there already" (the page says how
    many), so the first email is about something that happened AFTER the alert was set. A stock that leaves the scanner and comes back is new again.
    A scanner reads "the last few bars" for an event (a trend that turned, a high that was passed), so a missed day is caught up: an alert always reads at least the last 5 daily,
    2 weekly or 2 monthly bars, whatever "alert made within the last" the page shows, and a stock already noted is never emailed twice.
    A weekly or monthly bar that is still forming is not used: it can disappear before it closes, so the email comes once the week or month has closed.

THE RULES THAT KEEP THIS HONEST (the ones the stock alerts and the IPO alert follow)
    * What is to be emailed is SAVED BEFORE the email is tried, in the same statement that notes the stocks as seen. If the email fails it is retried on later runs
      (MAX_EMAIL_ATTEMPTS), so a mail outage loses nothing.
    * It says what is set up: every run leaves a row in scanner_alert_run, and the page says "last checked ..." or "has not run for N hours".
    * A scanner that cannot be read right now (an error, or its data is not up to date yet) is NOT treated as "nothing matches": the stocks seen before stay seen, so
      a data outage cannot wipe the list and then email every stock as new.
    * LIMITS: MAX_ALERTS per person, MAX_LINES stocks named in one email (the rest are counted).
    * Two checkers at once would send the same email twice, so a database advisory lock keeps it to one.
    * Nothing here says buy or sell. It reports what a scanner found on the latest end-of-day data.
"""

import html as _html
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

MAX_ALERTS = 10                 # per person
MAX_LINES = 30                  # stocks named in one email
MAX_STILL = 40                  # names listed under "still on the scanner"
MAX_STORED_NEW = 200            # stocks kept in a waiting email
MAX_MEMBERS = 20000             # the stocks noted as on the scanner (a scanner never lists more than the market has)
MAX_EMAIL_ATTEMPTS = 6          # a failing send is retried on later runs, this many times in all
STALE_CHECK_HOURS = 3           # the checker is "not running" if its last run is older than this (cron is every 30 minutes)
LOCK_KEY = 727431               # advisory lock
MODES = ("new", "daily")
CAPS = ("Large", "Mid", "Small")
TIMEFRAMES = ("DAILY", "WEEKLY", "MONTHLY")
WINDOWS = (7, 14, 30, 60, 90)                       # the "due within" choices of the events screens
TF_WORDS = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly"}
# A scanner reading "within the last N bars" is read at least this far back by an alert, so a missed day is caught up (never emailed twice: the stock is noted).
CATCH_UP = {"DAILY": 5, "WEEKLY": 2, "MONTHLY": 2}
PATTERN_KEYS = ("double_top", "double_bottom", "head_shoulders", "inv_head_shoulders", "flag_pole", "flag_pole_down")      # chart_patterns.py
NEVER_STORED = ("recent", "quarter", "limit", "screen")         # not part of an alert: the newest bars and the newest quarter are always read
# The screens that read the stored indicators (stock_technical), which the nightly job fills AFTER the prices: until their data has caught up with the newest prices
# the checker waits (it does not treat the old data as today's answer).
TECHNICAL_SCREENS = ("rsi_cross", "cross", "macd_pco")
DISCLAIMER = ("This reports what a scanner found on the latest end-of-day data we hold. It is not a recommendation to buy, sell or hold anything, and a pattern or a signal can "
              "fail. Educational content only, not investment advice. FinChaya is not currently registered with SEBI as an Investment Adviser or Research Analyst.")


class AlertError(ValueError):
    """An alert that cannot be made, in words the person can act on."""


# =====================================================================
# THE SETTINGS
# =====================================================================
def _int(v, lo, hi, label):
    if isinstance(v, bool):
        raise AlertError("Choose one of the offered values for '%s'." % label)
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise AlertError("Choose one of the offered values for '%s'." % label)
    if f != f or f != int(f) or not (lo <= f <= hi):
        raise AlertError("'%s' must be a whole number from %d to %d." % (label, lo, hi))
    return int(f)


def _control(c, v):
    """One catalogue control, strictly: a choice must be one of its options, a number must be inside its range. (The page's own screen quietly falls back to the default;
    an alert must never quietly watch something other than what was asked.)"""
    label = c.get("label") or c["param"]
    if v is None or v == "":
        return c["default"]
    if isinstance(v, bool):
        raise AlertError("Choose one of the offered values for '%s'." % label)
    if c["kind"] == "number":
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise AlertError("'%s' must be a number." % label)
        if f != f or not (c["min"] <= f <= c["max"]):
            raise AlertError("'%s' must be between %g and %g." % (label, c["min"], c["max"]))
        return int(f) if isinstance(c["default"], int) and f == int(f) else f
    for o in c["options"]:
        if str(o[0]) == str(v):
            return o[0]
    raise AlertError("Choose one of the offered values for '%s'." % label)


def clean_settings(spec, raw, ipo_windows=()):
    """The settings of an alert on this screen, checked. Returns a dict holding only what applies to the screen: cap, listed_within, minimum, timeframe, level, window and the
    screen's own controls. Missing settings take the screen's defaults. 'recent' and 'quarter' are never stored (an alert always reads the newest bars and the newest quarter)."""
    if not isinstance(raw, dict):
        raise AlertError("The alert is not in a form I can read.")
    out = {}
    cap = raw.get("cap")
    if cap not in (None, ""):
        if cap not in CAPS:
            raise AlertError("Choose one of the company sizes offered.")
        out["cap"] = cap
    lw = raw.get("listed_within")
    if lw not in (None, "", 0, "0"):
        if spec.get("ipo_only"):
            raise AlertError("This screen is for IPO stocks already; use its own alert box.")
        n = _int(lw, 1, 100000, "Only IPOs listed in the last")
        if n not in [int(w) for w in ipo_windows]:
            raise AlertError("Choose one of the offered periods for 'Only IPOs listed in the last'.")
        out["listed_within"] = n
    if spec.get("min_label"):
        v = raw.get("minimum")
        out["minimum"] = _int(spec["min_default"] if v in (None, "") else v, 1, 50, spec["min_label"])
    if spec.get("timeframes"):
        tf = raw.get("timeframe")
        tf = "DAILY" if tf in (None, "") else str(tf).upper()
        if tf not in TIMEFRAMES:
            raise AlertError("Choose one of the offered values for 'Timeframe'.")
        out["timeframe"] = tf
    if spec.get("level"):
        v = raw.get("level")
        out["level"] = _int(spec["level"] if v in (None, "") else v, 5, 95, "Crossing up through")
    if spec.get("window_default"):
        v = raw.get("window")
        w = _int(spec["window_default"] if v in (None, "") else v, 1, 365, "Due within")
        if w not in WINDOWS:
            raise AlertError("Choose one of the offered values for 'Due within'.")
        out["window"] = w
    for c in spec.get("controls") or []:
        if c["param"] in NEVER_STORED or _not_stored(spec, c):
            continue
        out[c["param"]] = _control(c, raw.get(c["param"]))
    return out


def _not_stored(spec, c):
    """An alert on a chart pattern is ALWAYS about the breakout (the close through the neckline or out of the flag), never about the pattern merely forming: the page's
    'Show: formed, signal still to come' choice (a watch list) is not part of an alert, so a stock is emailed on the day it breaks, not before."""
    return spec.get("key") in PATTERN_KEYS and c["param"] == "status"


def settings_key(screen, settings):
    return json.dumps({"screen": screen, "settings": settings}, sort_keys=True, default=str)


def timeframe_of(spec, settings):
    return settings.get("timeframe") or "DAILY"


def _shown_controls(spec, settings):
    """The screen's controls that apply (a control can say it applies only when another has a value), without the ones an alert does not store."""
    ctl = [c for c in spec.get("controls") or [] if c["param"] not in NEVER_STORED and not _not_stored(spec, c)]
    out = []
    for c in ctl:
        w = c.get("when")
        if w:
            other = next((x for x in ctl if x["param"] == w["param"]), None)
            if other is not None and str(settings.get(other["param"], other["default"])) != str(w["value"]):
                continue
        out.append(c)
    return out


def describe(spec, settings, ipo_windows=()):
    """What the alert watches, as a list of short phrases."""
    bits = []
    if spec.get("timeframes"):
        bits.append("%s bars" % TF_WORDS[settings.get("timeframe", "DAILY")].capitalize())
    for c in _shown_controls(spec, settings):
        v = settings.get(c["param"], c["default"])
        if c["kind"] == "choice":
            lab = next((o[1] for o in c["options"] if str(o[0]) == str(v)), v)
            bits.append("%s: %s" % (c.get("label") or c["param"], lab))
        else:
            bits.append("%s: %g" % (c.get("label") or c["param"], float(v)))
    if "level" in settings:
        bits.append("crossing up through %d" % settings["level"])
    if "minimum" in settings and spec.get("min_label"):
        bits.append("%s: %d" % (spec["min_label"].lower() if spec["min_label"][:1].isupper() else spec["min_label"], settings["minimum"]))
    if "window" in settings:
        bits.append("due within %d days" % settings["window"])
    if settings.get("cap"):
        bits.append("%s caps only" % settings["cap"].lower())
    if settings.get("listed_within"):
        label = next((w[1] for w in ipo_windows if int(w[0]) == int(settings["listed_within"])), "%d days" % settings["listed_within"])
        bits.append("only IPOs listed in the last %s" % label)
    return bits


def sentence(spec, settings, ipo_windows=()):
    bits = describe(spec, settings, ipo_windows)
    return "%s%s." % (spec["name"], (" - " + "; ".join(bits)) if bits else "")


def scan_recent(spec, settings, user_value=None):
    """The 'alert made within the last N bars' an alert's scan uses: the screen's own offered choice that is at least the catch-up window (and at least what the reader chose)."""
    c = next((x for x in spec.get("controls") or [] if x["param"] == "recent"), None)
    if c is None:
        return None
    want = max(int(user_value or 0), CATCH_UP[timeframe_of(spec, settings)])
    offered = sorted(int(o[0]) for o in c["options"])
    return next((o for o in offered if o >= want), offered[-1])


# =====================================================================
# WHICH BARS ARE CLOSED
# =====================================================================
def bar_closed(timeframe, market_date, today):
    """Is the weekly or monthly bar that ends at `market_date` over? (A daily bar always is.) A week is over from the Friday's prices on; a month once the next
    weekday falls in the next month. A holiday at the end of a week or month is caught up the next time the date moves on."""
    if timeframe == "DAILY" or market_date is None:
        return True
    today = today or date.today()
    if timeframe == "WEEKLY":
        if market_date.weekday() >= 4:
            return True
        return tuple(today.isocalendar())[:2] > tuple(market_date.isocalendar())[:2]
    if timeframe == "MONTHLY":
        nxt = market_date + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        if (nxt.year, nxt.month) != (market_date.year, market_date.month):
            return True
        return (today.year, today.month) > (market_date.year, market_date.month)
    return True


def usable(row, timeframe, market_date, today):
    """A match worth telling about: not on a weekly or monthly bar that is still forming (it can vanish before it closes)."""
    return (not row.get("forming")) or bar_closed(timeframe, market_date, today)


# =====================================================================
# WHAT A MATCH SAYS, IN WORDS (one line per stock, per screen)
# =====================================================================
def _dmy(iso):
    try:
        d = date.fromisoformat(str(iso)[:10])
        return "%d %s %d" % (d.day, d.strftime("%b"), d.year)
    except (TypeError, ValueError):
        return ""


def _rs(v):
    return "" if v is None else "Rs %s" % "{:,.2f}".format(float(v))


def _pct(v, signed=False):
    if v is None:
        return ""
    v = round(float(v), 1)
    if v == 0:
        v = 0.0                                                  # never "-0.0%"
    return ("%.1f%%" % v) if (not signed or v == 0) else "%+.1f%%" % v


def _tf(row):
    return TF_WORDS.get(row.get("timeframe") or "", "")


def _dow(r):
    up = r.get("direction") != "down"
    tf = _tf(r)
    head = "%s turned %s" % (tf.capitalize() + " trend" if tf else "The trend", "up" if up else "down")
    if r.get("measure") == "MAJOR":
        return ("%s on %s: went %s %s (the %s, set %s), reaching %s, after a %s of %s on %s. The %s was %s."
                % (head, _dmy(r.get("alert_date")), "above" if up else "below", _rs(r.get("level_broken")),
                   "high the fall began from" if up else "low the rise began from", _dmy(r.get("level_broken_date")), _rs(r.get("trigger")),
                   "higher low" if up else "lower high", _rs(r.get("pullback")), _dmy(r.get("pullback_date")), "fall" if up else "rise", _pct(r.get("fall_pct"))))
    return ("%s on %s: a second %s of %s, %s the first one of %s, after a %s of %s (%s). The last %s, %s, was broken on %s."
            % (head, _dmy(r.get("alert_date")), "higher high" if up else "lower low", _rs(r.get("trigger")), "above" if up else "below", _rs(r.get("first_extreme")),
               "higher low" if up else "lower high", _rs(r.get("pullback")), _dmy(r.get("pullback_date")), "lower high" if up else "higher low", _rs(r.get("level_broken")),
               _dmy(r.get("break_date"))))


def _fib(r):
    s = ("Pulled back %s of a %s rise (low %s on %s, top %s on %s); the zone is %s to %s; closed at %s."
         % (_pct(r.get("retracement")), _pct(r.get("rise_pct")), _rs(r.get("bottom")), _dmy(r.get("bottom_date")), _rs(r.get("peak")), _dmy(r.get("peak_date")),
            _rs(r.get("level_low")), _rs(r.get("level_high")), _rs(r.get("close"))))
    return s + (" The latest candle reached the zone; its close is outside it." if r.get("near") == "touch" else "")


def _hl(r):
    return ("A %s higher low of %s (%s), %s above the low before it (%s, %s), after a pullback of %s from %s (%s); first seen %s. The price is now %s above that low."
            % (_tf(r), _rs(r.get("low")), _dmy(r.get("low_date")), _pct(r.get("rise_pct")), _rs(r.get("prev_low")), _dmy(r.get("prev_low_date")), _pct(r.get("pullback_pct")),
               _rs(r.get("high")), _dmy(r.get("high_date")), _dmy(r.get("confirmed_date")), _pct(r.get("held_by_pct")))).replace("A  higher", "A higher")


def _pat(r):
    """The chart-pattern scanners (chart_patterns.py): double top / bottom, head and shoulders (and inverted), flag and pole."""
    down = r.get("direction") == "down"
    pts = list(r.get("points") or [])
    f = r.get("facts") or {}
    tf = _tf(r) or "daily"
    watch = r.get("status") == "WATCH"
    kind = r.get("kind")

    def at(i):
        p = pts[i] if i < len(pts) else {}
        return "%s (%s)" % (_rs(p.get("price")), _dmy(p.get("date")))
    line = ("flag's lower line" if down else "flag's upper line") if kind == "flag" else "neckline"
    if watch:
        end = " Formed, not broken yet: the last close is %s, the %s is %s." % (_rs(r.get("close")), line, _rs(r.get("neck")))
    else:
        end = " %s: closed at %s on %s, %s the %s of %s." % ("Broke out of the flag" if kind == "flag" else "Neckline broken", _rs(r.get("close")), _dmy(r.get("signal_date")),
                                                           "below" if down else "above", line, _rs(r.get("neck")))
    if kind == "double":
        word = "top" if down else "bottom"
        past = float(f.get("past_first_pct") or 0)
        how = ("a fake %s: the second %s went %s past the first" % ("breakout" if down else "breakdown", word, _pct(past))) if past > 0 else "%s short of the first" % _pct(abs(past))
        return "%s on %s bars: first %s %s, second %s %s, %s; the %s between them was %s deep.%s" % (
            "Double top" if down else "Double bottom", tf, word, at(0), word, at(2), how, "pullback" if down else "bounce", _pct(f.get("depth_pct")), end)
    if kind == "hs":
        head = next((i for i, p in enumerate(pts) if p.get("label") == "Head"), 2)
        ls = next((i for i, p in enumerate(pts) if p.get("label") == "Left shoulder"), 0)
        rs = next((i for i, p in enumerate(pts) if p.get("label") == "Right shoulder"), len(pts) - 1)
        gaps = (" (%s bars from the end of the left shoulder to the head, %s from the head to the right shoulder)" % (f.get("left_bars"), f.get("right_bars"))) if f.get("left_bars") is not None else ""
        return "%s on %s bars: left shoulder %s, head %s (%s past the shoulders), right shoulder %s; the head stands %s from the neckline%s.%s" % (
            "Head and shoulders" if down else "Inverted head and shoulders", tf, at(ls), at(head), _pct(f.get("head_pct")), at(rs), _pct(f.get("depth_pct")), gaps, end)
    return "%s flag and pole on %s bars: a pole of %s (from %s to %s), then a flag of %s bars that gave back %s of it.%s" % (
        "Bearish" if down else "Bullish", tf, _pct(f.get("pole_pct")), at(0), at(1), f.get("flag_bars"), _pct(f.get("retrace_pct")), end)


def _flat(r):
    down = r.get("direction") == "down"
    return ("Expanded flat (%s) on %s bars: 0 at %s (%s), A %s, B %s (%s), C %s (%s), which is %.2f times the first %s %s B; formed within %s bars."
            % ("downtrend" if down else "uptrend", _tf(r) or "daily", _rs(r.get("p0")), _dmy(r.get("p0_date")), _rs(r.get("a")), _rs(r.get("b")), _dmy(r.get("b_date")),
               _rs(r.get("c")), _dmy(r.get("c_date")), float(r.get("ratio_c") or 0), "bounce" if down else "pullback", "above" if down else "below", r.get("span_bars")))


def _breakout(r):
    lvl = (" at %s" % _rs(r.get("level"))) if r.get("level") is not None and not r.get("level_hidden") else ""
    vol = (" on %.1f times the usual volume" % float(r["volume_ratio"])) if r.get("volume_ratio") is not None else ""
    return ("Went through its %s-year high on %s, reaching %s%s. The old high was set on %s%s.%s"
            % (r.get("tier"), _dmy(r.get("broke_date")), _rs(r.get("broke_price")), vol, _dmy(r.get("level_date")), lvl, "" if r.get("still_above", True) else " It is back under it now."))


def _rsi(r):
    return "RSI rose from %.1f to %.1f, through %s, on %s bars (%s)." % (float(r.get("rsi_prev") or 0), float(r.get("rsi") or 0), r.get("level"), _tf(r) or "daily", _dmy(r.get("as_of")))


def _cross(r):
    return "%s (%s bars, %s)." % (r.get("kind_label") or "A crossover", _tf(r) or "daily", _dmy(r.get("as_of")))


def _macd(r):
    return "MACD is above its signal line on the daily, the weekly and the monthly chart at once; just turned up: %s." % (r.get("just_label") or "none of them")


def _event(r):
    n = r.get("days_away")
    when = ("" if n is None else "today" if n == 0 else "tomorrow" if n == 1 else "yesterday" if n == -1 else "in %d days" % n if n > 1 else "%d days ago" % -n)
    return "%s on %s%s: %s" % (r.get("kind_label") or "Event", _dmy(r.get("event_date")), (" (%s)" % when) if when else "", r.get("headline") or "")


def _own(r, res):
    vals = r.get("values") or {}
    cols = (res or {}).get("columns") or []
    parts = []
    for i, c in enumerate(cols):
        v = vals.get(c.get("key")) or {}
        if v.get("now") is None:
            continue
        ch = v.get("change")
        if i > 0 and (ch is None or abs(float(ch)) < 1.0 or len(parts) >= 3):
            continue                                             # the first column is what the screen is about; the others only when they moved by a point or more
        parts.append("%s %.2f%%%s" % (c.get("label") or c.get("key"), float(v["now"]), (" (%+.2f points)" % float(ch)) if ch is not None else ""))
    s = "; ".join(parts)
    if r.get("trail"):
        s += ". Each quarter, oldest first: " + " -> ".join("%.2f" % x for x in r["trail"])
    if r.get("price_move") is not None:
        s += ". The price moved %s over the quarter" % _pct(r["price_move"], True)
    return (s + ".") if s else "On the screen now."


def _funds(screen, r):
    b, s, n, x, h = r.get("buyers") or 0, r.get("sellers") or 0, r.get("new") or 0, r.get("exits") or 0, r.get("holders") or 0
    if screen == "new":
        return "%d fund%s bought it for the first time this month; %d hold it." % (n, "" if n == 1 else "s", h)
    if screen == "leaving":
        return "%d fund%s sold or cut it (%d sold out entirely) and %d added to it; %d hold it." % (s + x, "" if s + x == 1 else "s", x, b, h)
    extra = (" The price moved %s over the month." % _pct(r["price_move"], True)) if screen == "quiet" and r.get("price_move") is not None else ""
    return "%d fund%s added to it, %d cut it, %d bought it for the first time; %d hold it.%s" % (b, "" if b == 1 else "s", s, n, h, extra)


def row_text(screen, r, res=None):
    """One match in words. Never raises: a row this code does not know is still named."""
    try:
        if screen.startswith("dow_change"):
            return _dow(r)
        if screen == "fib_retrace":
            return _fib(r)
        if screen == "higher_low":
            return _hl(r)
        if screen in PATTERN_KEYS:
            return _pat(r)
        if screen.startswith("expanded_flat"):
            return _flat(r)
        if screen == "multiyear_breakout":
            return _breakout(r)
        if screen == "rsi_cross":
            return _rsi(r)
        if screen == "cross":
            return _cross(r)
        if screen == "macd_pco":
            return _macd(r)
        if screen.startswith("upcoming_"):
            return _event(r)
        if screen.startswith("own_"):
            return _own(r, res)
        if screen in ("buying", "leaving", "new", "quiet"):
            return _funds(screen, r)
    except Exception:                                            # noqa: BLE001
        pass
    return "On the scanner now."


def snapshot(screen, r, res=None):
    """What an email needs about a match, kept with it (so a retried email says what was true at the time)."""
    keep = ("isin", "symbol", "name", "cap", "sector", "last_close", "last_close_date", "last_change_pct")
    d = {k: r.get(k) for k in keep}
    d["text"] = row_text(screen, r, res)
    return d


# =====================================================================
# THE EMAIL
# =====================================================================
def _price(s):
    if s.get("last_close") is None:
        return ""
    chg = s.get("last_change_pct")
    return "%s%s%s" % (_rs(s["last_close"]), (" (%+.2f%%)" % float(chg)) if chg is not None else "", (" on " + _dmy(s["last_close_date"])) if s.get("last_close_date") else "")


def build_email(alert, payload, site):
    """(subject, text, html) for one alert's waiting email. Everything that came from outside is escaped in the html and kept on one line in the subject.
    payload: {"new": [snapshot...], "new_total": n, "total": n, "still": [{"name","symbol"}...], "as_of": date}"""
    new = payload.get("new") or []
    n_new = payload.get("new_total", len(new))
    total = payload.get("total", 0)
    daily = alert.get("mode") == "daily"
    title = alert.get("title") or "your scanner"
    if daily:
        subject = "FinChaya scanner: %s - %s today%s" % (title, ("%d stock%s" % (total, "" if total == 1 else "s")) if total else "nothing matched",
                                                          (" (%d new)" % n_new) if n_new else "")
    else:
        subject = "FinChaya scanner: %d new stock%s on %s" % (n_new, "" if n_new == 1 else "s", title)
    shown, more = new[:MAX_LINES], max(0, n_new - MAX_LINES)
    screen = "%s/#/screener/%s" % (site.rstrip("/"), alert.get("screen") or "")
    as_of = payload.get("as_of")
    if daily:
        intro = ("%d stock%s on the scanner you set an alert on" % (total, "" if total == 1 else "s")) if total else "Nothing matched on the scanner you set an alert on"
        intro += (", %d of them new since the last look:" % n_new) if n_new else (":" if total else " today.")
    else:
        intro = "%d new stock%s appeared on the scanner you set an alert on:" % (n_new, "" if n_new == 1 else "s")
    lines = ["Hello,", "", intro, "", "  " + (alert.get("sentence") or title), ""]
    h = ["<html><body style=\"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#1b2b24;font-size:15px;line-height:1.5\">",
         "<p>Hello,</p><p><b>%s</b></p>" % _html.escape(intro), "<p style='color:#51615a'>%s</p>" % _html.escape(alert.get("sentence") or title)]
    if shown:
        h.append("<p style='margin-bottom:4px'><b>%s</b></p><ol style='padding-left:20px;margin-top:4px'>" % ("New" if daily else "The new stocks"))
        if daily:
            lines += ["NEW SINCE THE LAST LOOK:", ""]
    for i, s in enumerate(shown, 1):
        name = "%s (%s)" % (s.get("name") or s.get("symbol") or s["isin"], s.get("symbol") or s["isin"])
        link = "%s/#/stock/%s" % (site.rstrip("/"), s["isin"])
        meta = " - ".join(x for x in ((s.get("cap") + " cap") if s.get("cap") else "", _price(s)) if x)
        lines += ["%d. %s%s" % (i, name, (" - " + meta) if meta else ""), "   " + (s.get("text") or ""), "   " + link, ""]
        h.append("<li style='margin-bottom:10px'><b>%s</b>%s<br><span style='color:#51615a'>%s</span><br><a href=\"%s\">Open the stock page</a></li>"
                 % (_html.escape(name), (" &ndash; " + _html.escape(meta)) if meta else "", _html.escape(s.get("text") or ""), _html.escape(link)))
    if shown:
        h.append("</ol>")
    if more:
        lines += ["...and %d more new stocks. See them all on the screen." % more, ""]
        h.append("<p>...and %d more new stocks. See them all on the screen.</p>" % more)
    still = payload.get("still") or []
    others = max(0, total - n_new)
    if others:
        names = ", ".join("%s" % (s.get("symbol") or s.get("name")) for s in still[:MAX_STILL])
        left = others - len(still[:MAX_STILL])
        lines += ["%d other stock%s %s still on the scanner: %s%s" % (others, "" if others == 1 else "s", "is" if others == 1 else "are", names, (" and %d more" % left) if left > 0 else ""), ""]
        h.append("<p style='color:#51615a'>%d other stock%s %s still on the scanner: %s%s</p>"
                 % (others, "" if others == 1 else "s", "is" if others == 1 else "are", _html.escape(names), (" and %d more" % left) if left > 0 else ""))
    if as_of:
        lines += ["Read from the prices of %s." % _dmy(as_of), ""]
        h.append("<p style='color:#51615a;font-size:13px'>Read from the prices of %s.</p>" % _html.escape(_dmy(as_of)))
    lines += ["See the whole screen: " + screen, "To stop these emails, open the screen and press Stop beside this alert.", "", DISCLAIMER, ""]
    h.append("<p><a href=\"%s\">See the whole screen</a>. To stop these emails, open the screen and press Stop beside this alert.</p>"
             "<p style='color:#6b7c76;font-size:12.5px'>%s</p></body></html>" % (_html.escape(screen), _html.escape(DISCLAIMER)))
    return re.sub(r"[\r\n\x00]+", " ", subject).strip(), "\n".join(lines), "".join(h)


# =====================================================================
# THE CHECKER
# =====================================================================
def _j(v):
    return json.loads(v) if isinstance(v, (str, bytes)) else v


def _utc(now):
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def readable(screen, res, market_date, had_members=True):
    """(ok, why not). A scanner result is only believed when it could be read and its data has caught up with the newest prices; an empty answer from a screen that tested
    nothing, after it had listed stocks, is a data problem, not 'no stock matches' (it would wipe the list, and every stock would be new when the data came back)."""
    if res is None:
        return False, "the scanner could not be read"
    if res.get("unavailable"):
        return False, str(res["unavailable"])
    if res.get("considered") == 0 and had_members:
        return False, "the scanner had no data to test"
    if screen in TECHNICAL_SCREENS and market_date is not None:
        got = _as_date(res.get("as_of"))
        if got is not None and got < market_date:
            return False, "its indicators (%s) have not caught up with the prices of %s yet" % (got, market_date)
    return True, None


def _merge_new(old, new):
    seen, out = set(), []
    for s in list(old or []) + list(new or []):
        if s["isin"] not in seen:
            seen.add(s["isin"])
            out.append(s)
    return out[:MAX_STORED_NEW]


def run_once(query, execute, scan, specs, market_date=None, send=None, today=None, now=None, dry_run=False, log=print, env=None):
    """One pass over every active alert. Everything it needs from outside is passed in, so it runs the same against the real database, the test database and a fake
    mail server.

      query(sql, params=None, one=False) / execute(sql, params=None, returning=False)   the database
      scan(screen, settings) -> the scanner's whole answer {"rows": [...], "as_of": ...}   the screen, run with these settings (None or an exception: could not be read)
      specs                                                                               {screen key: catalogue entry}
      market_date                                                                         the newest trading day with prices (None: no skipping)
      send(to, subject, text, html) -> (ok, detail)                                       mail

    Returns the counts. Never raises for one bad alert: it is logged and the rest are still looked at."""
    if send is None:
        import mailer
        send = lambda to, subject, text, html: mailer.send_email(to, subject, text, html=html, env=env)    # noqa: E731
        site = mailer.site_url(env)
    else:
        site = (env or os.environ).get("FINCHAYA_SITE_URL", "https://finchaya.com").rstrip("/")
    now = now or datetime.now(timezone.utc)
    today = today or date.today()
    out = {"checked": 0, "skipped": 0, "waiting": 0, "new": 0, "emailed": 0, "failed": 0, "errors": []}
    alerts = query("SELECT alert_id, user_id, screen, title, settings, settings_key, sentence, mode, members, pending, pending_attempts, last_stamp, last_digest_date "
                   "FROM scanner_alert WHERE active ORDER BY alert_id")
    if not alerts:
        if not dry_run:
            _heartbeat(execute, out)
        return out
    users = {r["user_id"]: r for r in query("SELECT user_id, email, is_active FROM mf_user WHERE user_id = ANY(%(u)s)", {"u": sorted({a["user_id"] for a in alerts})})}
    stamp = ("%s|%s" % (market_date, today)) if market_date else None
    scans = {}                                                  # (screen, settings key) -> the scanner's answer, or None when it could not be read

    for a in alerts:
        try:
            spec = specs.get(a["screen"])
            if spec is None:                                    # a screen this server no longer has: leave the alert alone
                out["errors"].append("alert %s: unknown screen %s" % (a["alert_id"], a["screen"]))
                continue
            settings = _j(a["settings"])
            pending = _j(a["pending"])
            due = stamp is None or a["last_stamp"] != stamp
            if not due and not pending:
                out["skipped"] += 1
                continue
            payload, state = pending, None
            if due:
                k = (a["screen"], a["settings_key"])
                if k not in scans:
                    try:
                        scans[k] = scan(a["screen"], settings)
                    except Exception as e:                       # noqa: BLE001  one scanner failing must not stop the others
                        scans[k] = None
                        out["errors"].append("scan %s: %s: %s" % (a["screen"], type(e).__name__, e))
                        log(out["errors"][-1])
                res = scans[k]
                ok, why = readable(a["screen"], res, market_date, had_members=bool(_j(a["members"])))
                if not ok:
                    if res is not None:
                        out["waiting"] += 1
                        log("alert %s: not checked: %s" % (a["alert_id"], why))
                else:
                    tf = timeframe_of(spec, settings)
                    rows = [r for r in res["rows"] if usable(r, tf, market_date, today)]
                    members = set(_j(a["members"]) or [])
                    new_rows = [r for r in rows if r["isin"] not in members]
                    out["checked"] += 1
                    out["new"] += len(new_rows)
                    for r in new_rows:
                        log("alert %s: new match %s" % (a["alert_id"], r.get("symbol")))
                    news = [snapshot(a["screen"], r, res) for r in new_rows[:MAX_STORED_NEW]]
                    new_ids = {r["isin"] for r in new_rows}
                    still = [{"symbol": r.get("symbol"), "name": r.get("name")} for r in rows if r["isin"] not in new_ids][:MAX_STILL]
                    daily = a["mode"] == "daily"
                    digest_due = daily and (_as_date(a["last_digest_date"]) != market_date or bool(new_rows) or market_date is None)
                    if new_rows or digest_due or pending:
                        payload = {"new": _merge_new((pending or {}).get("new"), news), "new_total": len(new_rows) + (((pending or {}).get("new_total", 0)) if pending else 0),
                                   "total": len(rows), "still": still, "as_of": str(res.get("as_of") or market_date or today), "market_date": str(market_date) if market_date else None}
                        if pending:                              # an unsent email is brought up to date, never doubled
                            payload["new_total"] = max(payload["new_total"], len(payload["new"]))
                    if (not new_rows and not digest_due) and not pending:
                        payload = None
                    state = {"members": sorted(r["isin"] for r in rows)[:MAX_MEMBERS], "as_of": str(res.get("as_of") or "")}
            if not dry_run and state is not None:
                # what is to be emailed, and the stocks now noted as seen, are saved together BEFORE the email is tried
                execute("UPDATE scanner_alert SET members = %(m)s::jsonb, pending = %(p)s::jsonb, pending_attempts = CASE WHEN %(p)s::jsonb IS NULL THEN 0 ELSE pending_attempts END, "
                        "last_checked_at = %(at)s, last_stamp = %(s)s, last_as_of = %(asof)s, last_error = NULL WHERE alert_id = %(id)s",
                        {"m": json.dumps(state["members"]), "p": json.dumps(payload, default=str) if payload else None, "at": _utc(now), "s": stamp, "asof": state["as_of"], "id": a["alert_id"]})
            if payload:
                _email(a, payload, users, send, site, execute, now, dry_run, log, out)
        except Exception as e:                                   # noqa: BLE001
            out["errors"].append("alert %s: %s: %s" % (a["alert_id"], type(e).__name__, e))
            log(out["errors"][-1])
    if not dry_run:
        _heartbeat(execute, out)
    return out


def _email(alert, payload, users, send, site, execute, now, dry_run, log, out):
    u = users.get(alert["user_id"])
    if not u or not u.get("is_active", True) or not u.get("email"):
        if not dry_run:                                          # nobody to tell: do not retry for ever
            execute("UPDATE scanner_alert SET pending = NULL, pending_attempts = 0, last_error = 'no active account with an email address' WHERE alert_id = %(a)s", {"a": alert["alert_id"]})
        log("alert %s: nobody to email" % alert["alert_id"])
        return
    subject, text, html = build_email(alert, payload, site)
    if dry_run:
        log("  [dry run] would email %s: %s" % (u["email"], subject))
        return
    ok, detail = send(u["email"], subject, text, html)
    if ok:
        execute("UPDATE scanner_alert SET pending = NULL, pending_attempts = 0, last_error = NULL, emails_sent = emails_sent + 1, emailed_count = emailed_count + %(n)s, "
                "last_emailed_at = %(at)s, last_digest_date = CASE WHEN mode = 'daily' THEN %(d)s::date ELSE last_digest_date END WHERE alert_id = %(a)s",
                {"n": payload.get("new_total", 0), "at": _utc(now), "d": payload.get("market_date"), "a": alert["alert_id"]})
        out["emailed"] += 1
        log("alert %s: emailed %s (%d new, %d on the scanner)" % (alert["alert_id"], u["email"], payload.get("new_total", 0), payload.get("total", 0)))
    else:
        tries = (alert.get("pending_attempts") or 0) + 1
        if tries >= MAX_EMAIL_ATTEMPTS:                          # give up: the page shows why
            execute("UPDATE scanner_alert SET pending = NULL, pending_attempts = 0, last_error = %(e)s WHERE alert_id = %(a)s",
                    {"e": ("the email could not be sent after %d tries: %s" % (tries, detail))[:300], "a": alert["alert_id"]})
        else:
            execute("UPDATE scanner_alert SET pending_attempts = %(t)s, last_error = %(e)s WHERE alert_id = %(a)s", {"t": tries, "e": str(detail)[:300], "a": alert["alert_id"]})
        out["failed"] += 1
        log("alert %s: email FAILED (%s)" % (alert["alert_id"], detail))


def _heartbeat(execute, out):
    """The row the page reads to say alerts are really being looked at."""
    execute("INSERT INTO scanner_alert_run (checked, skipped, waiting, new_matches, emailed, failed, error) VALUES (%(c)s, %(s)s, %(w)s, %(n)s, %(e)s, %(f)s, %(err)s) RETURNING run_id",
            {"c": out["checked"], "s": out["skipped"], "w": out["waiting"], "n": out["new"], "e": out["emailed"], "f": out["failed"], "err": ("; ".join(out["errors"])[:500] or None)},
            returning=True)


def checker_status(query, now=None):
    """Whether alerts are being checked: when the checker last ran, and whether that is too long ago. A failed read is 'unknown', never 'healthy'."""
    now = now or datetime.now(timezone.utc)
    try:
        row = query("SELECT started_at, checked, new_matches, emailed, failed, error FROM scanner_alert_run ORDER BY run_id DESC LIMIT 1", None, one=True)
    except Exception:                                            # noqa: BLE001
        return {"known": False, "never_run": None, "last_run_at": None, "stale": None}
    if not row:
        return {"known": True, "never_run": True, "last_run_at": None, "stale": True}
    at = row["started_at"]
    if isinstance(at, str):
        at = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return {"known": True, "never_run": False, "last_run_at": at, "stale": (_utc(now) - _utc(at)) > timedelta(hours=STALE_CHECK_HOURS), "emailed": row["emailed"], "failed": row["failed"],
            "error": row["error"]}


# =====================================================================
# COMMAND LINE
# =====================================================================
def _connect():
    import psycopg
    from dotenv import load_dotenv
    from psycopg.rows import dict_row
    here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(here, ".env"))
    load_dotenv("/opt/mfapi/.env")
    dsn = os.environ.get("FINCHAYA_DB")
    if not dsn:
        sys.exit("FINCHAYA_DB is not set.")
    return psycopg.connect(dsn, row_factory=dict_row)


def main(argv):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    load_dotenv("/opt/mfapi/.env")

    if "--sample" in argv:                                    # one real email, built the way an alert's is, from made-up figures: proves delivery without waiting for a match
        i = argv.index("--sample")
        to = argv[i + 1] if i + 1 < len(argv) else ""
        import mailer
        if not mailer.valid_address(to):
            sys.exit("Usage: scanner_alerts.py --sample you@example.com")
        today = date.today().isoformat()
        row = {"isin": "INE000A01001", "symbol": "SAMPLE", "name": "Sample Company Limited", "cap": "Large", "last_close": 113.0, "last_close_date": today, "last_change_pct": 1.4,
               "direction": "up", "measure": "MAJOR", "timeframe": "DAILY", "alert_date": today, "trigger": 113.0, "level_broken": 112.0, "level_broken_date": today,
               "pullback": 105.0, "pullback_date": today, "fall_pct": 24.0}
        payload = {"new": [snapshot("dow_change", row)], "new_total": 1, "total": 3, "still": [{"symbol": "ABC", "name": "ABC"}, {"symbol": "DEF", "name": "DEF"}], "as_of": today}
        alert = {"alert_id": 0, "screen": "dow_change", "title": "Change in Dow Theory: turning up", "mode": "new",
                 "sentence": "Change in Dow Theory: turning up - Daily bars; Chart: Line (closing prices)."}
        subject, text, html = build_email(alert, payload, mailer.site_url())
        ok, detail = mailer.send_email(to, "[SAMPLE, made-up figures] " + subject, "THIS IS A SAMPLE. The stock and the figures are made up.\n\n" + text, html=html)
        print("sample email to %s: %s (%s)" % (to, "SENT" if ok else "FAILED", detail))
        sys.exit(0 if ok else 1)

    conn = _connect()

    def query(sql, params=None, one=False):
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            rows = cur.fetchall()
        conn.commit()
        return (rows[0] if rows else None) if one else rows

    def execute(sql, params=None, returning=False):
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            row = cur.fetchone() if returning else None
        conn.commit()
        return row

    if "--status" in argv:
        import mailer
        print("\n".join(mailer.describe()))
        st = checker_status(query)
        print("checker: %s" % ("never run" if st.get("never_run") else "last ran %s%s" % (st.get("last_run_at"), "  (TOO LONG AGO)" if st.get("stale") else "")))
        for r in query("SELECT active, mode, COUNT(*) AS n FROM scanner_alert GROUP BY 1, 2 ORDER BY 1, 2"):
            print("  alerts %-8s %-6s %d" % ("active" if r["active"] else "stopped", r["mode"], r["n"]))
        for r in query("SELECT screen, COUNT(*) AS n, COUNT(*) FILTER (WHERE pending IS NOT NULL) AS waiting FROM scanner_alert WHERE active GROUP BY 1 ORDER BY 2 DESC"):
            print("  %-22s %3d alert%s%s" % (r["screen"], r["n"], "" if r["n"] == 1 else "s", ("  (%d email waiting)" % r["waiting"]) if r["waiting"] else ""))
        return

    dry = "--dry-run" in argv
    got = conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (LOCK_KEY + (1 if dry else 0),)).fetchone()["got"]
    if not got:
        print("another scanner-alert checker is already running; nothing done")
        return
    import price_bars
    import screener_api as S
    with conn.cursor() as cur:
        market = price_bars.latest_market_date(cur)
    conn.rollback()
    specs = {s["key"]: s for s in S.SCREENS}
    out = run_once(query, execute, S.alert_scan, specs, market_date=market, dry_run=dry)
    print("%schecked %d, unchanged %d, waiting for data %d, new matches %d, emailed %d, email failed %d%s"
          % ("[dry run] " if dry else "", out["checked"], out["skipped"], out["waiting"], out["new"], out["emailed"], out["failed"], ("; ERRORS: " + " | ".join(out["errors"])) if out["errors"] else ""))
    sys.exit(1 if out["errors"] else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
