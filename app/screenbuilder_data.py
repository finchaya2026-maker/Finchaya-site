"""
screenbuilder_data.py -- the readings behind "Build your own screener": for every active stock, the numbers each field of
screenbuilder_engine.py needs, read from the database once and kept (about six hours, or until a newer trading day is stored),
and run(): a person's screen in, the stocks that match it out.
---------------------------------------------------------------------------------------------------------------------------
WHERE EACH GROUP COMES FROM (all of it is code the site already trusts; nothing here is a second opinion of a number the site shows)
    technical    stock_ohlc_daily. DAILY readings from adjusted daily bars; WEEKLY and MONTHLY from the same bars grouped by calendar week and month
                 (the one in progress counts as it stands), the closes adjusted for splits and bonuses. RSI / MACD / EMA / SMA are
                 screenbuilder_tech.py, proved equal to indicators.py. The 52-week high and low are the highest high and lowest low of 365 days.
    patterns     the site's own chart-pattern screens (dow_change, fib_retracement, expanded_flat, multiyear_breakout), called with the person's settings
    funds        screener_api._flow: the funds' last two monthly portfolios, funds that filed both, corporate actions removed
    ownership    ownership_screens: the newest quarter that enough companies have filed, against the one before; a change that looks like
                 holders being re-labelled (ownership_screens.looks_reclassified) is no value, not a number
    fundamental  api.build_ratios on stock_financials + stock_balance_sheet at the latest close (the stock page's Valuation table), and sales / profit /
                 EPS growth, latest quarter against the same quarter a year earlier on the same basis
    events       the next board-meeting date for results (stock_event)

SPLITS AND BONUSES. Stored prices are a mix of adjusted and unadjusted history. Every price series is adjusted by price_adjust.py (an action is divided out
only where the prices show it), and a stock that still shows a one-day move of 1.8x (or 1/1.8) that no split or bonus explains has NO technical reading
for any series that contains it: a step in the prices would fake an RSI or a trend. The page says how many stocks were left out.

HONESTY. A reading that cannot be made is None (the engine treats it as unknown), never a guess; a provider that cannot be read at all (a table that is not
loaded) makes its fields unavailable with the reason, and run() reports it as a "problem" instead of quietly matching nothing.
"""

import threading
import time
from datetime import date, timedelta

import price_adjust as pa
import price_bars as pb
import screenbuilder_engine as E
import screenbuilder_tech as T

CACHE_SECONDS = 6 * 3600
STALE_DAYS = 7
STEP = 1.8
BATCH = 80
D_DAYS = 640                      # about 440 daily bars: a 200-bar EMA needs 400
W_DAYS = 1900                     # about 270 weeks: a 100-bar EMA needs 200
M_DAYS = 2000                     # about 65 months: a 26-bar EMA needs 52
FIN_BATCH = 150
FIN_DAYS = 1250                   # quarterly results read back this far (eight quarters for PEG, with room)
MIN_PEERS = 5                     # an industry needs this many companies with the ratio before it has an average
MAX_ROWS = 500
SORTS = ("size", "name")

_MEMO = {}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class ProviderError(Exception):
    """A provider that cannot be read right now; the message is shown to the person (in plain words)."""


def _lock(name):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(name, threading.Lock())


def memo(name, key, build, ttl=CACHE_SECONDS):
    """One cached value per (name, key); one caller builds while the others wait; a failure is not cached."""
    with _lock(name):
        hit = _MEMO.get(name)
        if hit and hit["key"] == key and time.time() - hit["at"] < ttl:
            return hit["value"]
        value = build()
        _MEMO[name] = {"key": key, "at": time.time(), "value": value}
        return value


def clear():
    with _LOCKS_GUARD:
        _MEMO.clear()


def ready():
    """{provider: seconds since it was read} for the providers that are cached and fresh (the page says what is being prepared)."""
    now = time.time()
    return {k: round(now - v["at"]) for k, v in list(_MEMO.items()) if now - v["at"] < CACHE_SECONDS}


_WARM = {"running": False}


def warm_async():
    """Read every provider in the background, one after the other (the price read is the slow one), so that the first screen somebody runs after a
    new trading day does not have to wait for all of it. Harmless when everything is cached; only one warm-up runs at a time."""
    if _WARM["running"]:
        return False
    if all(n in ready() for n in ("profile", "tech", "funds", "ownership", "fund")):
        return False
    _WARM["running"] = True

    def work():
        try:
            import psycopg
            from psycopg.rows import dict_row
            from portfolio_api import DB
            with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
                for step in (profile, tech, funds, ownership, fundamentals, events):
                    try:
                        step(cur)
                    except ProviderError:
                        pass                                      # a provider that is not there is reported when a screen needs it
        except Exception as e:                                   # noqa: BLE001  a warm-up must never hurt anything
            print("mfapi: could not warm the screener builder readings: %s: %s" % (type(e).__name__, str(e)[:120]), flush=True)
        finally:
            _WARM["running"] = False
    threading.Thread(target=work, daemon=True, name="screenbuilder-warm").start()
    return True


def _tuples(cur, sql, params):
    return pb._run_tuples(cur, sql, params)


def _optional(cur, sql, params):
    """Rows of a query that may fail because a table or column is not there yet: [] then (the savepoint keeps the transaction usable)."""
    import psycopg
    try:
        with cur.connection.transaction():
            cur.execute(sql, params)
            return cur.fetchall()
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        return None


# =====================================================================
# THE UNIVERSE: who is a stock here, what it is called, what it costs
# =====================================================================
PROFILE_SQL = """
SELECT m.isin, m.symbol, m.company_name, m.sector, m.industry, cc.cap_class, cc.avg_mcap_cr, p.close, p.as_of_date, p.prev_close
FROM stock_master m
LEFT JOIN LATERAL (SELECT x.cap_class, x.avg_mcap_cr FROM stock_cap_class x WHERE x.isin = m.isin
                   ORDER BY (x.as_of_period <= %(now)s) DESC, abs(x.as_of_period - %(now)s) LIMIT 1) cc ON true
JOIN LATERAL (SELECT (array_agg(o.close ORDER BY o.as_of_date DESC))[1] AS close,
                     (array_agg(o.as_of_date ORDER BY o.as_of_date DESC))[1] AS as_of_date,
                     (array_agg(o.close ORDER BY o.as_of_date DESC))[2] AS prev_close
              FROM (SELECT close, as_of_date FROM stock_ohlc_daily
                    WHERE isin = m.isin AND close IS NOT NULL AND close > 0 ORDER BY as_of_date DESC LIMIT 2) o) p ON p.close IS NOT NULL
WHERE m.is_active AND m.company_name IS NOT NULL
"""


