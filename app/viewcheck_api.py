"""
viewcheck_api.py -- "Check your view".
---------------------------------------------------------------------------
Mounted in api.py through _mount_optional("viewcheck_api", ...).

    GET /api/viewcheck/{isin}?view=bullish|bearish[&plan_id=N]

YOUR OWN RULES COME FIRST (plan_id)
    A person can write their own rules in My trade plan: any mix of the readings the site holds (MACD, RSI, Supertrend and the other chart
    indicators, Dow theory, the company's profit, revenue and EPS, its ratios, who owns it, what the mutual funds did, when results are due),
    joined with AND / OR. Asked with plan_id, this checks THAT plan on this stock for the chosen view first and returns it as "my_rules": every
    condition met, not met or no data (with the reading behind it), the sets, and the overall answer. Everything below is then the extra
    information: each reading is marked in_rules when one of the person's rules already covers it, and a reading no rule covers (a Supertrend,
    say, that is not in their plan) is still shown, as extra. Somebody else's plan is a 404. A plan that cannot be used (written for an index, or
    no longer valid) is explained in words, and the readings still come. Without plan_id nothing changes.

WHAT IT DOES
    A person picks a stock and says how they see it -- bullish or bearish.
    This answers with the chart readings that LINE UP with that view and,
    just as prominently, the ones that RUN AGAINST it, each read separately
    on the monthly, weekly and daily candles. The point of the page is the
    second list: people go looking for what agrees with them, and a single
    MACD turning down on one timeframe is exactly the thing they would not
    have gone looking for.

WHO MAY USE IT
    Premium members and administrators, nobody else. Three decisions in
    allowed() are worth knowing about:

    * PREMIUM MEANS PREMIUM. api.has_subscription() returns True for EVERY
      caller when MF_PAYWALL_ENABLED is false, which is right for the
      products that were free before the paywall existed. This tool never
      was, so it asks the account itself (is_premium and not lapsed) and a
      paywall switched off by accident cannot open it to the public.
    * ADMIN is the app_admin table, the same list admin_api.py and
      distributor_api.py check -- one answer to "who is an admin".
    * The check is made here, on every call. The page hides itself from
      other people, but a hidden link protects nothing.
    * MF_VIEWCHECK_ENABLED=false is a hard off for everybody, admins too.

WORDING, AND WHY
    FinChaya is not a SEBI-registered Research Analyst or Investment
    Adviser, and scores are switched off for that reason (see
    MF_SCORES_ENABLED in api.py). So this module never ranks, rates or
    totals anything into a verdict, never says "buy", "sell", "good" or
    "bad", and never agrees or disagrees with the person. It reports what
    each indicator currently reads, and which side of the person's view
    that falls on. Counts of points per side are shown; a combined score
    is deliberately not computed.

    Every point carries its own plain-language rule, and the response
    carries the thresholds used, so nobody has to take the sorting on trust.

WHAT THE READINGS ARE
    The values stored in stock_technical by fetch_technicals.py -- MACD,
    RSI, ADX/DI, Supertrend and the EMAs -- for DAILY, WEEKLY and MONTHLY.
    Nothing is recomputed here and nothing is written. The person's view is
    not stored either: it is a question, not a record.

    PLUS THE COMPANY'S OWN RESULTS, when it has filed them. Revenue, profit
    and EPS for the latest quarter against a year earlier and against the
    previous quarter, and for the latest financial year against the year
    before, from stock_financials. They are judged with the same two words
    as the chart readings (revenue up lines up with a bullish view, revenue
    down runs against it) and shown as a table of the latest quarters.
    The arithmetic is NOT redone here: it uses api.build_financials and
    its _fin_* helpers -- the ones behind the Financials tab -- so the two
    pages cannot disagree about whether profit rose, and EPS is withheld
    across a bonus or split by the same share-count test.

KNOWN LIMITS, SAID TO THE READER RATHER THAN HIDDEN
    * Stored candles are not adjusted for splits and bonuses, which bends
      the averages and MACD of a stock that had one. A one-day move of 30%
      or more in the last ~3 years raises a note naming the date.
    * The weekly and monthly bars include the candle that is still forming.
    * A stock with a short history has fewer timeframes, and a long EMA
      needs a long history. What could not be read is listed, not guessed.
    * The data can be stale; the date of each reading is returned and a
      daily reading older than four days raises a note.
    * Results are only as complete as stock_financials. A stock with none
      on file says so; a stock whose newest quarter on file is old says so.
      A change is only worked out against the same basis (consolidated
      against consolidated), and EPS only when the share count is
      unchanged -- otherwise that point is listed as not taken.
"""

import os
import re
import time
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/viewcheck", tags=["viewcheck"])

VIEWCHECK_ENABLED = os.getenv("MF_VIEWCHECK_ENABLED", "true").lower() == "true"

# WHO may use it: "admin" while the tool is still being finished (the
# default -- it is not part of any plan yet and is not advertised with
# them), "premium" the day it is released to the Stocks tier. Anything
# else reads as admin: a typo must shut people out, not let them in.
ACCESS = os.getenv("MF_VIEWCHECK_ACCESS", "admin").strip().lower()


def mode() -> str:
    return ACCESS if ACCESS in ("admin", "premium") else "admin"

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")

VIEWS = ("bullish", "bearish")

