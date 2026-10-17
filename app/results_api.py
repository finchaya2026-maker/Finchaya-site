"""
results_api.py -- the quarterly-results page (results.html).

WHAT IT SHOWS
    Three views, Monday to Sunday in Indian time:
      this_week   results filed this week, plus the companies still due later this week
      last_week   results filed last week
      upcoming    companies whose board meeting to consider results is in the weeks after this one
    For a company that has filed: Sales, Operating profit (Profit before tax for banks, finance companies and
    insurers), Net profit and EPS, for the quarter just reported, the quarter before and the same quarter a year
    earlier, with the change against each; market cap; and P/E and PEG with an arrow for which way they moved.

WHERE EVERY NUMBER COMES FROM
    Figures: stock_financials, as the company filed them with BSE (result_watcher.py). Filing day: filed_on.
    Dates of results still to come: stock_event, kind 'result' (the exchanges' board-meeting notices).
    Price: the latest close in stock_ohlc_daily. P/E, PEG and market cap: api.build_ratios, the SAME working as the
    stock page, so the two can never disagree.

THE ARROWS
    Growth arrows say which way a figure moved against the earlier quarter. P/E and PEG arrows compare the ratio
    NOW with the ratio BEFORE these results, both at TODAY's price, so they move only because earnings changed:
    a P/E that falls after strong earnings is an arrow down. An arrow is direction, never a verdict: nothing here
    says a result is good or bad, and nothing is a recommendation (FinChaya is not a SEBI-registered research
    analyst). On the page, growth in sales, profit and EPS follows the site's own rule (green for a positive figure,
    red for a negative one, always with the arrow and sign as well), while the P/E and PEG arrows are a neutral
    dark colour because a higher P/E is not "good" or "bad".

WHEN A FIGURE IS MISSING IT SAYS SO
    No earlier quarter on the same basis (consolidated against consolidated), a loss, or a change in share count
    (bonus, split) means no percentage, and the card shows a short word or a dash with the reason, never a made-up
    number. Operating profit is filled in by result_watcher.py a little after the result; until then that row is
    dashes.

ACCESS
    FOR SUBSCRIBERS: members on a plan that covers stocks (Stocks, or Mutual funds + stocks; not lapsed) and
    administrators. stock_access.py decides, on every call, and does NOT look at the paywall switch: this page was
    asked for as "subscription users only", so it is shut to everyone else even while the rest of the site is free.
        GET /api/results/access   may I see it? (never an error: the page asks this first)
        GET /api/results          the cards (401 not signed in, 403 no Stocks plan)
    MF_RESULTS_ACCESS=signed_in | everyone | admin widens or narrows it (default premium);
    MF_RESULTS_ENABLED=false switches the page's API off for everybody.

DEPENDS ON api.py for the ratio working (imported when a request arrives, so a missing or older api.py costs the
ratios and nothing else) and on the columns operating_profit / profit_before_tax of stock_financials, which it
works without (add_stock_financials_operating.py adds them).
"""

import os
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import dict_row

import stock_access
from portfolio_api import DB

router = APIRouter(prefix="/api/results", tags=["results"])
GATE = stock_access.Gate("MF_RESULTS_ACCESS", "MF_RESULTS_ENABLED")

IST = timezone(timedelta(hours=5, minutes=30))
VIEWS = ("this_week", "last_week", "upcoming")
MAX_CARDS = 1200                    # results filed in one week peak at about 1,000 in a season
UPCOMING_DAYS_DEFAULT = 21
UPCOMING_DAYS_MAX = 60
HISTORY_DAYS = 1130                 # three years and a month of quarters: enough for P/E (4) and PEG (8)
CACHE_SECONDS = 60
FLAT_BELOW_PCT = 0.05               # a change that rounds to 0.0% is "little changed"
RATIO_FLAT_PCT = 0.5                # a P/E or PEG that moved less than this is "little changed"
QOQ_DAYS = (80, 100)                # the quarter before: about 91 days earlier
YOY_DAYS = (350, 380)               # the same quarter a year earlier