def market_date(cur):
    return pb.latest_market_date(cur)


def profile(cur):
    """{"market_date", "stocks": {isin: {symbol, name, sector, industry, cap, size, close, date, change_pct}}, "stale": n}"""
    md = market_date(cur)
    if md is None:
        raise ProviderError("No prices are stored for the last few weeks.")

    def build():
        cur.execute(PROFILE_SQL, {"now": md})
        out, stale = {}, 0
        for r in cur.fetchall():
            if (md - r["as_of_date"]).days > STALE_DAYS:
                stale += 1
                continue
            close, prev = float(r["close"]), float(r["prev_close"]) if r["prev_close"] else None
            out[r["isin"]] = {"symbol": r["symbol"], "name": r["company_name"], "sector": r["sector"], "industry": r["industry"],
                              "cap": r["cap_class"], "size": float(r["avg_mcap_cr"]) if r["avg_mcap_cr"] is not None else None,
                              "close": close, "date": r["as_of_date"], "change_pct": round((close / prev - 1) * 100, 2) if prev else None}
        return {"market_date": md, "stocks": out, "stale": stale}
    return memo("profile", md, build)


# =====================================================================
# TECHNICAL
# =====================================================================
D_SQL = """
WITH d AS (
    SELECT isin, as_of_date, close, COALESCE(NULLIF(open, 0), close) AS o, COALESCE(NULLIF(high, 0), close) AS hh,
           COALESCE(NULLIF(low, 0), close) AS ll, volume
    FROM stock_ohlc_daily
    WHERE isin = ANY(%(isins)s) AND as_of_date >= %(since)s AND close IS NOT NULL AND close > 0
)
SELECT isin, as_of_date, o::float8 AS o,
       LEAST(GREATEST(hh, close, o), GREATEST(close, o) * {spike})::float8 AS h,
       GREATEST(LEAST(ll, close, o), LEAST(close, o) / {spike})::float8 AS l,
       close::float8 AS c, COALESCE(volume, 0)::float8 AS v
FROM d ORDER BY isin, as_of_date
""".format(spike=pb.SPIKE)
# a week / a month: its last date and its last close (the one in progress counts as it stands)
PERIOD_CLOSE_SQL = """
SELECT isin, max(as_of_date) AS d_end, (array_agg(close ORDER BY as_of_date DESC))[1]::float8 AS c
FROM stock_ohlc_daily
WHERE isin = ANY(%(isins)s) AND as_of_date >= %(since)s AND close IS NOT NULL AND close > 0
GROUP BY isin, date_trunc('{unit}', as_of_date)
ORDER BY isin, 2
"""
ISINS_SQL = "SELECT DISTINCT isin FROM stock_ohlc_daily WHERE as_of_date >= %(recent)s"
EVENTS_SQL = "SELECT isin, kind, event_date, headline FROM stock_event WHERE kind IN ('split', 'bonus') AND isin = ANY(%(isins)s)"


# A one-day move of 1.8x is not a price; but a real week can double a small stock and a real month can halve a crashing one (Adani Enterprises in Feb 2023
# fell by 2.76x in a month), so the bigger the bar, the bigger the jump that is called a data problem rather than a market move.
STEPS = {"DAILY": 1.8, "WEEKLY": 2.2, "MONTHLY": 3.0}


def last_step(c, step=STEP):
    """Index of the newest bar whose close is `step` times, or 1/step of, the bar before it; -1 when there is none."""
    for i in range(len(c) - 1, 0, -1):
        if c[i] >= c[i - 1] * step or c[i] * step <= c[i - 1]:
            return i
    return -1


def confirm_on_bars(actions, ends, c):
    """Which split/bonus actions the closes of a WEEKLY or MONTHLY series show: the bar that contains (or first follows) the ex-date fell by about the ratio
    against the bar before it. Returns [{"date", "factor"}] confirmed, as price_adjust.confirm_actions does for daily bars (which would miss them: a
    weekly bar's date can be days after the ex-date)."""
    out = []
    for a in actions:
        j = next((i for i in range(1, len(ends)) if ends[i] >= a["date"]), None)
        if j is None or ends[j - 1] >= a["date"]:
            continue                                     # the bar before already ends on or after the ex-date: the action is not between two bars
        if c[j] > 0 and pa._ratio_confirms(a["factor"], c[j - 1] / c[j]):
            out.append({"date": a["date"], "factor": a["factor"]})
    return out


def adjust_closes(ends, c, confirmed):
    """closes divided by every confirmed ratio whose ex-date is after the bar's last date"""
    if not confirmed:
        return c
    out = []
    for e, x in zip(ends, c):
        f = 1.0
        for a in confirmed:
            if e < a["date"]:
                f *= a["factor"]
        out.append(x / f)
    return out


OHLC_KEEP = 9                  # bars kept of each time frame (the candle conditions reach 5 back, with room for a shape that needs the bar before)
OHLC_MIN_BARS = 3              # a stock listed a few days ago can still have its candles compared


def _monday(d):
    return d - timedelta(days=d.weekday())


def period_done(tf, last_date, md):
    """Is the week / month that `last_date` falls in finished? A week is treated as finished from its Friday (or when the market has moved on to a later week),
    a month from its last weekday (or when it has moved on to a later month). A period shortened by a holiday is judged by this rule, not by the exchange's calendar."""
    if tf == "DAILY":
        return True
    if tf == "WEEKLY":
        return last_date.weekday() >= 4 or _monday(md) > _monday(last_date)
    if (md.year, md.month) != (last_date.year, last_date.month):
        return True
    nxt = date(last_date.year + (last_date.month == 12), last_date.month % 12 + 1, 1)
    last_weekday = nxt - timedelta(days=1)
    while last_weekday.weekday() >= 5:
        last_weekday -= timedelta(days=1)
    return last_date >= last_weekday


