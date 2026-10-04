"""
screener_api.py -- stock screeners, mounted in api.py:

    from screener_api import router as screener_router
    app.include_router(screener_router)

WHY THESE SCREENS AND NOT THE USUAL ONES
    Every free charting site screens on RSI and moving averages. None of
    them can tell you what the fund managers did last month, because none
    of them stores 43 month-ends of every fund's share counts.

    So four of the seven here are about the funds. The other three are
    chart screens, and each is written as a CROSSING rather than a state:
    RSI rising through a level, MACD turning up through its signal, the
    50-bar average crossing the 200-bar. "Above 60" may have been true
    since March and says nothing about today; "went through 60 in the last
    bar" is news, and news is the only thing a screen can honestly offer.

    All three take the timeframe from the reader, and all three compare
    one calendar period with the one before it rather than one stored row
    with the row before it. See PERIOD_PAIR for why those are not the same
    thing and why getting it wrong fails silently.

TWO RULES THAT DECIDE WHETHER THIS IS TRUE OR JUST PLAUSIBLE
    Both were learned in this codebase already, and a screener that
    forgets either produces confident nonsense at scale.

    1. A FUND THAT HAS NOT FILED HAS NOT SOLD.
       115 of the comparable equity funds sit a month behind -- whole
       houses at a time, Bandhan, DSP, UTI, Quantum, Bank of India. If a
       missing disclosure counted as an exit, every stock those houses
       hold would appear on "funds are leaving" with dozens of invented
       sellers. Only funds that filed for BOTH dates are compared.

    2. A BONUS IS NOT A PURCHASE.
       A bonus or split multiplies the share count without anybody
       trading, and it does it to every holder by the same ratio. So the
       ratio agreeing across funds is the signature of a corporate action
       and disagreeing is the signature of a trade. Without this a 1:1
       bonus puts a stock at the top of "funds are buying" with every
       holder apparently doubling down.

    The same CA_BAND and the same median-ratio test as the change log, so
    the screener and the stock page can never disagree about whether
    somebody bought.

WHAT THESE SCREENS ARE NOT
    They are not recommendations, and not one of them says buy. They
    report what already happened, and the holdings behind them are a
    month old at best -- disclosed monthly, published up to ten days
    after the month ends. Every response carries the dates it was read
    from so the page can say so.
"""

from datetime import date, timedelta
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, HTTPException, Query, Request

from portfolio_api import DB, _q, CA_BAND, _require_subscription

router = APIRouter(prefix="/api/screener", tags=["screener"])

MAX_ROWS = 200

# How much of a share-count move counts as a trade rather than noise.
# Below this a fund has essentially stood still: a 0.4% drift in units is
# rounding in the disclosure, not a decision.
TRADE_BAND = 0.02

# "Price has not moved" for the quiet-accumulation screen. Wide enough to
# survive a month of ordinary drift, tight enough that a stock that has
# already run does not qualify.
QUIET_MOVE = 5.0