# =====================================================================================================
# Pure functions (no database, no web framework): tested on their own
# =====================================================================================================
def week_bounds(today):
    """(Monday, Sunday) of the week that contains `today`."""
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def window_for(view, today, days=UPCOMING_DAYS_DEFAULT):
    """(from, to, label) for a view. Weeks run Monday to Sunday."""
    mon, sun = week_bounds(today)
    if view == "this_week":
        a, b = mon, sun
    elif view == "last_week":
        a, b = mon - timedelta(days=7), mon - timedelta(days=1)
    elif view == "upcoming":
        a, b = sun + timedelta(days=1), sun + timedelta(days=days)
    else:
        raise ValueError("unknown view %r" % view)
    return a, b, _span(a, b)


def _span(a, b):
    if a.year == b.year:
        if a.month == b.month:
            return "%s %d to %s %d %s" % (a.strftime("%a"), a.day, b.strftime("%a"), b.day, "%s %d" % (b.strftime("%b"), b.year))
        return "%s %d %s to %s %d %s %d" % (a.strftime("%a"), a.day, a.strftime("%b"), b.strftime("%a"), b.day, b.strftime("%b"), b.year)
    return "%s %d %s %d to %s %d %s %d" % (a.strftime("%a"), a.day, a.strftime("%b"), a.year, b.strftime("%a"), b.day, b.strftime("%b"), b.year)


def quarter_end_before(d):
    """The latest calendar quarter end strictly before `d`: the quarter a result announced on `d` is about."""
    ends = [date(d.year - 1, 12, 31), date(d.year, 3, 31), date(d.year, 6, 30), date(d.year, 9, 30), date(d.year, 12, 31)]
    return max(e for e in ends if e < d)


def split_due(due, hist, today):
    """Sort companies that have a results date into (still to come, awaited). Each row of `due` needs isin and event_date;
    `hist` maps isin -> the stored quarterly rows (each with period_end). A company whose figures for that quarter are
    already stored is neither (it is among the published ones). The date has not passed (today counts) -> still to come;
    the date has passed and no figures are on file -> awaited. The Upcoming list and the Upcoming tab's badge both use
    this, so the number on the tab is always the number in the list."""
    scheduled, awaited = [], []
    for s in due:
        s["_done"] = any(h["period_end"] == quarter_end_before(s["event_date"]) for h in hist.get(s["isin"], []))
        if not s["_done"]:
            (scheduled if s["event_date"] >= today else awaited).append(s)
    return scheduled, awaited


def _f(v):
    return None if v is None else float(v)


_NUMERIC = ("revenue", "profit", "eps", "operating_profit", "profit_before_tax")


def num_row(r):
    """A database row with its numeric columns as floats (the driver hands back Decimal)."""
    return {**r, **{c: _f(r.get(c)) for c in _NUMERIC if c in r}}


def _cr(v):
    """Rupees to crore, two decimals."""
    return None if v is None else round(float(v) / 1e7, 2)


def neighbour(rows, r, lo_days, hi_days):
    """The row of the same kind and basis whose period ended between lo and hi days before r's. `rows` are quarterly."""
    for o in rows:
        if o is r or o["consolidated"] != r["consolidated"]:
            continue
        if lo_days <= (r["period_end"] - o["period_end"]).days <= hi_days:
            return o
    return None


def growth(now, base):
    """How `now` moved against `base`: {"pct", "dir", "note"}. pct only where a percentage means something (the earlier
    figure was positive); across a loss it is a short word instead. dir is "up", "down", "flat" or None (no comparison)."""
    none = {"pct": None, "dir": None, "note": None}
    if now is None or base is None:
        return none
    if base > 0:
        pct = round((now - base) / base * 100, 1)
        d = "flat" if abs(pct) < FLAT_BELOW_PCT else "up" if pct > 0 else "down"
        return {"pct": pct, "dir": d, "note": ("went into a loss" if now < 0 else None)}
    if base < 0:
        if now > 0:
            return {"pct": None, "dir": "up", "note": "back in profit"}
        if now == 0:
            return {"pct": None, "dir": "up", "note": "break-even"}
        if now > base:
            return {"pct": None, "dir": "up", "note": "loss narrowed"}
        if now < base:
            return {"pct": None, "dir": "down", "note": "loss widened"}
        return {"pct": None, "dir": "flat", "note": "same loss"}
    # base == 0
    if now > 0:
        return {"pct": None, "dir": "up", "note": "from nil"}
    if now < 0:
        return {"pct": None, "dir": "down", "note": "went into a loss"}
    return {"pct": None, "dir": "flat", "note": None}