def ohlc_tails(dd, o, h, l, c, md, keep=OHLC_KEEP):
    """The newest `keep` candles of each time frame, oldest first, as (open, high, low, close), built from the (adjusted) daily bars: a week is its first open, highest
    high, lowest low and last close, a month likewise. {"DAILY": {"bars", "done"}, "WEEKLY": ..., "MONTHLY": ...}; "done" says whether the newest bar is finished."""
    n = len(c)
    out = {"DAILY": {"bars": [(o[i], h[i], l[i], c[i]) for i in range(max(0, n - keep), n)], "done": True}}
    for tf, key in (("WEEKLY", _monday), ("MONTHLY", lambda d: (d.year, d.month))):
        groups, last = [], None
        for i in range(n - 1, -1, -1):
            k = key(dd[i])
            if groups and k == last:
                g = groups[-1]
                g[1], g[2], g[3] = o[i], max(g[2], h[i]), min(g[3], l[i])          # walking back: the open becomes the earlier day's open
            else:
                if len(groups) == keep:
                    break
                groups.append([k, o[i], h[i], l[i], c[i]])
                last = k
        groups.reverse()
        out[tf] = {"bars": [(g[1], g[2], g[3], g[4]) for g in groups], "done": period_done(tf, dd[-1], md)}
    return out


def _tech_one(daily, weekly, monthly, actions, md):
    """{"DAILY": readings|None, "WEEKLY": ..., "MONTHLY": ..., "OHLC": candles of each time frame} and {tf: why it has no reading} for one stock."""
    out, why = {}, {}
    # --- daily, adjusted
    dd = [r[0] for r in daily]
    o = [r[1] for r in daily]
    h = [r[2] for r in daily]
    l = [r[3] for r in daily]
    c = [r[4] for r in daily]
    v = [r[5] for r in daily]
    confirmed_days = []
    if actions and len(c) > 1:
        raw = [{"d": dd[i], "open": o[i], "high": h[i], "low": l[i], "close": c[i], "volume": v[i]} for i in range(len(c))]
        applied, _ = pa.confirm_actions(actions, raw)
        if applied:
            adj = pa.adjust_bars(raw, applied)
            o, h, l, c, v = [b["open"] for b in adj], [b["high"] for b in adj], [b["low"] for b in adj], [b["close"] for b in adj], [b["volume"] for b in adj]
        confirmed_days = [{"date": a["date"], "factor": a["factor"]} for a in applied]
    if len(c) < T.MIN_BARS["DAILY"]:
        why["DAILY"] = "short"
    elif last_step(c, STEPS["DAILY"]) >= 0:
        why["DAILY"] = "step"
    else:
        out["DAILY"] = T.compute([x.toordinal() for x in dd], h, l, c, v, "DAILY")
    # the candles: from the adjusted daily bars, unless a jump the records cannot explain makes them untrustworthy
    if len(c) >= OHLC_MIN_BARS and last_step(c, STEPS["DAILY"]) < 0:
        out["OHLC"] = ohlc_tails(dd, o, h, l, c, md)
    # --- weekly and monthly: the closes, adjusted by what the daily bars confirmed plus what these bars themselves show
    for tf, series in (("WEEKLY", weekly), ("MONTHLY", monthly)):
        ends = [r[0] for r in series]
        cl = [r[1] for r in series]
        if len(cl) < T.MIN_BARS[tf]:
            why[tf] = "short"
            continue
        # an action the daily bars already confirmed is not applied a second time: its date there is the first lower-priced DAY, which can be a few days
        # after the announced ex-date (a weekend, a holiday), so "the same" means the same ratio within a few days, not the same date
        extra = [a for a in confirm_on_bars(actions, ends, cl)
                 if not any(abs((a["date"] - k["date"]).days) <= 5 and abs(a["factor"] / k["factor"] - 1.0) < 0.01 for k in confirmed_days)]
        cl = adjust_closes(ends, cl, confirmed_days + extra)
        if last_step(cl, STEPS[tf]) >= 0:
            why[tf] = "step"
            continue
        out[tf] = T.compute([x.toordinal() for x in ends], cl, cl, cl, None, tf)
        if out[tf] is not None:
            out[tf].pop("high52", None)
            out[tf].pop("low52", None)
    if out.get("DAILY"):
        for k in ("high52", "low52"):
            for tf in ("WEEKLY", "MONTHLY"):
                if out.get(tf) is not None:
                    out[tf][k] = out["DAILY"].get(k)
    return out, why


def build_tech(cur, md):
    """{"market_date", "stocks": {isin: {tf: readings}}, "left_out": {tf: {"short": n, "step": n}}, "stale": n, "seconds", "bars"}"""
    started = time.time()
    isins = sorted(r[0] for r in _tuples(cur, ISINS_SQL, {"recent": md - timedelta(days=45)}))
    stocks, left, stale, bars = {}, {tf: {"short": 0, "step": 0} for tf in T.MIN_BARS}, 0, 0
    for at in range(0, len(isins), BATCH):
        batch = isins[at:at + BATCH]
        events = {}
        for isin, kind, ex, headline in _tuples(cur, EVENTS_SQL, {"isins": batch}):
            events.setdefault(isin, []).append({"kind": kind, "event_date": ex, "headline": headline})
        daily, weekly, monthly = {}, {}, {}
        for isin, day, op, hi, lo, cl, vo in _tuples(cur, D_SQL, {"isins": batch, "since": md - timedelta(days=D_DAYS)}):
            daily.setdefault(isin, []).append((day, op, hi, lo, cl, vo))
            bars += 1
        for target, unit, days in ((weekly, "week", W_DAYS), (monthly, "month", M_DAYS)):
            for isin, d_end, cl in _tuples(cur, PERIOD_CLOSE_SQL.format(unit=unit), {"isins": batch, "since": md - timedelta(days=days)}):
                target.setdefault(isin, []).append((d_end, cl))
        for isin, rows in daily.items():
            if (md - rows[-1][0]).days > STALE_DAYS:
                stale += 1
                continue
            res, why = _tech_one(rows, weekly.get(isin, []), monthly.get(isin, []), pa.collect_actions(events.get(isin, [])), md)
            for tf, reason in why.items():
                left[tf][reason] += 1
            if res:
                stocks[isin] = res
        time.sleep(0.02)                                       # let the site's own requests in between batches
    return {"market_date": md, "stocks": stocks, "left_out": left, "stale": stale, "bars": bars, "seconds": round(time.time() - started, 1)}