# =====================================================================
# THE CATALOGUE
# =====================================================================
# One definition, read by the page and by this module, so the wording on
# screen and the rule in the query cannot drift apart.
SCREENS = [
    {
        "key": "buying",
        "name": "Funds are buying",
        "what": "Companies where more fund managers increased their share "
                "count than reduced it, between the last two disclosures.",
        "why": "Share counts, not percentages. A holding can grow because "
               "the price rose and nobody did anything; only the number of "
               "shares says somebody actually bought.",
        "min_label": "At least this many buyers",
        "min_default": 3,
    },
    {
        "key": "leaving",
        "name": "Funds are leaving",
        "what": "Companies where more managers cut their share count than "
                "added to it, including those who sold out entirely.",
        "why": "The exits are the half most tools never show. A fund that "
               "has simply not filed yet is not counted as a seller.",
        "min_label": "At least this many sellers",
        "min_default": 3,
    },
    {
        "key": "new",
        "name": "Newly bought",
        "what": "Companies that appeared in a fund's portfolio for the "
                "first time this month.",
        "why": "The earliest point at which a conviction decision becomes "
               "visible to anybody outside the fund.",
        "min_label": "At least this many new holders",
        "min_default": 2,
    },
    {
        "key": "quiet",
        "name": "Bought quietly",
        "what": "Funds added to it while the share price barely moved "
                "&mdash; buying that the chart has not reacted to yet.",
        "why": "This needs both halves of the database: the holdings say "
               "what was bought, the candles say the price did not move. "
               "It is the one screen here that cannot be built anywhere "
               "else.",
        "min_label": "At least this many buyers",
        "min_default": 3,
    },
    {
        "key": "rsi_cross",
        "name": "RSI crossing up",
        "what": "Momentum that has just risen through a level &mdash; 60 by "
                "default, on whichever timeframe you choose.",
        "why": "The level matters less than the crossing. RSI above 60 may "
               "have been true for months; RSI rising THROUGH 60 happened "
               "in the last bar.",
        "min_label": None, "min_default": None,
        "timeframes": True, "level": 60,
    },
    {
        "key": "macd_pco",
        "name": "MACD positive on all three",
        "what": "MACD above its signal line on the daily, the weekly AND "
                "the monthly at once &mdash; with the timeframes that have "
                "just crossed named on each row.",
        "why": "One timeframe agreeing with itself is not a finding. Three "
               "agreeing is rarer, and the row says which of them turned "
               "most recently rather than leaving you to assume all three "
               "just did.",
        "min_label": None, "min_default": None,
    },
    {
        "key": "upcoming_results",
        "name": "Results due soon",
        "what": "Companies with a board meeting scheduled to consider "
                "results within the window you choose.",
        "why": "The date a board INTENDS to meet, read off BSE's own "
               "calendar -- not a filing after the fact, which is the only "
               "way a screen can tell you something is coming rather than "
               "report that it already happened.",
        "min_label": None, "min_default": None,
        "window_default": 30,
    },
    {
        "key": "upcoming_dividends",
        "name": "Dividends due soon",
        "what": "Companies with a dividend ex-date within the window you "
                "choose.",
        "why": "The ex-date, not the announcement date. A dividend can be "
               "announced weeks ahead of it; the ex-date is the one that "
               "decides whether buying today still gets you the payout.",
        "min_label": None, "min_default": None,
        "window_default": 30,
    },
    {
        "key": "upcoming_actions",
        "name": "Splits, bonuses & rights due soon",
        "what": "Companies with a stock split, bonus issue or rights issue "
                "due within the window you choose.",
        "why": "Each changes the share count on your statement without "
               "necessarily changing what it is worth -- worth knowing "
               "about before it happens, not after the number suddenly "
               "looks different.",
        "min_label": None, "min_default": None,
        "window_default": 30,
    },
    {
        "key": "cross",
        "name": "Crossovers",
        "what": "Where the 50-bar average has just crossed the 200-bar, or "
                "the price has just crossed its 200-bar average, on "
                "whichever timeframe you choose.",
        "why": "A crossover is news. “Above every average” is a "
               "state that may have been true since March and tells you "
               "nothing about today. Note that on monthly bars a 200-bar "
               "average is sixteen years of history, so few companies "
               "have one.",
        "min_label": None,
        "min_default": None,
        "timeframes": True,
    },
]
SCREEN_KEYS = {s["key"] for s in SCREENS}

CAPS = ("Large", "Mid", "Small")
TIMEFRAMES = ("DAILY", "WEEKLY", "MONTHLY")


# =====================================================================
# FUND FLOW
# =====================================================================
LAST_TWO = """
SELECT DISTINCT as_of_date FROM mf_holding
ORDER BY as_of_date DESC LIMIT 2
"""

# Only funds present on BOTH dates. See rule 1 in the module docstring.
FILED_BOTH = """
SELECT scheme_code FROM mf_holding WHERE as_of_date = %(now)s GROUP BY 1
INTERSECT
SELECT scheme_code FROM mf_holding WHERE as_of_date = %(prev)s GROUP BY 1
"""