def ratio_trend(now, before):
    """{"value", "before", "change_pct", "dir"} for a ratio now and before these results; dir None unless both exist."""
    out = {"value": now, "before": before, "change_pct": None, "dir": None}
    if now is None or before is None or before == 0:
        return out
    ch = (now - before) / abs(before) * 100
    out["change_pct"] = round(ch, 1)
    out["dir"] = "flat" if abs(ch) < RATIO_FLAT_PCT else "up" if ch > 0 else "down"
    return out


def _metric(key, label, unit, now, prev, ya, state_prev="ok", state_ya="ok"):
    """One row of the table. state_*: "ok" (compare), "changed" (share count changed: EPS not compared, and says why)
    or None (cannot tell: not compared). Only EPS passes anything but "ok"."""
    def g(base, state):
        if state == "changed":
            return {"pct": None, "dir": None, "note": "share count changed"}
        if state is None and key == "eps":
            return {"pct": None, "dir": None, "note": None}
        return growth(now, base)
    return {"key": key, "label": label, "unit": unit, "now": now, "prev": prev, "year_ago": ya,
            "qoq": g(prev, state_prev), "yoy": g(ya, state_ya)}


def _margin(num, den):
    return round(num / den * 100, 1) if num is not None and den is not None and den > 0 else None


def build_card(r, hist, price, events, bs_rows, face_value, cap_class, api, today):
    """One company's card. `r` is the result row (rupees), `hist` all its stored quarterly rows newest first (rupees),
    `price` {"close","date","change_pct"} or None, `events` split/bonus rows, `bs_rows` balance sheets, `api` the
    module that owns the ratio working (api.py). Everything comes back JSON-ready, money in crore."""
    qoq = neighbour(hist, r, *QOQ_DAYS)
    yoy = neighbour(hist, r, *YOY_DAYS)
    like = api._fin_eps_comparable

    # operating profit for an ordinary company, profit before tax for a bank / finance company / insurer
    kind = "pbt" if r.get("operating_profit") is None and r.get("profit_before_tax") is not None else "operating"
    col = "profit_before_tax" if kind == "pbt" else "operating_profit"

    def val(row, c):
        return _cr(row.get(c)) if row else None

    def eps_state(other):
        """EPS is compared only when the share count is the same (a bonus issue or split changes what EPS means)."""
        if other is None or r.get("eps") is None or other.get("eps") is None:
            return None
        ok = like(r, other)
        return "ok" if ok is True else "changed" if ok is False else None

    metrics = [
        _metric("sales", "Sales", "cr", val(r, "revenue"), val(qoq, "revenue"), val(yoy, "revenue")),
        _metric("operating", "Operating profit" if kind == "operating" else "Profit before tax", "cr",
                val(r, col), val(qoq, col), val(yoy, col)),
        _metric("profit", "Net profit", "cr", val(r, "profit"), val(qoq, "profit"), val(yoy, "profit")),
        _metric("eps", "EPS", "rs", _f(r.get("eps")), _f(qoq.get("eps")) if qoq else None, _f(yoy.get("eps")) if yoy else None,
                eps_state(qoq), eps_state(yoy)),
    ]
    margins = None
    if kind == "operating" and r.get("operating_profit") is not None:
        rev, prev_rev = _f(r.get("revenue")), _f(yoy.get("revenue")) if yoy else None
        opm_now, opm_ya = _margin(_f(r["operating_profit"]), rev), _margin(_f(yoy.get("operating_profit")) if yoy else None, prev_rev)
        npm_now, npm_ya = _margin(_f(r.get("profit")), rev), _margin(_f(yoy.get("profit")) if yoy else None, prev_rev)
        def pp(a, b):
            return None if a is None or b is None else round(a - b, 1)
        margins = {"operating": {"now": opm_now, "year_ago": opm_ya, "change_pp": pp(opm_now, opm_ya)},
                   "net": {"now": npm_now, "year_ago": npm_ya, "change_pp": pp(npm_now, npm_ya)}}

    # ratios: now, and before these results at the same price
    fin = [dict(h, period_type="quarterly") for h in hist]
    before = [h for h in fin if h["period_end"] < r["period_end"]]
    now_r = api.build_ratios(fin, events, price, today=today, bs_rows=bs_rows, face_value=face_value)
    pre_r = api._build_pe_peg(before, events, price, today=today)

    def pick(name):
        a, b = now_r.get(name) or {}, pre_r.get(name) or {}
        t = ratio_trend(a.get("value"), b.get("value"))
        t["reason"] = a.get("reason")
        t["detail"] = a.get("detail")
        t["before_reason"] = b.get("reason")
        return t
    ratios = {"pe": pick("pe"), "peg": pick("peg"), "eps_ttm": pick("eps_ttm")}
    mcap = (now_r.get("market_cap") or {})
    try:
        headline = api._fin_headline({**r, "revenue": _f(r.get("revenue")), "profit": _f(r.get("profit")), "eps": _f(r.get("eps"))},
                                     _plain(yoy), _plain(qoq))
    except Exception:                                  # noqa: BLE001  the headline is a nicety; never lose the card over it
        headline = None
    filed = r.get("filed_on")
    return {
        "isin": r["isin"], "symbol": r.get("symbol"), "name": r.get("company_name"), "sector": r.get("sector"),
        "cap": cap_class,
        "filed_on": str(filed) if filed else None,
        "days_since_filed": (today - filed).days if filed else None,
        "days_after_quarter": (filed - r["period_end"]).days if filed else None,      # the page notes a filing made long after the quarter ended
        "period_label": r["period_label"], "period_end": str(r["period_end"]),
        "basis": "Consolidated" if r["consolidated"] else "Standalone",
        "prev_label": qoq["period_label"] if qoq else None, "year_ago_label": yoy["period_label"] if yoy else None,
        "prev_end": str(qoq["period_end"]) if qoq else None, "year_ago_end": str(yoy["period_end"]) if yoy else None,
        "metrics": metrics, "margins": margins, "headline": headline,
        "market_cap_cr": mcap.get("value"), "market_cap_note": mcap.get("detail"),
        "price": ({"close": price["close"], "date": str(price["date"]), "change_pct": price.get("change_pct")} if price else None),
        "ratios": ratios,
        "operating_kind": kind,
    }