# =====================================================================
# THE RULES
# =====================================================================
# One place, read by the judging code below AND returned to the page, so
# the wording on screen and the arithmetic cannot drift apart.
RSI_STRONG = 60        # at or above: strong zone
RSI_WEAK = 40          # below: weak zone; between the two: no lean
RSI_STRETCH_HI = 70    # at or above: the zone usually called overbought
RSI_STRETCH_LO = 30    # at or below: the zone usually called oversold
ADX_TREND = 20         # at or above: a trend is in place
EMA_CHECKS = (20, 50, 200)

STALE_DAYS = 4         # same line the stock page uses for its "stale?" tag
JUMP_DOWN = 0.70       # a one-day close at or below 70% of the day before
JUMP_UP = 1.40         # ... or at or above 140% of it
JUMP_LOOKBACK_DAYS = 1100

TIMEFRAMES = ("MONTHLY", "WEEKLY", "DAILY")    # chart candles, biggest picture first
RESULTS = "RESULTS"                             # not a candle: the company's filings
ALL_TF = TIMEFRAMES + (RESULTS,)
TF_LABEL = {"MONTHLY": "Monthly", "WEEKLY": "Weekly", "DAILY": "Daily",
            RESULTS: "Company results"}
TF_UNIT = {"MONTHLY": "month", "WEEKLY": "week", "DAILY": "day"}

FLAT_PCT = 1.0          # a change smaller than this is "little changed" -- the
                        # same line api._fin_move draws on the Financials tab
RESULTS_STALE_DAYS = 160    # a quarter ends, then up to ~60 days to file; past
                            # this the newest quarter on file is probably not
                            # the newest one the company has reported

# Order and names of the checks. The page builds its at-a-glance grid from
# this, so a check added here shows up there without touching the page.
CHECKS = (
    ("macd_signal", "MACD vs its signal line"),
    ("macd_dir",    "MACD direction"),
    ("rsi_zone",    "RSI level"),
    ("rsi_dir",     "RSI direction"),
    ("rsi_stretch", "RSI stretched"),
    ("adx_di",      "Trend and who leads it (ADX / DI)"),
    ("supertrend",  "Supertrend"),
    ("ema_20",      "Price vs 20 EMA"),
    ("ema_50",      "Price vs 50 EMA"),
    ("ema_200",     "Price vs 200 EMA"),
    ("ema_50_200",  "50 EMA vs 200 EMA"),
)

# The results checks. Kept apart from CHECKS: that tuple drives the
# at-a-glance grid, whose columns are candle timeframes, and a results check
# has no timeframe to sit under.
RESULT_CHECKS = (
    ("rev_yoy",    "Revenue vs a year ago"),
    ("profit_yoy", "Profit vs a year ago"),
    ("eps_yoy",    "EPS vs a year ago"),
    ("rev_qoq",    "Revenue vs the previous quarter"),
    ("profit_qoq", "Profit vs the previous quarter"),
    ("eps_qoq",    "EPS vs the previous quarter"),
    ("rev_fy",     "Revenue, latest financial year"),
    ("profit_fy",  "Profit, latest financial year"),
    ("eps_fy",     "EPS, latest financial year"),
)
CHECK_LABEL = dict(CHECKS + RESULT_CHECKS)
_CHECK_ORDER = {k: i for i, (k, _) in enumerate(CHECKS + RESULT_CHECKS)}


def rules_text():
    """How each point is judged, in the reader's words."""
    return [
        "MACD is compared with its signal line, and with its own reading "
        "on the previous candle (up or down).",
        "RSI at %d or above counts as strong, below %d as weak, and "
        "anywhere in between is shown as not leaning either way. At %d or "
        "above, or %d or below, it is also flagged as stretched, because "
        "readings that far out are commonly followed by a pause."
        % (RSI_STRONG, RSI_WEAK, RSI_STRETCH_HI, RSI_STRETCH_LO),
        "Trend: ADX at %d or above means a trend is in place, and +DI "
        "against -DI says whether buyers or sellers are leading it. Below "
        "%d there is no clear trend, so it does not lean either way."
        % (ADX_TREND, ADX_TREND),
        "Supertrend is judged by the direction it currently points.",
        "Moving averages: where the latest close sits against the 20, 50 "
        "and 200 EMA, and whether the 50 EMA is above the 200 EMA.",
        "Every reading is taken separately on the monthly, weekly and "
        "daily candles. Weekly and monthly include the candle that is "
        "still forming.",
        "Company results (when filed): revenue, profit and EPS of the "
        "latest quarter against a year earlier and against the previous "
        "quarter, and of the latest financial year against the year before. "
        "A rise of %g%% or more counts as up, a fall of %g%% or more as "
        "down, anything smaller as little changed. A move from profit to "
        "loss, or the other way, is judged by its direction. EPS is left "
        "out of any comparison where the share count changed, for example "
        "through a bonus issue or split."
        % (FLAT_PCT, FLAT_PCT),
    ]


# =====================================================================
# WHO MAY USE IT
# =====================================================================
# Read through a short cache so /api/auth/me (called by every page load)
# is not an extra database round trip for every signed-in visitor. The
# price is that a grant or a revoke takes up to _ADMIN_TTL seconds to be
# noticed. FAILS CLOSED: an error leaves the list empty, which can only
# ever mean FEWER people get in.
_ADMIN_TTL = 30
_admin_cache = {"ids": frozenset(), "at": None}