def tech(cur):
    md = market_date(cur)
    if md is None:
        raise ProviderError("No prices are stored for the last few weeks.")
    return memo("tech", md, lambda: build_tech(cur, md))


# =====================================================================
# MUTUAL FUNDS
# =====================================================================
def funds(cur):
    """{"now", "prev", "stocks": {isin: {...counts}}} from the screener's own flow reading."""
    try:
        import screener_api as S
        rows = S._q(cur, S.LAST_TWO)
    except Exception as e:                                       # noqa: BLE001
        raise ProviderError("The fund-flow reading is not available on this server (%s)." % type(e).__name__)
    if len(rows) < 2:
        raise ProviderError("There are not two months of fund portfolios on file yet.")
    now, prev = rows[0]["as_of_date"], rows[1]["as_of_date"]

    def build():
        flow = S._flow(cur, now, prev)
        out = {}
        for isin, s in flow.items():
            out[isin] = {"funds_new": s["new"], "funds_buying": s["buyers"], "funds_selling": s["sellers"], "funds_exited": s["exits"],
                         "funds_holding": s["holders"], "funds_net": (s["new"] + s["buyers"]) - (s["sellers"] + s["exits"])}
        return {"now": now, "prev": prev, "stocks": out}
    return memo("funds", (now, prev), build)


# =====================================================================
# WHO OWNS IT
# =====================================================================
def ownership(cur):
    """{"quarter", "prev", "label", "filed", "partial", "stocks": {isin: {field: value}}}"""
    try:
        import ownership_screens as O
    except ImportError:
        raise ProviderError("The shareholding reading is not on this server.")
    today = date.today()
    rows = _optional(cur, O.QUARTERS_SQL, {"since": today - timedelta(days=O.HISTORY_DAYS)})
    if rows is None:
        raise ProviderError("The shareholding data has not been loaded on this server yet.")
    offered, default = O.offered_quarters(rows, True, 1)
    if not offered:
        raise ProviderError("The breakdown of who holds what has not been read for two quarters in a row yet.")
    chain = O.quarter_chain(default, 1)
    now_q, prev_q = chain[0], chain[1]

    def build():
        by = {}
        for r in O._q(cur, O.ROWS_SQL, {"quarters": chain}):
            by.setdefault(r["isin"], {})[r["quarter_end"]] = r
        out = {}
        for isin, qs in by.items():
            now_row, prev_row = qs.get(now_q), qs.get(prev_q)
            if now_row is None:
                continue
            item = {}
            for key, f_pct, f_chg in (("fii", "fii_pct", "fii_change"), ("mf", "mf_pct", "mf_change"), ("institutions", "inst_pct", "inst_change"),
                                      ("promoter", "promoter_pct", "promoter_change"), ("individuals", "retail_pct", None)):
                now = O.measure(now_row, key)
                item[f_pct] = now
                if f_chg:
                    before = O.measure(prev_row, key)
                    change = None if now is None or before is None else round(now - before, 4)        # (45.6 - 45.3 is 0.29999999999999716 until rounded)
                    if change is not None and O.looks_reclassified(now_row, prev_row, key):
                        change = None
                    item[f_chg] = change
            item["pledged_pct"] = O.measure(now_row, "pledged")
            out[isin] = item
        chosen = next(o for o in offered if o["quarter_end"] == default)
        return {"quarter": now_q, "prev": prev_q, "label": O.quarter_label(now_q), "filed": chosen["filed"], "partial": chosen["partial"], "stocks": out}
    return memo("ownership", (now_q, prev_q), build)


# =====================================================================
# FUNDAMENTALS
# =====================================================================
INDUSTRY_FIELDS = ("pe", "pb", "peg", "roe", "de", "interest_cover", "sales_growth", "profit_growth")
BALANCE_SHEET_FIELDS = ("pb", "roe", "de", "interest_cover", "mcap")
FIN_COLS_FILED = "isin, period_end, period_label, period_type, consolidated, revenue, profit, eps, filed_on"
FIN_COLS_PLAIN = "isin, period_end, period_label, period_type, consolidated, revenue, profit, eps, NULL::date AS filed_on"
BS_SQL = """
SELECT * FROM (
    SELECT isin, period_end, period_type, kind, consolidated, net_worth, total_equity, paid_up, face_value, shares, debt, total_assets,
           profit_owners, profit_before_tax, finance_costs, depreciation,
           row_number() OVER (PARTITION BY isin ORDER BY period_end DESC) AS rn
    FROM stock_balance_sheet WHERE isin = ANY(%(isins)s)) t
WHERE rn <= 12
"""


def _num(v):
    return None if v is None else float(v)


def growth(now, before):
    """% growth of `now` over `before`, or None when `before` is zero or a loss (a percentage of that is not a number worth screening on)."""
    if now is None or before is None or before <= 0:
        return None
    return (now / before - 1.0) * 100.0


def year_ago(rows, latest):
    """the same quarter a year before `latest`, on the same basis (consolidated with consolidated), from rows newest first"""
    for r in rows:
        if r["consolidated"] == latest["consolidated"] and 330 <= (latest["period_end"] - r["period_end"]).days <= 400:
            return r
    return None