def _plain(row):
    return None if row is None else {**row, "revenue": _f(row.get("revenue")), "profit": _f(row.get("profit")), "eps": _f(row.get("eps"))}


def build_scheduled(m, event_date, hist, price, events, bs_rows, face_value, cap_class, api, today):
    """One company that has a results date coming: who it is, when, what it is worth, and what it reported last time."""
    fin = [dict(h, period_type="quarterly") for h in hist]
    now_r = api.build_ratios(fin, events, price, today=today, bs_rows=bs_rows, face_value=face_value) if fin or price else {}
    last = hist[0] if hist else None
    ya = neighbour(hist, last, *YOY_DAYS) if last else None
    last_info = None
    if last:
        last_info = {"label": last["period_label"], "period_end": str(last["period_end"]),
                     "sales_cr": _cr(last.get("revenue")), "profit_cr": _cr(last.get("profit")),
                     "sales_yoy": growth(_cr(last.get("revenue")), _cr(ya.get("revenue")) if ya else None),
                     "profit_yoy": growth(_cr(last.get("profit")), _cr(ya.get("profit")) if ya else None)}
    pe = (now_r.get("pe") or {}).get("value") if now_r else None
    return {
        "isin": m["isin"], "symbol": m.get("symbol"), "name": m.get("company_name"), "sector": m.get("sector"), "cap": cap_class,
        "event_date": str(event_date), "days_away": (event_date - today).days,
        "market_cap_cr": ((now_r.get("market_cap") or {}).get("value") if now_r else None), "pe": pe, "last": last_info,
        "price": ({"close": price["close"], "date": str(price["date"]), "change_pct": price.get("change_pct")} if price else None),
    }


def summarise(cards):
    """Facts about a list of cards, for the strip above them: how many, and how many grew against a year earlier."""
    def moved(card, key):
        m = next((x for x in card["metrics"] if x["key"] == key), None)
        return m["yoy"]["dir"] if m else None
    n = len(cards)
    out = {"total": n}
    for key, name in (("sales", "sales"), ("profit", "profit")):
        dirs = [moved(c, key) for c in cards]
        out[name + "_up"] = sum(1 for d in dirs if d == "up")
        out[name + "_down"] = sum(1 for d in dirs if d == "down")
        out[name + "_compared"] = sum(1 for d in dirs if d is not None)
    return out