def _admin_ids():
    now = time.monotonic()
    at = _admin_cache["at"]
    if at is None or now - at > _ADMIN_TTL:
        try:
            from api import query
            ids = frozenset(r["user_id"] for r in
                            query("SELECT user_id FROM app_admin"))
        except Exception:                                # noqa: BLE001
            ids = frozenset()
        _admin_cache.update(ids=ids, at=now)
    return _admin_cache["ids"]


def _premium_now(user) -> bool:
    """is_premium, not lapsed, and on a tier that covers the STOCK tools
    ("stocks" or "all"; no scope on record means everything) -- WITHOUT
    the paywall-off shortcut."""
    if not user or not user.get("is_premium"):
        return False
    if (user.get("premium_scope") or "all") not in ("stocks", "all"):
        return False
    until = user.get("premium_until")
    if until is None:
        return True
    if hasattr(until, "date"):          # a datetime; a plain date has no .date
        until = until.date()
    return until >= date.today()


def allowed(user) -> bool:
    """May this caller use the tool? Signed in, switch on, and either an
    admin or -- only once MF_VIEWCHECK_ACCESS=premium -- on a tier that
    covers the stock tools."""
    if not VIEWCHECK_ENABLED or not user:
        return False
    if mode() == "premium" and _premium_now(user):
        return True
    return user.get("user_id") in _admin_ids()


# =====================================================================
# THE JUDGING -- pure functions, no database
# =====================================================================
def _f(row, key):
    """A stored value as a float, or None. Decimal and NULL both handled."""
    if not row:
        return None
    v = row.get(key)
    return float(v) if v is not None else None


def _inr(v):
    return "₹{:,.2f}".format(v)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _dmy(iso):
    """'2026-03-02' -> '2 Mar 2026'. Written out rather than strftime, whose
    month names follow the server's locale."""
    y, m, d = (int(x) for x in str(iso)[:10].split("-"))
    return "%d %s %d" % (d, _MONTHS[m - 1], y)


def _side(view, lean):
    """Which side of the person's view does this reading fall on?

    `lean` is what the reading says about the price, independent of anybody's
    opinion: bull, bear or flat. Flat is never forced onto a side -- a
    reading that does not lean is reported as not leaning."""
    if lean == "flat":
        return "mixed"
    return "with" if (lean == "bull") == (view == "bullish") else "against"


def _item(tf, key, lean, title, detail, view):
    return {
        "id": "%s.%s" % (tf.lower(), key),
        "tf": tf,
        "tf_label": TF_LABEL[tf],
        "check": key,
        "group": CHECK_LABEL[key],
        "lean": lean,
        "side": _side(view, lean),
        "title": title,
        "detail": detail,
    }


def _cmp_lean(a, b):
    """bull if a > b, bear if a < b, flat if equal."""
    if a > b:
        return "bull"
    if a < b:
        return "bear"
    return "flat"