def growth_figures(rows):
    """sales and profit growth (year on year) from one stock's quarterly rows, newest first, the latest quarter against the same quarter a year earlier
    on the same basis; None where the earlier figure is zero or a loss."""
    if not rows:
        return {}
    latest = rows[0]
    ya = year_ago(rows, latest)
    if ya is None:
        return {}
    out = {"sales_growth": growth(_num(latest["revenue"]), _num(ya["revenue"])), "profit_growth": growth(_num(latest["profit"]), _num(ya["profit"]))}
    return out


def build_fundamentals(cur, prof):
    try:
        import api
        build_ratios = api.build_ratios
    except Exception as e:                                       # noqa: BLE001
        raise ProviderError("The valuation working lives in api.py, which could not be read here (%s)." % type(e).__name__)
    isins = list(prof["stocks"])
    floor = prof["market_date"] - timedelta(days=FIN_DAYS)
    master = {}
    cur.execute("SELECT isin, face_value FROM stock_master WHERE isin = ANY(%(i)s)", {"i": isins})
    for r in cur.fetchall():
        master[r["isin"]] = r["face_value"]
    values, have_bs = {}, True
    for at in range(0, len(isins), FIN_BATCH):
        batch = isins[at:at + FIN_BATCH]
        fin = _optional(cur, "SELECT %s FROM stock_financials WHERE isin = ANY(%%(i)s) AND period_type = 'quarterly' AND period_end >= %%(f)s "
                             "AND (revenue IS NOT NULL OR profit IS NOT NULL OR eps IS NOT NULL) ORDER BY isin, period_end DESC, consolidated DESC" % FIN_COLS_FILED,
                        {"i": batch, "f": floor})
        if fin is None:                                          # no filed_on column yet
            fin = _optional(cur, "SELECT %s FROM stock_financials WHERE isin = ANY(%%(i)s) AND period_type = 'quarterly' AND period_end >= %%(f)s "
                                 "AND (revenue IS NOT NULL OR profit IS NOT NULL OR eps IS NOT NULL) ORDER BY isin, period_end DESC, consolidated DESC" % FIN_COLS_PLAIN,
                            {"i": batch, "f": floor})
        if fin is None:
            raise ProviderError("The quarterly results table is not available.")
        by_fin = {}
        for r in fin:
            by_fin.setdefault(r["isin"], []).append(r)
        cur.execute("SELECT isin, kind, event_date, headline FROM stock_event WHERE isin = ANY(%(i)s) AND kind IN ('split', 'bonus') "
                    "AND event_date >= CURRENT_DATE - 1100 ORDER BY event_date", {"i": batch})
        by_ev = {}
        for r in cur.fetchall():
            by_ev.setdefault(r["isin"], []).append(r)
        bs = _optional(cur, BS_SQL, {"isins": batch})
        if bs is None:
            have_bs = False
            bs = []
        by_bs = {}
        for r in bs:
            by_bs.setdefault(r["isin"], []).append(r)
        for isin in batch:
            p = prof["stocks"][isin]
            price = {"close": p["close"], "date": p["date"], "change_pct": p["change_pct"]}
            rows = by_fin.get(isin, [])
            fin_rows = [dict(r, period_type="quarterly") for r in rows]
            try:
                r = build_ratios(fin_rows, by_ev.get(isin, []), price, today=prof["market_date"], bs_rows=by_bs.get(isin, []), face_value=master.get(isin))
            except Exception:                                    # noqa: BLE001  one odd company must not stop 2,500
                r = {}
            item = {k: (r.get(k) or {}).get("value") for k in ("pe", "peg", "pb", "roe", "de", "interest_cover")}
            item["mcap"] = (r.get("market_cap") or {}).get("value")
            item.update(growth_figures(rows))
            values[isin] = item
    # the industry average: the median over the industry's companies that have the ratio (positive for P/E, P/B and PEG), when enough of them do
    ref = {f: {} for f in INDUSTRY_FIELDS}
    by_ind = {}
    for isin, item in values.items():
        ind = prof["stocks"][isin].get("industry")
        if ind:
            by_ind.setdefault(ind, []).append(item)
    for ind, items in by_ind.items():
        for f in INDUSTRY_FIELDS:
            xs = sorted(i[f] for i in items if i.get(f) is not None and (f not in ("pe", "pb", "peg") or i[f] > 0))
            if len(xs) >= MIN_PEERS:
                n = len(xs)
                ref[f][ind] = (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0, n)
    return {"market_date": prof["market_date"], "stocks": values, "industry": ref, "balance_sheets": have_bs}


def fundamentals(cur):
    prof = profile(cur)
    return memo("fund", prof["market_date"], lambda: build_fundamentals(cur, prof))


# =====================================================================
# EVENTS
# =====================================================================
def events(cur):
    md = market_date(cur) or date.today()

    def build():
        rows = _optional(cur, "SELECT isin, min(event_date) AS d FROM stock_event WHERE kind = 'result' AND event_date >= %(t)s GROUP BY isin", {"t": md})
        if rows is None:
            raise ProviderError("The announced-dates table is not available.")
        return {"as_of": md, "stocks": {r["isin"]: (r["d"] - md).days for r in rows}}
    return memo("events", md, build, ttl=1800)


# =====================================================================
# THE CHART PATTERNS (the site's own screens, used as conditions)
# =====================================================================
def pattern_members(cur, key, params):
    """{isin: row} of every stock the pattern screen lists with these settings. Refuses a list that was cut short (an older module caps rows at 200,
    and a cut list would make an AND look wrong without saying so)."""
    import screener_api as S
    mod = None
    for name in ("dow", "fib", "flat", "breakout", "ipotrend", "hlow"):
        m = getattr(S, name, None)
        if m is not None and (key in getattr(m, "SCREEN_KEYS", ()) or key == getattr(m, "SCREEN", {}).get("key")):
            mod = m
            break
    if mod is None:
        raise ProviderError("That chart pattern is not on this server.")
    raw = {k: v for k, v in params.items()}
    if mod is getattr(S, "fib", None):
        res = mod.run_screen(cur, raw, None, 100000, S._meta, lambda c, rows: rows)
    else:
        res = mod.run_screen(cur, key, raw, None, 100000, S._meta, lambda c, rows: rows)
    if res.get("unavailable"):
        raise ProviderError(res["unavailable"])
    rows = res.get("rows", [])
    if res.get("total", len(rows)) > len(rows):
        raise ProviderError("The chart-pattern list was cut short on this server (an older version of the pattern files). Upload the current "
                            "dow_change.py, fib_retracement.py, expanded_flat.py and multiyear_breakout.py.")
    return {"rows": {r["isin"]: r for r in rows}, "as_of": res.get("as_of"), "considered": res.get("considered"), "set_aside": res.get("set_aside"),
            "testable": pattern_testable(cur, S, mod, params)}


