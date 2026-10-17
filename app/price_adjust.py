"""
price_adjust.py -- split and bonus adjustment of daily candles.
---------------------------------------------------------------------------
A VERBATIM COPY of these helpers from api.py (the stock-page price chart):

    action_factor, collect_actions, confirm_actions, adjust_bars, _fmt_day

WHY A COPY. The back-test (backtest_api.py) needs them, and api.py on the
live server may not have them yet: the price chart that introduced them is
deployed separately. Importing them from api.py would make the back-test fail
at its first run on any server whose api.py is older. This file is new and
stands alone, so the back-test can be deployed by itself.

HOW IT IS KEPT HONEST. backtest_demo/test_http.py runs the same inputs through
api.py's version and this one and fails if they ever differ, so a fix made in
one place and not the other is caught. When api.py with the chart is live
everywhere, api.py can import these from here instead and the copy goes away.

WHAT THEY DO (see the long note above build_prices in api.py): a split or
bonus is divided out of the earlier prices ONLY where the prices themselves
confirm it, because stored candles are a mix of adjusted and unadjusted.
"""

import math
import re
from datetime import timedelta

_ACTION_SKIP = re.compile(r"ncrps|preference|debenture|warrant|\bncd", re.I)
_SPLIT_RE = re.compile(r"From\s+R(?:s|e)s?\.?\s*([\d.]+)\s*/?-?\s*(?:Per Share|each)?\s*To\s+R(?:s|e)s?\.?\s*([\d.]+)", re.I)
_BONUS_RE = re.compile(r"(\d+)\s*:\s*(\d+)")


_EX_DATE_SLACK_DAYS = 3             # how far from the listed ex-date the price drop may sit


def action_factor(kind, headline):
    """How many old shares one old share became: 2.0 for a 1:1 bonus or a Rs10 -> Rs5 split, 0.1 for a
    1-for-10 consolidation. None when the headline gives no usable ratio or is not about equity shares."""
    h = headline or ""
    if _ACTION_SKIP.search(h):
        return None
    try:
        if kind == "split":
            m = _SPLIT_RE.search(h)
            old, new = (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)
            f = old / new if old > 0 and new > 0 else None
        elif kind == "bonus":
            m = _BONUS_RE.search(h)
            a, b = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            f = (a + b) / b if a > 0 and b > 0 else None
        else:
            return None
    except ValueError:
        return None
    return f if f and abs(f - 1) > 1e-6 and 0.001 < f < 1000 else None


def _action_label(kind, headline):
    h = headline or ""
    if kind == "split":
        m = _SPLIT_RE.search(h)
        if m:
            return "split, face value Rs %s to Rs %s" % (m.group(1).rstrip("."), m.group(2).rstrip("."))
        return "share split"
    m = _BONUS_RE.search(h)
    return "bonus issue %s:%s" % (m.group(1), m.group(2)) if m else "bonus issue"


def collect_actions(event_rows):
    """[{"date", "factor", "kind", "label"}] from stock_event rows, oldest first. The same action is on file
    from BSE and from NSE; two readings of one day with (nearly) the same ratio count once."""
    out = []
    for e in event_rows:
        f = action_factor(e["kind"], e["headline"])
        if f is None:
            continue
        if any(o["date"] == e["event_date"] and o["kind"] == e["kind"] and abs(o["factor"] / f - 1) < 0.01
               for o in out):
            continue
        out.append({"date": e["event_date"], "factor": f, "kind": e["kind"],
                    "label": _action_label(e["kind"], e["headline"])})
    return sorted(out, key=lambda a: (a["date"], a["factor"]))


def _ratio_confirms(f, observed):
    """Does an observed fall of `observed` (close before / close on the day) fit a ratio of `f`? Prices move on
    the day as well, so the match is loose: within roughly a third of the ratio for a big one, and a
    clear step of at least 40% of the way for a small one such as a 1:4 bonus (1.25). A ratio in the 1.5 to 1.85 range (a 1:2 bonus
    is 1.5) must show a real fall (at least 1.2): 0.65 x 1.5 is 0.975, under which a price that did not fall at all would "confirm" it."""
    if observed is None or observed <= 0:
        return False
    if f < 1:
        return _ratio_confirms(1 / f, 1 / observed)
    if f >= 1.5:
        return max(0.65 * f, 1.2) <= observed <= 1.5 * f
    return observed >= 1 + 0.4 * (f - 1)


def confirm_actions(actions, bars):
    """Split `actions` into those the prices confirm and those they do not.
    `bars`: [{"d": date, "close": float}, ...] oldest first.
    Returns (applied, unconfirmed); an applied item has "idx" (the first bar at the new, lower price),
    "date" (that bar's date), "factor" and "label"."""
    n = len(bars)
    applied, unconfirmed, used = [], [], set()

    def observed(j):
        prev, cur = bars[j - 1]["close"], bars[j]["close"]
        return prev / cur if prev and cur and prev > 0 and cur > 0 else None

    def best_bar(day, f):
        near = [j for j in range(1, n) if abs((bars[j]["d"] - day).days) <= _EX_DATE_SLACK_DAYS
                and j not in used and observed(j) is not None]
        if not near:
            return None
        return min(near, key=lambda j: (abs(math.log(observed(j) / f)), abs((bars[j]["d"] - day).days)))

    groups = {}
    for a in actions:
        groups.setdefault(a["date"], []).append(a)
    for day in sorted(groups):
        items = groups[day]
        if n < 2 or day <= bars[0]["d"] or day > bars[-1]["d"] + timedelta(days=_EX_DATE_SLACK_DAYS):
            unconfirmed.extend(dict(a, why="outside the price history") for a in items)
            continue
        product = 1.0
        for a in items:
            product *= a["factor"]
        # the whole day's actions together (a split and a bonus can share an ex-date), else the best single one
        tries = [(product, items)] + ([(a["factor"], [a]) for a in items] if len(items) > 1 else [])
        done = None
        for f, part in tries:
            j = best_bar(day, f)
            if j is not None and _ratio_confirms(f, observed(j)):
                done = (j, f, part)
                break
        if done is None:
            unconfirmed.extend(dict(a, why="prices do not show it") for a in items)
            continue
        j, f, part = done
        used.add(j)
        applied.append({"idx": j, "date": bars[j]["d"], "factor": f,
                        "label": " and ".join(a["label"] for a in part), "kind": part[0]["kind"]})
        unconfirmed.extend(dict(a, why="not the one the prices show") for a in items if a not in part)
    return sorted(applied, key=lambda a: a["idx"]), unconfirmed


def _fmt_day(d):
    return "%d %s" % (d.day, d.strftime("%b %Y"))


def adjust_bars(rows, applied):
    """Prices divided, and volumes multiplied, by every applied ratio that came AFTER the bar.
    rows: [{"d", "open", "high", "low", "close", "volume"}, ...] oldest first."""
    out = [None] * len(rows)
    factor = 1.0
    cut = {a["idx"]: a["factor"] for a in applied}
    for i in range(len(rows) - 1, -1, -1):
        if i + 1 in cut:
            factor *= cut[i + 1]
        r = rows[i]
        out[i] = {"d": r["d"], "factor": factor,
                  "open": r["open"] / factor if r["open"] is not None else None,
                  "high": r["high"] / factor if r["high"] is not None else None,
                  "low": r["low"] / factor if r["low"] is not None else None,
                  "close": r["close"] / factor if r["close"] is not None else None,
                  "volume": int(round(r["volume"] * factor)) if r["volume"] is not None else None}
    return out