def judge_timeframe(tf, cur, prv, view):
    """Every check for one timeframe. Returns (items, skipped).

    `cur` is the latest bar, `prv` the one before it (or None). A check
    whose inputs are missing is SKIPPED with a reason, never defaulted --
    a 200 EMA on a stock with 90 candles is no number, and inventing one
    would put a confident line on a page that has nothing behind it."""
    items, skipped = [], []
    unit = TF_UNIT[tf]

    def skip(key, why):
        skipped.append({"tf": tf, "tf_label": TF_LABEL[tf], "check": key,
                        "group": CHECK_LABEL[key], "why": why})

    close = _f(cur, "close_price")

    # ---- MACD ----------------------------------------------------------
    line, sig = _f(cur, "macd_line"), _f(cur, "macd_signal")
    if line is None or sig is None:
        skip("macd_signal", "MACD is not available for this stock yet")
        skip("macd_dir", "MACD is not available for this stock yet")
    else:
        lean = _cmp_lean(line, sig)
        title = {"bull": "MACD is above its signal line",
                 "bear": "MACD is below its signal line",
                 "flat": "MACD is level with its signal line"}[lean]
        items.append(_item(
            tf, "macd_signal", lean, title,
            "MACD %.2f against a signal line of %.2f (difference %+.2f)."
            % (line, sig, line - sig), view))

        pline = _f(prv, "macd_line")
        if pline is None:
            skip("macd_dir", "no earlier %s candle to compare with" % unit)
        else:
            lean = _cmp_lean(line, pline)
            title = {"bull": "MACD is rising",
                     "bear": "MACD is falling",
                     "flat": "MACD has not moved"}[lean]
            items.append(_item(
                tf, "macd_dir", lean, title,
                "MACD went from %.2f to %.2f since the previous %s candle."
                % (pline, line, unit), view))

    # ---- RSI -----------------------------------------------------------
    rsi = _f(cur, "rsi")
    if rsi is None:
        skip("rsi_zone", "RSI is not available for this stock yet")
        skip("rsi_dir", "RSI is not available for this stock yet")
    else:
        if rsi >= RSI_STRONG:
            lean, title = "bull", "RSI is in its strong zone (%d or above)" % RSI_STRONG
            detail = "RSI is %.1f." % rsi
        elif rsi < RSI_WEAK:
            lean, title = "bear", "RSI is in its weak zone (below %d)" % RSI_WEAK
            detail = "RSI is %.1f." % rsi
        else:
            lean = "flat"
            title = "RSI is in the middle zone (%d to %d)" % (RSI_WEAK, RSI_STRONG)
            detail = ("RSI is %.1f, %s the 50 line but not in either the "
                      "strong or the weak zone."
                      % (rsi, "above" if rsi > 50 else
                         "below" if rsi < 50 else "on"))
        items.append(_item(tf, "rsi_zone", lean, title, detail, view))

        prsi = _f(prv, "rsi")
        if prsi is None:
            skip("rsi_dir", "no earlier %s candle to compare with" % unit)
        else:
            lean = _cmp_lean(rsi, prsi)
            title = {"bull": "RSI is rising", "bear": "RSI is falling",
                     "flat": "RSI has not moved"}[lean]
            items.append(_item(
                tf, "rsi_dir", lean, title,
                "RSI went from %.1f to %.1f since the previous %s candle."
                % (prsi, rsi, unit), view))

        # Only when triggered. A "not stretched" line on every timeframe
        # would be noise in the one list people read for surprises.
        if rsi >= RSI_STRETCH_HI:
            items.append(_item(
                tf, "rsi_stretch", "bear",
                "RSI is above %d, the zone usually called overbought"
                % RSI_STRETCH_HI,
                "RSI is %.1f. Momentum is strong, but readings this high "
                "are commonly followed by a pause or a pullback, although "
                "a strong trend can stay here for a long time." % rsi, view))
        elif rsi <= RSI_STRETCH_LO:
            items.append(_item(
                tf, "rsi_stretch", "bull",
                "RSI is below %d, the zone usually called oversold"
                % RSI_STRETCH_LO,
                "RSI is %.1f. Momentum is weak, but readings this low are "
                "commonly followed by a pause or a bounce, although a "
                "falling stock can stay here for a long time." % rsi, view))

    # ---- trend: ADX / DI -------------------------------------------------
    adx, pdi, mdi = _f(cur, "adx"), _f(cur, "plus_di"), _f(cur, "minus_di")
    if adx is None or pdi is None or mdi is None:
        skip("adx_di", "ADX needs a longer history than this stock has")
    else:
        detail = "ADX %.1f, +DI %.1f, -DI %.1f." % (adx, pdi, mdi)
        if adx >= ADX_TREND:
            if pdi > mdi:
                lean, title = "bull", "A trend is in place and buyers are leading it"
            elif pdi < mdi:
                lean, title = "bear", "A trend is in place and sellers are leading it"
            else:
                lean, title = "flat", "A trend is in place but neither side is leading it"
        else:
            lean = "flat"
            title = "No clear trend yet (ADX below %d)" % ADX_TREND
        items.append(_item(tf, "adx_di", lean, title, detail, view))

    # ---- Supertrend ------------------------------------------------------
    st_dir = cur.get("supertrend_dir") if cur else None
    st_val = _f(cur, "supertrend_value")
    if st_dir not in ("UP", "DOWN"):
        skip("supertrend", "Supertrend is not available for this stock yet")
    else:
        lean = "bull" if st_dir == "UP" else "bear"
        if st_val is None:
            detail = ""
        elif close is None:
            detail = "The Supertrend line is at %s." % _inr(st_val)
        else:
            detail = ("The Supertrend line is at %s; the last close was %s."
                      % (_inr(st_val), _inr(close)))
        items.append(_item(
            tf, "supertrend", lean,
            "Supertrend is pointing %s" % ("up" if st_dir == "UP" else "down"),
            detail, view))

    # ---- price against the moving averages --------------------------------
    for p in EMA_CHECKS:
        key = "ema_%d" % p
        e = _f(cur, key)
        if e is None or close is None:
            skip(key, "not enough %s candles yet for a %d-%s average"
                 % (unit, p, unit))
            continue
        lean = _cmp_lean(close, e)
        word = {"bull": "above", "bear": "below", "flat": "level with"}[lean]
        items.append(_item(
            tf, key, lean,
            "Price is %s the %d-%s EMA" % (word, p, unit),
            "Last close %s against a %d-%s EMA of %s (%+.1f%%)."
            % (_inr(close), p, unit, _inr(e), (close / e - 1) * 100 if e else 0),
            view))

    e50, e200 = _f(cur, "ema_50"), _f(cur, "ema_200")
    if e50 is None or e200 is None:
        skip("ema_50_200", "needs 200 %s candles of history" % unit)
    else:
        lean = _cmp_lean(e50, e200)
        word = {"bull": "above", "bear": "below", "flat": "level with"}[lean]
        items.append(_item(
            tf, "ema_50_200", lean,
            "The 50-%s EMA is %s the 200-%s EMA" % (unit, word, unit),
            "50 EMA %s against 200 EMA %s." % (_inr(e50), _inr(e200)), view))

    return items, skipped


# ---------------------------------------------------------------------
# COMPANY RESULTS
#
# `rows` are stock_financials rows as api.build_financials prepares them
# (floats, newest first), and `h` is the api module, passed in rather than
# imported so this file still loads on its own (api imports this one while
# it is being built). Every comparison goes through h._fin_neighbour,
# h._fin_pct and h._fin_eps_comparable -- the Financials tab's own rules.
# ---------------------------------------------------------------------
_WHEN = {          # (after "against", after "from")
    "yoy": ("a year ago", "a year ago"),
    "qoq": ("in the previous quarter", "the previous quarter"),
    "fy":  ("in the year before", "the year before"),
}
_MEASURE = {"rev": "Revenue", "profit": "Profit", "eps": "EPS"}
_FIELD = {"rev": "revenue", "profit": "profit", "eps": "eps"}