def pattern_testable(cur, S, mod, params):
    """The stocks the pattern screen could actually TEST with these settings, or None when that cannot be told. A stock it set aside (too little history, a
    split the stored prices do not reflect, no recent price) is neither "has the pattern" nor "has not": it is unknown, so "has NOT happened" does not
    list it. Uses the screens' own rules (their MIN_BARS and STALE_DAYS, the bars' own unadjusted list)."""
    try:
        if hasattr(mod, "testable"):                             # the IPO trend screen knows its own universe (IPOs inside its window, with enough bars)
            return mod.testable(cur, params)
        if mod is getattr(S, "breakout", None):
            data = mod.get_data(cur)
            years = int(params.get("years", 5))
            return {i for i, st in data["stocks"].items() if years in st["valid"]} if data else set()
        bars = S.price_bars.get_bars(cur, params.get("timeframe", "DAILY"))
        if bars is None:
            return set()
        md = bars["market_date"].toordinal()
        return {isin for isin, b in bars["series"].items()
                if len(b) >= mod.MIN_BARS and md - b.d[-1] <= mod.STALE_DAYS and isin not in bars["unadjusted"]}
    except Exception:                                            # noqa: BLE001  not knowing is allowed; guessing is not
        return None


# =====================================================================
# WHAT CAN BE USED RIGHT NOW
# =====================================================================
def availability(cur):
    """{field_key: reason it cannot be used on this server right now}. Cheap, and checked at most every five minutes."""
    def build():
        out = {}
        S = None
        try:
            import screener_api as S
        except Exception:                                        # noqa: BLE001
            S = None
        for key in E.FIELDS:
            f = E.FIELDS[key]
            if f["needs"] == "pattern":
                known = S is not None and any(key in getattr(getattr(S, n, None), "SCREEN_KEYS", ()) or key == getattr(getattr(S, n, None), "SCREEN", {}).get("key")
                                              for n in ("dow", "fib", "flat", "breakout", "ipotrend", "hlow"))
                if not known:
                    out[key] = "This chart pattern is not on this server yet."
        # cheap checks only (the readings themselves are built when a screen needs them, or by warm_async): opening the page must not wait for them
        why = None
        try:
            import screener_api as S2
            if len(S2._q(cur, S2.LAST_TWO)) < 2:
                why = "There are not two months of fund portfolios on file yet."
        except Exception as e:                                   # noqa: BLE001
            why = "The fund-flow reading is not available on this server (%s)." % type(e).__name__
        if why:
            for k, f in E.FIELDS.items():
                if f["needs"] == "funds":
                    out[k] = why
        try:
            import ownership_screens as O
            rows = _optional(cur, O.QUARTERS_SQL, {"since": date.today() - timedelta(days=O.HISTORY_DAYS)})
            if rows is None:
                why = "The shareholding data has not been loaded on this server yet."
            elif not O.offered_quarters(rows, True, 1)[0]:
                why = "The breakdown of who holds what has not been read for two quarters in a row yet."
            else:
                why = None
        except ImportError:
            why = "The shareholding reading is not on this server."
        if why:
            for k, f in E.FIELDS.items():
                if f["needs"] == "own":
                    out[k] = why
        try:
            import api
            api.build_ratios
        except Exception:                                        # noqa: BLE001
            for k, f in E.FIELDS.items():
                if f["needs"] == "fund":
                    out[k] = "The valuation working (api.py) is not available on this server."
        else:
            rows = _optional(cur, "SELECT 1 FROM stock_balance_sheet LIMIT 1", {})
            if not rows:
                for k in ("pb", "roe", "de", "interest_cover", "mcap"):
                    out[k] = "The balance sheets have not been loaded on this server yet."
        return out
    return memo("availability", market_date(cur), build, ttl=300)


# =====================================================================
# READING ONE CONDITION FOR ONE STOCK
# =====================================================================
def tech_value(field, p, r):
    """The reading of technical field `field` with settings `p` from one stock's readings `r` on that time frame (None when it cannot be made)."""
    if r is None:
        return None
    if field == "rsi":
        return r["rsi"]
    if field == "rsi_cross":
        return T.crossed(r["rsi_tail"], int(p["within"]), p["direction"] == "up", float(p["level"]))
    if field == "macd_state":
        m, s = r["macd"], r["macd_signal"]
        if m is None or s is None:
            return None
        return {"above_signal": m > s, "below_signal": m < s, "above_zero": m > 0, "below_zero": m < 0}[p["state"]]
    if field == "macd_cross":
        return T.crossed_gap(r["hist_tail"], int(p["within"]), p["direction"] == "up")
    if field == "ma_dist":
        ma = r["ema"].get(int(p["period"])) if p["kind"] == "EMA" else r["sma"].get(int(p["period"]))
        return None if ma is None or ma <= 0 else (r["close"] / ma - 1.0) * 100.0
    if field == "ma_gap":
        a, b = r["ema"].get(int(p["fast"])), r["ema"].get(int(p["slow"]))
        return None if a is None or b is None or b <= 0 else (a / b - 1.0) * 100.0
    if field == "ema_cross":
        fa, sl = r["ema_tail"].get(int(p["fast"])), r["ema_tail"].get(int(p["slow"]))
        if fa is None or sl is None:
            return None
        return T.crossed_gap([a - b for a, b in zip(fa, sl)], int(p["within"]), p["direction"] == "up")
    if field == "from_high":
        hi = r.get("high52")
        return None if hi is None or hi <= 0 else max(0.0, (1.0 - r["close"] / hi) * 100.0)
    if field == "from_low":
        lo = r.get("low52")
        return None if lo is None or lo <= 0 else max(0.0, (r["close"] / lo - 1.0) * 100.0)
    if field == "ret":
        return (r.get("ret") or {}).get(str(p["bars"]))
    if field == "vol_ratio":
        return (r.get("vol_ratio") or {}).get(str(p["days"]))
    raise KeyError(field)