# Every position, both dates, for the funds that filed twice. About six
# thousand rows a date -- small enough to group in Python, which is worth
# it because the corporate-action test is then the same Python the change
# log already uses rather than a second copy of it in SQL.
#
# WRITTEN OUT, NOT GENERATED. The first version built this by
# %-formatting FILED_BOTH into it, which turned the subquery's %(now)s
# into %%(now)s -- an escaped percent, so psycopg would have passed the
# literal text "%(now)s" to Postgres instead of binding a date. Composing
# SQL that contains placeholders with a formatter that also uses % is a
# trap with no upside here.
POSITIONS = """
SELECT h.as_of_date, h.scheme_code, h.isin, SUM(h.quantity) AS qty,
       SUM(h.pct_of_nav) AS pct
FROM mf_holding h
JOIN (
    SELECT scheme_code FROM mf_holding
    WHERE as_of_date = %(now)s GROUP BY 1
    INTERSECT
    SELECT scheme_code FROM mf_holding
    WHERE as_of_date = %(prev)s GROUP BY 1
) f ON f.scheme_code = h.scheme_code
WHERE h.as_of_date IN (%(now)s, %(prev)s)
  AND h.isin IS NOT NULL
  AND h.quantity > 0
GROUP BY 1, 2, 3
"""

# Names, sector and the cap class in force. Nearest AMFI period rather
# than the newest: the list is refreshed every six months and a stock
# classified last March has not become unclassified since.
META = """
SELECT m.isin, m.symbol, m.company_name, m.sector, cc.cap_class
FROM stock_master m
LEFT JOIN LATERAL (
    SELECT x.cap_class FROM stock_cap_class x
    WHERE x.isin = m.isin
    ORDER BY (x.as_of_period <= %(now)s) DESC,
             abs(x.as_of_period - %(now)s)
    LIMIT 1
) cc ON true
WHERE m.isin = ANY(%(isins)s)
"""

# What the price did over the same window, from the candles. Each end is
# the last close at or before that date, so a disclosure landing on a
# weekend still gets a real price.
PRICE_MOVE = """
SELECT o.isin,
       (SELECT close FROM stock_ohlc_daily a
         WHERE a.isin = o.isin AND a.as_of_date <= %(now)s
         ORDER BY a.as_of_date DESC LIMIT 1) AS close_now,
       (SELECT close FROM stock_ohlc_daily b
         WHERE b.isin = o.isin AND b.as_of_date <= %(prev)s
         ORDER BY b.as_of_date DESC LIMIT 1) AS close_prev
FROM (SELECT DISTINCT isin FROM stock_ohlc_daily
      WHERE isin = ANY(%(isins)s)) o
"""


def _two_dates(cur):
    rows = _q(cur, LAST_TWO)
    if len(rows) < 2:
        raise HTTPException(503, "Not enough disclosure history yet.")
    return rows[0]["as_of_date"], rows[1]["as_of_date"]