# How a change that is not a plain percentage reads. {w} takes the "against"
# phrase from _WHEN, {f} the "from" one. Revenue never needs these: it is
# only compared when the earlier figure is a positive number.
_SWING = {
    "profit": {
        "to_loss":      "The company has moved into a loss, from a profit {w}",
        "to_zero":      "Profit has fallen to nothing, from a profit {w}",
        "from_loss":    "The company made a profit, after a loss {w}",
        "smaller_loss": "The loss is smaller than {f}",
        "bigger_loss":  "The loss is bigger than {f}",
        "same_loss":    "The loss is unchanged from {f}",
    },
    "eps": {
        "to_loss":      "EPS has turned negative, from positive earnings {w}",
        "to_zero":      "EPS has fallen to zero, from positive earnings {w}",
        "from_loss":    "EPS is positive, after a loss per share {w}",
        "smaller_loss": "The loss per share is smaller than {f}",
        "bigger_loss":  "The loss per share is bigger than {f}",
        "same_loss":    "The loss per share is unchanged from {f}",
    },
}


def _res_text(measure, v, h):
    """A figure in words, losses included."""
    if measure == "rev":
        return h._fin_money(v / 1e7)
    if measure == "profit":
        return h._fin_money(v / 1e7) if v >= 0 else "a loss of " + h._fin_money(-v / 1e7)
    return h._RUPEE + "%.2f" % v if v >= 0 else "a loss of %s%.2f per share" % (h._RUPEE, -v)


def _pct_lean(pct):
    return "bull" if pct >= FLAT_PCT else "bear" if pct <= -FLAT_PCT else "flat"


def _res_lean(measure, now, before, h):
    """(lean, kind, pct) for one measure against an earlier period, or None.

    kind is "pct" for an ordinary percentage change, otherwise a key of
    _SWING. Profit and EPS can be negative, so the direction needs care: a
    percentage of a loss means nothing, and "up" from -50 to -10 is a
    smaller loss, not growth."""
    if now is None or before is None:
        return None
    if measure == "rev" or (before > 0 and now > 0):
        pct = h._fin_pct(now, before)          # None unless the earlier figure is positive
        return None if pct is None else (_pct_lean(pct), "pct", pct)
    if before > 0:                             # and now <= 0
        return "bear", "to_loss" if now < 0 else "to_zero", None
    if now > 0:                                # and before <= 0
        return "bull", "from_loss", None
    if before < 0 and now < 0:
        if now > before:
            return "bull", "smaller_loss", None
        if now < before:
            return "bear", "bigger_loss", None
        return "flat", "same_loss", None
    return None                                # both zero: nothing to say


def judge_results(rows, view, h):
    """Items and skips from the latest quarter and the latest year.

    Returns (items, skipped, meta) -- meta describes the newest quarter on
    file (or the newest year if that is all there is), or None."""
    items, skipped = [], []

    def skip(key, why, label):
        skipped.append({"tf": RESULTS, "tf_label": TF_LABEL[RESULTS], "check": key,
                        "group": CHECK_LABEL[key], "why": "%s: %s" % (label, why)})

    def one(key, measure, r, o, w):
        """Judge one measure of row r against row o; record an item or a skip."""
        label = r["period_label"]
        field = _FIELD[measure]
        now = r[field]
        if now is None:
            skip(key, "not on file", label)
            return
        if o is None:
            skip(key, "no earlier figure on the same basis to compare with", label)
            return
        before = o[field]
        if measure == "eps":
            like = h._fin_eps_comparable(r, o)
            if like is False:
                skip(key, "the share count changed (a bonus or split), so EPS "
                          "is not compared like for like", label)
                return
            if like is None or before is None:
                skip(key, "EPS is missing for one of the two periods", label)
                return
        res = _res_lean(measure, now, before, h)
        if res is None:
            skip(key, "the earlier figure cannot be compared (zero or a loss)", label)
            return
        lean, kind, pct = res
        against_w, from_w = _WHEN[w]
        if kind == "pct":
            title = "%s is %s from %s" % (_MEASURE[measure], h._fin_move(pct), from_w)
        else:
            title = _SWING[measure][kind].format(w=against_w, f=from_w)

        noun = {"rev": "revenue", "eps": "EPS",
                "profit": "profit" if now >= 0 else "result"}[measure]
        detail = "%s %s was %s" % (label, noun, _res_text(measure, now, h))
        if measure == "rev":
            detail += " (%s)" % ("consolidated" if r["consolidated"] else "standalone")
        detail += ", against %s %s" % (_res_text(measure, before, h), against_w)
        if pct is not None:
            detail += " (%s)" % h._fin_move(pct)
        items.append(_item(RESULTS, key, lean, title, detail + ".", view))

    def latest(kind):
        return next((x for x in rows if x["period_type"] == kind), None)

    q, y = latest("quarterly"), latest("yearly")
    if q:
        ya = h._fin_neighbour(rows, q, 350, 380)
        pv = h._fin_neighbour(rows, q, 80, 100)
        for m, key_y, key_q in (("rev", "rev_yoy", "rev_qoq"),
                                ("profit", "profit_yoy", "profit_qoq"),
                                ("eps", "eps_yoy", "eps_qoq")):
            one(key_y, m, q, ya, "yoy")
            one(key_q, m, q, pv, "qoq")
    if y:
        prev_y = h._fin_neighbour(rows, y, 350, 380)
        for m, key in (("rev", "rev_fy"), ("profit", "profit_fy"), ("eps", "eps_fy")):
            one(key, m, y, prev_y, "fy")

    head = q or y
    meta = None
    if head:
        filed = head.get("filed_on")
        meta = {"period_label": head["period_label"],
                "bar_date": str(head["period_end"]),
                "basis": "Consolidated" if head["consolidated"] else "Standalone",
                "filed_on": str(filed) if filed else None,
                "quarterly": head is q,
                "in_progress": False}
    return items, skipped, meta