NOTES = [
    "Figures are as each company filed them with the stock exchange, in rupees crore. Sales is revenue from operations.",
    "Operating profit is sales minus costs before interest and depreciation; other income is not counted. For banks, "
    "finance companies and insurers, whose main cost is interest or claims, profit before tax is shown instead.",
    "Growth is against the quarter before and against the same quarter a year earlier, only where both are on the same "
    "basis (consolidated with consolidated). Where the earlier figure was a loss a short word is shown instead of a "
    "percentage, and EPS is not compared across a bonus issue or split.",
    "P/E is the latest closing price over earnings per share for the last four quarters; PEG is that P/E over the growth "
    "of those earnings against the year before (it looks back at reported growth, not forecasts). The arrows compare each "
    "with the same ratio before these results, at today's price, so they move only because earnings changed.",
    "Arrows show direction only. They do not say a result is good or bad, and nothing here is a recommendation.",
]
UPCOMING_NOTE = ("Dates come from the exchanges' board-meeting notices. A company can move its date; the list is refreshed "
                 "through the day.")


# =====================================================================================================
# Database
# =====================================================================================================
_COLS = {"has": None, "checked": 0.0}
_CACHE = {}


def _connect():
    return psycopg.connect(DB, row_factory=dict_row)


def _columns(cur):
    """Which optional columns stock_financials has (checked once a minute, so the page works whichever of the
    migrations has been run)."""
    if _COLS["has"] is not None and time.monotonic() - _COLS["checked"] < 60:
        return _COLS["has"]
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name = 'stock_financials' AND column_name IN ('filed_on', 'operating_profit', 'profit_before_tax')""")
    names = {r["column_name"] for r in cur.fetchall()}
    _COLS["has"] = {"filed_on": "filed_on" in names, "operating": {"operating_profit", "profit_before_tax"} <= names}
    _COLS["checked"] = time.monotonic()
    return _COLS["has"]


def _fin_select(cols, alias=""):
    p = alias + "." if alias else ""
    extra = ""
    extra += ", %sfiled_on" % p if cols["filed_on"] else ", NULL::date AS filed_on"
    extra += ", %soperating_profit, %sprofit_before_tax" % (p, p) if cols["operating"] else ", NULL::numeric AS operating_profit, NULL::numeric AS profit_before_tax"
    return "%sisin, %speriod_end, %speriod_label, %sconsolidated, %srevenue, %sprofit, %seps%s" % (p, p, p, p, p, p, p, extra)


def _group(rows, key="isin"):
    out = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def _load_context(cur, isins, cols, floor):
    """history, prices, split/bonus events, balance sheets and cap classes for these stocks, each in one query."""
    isins = list(isins)
    hist = _group([num_row(r) for r in _fetch(cur, """SELECT %s FROM stock_financials f
                                 WHERE f.isin = ANY(%%(isins)s) AND f.period_type = 'quarterly' AND f.period_end >= %%(floor)s
                                   AND (f.revenue IS NOT NULL OR f.profit IS NOT NULL)
                                 ORDER BY f.isin, f.period_end DESC""" % _fin_select(cols, "f"), {"isins": isins, "floor": floor})])
    events = _group(_fetch(cur, """SELECT isin, kind, event_date, headline FROM stock_event
                                   WHERE isin = ANY(%(isins)s) AND kind IN ('split', 'bonus') AND event_date >= CURRENT_DATE - 1100
                                   ORDER BY event_date""", {"isins": isins}))
    bs = _group(_fetch(cur, """SELECT * FROM (
                                   SELECT isin, period_end, period_type, kind, consolidated, net_worth, total_equity, paid_up, face_value,
                                          shares, debt, total_assets, profit_owners, profit_before_tax, finance_costs, depreciation,
                                          row_number() OVER (PARTITION BY isin ORDER BY period_end DESC) AS rn
                                   FROM stock_balance_sheet WHERE isin = ANY(%(isins)s)) t
                               WHERE rn <= 12""", {"isins": isins}, optional=True))
    caps = {r["isin"]: r["cap_class"] for r in _fetch(cur, """SELECT DISTINCT ON (isin) isin, cap_class FROM stock_cap_class
                                                           WHERE isin = ANY(%(isins)s)
                                                           ORDER BY isin, as_of_period DESC""", {"isins": isins}, optional=True)}
    return hist, events, bs, caps


def _fetch(cur, sql, params, optional=False):
    """Run a query; with optional=True a missing table or column costs this panel and nothing else."""
    if not optional:
        cur.execute(sql, params)
        return cur.fetchall()
    try:
        with cur.connection.transaction():
            cur.execute(sql, params)
            return cur.fetchall()
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        return []


def _due_between(cur, a, b):
    """Active companies with a board meeting to consider results between a and b, one row each, on their earliest date."""
    return _fetch(cur, """SELECT e.isin, min(e.event_date) AS event_date, m.symbol, m.company_name, m.sector, m.face_value
                          FROM stock_event e JOIN stock_master m ON m.isin = e.isin AND m.is_active
                          WHERE e.kind = 'result' AND e.event_date BETWEEN %(a)s AND %(b)s
                          GROUP BY e.isin, m.symbol, m.company_name, m.sector, m.face_value
                          ORDER BY min(e.event_date), m.company_name""", {"a": a, "b": b})


def _still_to_come_count(cur, a, b, today):
    """How many companies the Upcoming list holds for a..b, counted the way the list is built (split_due on the same
    rows), without building the cards. Used for the tab badge when the page is showing another tab."""
    due = _due_between(cur, a, b)
    if not due:
        return 0
    # the quarters these dates are about all end on or after the one before the window opens
    stored = _fetch(cur, """SELECT isin, period_end FROM stock_financials
                            WHERE isin = ANY(%(isins)s) AND period_type = 'quarterly' AND period_end >= %(since)s
                              AND (revenue IS NOT NULL OR profit IS NOT NULL)""",
                    {"isins": [r["isin"] for r in due], "since": quarter_end_before(a)})
    return len(split_due(due, _group(stored), today)[0])


def build_view(view, today, days, api):
    """Everything the page needs for one view. Reads the database; the arithmetic is in the pure functions above."""
    a, b, label = window_for(view, today, days)
    with _connect() as conn:
        cur = conn.cursor()
        cols = _columns(cur)
        out = {"view": view, "today": str(today), "window": {"from": str(a), "to": str(b), "label": label},
               "published": [], "scheduled": [], "awaited": [], "notes": list(NOTES), "summary": None, "tabs": {}, "capped": False}
        if not cols["filed_on"]:
            out["notes"].insert(0, "Filing dates are not on file yet, so results cannot be listed by week. "
                                   "(Run add_stock_financials_filed_on.py, then result_watcher.py --stamp-dates.)")
        published, scheduled = [], []
        if view != "upcoming" and cols["filed_on"]:
            published = _fetch(cur, """SELECT %s, m.symbol, m.company_name, m.sector, m.face_value
                                       FROM stock_financials f JOIN stock_master m ON m.isin = f.isin AND m.is_active
                                       WHERE f.period_type = 'quarterly' AND f.filed_on BETWEEN %%(a)s AND %%(b)s
                                         AND (f.revenue IS NOT NULL OR f.profit IS NOT NULL)
                                       ORDER BY f.filed_on DESC, m.company_name LIMIT %%(lim)s""" % _fin_select(cols, "f"),
                               {"a": a, "b": b, "lim": MAX_CARDS + 1})
            published = [num_row(r) for r in published]
            if len(published) > MAX_CARDS:
                published, out["capped"] = published[:MAX_CARDS], True
        # companies with a results date in the window; a company whose figures for that quarter are already stored is
        # not "still to come" (it is among the published ones)
        due = []
        if view in ("this_week", "last_week", "upcoming"):
            due = _due_between(cur, a, b)
        # one context for everyone on the page
        isins = list({r["isin"] for r in published} | {r["isin"] for r in due})
        floor = (min([r["period_end"] for r in published] or [today])) - timedelta(days=HISTORY_DAYS)
        hist, events, bs, caps = _load_context(cur, isins, cols, floor) if isins else ({}, {}, {}, {})
        prices = api.latest_prices(isins) if isins else {}
        # still to come (today counts) / the date has passed and no figures are on file; figures already stored -> neither
        scheduled, awaited = split_due(due, hist, today)

        cards = []
        for r in published:
            h = hist.get(r["isin"], [])
            px = _price(prices.get(r["isin"]))
            cards.append(build_card(r, h, px, events.get(r["isin"], []), bs.get(r["isin"], []), r.get("face_value"),
                                    caps.get(r["isin"]), api, today))
        out["published"] = cards
        out["summary"] = summarise(cards) if cards else None
        out["scheduled"] = [build_scheduled(s, s["event_date"], hist.get(s["isin"], []), _price(prices.get(s["isin"])),
                                            events.get(s["isin"], []), bs.get(s["isin"], []), s.get("face_value"),
                                            caps.get(s["isin"]), api, today) for s in scheduled]
        out["awaited"] = [{"isin": s["isin"], "symbol": s.get("symbol"), "name": s.get("company_name"), "sector": s.get("sector"),
                           "event_date": str(s["event_date"])} for s in awaited]
        # badges for the three tabs: how many results were filed in each week, and how many companies the Upcoming list
        # holds (still to come after this week; the "awaited" ones belong to the week tabs and are not counted anywhere)
        tabs = {}
        if cols["filed_on"]:
            for v in ("this_week", "last_week"):
                wa, wb, _ = window_for(v, today)
                cur.execute("""SELECT count(DISTINCT isin) AS n FROM stock_financials
                               WHERE period_type = 'quarterly' AND filed_on BETWEEN %s AND %s AND (revenue IS NOT NULL OR profit IS NOT NULL)""", (wa, wb))
                tabs[v] = cur.fetchone()["n"]
        ua, ub, _ = window_for("upcoming", today, days)
        tabs["upcoming"] = len(scheduled) if view == "upcoming" else _still_to_come_count(cur, ua, ub, today)
        out["tabs"] = tabs
        if view == "upcoming" or scheduled:
            out["notes"].append(UPCOMING_NOTE)
        sectors = sorted({c["sector"] for c in cards if c.get("sector")} | {s["sector"] for s in out["scheduled"] if s.get("sector")})
        out["sectors"] = sectors
    return out


def _price(p):
    return None if not p or p.get("close") is None else {"close": p["close"], "date": p["date"], "change_pct": p.get("change_pct")}


# =====================================================================================================
# Switching it on without replacing api.py
# =====================================================================================================
def mount(app):
    """Add the API and the page at /results to a running app. This is what enable_results.py appends to the server's
    api.py, so the feature can go live without uploading the laptop's api.py (which carries every other pending
    change). The laptop's api.py mounts the feature itself and does not use this."""
    from fastapi.responses import FileResponse
    app.include_router(router)
    here = os.path.dirname(os.path.abspath(__file__))

    @app.get("/results", include_in_schema=False)
    def results_page():
        path = os.path.join(here, "results.html")
        if not os.path.exists(path):
            raise HTTPException(404, "results.html is missing from %s" % here)
        return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})