_PRICE_AT = {"O": 0, "H": 1, "L": 2, "C": 3}
CANDLE_FIELDS = ("candle_compare", "candle_diff", "candle_price", "candle_shape", "candle_streak")


def _candle(ohlc, tf, mode, bar):
    """(open, high, low, close) of the candle `bar` back from the newest (0 = the newest) on `tf`, or None. With mode "finished" a week or month still in progress
    is not counted, so bar 0 is the last finished one."""
    t = (ohlc or {}).get(tf)
    if not t:
        return None
    bars = t["bars"][:-1] if mode == "finished" and not t["done"] else t["bars"]
    return bars[-1 - bar] if 0 <= bar < len(bars) else None


def candle_value(field, p, ohlc):
    """The reading of a candle field from one stock's candles ({tf: {"bars", "done"}}), or None when a candle it needs does not exist."""
    mode, tf = p["bars"], p["tf"]
    if field in ("candle_compare", "candle_diff"):
        a = _candle(ohlc, tf, mode, int(p["left_bar"]))
        b = _candle(ohlc, p["tf"] if p["right_tf"] == "SAME" else p["right_tf"], mode, int(p["right_bar"]))
        if a is None or b is None:
            return None
        left, right = a[_PRICE_AT[p["left_price"]]], b[_PRICE_AT[p["right_price"]]]
        if field == "candle_compare":
            m = float(p["margin"]) / 100.0
            return left > right * (1.0 + m) if p["relation"] == "above" else left < right * (1.0 - m)
        if right <= 0:
            return None
        return (left / right - 1.0) * 100.0 if p["relation"] == "above" else (1.0 - left / right) * 100.0
    if field == "candle_price":
        a = _candle(ohlc, tf, mode, int(p["bar"]))
        return None if a is None else a[_PRICE_AT[p["price"]]]
    if field == "candle_shape":
        bar, shape = int(p["bar"]), p["shape"]
        a = _candle(ohlc, tf, mode, bar)
        if a is None:
            return None
        o, h, l, c = a
        if shape in ("bullish", "bearish"):
            return c > o if shape == "bullish" else c < o
        if shape in ("doji", "close_top", "close_bottom"):
            if h <= l:
                return None                                       # a bar with no range has no shape to speak of
            if shape == "doji":
                return abs(c - o) <= 0.10 * (h - l)
            return (c - l) >= 0.75 * (h - l) if shape == "close_top" else (c - l) <= 0.25 * (h - l)
        prev = _candle(ohlc, tf, mode, bar + 1)
        if prev is None:
            return None
        po, ph, pl, pc = prev
        return {"inside": h < ph and l > pl, "outside": h > ph and l < pl, "gap_up": o > ph, "gap_down": o < pl,
                "higher_hl": h > ph and l > pl, "lower_hl": h < ph and l < pl}[shape]
    if field == "candle_streak":
        n = int(p["count"])
        bars = [_candle(ohlc, tf, mode, i) for i in range(n + 1)]
        if any(b is None for b in bars):
            return None
        up = p["direction"] == "up"
        return all((bars[i][3] > bars[i + 1][3]) if up else (bars[i][3] < bars[i + 1][3]) for i in range(n))
    raise KeyError(field)


class Reader:
    """Everything one run needs: the providers its conditions use, read lazily, and read(cond, isin) -> (value, industry average)."""

    def __init__(self, cur, spec):
        self.cur, self.spec = cur, spec
        self.prof = profile(cur)
        self.problems = {}                 # condition n -> reason
        self.info = {}                     # provider -> as-of text
        self.left_out = {}
        self.conds = E.conditions(spec["rules"])
        self._tech = self._funds = self._own = self._fund = self._events = None
        self._patterns = {}
        for c in self.conds:
            self._prepare(c)

    def _prepare(self, c):
        f = E.FIELDS[c["field"]]
        need = f["needs"]
        try:
            if need == "tech":
                if self._tech is None:
                    self._tech = tech(self.cur)
                    self.info["technical"] = "prices to %s" % self._tech["market_date"]
                tf = c["params"].get("tf", "DAILY")
                if c["field"] in ("from_high", "from_low", "ret", "vol_ratio"):
                    tf = "DAILY"
                if c["field"] not in CANDLE_FIELDS:                # (the candles come from the daily bars whichever time frame is asked)
                    self.left_out[tf] = self._tech["left_out"].get(tf, {})
            elif need == "funds":
                if self._funds is None:
                    self._funds = funds(self.cur)
                    self.info["funds"] = "portfolios of %s against %s" % (self._funds["now"], self._funds["prev"])
            elif need == "own":
                if self._own is None:
                    self._own = ownership(self.cur)
                    self.info["ownership"] = "%s shareholding%s" % (self._own["label"], " (still being filed)" if self._own["partial"] else "")
            elif need == "fund":
                if self._fund is None:
                    self._fund = fundamentals(self.cur)
                    self.info["fundamentals"] = "results and balance sheets, prices of %s" % self._fund["market_date"]
                if not self._fund["balance_sheets"] and c["field"] in BALANCE_SHEET_FIELDS:
                    self.problems[c["n"]] = "The balance sheets have not been loaded on this server yet."
            elif need == "events":
                if self._events is None:
                    self._events = events(self.cur)
            elif need == "pattern":
                key = (c["field"], tuple(sorted((k, str(v)) for k, v in c["params"].items())))
                if key not in self._patterns:
                    self._patterns[key] = pattern_members(self.cur, c["field"], c["params"])
                    self.info.setdefault("patterns", []).append("%s to %s" % (E.FIELDS[c["field"]]["label"], self._patterns[key]["as_of"]))
        except ProviderError as e:
            self.problems[c["n"]] = str(e)

    def read(self, c, isin):
        if c["n"] in self.problems:
            return None, None
        f = E.FIELDS[c["field"]]
        need, p, k = f["needs"], c["params"], c["field"]
        st = self.prof["stocks"].get(isin)
        if need == "profile":
            return st["close"], None
        if need == "tech":
            stock = self._tech["stocks"].get(isin) or {}
            if k in CANDLE_FIELDS:
                return candle_value(k, p, stock.get("OHLC")), None
            tf = "DAILY" if k in ("from_high", "from_low", "ret", "vol_ratio") else p["tf"]
            return tech_value(k, p, stock.get(tf)), None
        if need == "funds":
            row = self._funds["stocks"].get(isin)
            return (row[k] if row else 0), None                    # a stock no fund holds has none that bought it
        if need == "own":
            row = self._own["stocks"].get(isin)
            return (row.get(k) if row else None), None
        if need == "fund":
            v = (self._fund["stocks"].get(isin) or {}).get(k)
            ref = None
            if c.get("ref") == "industry" and st.get("industry"):
                hit = self._fund["industry"].get(k, {}).get(st["industry"])
                ref = hit[0] if hit else None
            return v, ref
        if need == "events":
            return self._events["stocks"].get(isin), None
        if need == "pattern":
            key = (k, tuple(sorted((a, str(b)) for a, b in p.items())))
            pat = self._patterns[key]
            if isin in pat["rows"]:
                return True, None
            tested = pat.get("testable")
            return (False if tested is None or isin in tested else None), None        # not tested for it: unknown, not "no"
        raise KeyError(need)

    def pattern_row(self, c, isin):
        key = (c["field"], tuple(sorted((a, str(b)) for a, b in c["params"].items())))
        return self._patterns.get(key, {"rows": {}})["rows"].get(isin)