def _bar_meta(tf, cur, today):
    """When the reading is from, and whether its candle is still forming."""
    d = cur.get("as_of_date") if cur else None
    if d is None:
        return None
    if hasattr(d, "date"):
        d = d.date()
    if tf == "MONTHLY":
        forming = (d.year, d.month) == (today.year, today.month)
    elif tf == "WEEKLY":
        forming = d.isocalendar()[:2] == today.isocalendar()[:2]
    else:
        forming = False             # a daily candle is not "forming" after the close
    return {"bar_date": str(d), "age_days": (today - d).days,
            "in_progress": forming}


def evaluate(view, bars, today=None, jumps=None, fin_rows=None, h=None):
    """The whole answer from already-fetched data. Pure: no I/O.

    bars     -- {"MONTHLY": [latest, previous], "WEEKLY": [...], "DAILY": [...]}
                each bar a dict of stored stock_technical values; a timeframe
                with no bars is absent or empty.
    jumps    -- big one-day moves found in the candles (see _find_jumps).
    fin_rows -- stock_financials rows with the numbers already as floats,
                newest first; None or empty means no results on file.
    h        -- the api module, for the shared _fin_* helpers. Required when
                fin_rows is given.
    """
    if view not in VIEWS:
        raise ValueError("view must be one of %s" % (VIEWS,))
    today = today or date.today()

    items, skipped, meta, notes = [], [], {}, []
    for tf in TIMEFRAMES:
        rows = bars.get(tf) or []
        if not rows:
            skipped.append({
                "tf": tf, "tf_label": TF_LABEL[tf], "check": None,
                "group": "All %s readings" % TF_LABEL[tf].lower(),
                "why": "this stock does not have enough %s candles yet"
                       % TF_UNIT[tf]})
            continue
        cur = rows[0]
        prv = rows[1] if len(rows) > 1 else None
        got, miss = judge_timeframe(tf, cur, prv, view)
        items += got
        skipped += miss
        meta[tf] = _bar_meta(tf, cur, today)

    if fin_rows:
        got, miss, rmeta = judge_results(fin_rows, view, h)
        items += got
        skipped += miss
        if rmeta:
            rmeta["age_days"] = (today - date.fromisoformat(rmeta["bar_date"])).days
            meta[RESULTS] = rmeta

    items.sort(key=lambda i: (ALL_TF.index(i["tf"]), _CHECK_ORDER[i["check"]]))

    # ---- tallies: counts per side, never a combined score -----------------
    def tally(rows):
        t = {"with": 0, "against": 0, "mixed": 0}
        for i in rows:
            t[i["side"]] += 1
        return t

    per_tf = {tf: tally([i for i in items if i["tf"] == tf])
              for tf in ALL_TF if tf in meta}
    totals = tally(items)

    # ---- notes the reader needs in order to read the rest correctly -------
    d = meta.get("DAILY")
    if d and d["age_days"] > STALE_DAYS:
        notes.append(
            "The latest daily reading is from %s, %d days ago, so the daily "
            "points may not reflect the most recent sessions."
            % (_dmy(d["bar_date"]), d["age_days"]))
    forming = [TF_LABEL[tf].lower() for tf in ("WEEKLY", "MONTHLY")
               if meta.get(tf) and meta[tf]["in_progress"]]
    if forming:
        notes.append(
            "The %s reading%s include%s the candle that is still forming, so "
            "%s can change before it closes."
            % (" and ".join(forming), "s" if len(forming) > 1 else "",
               "" if len(forming) > 1 else "s",
               "they" if len(forming) > 1 else "it"))
    if jumps:
        j = jumps[0]
        notes.append(
            "On %s this stock's price moved from %s to %s in a single day "
            "(%+.0f%%). That is often a split, bonus or demerger. FinChaya's "
            "price history is not adjusted for those, so the averages and "
            "MACD of this stock may be bent by it until the move ages out."
            % (_dmy(j["date"]), _inr(j["from"]), _inr(j["to"]), j["pct"]))
    r = meta.get(RESULTS)
    if r and r["quarterly"] and r["age_days"] > RESULTS_STALE_DAYS:
        notes.append(
            "The newest quarterly results we have are for %s (quarter ended %s). "
            "The company may have reported more recently than that."
            % (r["period_label"], _dmy(r["bar_date"])))
    if skipped:
        notes.append(
            "%d reading%s could not be taken and %s listed at the bottom "
            "rather than guessed."
            % (len(skipped), "" if len(skipped) == 1 else "s",
               "is" if len(skipped) == 1 else "are"))

    return {
        "view": view,
        "items": items,
        "skipped": skipped,
        "per_timeframe": per_tf,
        "totals": totals,
        "as_of": meta,
        "notes": notes,
        "no_data": not items,
    }