# =====================================================================================================
# The endpoint
# =====================================================================================================
@router.get("/access")
def access(request: Request):
    """May this visitor see the page? Never an error: the page asks this before anything else so a signed-out visitor,
    a member without the Stocks plan and a subscriber each get the right screen."""
    return GATE.access(request)


@router.get("")
def results(request: Request,
            view: str = Query("this_week", pattern="^(this_week|last_week|upcoming)$"),
            days: int = Query(UPCOMING_DAYS_DEFAULT, ge=7, le=UPCOMING_DAYS_MAX)):
    """Quarterly results by week. FOR SUBSCRIBERS (Stocks plan) AND ADMINISTRATORS: the answer is checked on every call;
    MF_RESULTS_ACCESS can widen it (signed_in, everyone) and MF_RESULTS_ENABLED=false switches the page off."""
    GATE.require(request)
    try:
        import api as api_module          # the ratio working lives there; imported now so a boot never depends on it
    except Exception:                      # noqa: BLE001
        raise HTTPException(503, "The results page needs api.py on the server.")
    today = datetime.now(IST).date()
    key = (view, days if view == "upcoming" else 0, today)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    data = build_view(view, today, days, api_module)
    _CACHE.clear() if len(_CACHE) > 12 else None
    _CACHE[key] = (time.monotonic(), data)
    return data