def cell_text(c, value, ref, row=None):
    """The value as the page shows it in the results table."""
    f = E.FIELDS[c["field"]]
    if value is None:
        return "no data"
    if f["kind"] == "flag":
        return "yes" if value else "no"
    text = E.value_text(f, value) if f["unit"] != "x" else ("%.*f" % (f["decimals"], value)).rstrip("0").rstrip(".") + "x"
    if ref is not None:
        text += " (industry %s)" % (("%.*f" % (f["decimals"], ref)).rstrip("0").rstrip(".") + ("x" if f["unit"] == "x" else "%" if f["unit"] == "%" else ""))
    return text


def run(cur, spec, limit=300, sort="size"):
    """A cleaned screen over every stock. Returns {"rows", "total", "tested", "unknown", "counts", "problems", "as_of", "notes", "left_out"}."""
    limit = max(1, min(int(limit), MAX_ROWS))
    sort = sort if sort in SORTS else "size"
    rd = Reader(cur, spec)
    uni = spec["universe"]
    listed = ipo_listed(cur, uni["ipo"]) if uni.get("ipo") else None
    isins = []
    for isin, st in rd.prof["stocks"].items():
        if listed is not None and isin not in listed:
            continue
        if uni["caps"] and st["cap"] not in uni["caps"]:
            continue
        if uni["sectors"] and st["sector"] not in uni["sectors"]:
            continue
        if uni["industries"] and st["industry"] not in uni["industries"]:
            continue
        isins.append(isin)
    res = E.evaluate(spec, isins, rd.read)
    conds = rd.conds
    rows = []
    for isin, cells in res["matches"]:
        st = rd.prof["stocks"][isin]
        out_cells = []
        for c in conds:
            cell = cells[c["n"]]
            out_cells.append({"n": c["n"], "value": cell["value"] if not isinstance(cell["value"], float) else round(cell["value"], 4),
                              "text": cell_text(c, cell["value"], cell["ref"]), "ok": cell["ok"]})
        rows.append({"isin": isin, "symbol": st["symbol"], "name": st["name"], "sector": st["sector"], "industry": st["industry"], "cap": st["cap"],
                     "close": st["close"], "date": str(st["date"]), "change_pct": st["change_pct"], "cells": out_cells, "_size": st["size"]})
    if sort == "name":
        rows.sort(key=lambda r: r["name"] or "")
    else:
        rows.sort(key=lambda r: (-(r["_size"] or 0), r["name"] or ""))
    total = len(rows)
    rows = rows[:limit]
    for r in rows:
        r.pop("_size", None)
    counts = [{"n": c["n"], "text": E.sentence(c), **res["counts"][c["n"]]} for c in conds]
    problems = [{"n": n, "text": E.sentence(next(c for c in conds if c["n"] == n)), "reason": why} for n, why in sorted(rd.problems.items())]
    notes = []
    left = rd.left_out
    for tf, v in left.items():
        if v.get("step") or v.get("short"):
            notes.append("On %s bars, %d stocks have too little history and %d have a jump in the stored prices that no split or bonus explains: they have no "
                         "reading there." % (E.TF_WORD[tf], v.get("short", 0), v.get("step", 0)))
    out = {"rows": rows, "total": total, "shown": len(rows), "tested": res["tested"], "unknown": res["unknown"], "counts": counts,
           "problems": problems, "as_of": str(rd.prof["market_date"]), "sources": rd.info, "notes": notes,
           "stale": rd.prof["stale"], "universe_size": len(rd.prof["stocks"])}
    if listed is not None:
        out["ipo"] = {"listed": len(listed), "window": E.ipo_words(uni["ipo"])}
    return out


def ipo_listed(cur, ipo):
    """{isin: listing info} of the IPOs inside a screen's 'only IPOs' choice (the exchange's listing date). Raises ProviderError where this server has no IPO list."""
    try:
        import ipo_universe as U
    except ImportError:
        raise ProviderError("The IPO list is not on this server yet, so a screen cannot be narrowed to IPOs.")
    try:
        return U.select(U.listings(cur), ipo.get("within"), date.fromisoformat(ipo["from"]) if ipo.get("from") else None,
                        date.fromisoformat(ipo["to"]) if ipo.get("to") else None)
    except U.NotSetUp as e:
        raise ProviderError(str(e))