# =====================================================================
# THE DATABASE
# =====================================================================
# One row per PERIOD, newest first -- fetch_technicals.py writes a row every
# time it runs, so the current month exists several times over (25, 26 and
# 28 Oct are three snapshots of the SAME October candle). "Last two rows"
# would compare October with October and call every indicator flat; the
# DISTINCT ON collapses the snapshots so "previous" means the month before.
# Same query as api._period_bars, kept here so this file has no import of
# api at load time (api imports this one while it is still being built).
_BARS = """
    WITH stamped AS (
        SELECT t.*,
               CASE
                 WHEN %(tf)s = 'MONTHLY' THEN date_trunc('month', t.as_of_date)
                 WHEN %(tf)s = 'WEEKLY'  THEN date_trunc('week',  t.as_of_date)
                 ELSE t.as_of_date::timestamp
               END AS period_key
        FROM stock_technical t
        WHERE t.isin = %(isin)s AND t.timeframe = %(tf)s
    ),
    one_per_period AS (
        SELECT DISTINCT ON (period_key) *
        FROM stamped
        ORDER BY period_key DESC, as_of_date DESC
    )
    SELECT * FROM one_per_period ORDER BY period_key DESC LIMIT 2
"""

_JUMPS = """
    SELECT as_of_date, prev_close, close FROM (
        SELECT as_of_date, close::float AS close,
               LAG(close::float) OVER (ORDER BY as_of_date) AS prev_close
        FROM stock_ohlc_daily
        WHERE isin = %(isin)s AND as_of_date >= %(since)s
    ) x
    WHERE prev_close > 0
      AND (close / prev_close <= %(lo)s OR close / prev_close >= %(hi)s)
    ORDER BY as_of_date DESC
    LIMIT 3
"""


def _find_jumps(query, isin, today):
    rows = query(_JUMPS, {"isin": isin,
                          "since": today - timedelta(days=JUMP_LOOKBACK_DAYS),
                          "lo": JUMP_DOWN, "hi": JUMP_UP})
    return [{"date": str(r["as_of_date"]),
             "from": float(r["prev_close"]), "to": float(r["close"]),
             "pct": (float(r["close"]) / float(r["prev_close"]) - 1) * 100}
            for r in rows]


def _results_rows(h, isin):
    """The same rows the Financials tab reads (see api.stock_financials):
    only periods that have figures, newest first. Goes through
    query_optional, which logs loudly and returns [] if the table or the
    filed_on column is missing -- a stock with no results on file, and a
    server where they have not been loaded yet, both just show no results
    rather than losing the chart half of the page."""
    filed = "filed_on" if h._fin_has_filed_on() else "NULL::date AS filed_on"
    return h.query_optional(f"""
        SELECT period_type, period_end, period_label, revenue, profit, eps, debt,
               consolidated, {filed}
        FROM stock_financials
        WHERE isin = %(isin)s AND (revenue IS NOT NULL OR profit IS NOT NULL)
        ORDER BY period_end DESC
    """, {"isin": isin})


def _prepare(h, db_rows):
    """Numbers as floats, as build_financials does for itself."""
    return [{**r, "revenue": h._fin_f(r["revenue"]), "profit": h._fin_f(r["profit"]),
             "eps": h._fin_f(r["eps"]), "debt": h._fin_f(r["debt"])} for r in db_rows]


def results_block(h, db_rows, today):
    """What the page shows as its table of results. Built by the Financials
    tab's own build_financials, so the figures and the changes are identical
    to the ones on that tab."""
    fin = h.build_financials(db_rows, today=today)
    q = fin["summary"]["quarter"]
    return {
        "available": bool(db_rows),
        "quarterly": fin["quarterly"],          # the latest four
        "yearly": fin["yearly"][:2],            # the latest two
        "headline": q["headline"] if q else None,
        "title": q["title"] if q else None,
    }


# =====================================================================
# THE PERSON'S OWN RULES (My trade plan), checked first
# =====================================================================
# Which of this page's readings a plan's conditions already cover, as {(check key, timeframe key)}. A reading that is covered is still shown (it is part of the
# picture) but marked "also in your rules"; a reading that is not covered is the extra information: the rules come first, this is what else there is to see.
_TF_OF = {"D": "DAILY", "W": "WEEKLY", "M": "MONTHLY"}


def rules_cover(plan):
    out = set()
    for c in plan.get("conditions", []):
        tf, ik, p = _TF_OF.get(c["tf"]), c["indicator"], c.get("params") or {}
        if ik == "macd" and tf:
            out |= {("macd_signal", tf), ("macd_dir", tf)}
        elif ik == "rsi" and tf:
            out |= {("rsi_zone", tf), ("rsi_dir", tf), ("rsi_stretch", tf)}
        elif ik == "adx" and tf:
            out.add(("adx_di", tf))
        elif ik == "supertrend" and tf:
            out.add(("supertrend", tf))
        elif ik == "ema" and tf and p.get("period") in EMA_CHECKS:
            out.add(("ema_%d" % p["period"], tf))
        elif ik == "ema_cross" and tf and (p.get("fast"), p.get("slow")) == (50, 200):
            out.add(("ema_50_200", tf))
        elif ik in ("revenue", "profit", "eps"):
            pre = {"revenue": "rev", "profit": "profit", "eps": "eps"}[ik]
            out |= {("%s_%s" % (pre, k), RESULTS) for k in ("yoy", "qoq", "fy")}
    return out