def _flow(cur, now, prev):
    """Per-company: who bought, who sold, who arrived, who left.

    Returns {isin: {...}}. Corporate actions are removed the same way the
    change log removes them, because the same bonus must not be a purchase
    on one page and a non-event on another.
    """
    byfund = {}
    for r in _q(cur, POSITIONS, {"now": now, "prev": prev}):
        key = (r["isin"], r["scheme_code"])
        slot = byfund.setdefault(key, {"now": None, "prev": None})
        slot["now" if r["as_of_date"] == now else "prev"] = float(r["qty"] or 0)

    # THE CORPORATE-ACTION TEST, AND THE TRAP IN IT.
    #
    # A bonus moves EVERY holder by the same ratio, so a median far from
    # 1.0 is suggestive -- but a median far from 1.0 is also what a stock
    # most funds are selling looks like. On a company where two funds cut
    # hard and two stood still, the median lands at 0.85, and treating
    # that as a corporate action turns the two funds that did nothing into
    # buyers. Measured: ratios 0.6, 0.7, 1.0, 1.0 produced two invented
    # buyers before this was fixed.
    #
    # So the test is not "the median moved" but "the median moved AND most
    # holders are sitting on it". A real bonus takes essentially everybody
    # with it; a spread of trades does not.
    ratios = {}
    for (isin, _), v in byfund.items():
        if v["now"] and v["prev"]:
            ratios.setdefault(isin, []).append(v["now"] / v["prev"])

    corp_ratio = {}          # isin -> the bonus ratio, when there is one
    for isin, rs in ratios.items():
        if len(rs) < 3:
            continue
        rs = sorted(rs)
        n = len(rs)
        med = rs[n // 2] if n % 2 else (rs[n // 2 - 1] + rs[n // 2]) / 2
        if abs(med - 1.0) <= CA_BAND:
            continue                       # nothing moved together
        near = sum(1 for r in rs if abs(r - med) <= CA_BAND * max(1.0, med))
        if near >= max(3, 0.6 * n):
            corp_ratio[isin] = med

    out = {}
    for (isin, _), v in byfund.items():
        s = out.setdefault(isin, {"buyers": 0, "sellers": 0, "new": 0,
                                  "exits": 0, "holders": 0, "corp": False})
        qn, qp = v["now"], v["prev"]
        if qn:
            s["holders"] += 1
        if qn and not qp:
            s["new"] += 1
            continue
        if qp and not qn:
            s["exits"] += 1
            continue
        if not qn or not qp:
            continue

        mine = qn / qp
        med = corp_ratio.get(isin)
        if med:
            s["corp"] = True
            # Only the funds sitting ON the ratio are excused. One that
            # traded on top of a bonus is not on it, and is judged on its
            # raw move -- the same thing the change log does, so the two
            # pages cannot disagree about whether somebody bought.
            if abs(mine - med) <= CA_BAND * max(1.0, med):
                continue                      # the bonus, not a decision

        if mine > 1 + TRADE_BAND:
            s["buyers"] += 1
        elif mine < 1 - TRADE_BAND:
            s["sellers"] += 1
    return out


# =====================================================================
# THE TWO MOST RECENT BARS, ONE PER CALENDAR PERIOD
# =====================================================================
# Every indicator screen here is a comparison of the current bar with the
# one before it, so all three share this query.
#
# THE TRAP IT EXISTS TO AVOID, WHICH IS NOT OBVIOUS AND IS NOT AN ERROR
#   fetch_technicals.py writes a row every time it runs, and a MONTHLY bar
#   is dated the last trading day SO FAR in its month. So during September
#   the September bar is re-dated every night: 11 Sep, 12 Sep, 15 Sep are
#   three snapshots of the SAME bar. "The last two rows" therefore compares
#   September with September, every indicator comes out unchanged, and the
#   screen quietly returns nothing while looking like it worked. The same
#   applies to the in-progress week.
#
#   Bucketing by calendar period and taking the freshest row in each
#   collapses those snapshots, so "previous" genuinely means the month or
#   week before. This is the same fix _period_bars() in api.py makes for a
#   single stock; this is the whole-market version of it, and the two must
#   stay in step or the stock page and the screener will disagree about
#   whether something crossed.
#
#   Prefix-matching the timeframe keeps it working whether the column
#   holds 'monthly', 'M' or 'MONTHLY'.
PERIOD_PAIR = """
WITH stamped AS (
    SELECT t.isin, t.as_of_date, t.close_price, t.rsi,
           t.macd_line, t.macd_signal, t.ema_50, t.ema_200,
           CASE
             WHEN %(tf)s ILIKE 'm%%' THEN date_trunc('month', t.as_of_date)
             WHEN %(tf)s ILIKE 'w%%' THEN date_trunc('week',  t.as_of_date)
             ELSE t.as_of_date::timestamp
           END AS period_key
    FROM stock_technical t
    WHERE t.timeframe = %(tf)s
      AND t.as_of_date >= %(since)s
),
one_per AS (
    SELECT DISTINCT ON (isin, period_key) *
    FROM stamped
    ORDER BY isin, period_key DESC, as_of_date DESC
),
ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY isin
                                 ORDER BY period_key DESC) AS rn
    FROM one_per
)
SELECT c.isin,
       c.as_of_date AS d_now,   p.as_of_date AS d_prev,
       c.close_price AS close_now, p.close_price AS close_prev,
       c.rsi AS rsi_now,        p.rsi AS rsi_prev,
       c.macd_line AS macd_now, c.macd_signal AS sig_now,
       p.macd_line AS macd_prev, p.macd_signal AS sig_prev,
       c.ema_50 AS e50_now,     c.ema_200 AS e200_now,
       p.ema_50 AS e50_prev,    p.ema_200 AS e200_prev,
       (c.as_of_date - p.as_of_date) AS gap_days
FROM ranked c
JOIN ranked p ON p.isin = c.isin AND p.rn = 2
WHERE c.rn = 1
"""

NEWEST_BAR = """
SELECT max(as_of_date) AS d FROM stock_technical WHERE timeframe = %(tf)s
"""

# How far back to read. Only two bars per stock are needed, so this is
# just enough history to find them -- reading four years of daily rows to
# use two of them would cost the whole table for nothing.
LOOKBACK_DAYS = {"DAILY": 120, "WEEKLY": 300, "MONTHLY": 900}

# How old the current bar may be before the row is dropped. Without this,
# a stock that stopped updating in March keeps reporting its March cross
# as though it happened last night. Measured against the newest bar in the
# market rather than today, so a screen run on a holiday weekend does not
# empty itself.
FRESH_DAYS = {"DAILY": 10, "WEEKLY": 25, "MONTHLY": 50}

# What one bar is called, so "50-day average" does not appear on a screen
# built from weekly bars. ema_50 on monthly bars is fifty MONTHS.
BAR_NOUN = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month"}

CROSS_KINDS = ("golden", "death", "price_up", "price_down")


def _cross_label(kind, tf):
    n = BAR_NOUN.get(tf, "day")
    return {
        "golden": "50-%s average rose above the 200-%s" % (n, n),
        "death": "50-%s average fell below the 200-%s" % (n, n),
        "price_up": "price rose above its 200-%s average" % n,
        "price_down": "price fell below its 200-%s average" % n,
    }[kind]


def _f(v):
    """Numeric or None -- stock_technical columns are nullable numerics."""
    return None if v is None else float(v)


def _period_pairs(cur, tf):
    """Current and previous bar for every stock with a fresh pair.

    Returns (rows, newest_date). Stale stocks are dropped here rather
    than in each screen, so none of them can forget.
    """
    since = _q(cur, NEWEST_BAR, {"tf": tf})[0]["d"]
    if not since:
        return [], None
    newest = since
    window = LOOKBACK_DAYS.get(tf, 300)
    rows = _q(cur, PERIOD_PAIR,
              {"tf": tf, "since": newest - timedelta(days=window)})
    cutoff = newest - timedelta(days=FRESH_DAYS.get(tf, 10))
    return [r for r in rows if r["d_now"] >= cutoff], newest


@router.get("/screens")
def list_screens():
    """The catalogue, so the page never hard-codes a rule's wording."""
    return {"screens": [{k: v for k, v in s.items()} for s in SCREENS],
            "caps": list(CAPS)}


def _meta(cur, isins, when=None):
    """Names, sector and cap class for a list of ISINs.

    `when` defaults to today rather than None: META picks the cap class
    from the nearest AMFI period to that date, and NULL there would leave
    the ordering to chance rather than to the calendar.
    """
    if not isins:
        return {}
    return {m["isin"]: m for m in _q(cur, META,
                                     {"isins": isins,
                                      "now": when or date.today()})}


def _classify_cross(r):
    """Which crossing, if any, happened between these two bars."""
    e50n, e200n = _f(r["e50_now"]), _f(r["e200_now"])
    e50p, e200p = _f(r["e50_prev"]), _f(r["e200_prev"])
    cn, cp = _f(r["close_now"]), _f(r["close_prev"])
    if None not in (e50n, e200n, e50p, e200p):
        if e50p <= e200p and e50n > e200n:
            return "golden"
        if e50p >= e200p and e50n < e200n:
            return "death"
    if None not in (cn, cp, e200n, e200p):
        if cp <= e200p and cn > e200n:
            return "price_up"
        if cp >= e200p and cn < e200n:
            return "price_down"
    return None


EVENT_KIND_GROUPS = {
    "upcoming_results": ("result",),
    "upcoming_dividends": ("dividend",),
    "upcoming_actions": ("split", "bonus", "rights"),
}

EVENT_KIND_LABEL = {
    "result": "Results", "dividend": "Dividend", "split": "Stock split",
    "bonus": "Bonus issue", "rights": "Rights issue",
}


@router.get("/run")
def run(request: Request,
        screen: str = Query(...),
        cap: Optional[str] = None,
        minimum: Optional[int] = None,
        timeframe: str = "DAILY",
        level: int = 60,
        window: Optional[int] = None,
        limit: int = 50):
    # The screens are a paid product. The LIST of screens (/screens above)
    # stays open so a visitor can see what exists; running one does not.
    _require_subscription(request)
    if screen not in SCREEN_KEYS:
        raise HTTPException(400, "Unknown screen: %s" % screen)
    if cap and cap not in CAPS:
        raise HTTPException(400, "Unknown cap class: %s" % cap)
    timeframe = (timeframe or "DAILY").upper()
    if timeframe not in TIMEFRAMES:
        raise HTTPException(400, "Unknown timeframe: %s" % timeframe)
    # A level outside this does not screen anything: RSI below 5 or above
    # 95 is a handful of stocks in a decade, and a crossing of it is a
    # data error more often than a move.
    level = max(5, min(int(level), 95))
    limit = max(1, min(limit, MAX_ROWS))
    spec = next(s for s in SCREENS if s["key"] == screen)
    floor = minimum if minimum is not None else (spec["min_default"] or 0)

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:

        # ------------------------------------------- upcoming BSE events
        if screen in EVENT_KIND_GROUPS:
            kinds = EVENT_KIND_GROUPS[screen]
            win = max(1, min(int(window) if window else spec["window_default"], 90))
            events = _q(cur, """
                SELECT isin, kind, event_date, headline FROM (
                    SELECT DISTINCT ON (isin, kind, event_date)
                        isin, kind, event_date, headline
                    FROM stock_event
                    WHERE kind = ANY(%(kinds)s)
                      AND event_date BETWEEN CURRENT_DATE AND CURRENT_DATE + %(win)s
                    -- Two sources can carry the same event (e.g. a bonus
                    -- issue from both NSE's bulk feed and BSE's per-stock
                    -- history): prefer NSE's, since its text includes the
                    -- amount/ratio; otherwise take whichever is freshest.
                    ORDER BY isin, kind, event_date,
                             (source = 'NSE') DESC, updated_at DESC
                ) dedup
                ORDER BY event_date ASC
            """, {"kinds": list(kinds), "win": win})
            meta = _meta(cur, [r["isin"] for r in events])
            out = []
            for r in events:
                m = meta.get(r["isin"], {})
                if cap and m.get("cap_class") != cap:
                    continue
                out.append({
                    "isin": r["isin"],
                    "symbol": m.get("symbol"),
                    "name": m.get("company_name"),
                    "sector": m.get("sector"),
                    "cap": m.get("cap_class"),
                    "kind": r["kind"],
                    "kind_label": EVENT_KIND_LABEL.get(r["kind"], r["kind"]),
                    "event_date": str(r["event_date"]),
                    "days_away": (r["event_date"] - date.today()).days,
                    "headline": r["headline"],
                })
            return {"screen": screen, "rows": out[:limit], "total": len(out),
                    "window_days": win, "as_of": str(date.today())}

        # -------------------------------------------------- crossovers
        if screen == "cross":
            pairs, newest = _period_pairs(cur, timeframe)
            # Counted on the 200-bar average being present on BOTH bars,
            # not on the stock merely existing. On monthly bars a 200-bar
            # average is sixteen years of history and almost nothing has
            # it, so an empty screen there is a fact about the data, and
            # the page can only say so if it is given this number.
            considered = sum(1 for r in pairs
                             if r["e200_now"] is not None
                             and r["e200_prev"] is not None)
            kinds = [(r, _classify_cross(r)) for r in pairs]
            hits = [(r, k) for r, k in kinds if k]
            meta = _meta(cur, [r["isin"] for r, _ in hits])
            out = []
            for r, kind in hits:
                m = meta.get(r["isin"], {})
                if cap and m.get("cap_class") != cap:
                    continue
                out.append({
                    "isin": r["isin"],
                    "symbol": m.get("symbol"),
                    "name": m.get("company_name"),
                    "sector": m.get("sector"),
                    "cap": m.get("cap_class"),
                    "timeframe": timeframe,
                    "kind": kind,
                    "kind_label": _cross_label(kind, timeframe),
                    "close": _f(r["close_now"]),
                    "as_of": str(r["d_now"]),
                    "gap_days": (int(r["gap_days"])
                                 if r["gap_days"] is not None else None),
                })
            # Golden and price-up first, because a reader scanning this
            # wants the turns upward; the falls are still on the list.
            order = {"golden": 0, "price_up": 1, "price_down": 2, "death": 3}
            out.sort(key=lambda x: (order[x["kind"]], x["name"] or ""))
            return {"screen": screen, "rows": out[:limit], "total": len(out),
                    "timeframe": timeframe,
                    "considered": considered,
                    "as_of": str(newest) if newest else None,
                    "compared_with": None}

        # ------------------------------------------------- RSI crossing
        if screen == "rsi_cross":
            pairs, newest = _period_pairs(cur, timeframe)
            hits = []
            considered = 0
            for r in pairs:
                a, b = _f(r["rsi_prev"]), _f(r["rsi_now"])
                if a is None or b is None:
                    continue
                considered += 1
                # STRICTLY through the level. Above-and-still-above is the
                # state every other site screens on, and it may have been
                # true since March; the crossing is what happened in this
                # bar. `>=` on the near side so a bar landing exactly on
                # 60 still counts as having got there.
                if a < level <= b:
                    hits.append((r, a, b))
            meta = _meta(cur, [r["isin"] for r, _, _ in hits])
            out = []
            for r, a, b in hits:
                m = meta.get(r["isin"], {})
                if cap and m.get("cap_class") != cap:
                    continue
                out.append({
                    "isin": r["isin"],
                    "symbol": m.get("symbol"),
                    "name": m.get("company_name"),
                    "sector": m.get("sector"),
                    "cap": m.get("cap_class"),
                    "timeframe": timeframe,
                    "level": level,
                    "rsi": round(b, 1),
                    "rsi_prev": round(a, 1),
                    "close": _f(r["close_now"]),
                    "as_of": str(r["d_now"]),
                    "gap_days": (int(r["gap_days"])
                                 if r["gap_days"] is not None else None),
                })
            out.sort(key=lambda x: (-x["rsi"], x["name"] or ""))
            return {"screen": screen, "rows": out[:limit], "total": len(out),
                    "timeframe": timeframe, "level": level,
                    "considered": considered,
                    "as_of": str(newest) if newest else None,
                    "compared_with": None}

        # --------------------------------------- MACD on all three frames
        if screen == "macd_pco":
            # WHAT THIS DELIBERATELY DOES NOT DO
            #   A strict triple cross -- daily, weekly and monthly all
            #   crossing in the same window -- is almost never true: the
            #   monthly bar turns a few times a year and the odds of the
            #   daily turning in the same fortnight are small, so the
            #   screen would be empty most weeks and look broken.
            #
            #   So the rule is alignment plus recency: above the signal on
            #   all three, with the ones that turned in their LAST bar
            #   named on the row. That is a real finding and the row says
            #   exactly which part of it is new, instead of letting the
            #   name imply all three just crossed.
            per = {}
            newest = {}
            for tf in TIMEFRAMES:
                rows, nd = _period_pairs(cur, tf)
                newest[tf] = nd
                per[tf] = {r["isin"]: r for r in rows}

            common = set(per["DAILY"]) & set(per["WEEKLY"]) & set(per["MONTHLY"])
            hits = []
            for isin in common:
                state = {}
                ok = True
                for tf in TIMEFRAMES:
                    r = per[tf][isin]
                    mn, sn = _f(r["macd_now"]), _f(r["sig_now"])
                    mp, sp = _f(r["macd_prev"]), _f(r["sig_prev"])
                    if mn is None or sn is None or mn <= sn:
                        ok = False
                        break
                    state[tf] = {
                        "hist": round(mn - sn, 3),
                        "fresh": (mp is not None and sp is not None
                                  and mp <= sp),
                        "as_of": str(r["d_now"]),
                    }
                if ok:
                    hits.append((isin, state))

            meta = _meta(cur, [i for i, _ in hits])
            out = []
            for isin, state in hits:
                m = meta.get(isin, {})
                if not m or not m.get("company_name"):
                    continue
                if cap and m.get("cap_class") != cap:
                    continue
                just = [tf for tf in TIMEFRAMES if state[tf]["fresh"]]
                # The monthly turning is the rarest and the slowest to
                # reverse, so it outranks the weekly, which outranks the
                # daily. A stock where all three have just turned sits at
                # the top on 6; long-standing alignment sits at 0.
                rank = (3 if "MONTHLY" in just else 0) + \
                       (2 if "WEEKLY" in just else 0) + \
                       (1 if "DAILY" in just else 0)
                out.append({
                    "isin": isin,
                    "symbol": m.get("symbol"),
                    "name": m.get("company_name"),
                    "sector": m.get("sector"),
                    "cap": m.get("cap_class"),
                    "just_crossed": just,
                    "just_label": (", ".join(t.lower() for t in just)
                                   if just else "aligned, none new"),
                    "daily": state["DAILY"], "weekly": state["WEEKLY"],
                    "monthly": state["MONTHLY"],
                    "close": _f(per["DAILY"][isin]["close_now"]),
                    "as_of": state["DAILY"]["as_of"],
                    "rank": rank,
                })
            out.sort(key=lambda x: (-x["rank"], x["name"] or ""))
            return {"screen": screen, "rows": out[:limit], "total": len(out),
                    "considered": len(common),
                    "as_of": (str(newest["DAILY"])
                              if newest.get("DAILY") else None),
                    "compared_with": None}

        # ------------------------------------------------- the fund flows
        now, prev = _two_dates(cur)
        flow = _flow(cur, now, prev)
        isins = list(flow)
        if not isins:
            return {"screen": screen, "rows": [], "total": 0,
                    "as_of": str(now), "compared_with": str(prev)}

        meta = {m["isin"]: m for m in _q(cur, META,
                                         {"isins": isins, "now": now})}
        price = {}
        if screen == "quiet":
            for r in _q(cur, PRICE_MOVE, {"isins": isins,
                                          "now": now, "prev": prev}):
                if r["close_now"] and r["close_prev"]:
                    price[r["isin"]] = (float(r["close_now"]) /
                                        float(r["close_prev"]) - 1) * 100

        out = []
        for isin, s in flow.items():
            m = meta.get(isin)
            if not m or not m.get("company_name"):
                continue                      # not an equity we know
            if cap and m.get("cap_class") != cap:
                continue

            if screen == "buying":
                if s["buyers"] < floor or s["buyers"] <= s["sellers"]:
                    continue
                rank = s["buyers"] - s["sellers"]
            elif screen == "leaving":
                gone = s["sellers"] + s["exits"]
                if gone < floor or gone <= s["buyers"]:
                    continue
                rank = gone - s["buyers"]
            elif screen == "new":
                if s["new"] < floor:
                    continue
                rank = s["new"]
            else:                              # quiet
                mv = price.get(isin)
                if (s["buyers"] < floor or s["buyers"] <= s["sellers"]
                        or mv is None or abs(mv) > QUIET_MOVE):
                    continue
                rank = s["buyers"] - s["sellers"]

            out.append({
                "isin": isin,
                "symbol": m.get("symbol"),
                "name": m.get("company_name"),
                "sector": m.get("sector"),
                "cap": m.get("cap_class"),
                "buyers": s["buyers"], "sellers": s["sellers"],
                "new": s["new"], "exits": s["exits"],
                "holders": s["holders"],
                "corp_action": s["corp"],
                "price_move": (round(price[isin], 1)
                               if isin in price else None),
                "rank": rank,
            })

        out.sort(key=lambda x: (-x["rank"], -x["holders"]))
        return {"screen": screen, "rows": out[:limit], "total": len(out),
                "as_of": str(now), "compared_with": str(prev)}