def _my_rules(query, user, plan_id, isin, view):
    """The person's own saved plan, checked on this stock for this view: the block the page shows FIRST. Never raises for a plan that cannot be used (the answer says
    why); somebody else's plan is a 404 (as everywhere in My trade plan). Returns (block, plan or None)."""
    try:
        import tradeplan_api as tp
        import tradeplan_engine as te
    except ImportError:
        return {"available": False, "reason": "My trade plan is not on this server yet, so your own rules cannot be checked here."}, None
    if not tp.allowed(user):
        return {"available": False, "reason": "Your own rules are part of My trade plan, which is not open to your account yet."}, None
    row = tp._load_plan(query, user, plan_id)                  # 400 / 404
    try:
        plan = te.clean_plan(tp._as_plan(row))
    except te.SpecError as e:
        return {"available": True, "plan_id": row["plan_id"], "error": "This plan cannot be used any more: %s" % e}, None
    if plan["instrument"] != "stock":
        return {"available": True, "plan_id": row["plan_id"], "error": "This plan is written for %s, not for a stock, so it cannot be checked on one here."
                % (plan["instrument_label"] or next(i["label"] for i in te.INSTRUMENTS if i["key"] == plan["instrument"]))}, None
    block = tp.run_check(query, user, {"plan_id": row["plan_id"], "isin": isin, "view": view})
    block.update(available=True, plan_id=row["plan_id"])
    return block, plan


@router.get("/{isin}")
def check_view(isin: str, request: Request,
               view: str = Query(..., description="bullish or bearish"),
               plan_id: Optional[int] = Query(None, description="one of your saved trade plans: your own rules are checked first")):
    # Lazy: api.py imports this module while it is still being built.
    import api as h
    from api import current_user, latest_prices, query

    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in to use this.")
    if not allowed(user):
        # 403, not 404: the person is signed in and the page tells them why.
        # Say who it IS for: while it is administrators-only (the default) "premium members" would tell a paying member
        # they are entitled to something they are being refused.
        raise HTTPException(
            403, "This is available to %s." % (
                "subscribers to the Stocks plan and administrators" if mode() == "premium" else "administrators"))

    view = (view or "").strip().lower()
    if view not in VIEWS:
        raise HTTPException(400, "view must be 'bullish' or 'bearish'.")
    isin = (isin or "").strip().upper()
    if not ISIN_RE.match(isin):
        raise HTTPException(400, "That does not look like an ISIN.")

    stock = query("""
        SELECT isin, symbol, company_name, sector, is_active
        FROM stock_master WHERE isin = %(isin)s
    """, {"isin": isin}, one=True)
    if not stock:
        raise HTTPException(404, "No stock with that ISIN.")

    today = date.today()
    bars = {tf: query(_BARS, {"isin": isin, "tf": tf}) for tf in TIMEFRAMES}
    # The results half must not take the chart half down with it. A failure
    # here is logged where systemd keeps it (journalctl -u mfapi) and the page
    # says the results could not be loaded -- which is not the same statement
    # as "this company has none on file", so it is flagged separately.
    try:
        db_rows = _results_rows(h, isin)
        fin_rows = _prepare(h, db_rows)
        fin_block = results_block(h, db_rows, today)
    except Exception as e:                                    # noqa: BLE001
        print("mfapi: check-your-view could not read company results for %s "
              "(%s: %s). The chart readings are unaffected."
              % (isin, type(e).__name__, str(e).split("\n")[0]), flush=True)
        fin_rows = None
        fin_block = {"available": False, "error": True, "quarterly": [],
                     "yearly": [], "headline": None, "title": None}

    out = evaluate(view, bars, today=today, jumps=_find_jumps(query, isin, today),
                   fin_rows=fin_rows, h=h)

    # The person's own rules, when they asked for them: checked on the same stock for the same view and returned beside the readings, which are marked
    # where a rule of theirs already covers them. A rule that cannot be checked is said, in words; it never takes the readings down with it.
    plan = None
    if plan_id is not None:
        try:
            out["my_rules"], plan = _my_rules(query, user, plan_id, isin, view)
        except HTTPException as e:
            if e.status_code in (404, 400):
                raise
            out["my_rules"] = {"available": True, "plan_id": plan_id, "error": str(e.detail)}
        except Exception as e:                                # noqa: BLE001
            print("mfapi: check-your-view could not check the person's own rules (%s: %s)" % (type(e).__name__, str(e).split("\n")[0]), flush=True)
            out["my_rules"] = {"available": True, "plan_id": plan_id, "error": "Your rules could not be checked just now. The readings below are unaffected."}
    covered = rules_cover(plan) if plan else set()
    for i in out["items"]:
        i["in_rules"] = (i["check"], i["tf"]) in covered

    px = latest_prices([isin]).get(isin) or {}
    out["stock"] = stock
    out["financials"] = fin_block
    out["price"] = {"last": px.get("close"), "date": px.get("date"),
                    "change_pct": px.get("change_pct")}
    out["checks"] = [{"key": k, "label": v} for k, v in CHECKS]
    out["rules"] = rules_text()
    out["timeframes"] = [{"key": tf, "label": TF_LABEL[tf]} for tf in TIMEFRAMES]
    return out
