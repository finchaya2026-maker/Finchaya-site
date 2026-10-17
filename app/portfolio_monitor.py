"""
portfolio_monitor.py -- the monthly plan report and the plan alerts.
---------------------------------------------------------------------------------------------------
For every plan a member has locked ("Lock it and track it"), this does two things and nothing else:

  MONTHLY REPORT   once a month, between the 11th and the 15th (AMCs publish portfolios by the 10th), one email per plan:
                   are the funds still beating their benchmarks, are they still in the top quarter of their category, is the
                   plan still diversified, and what changed since last month.
  ALERTS           an email when one of three things has happened:
                     behind_benchmark   a fund's 1-year return was below its benchmark's at each of the last three month-ends
                     rank_slip          a fund was in the bottom half of its category (1-year return) at each of the last three
                                        month-ends   ("bottom half" = third or fourth quarter; the member can move the line)
                     overlap_high       two funds in the plan hold more than 50% of the same companies   (the member can move the line)
                   Each condition is told once when it starts, not every night, and told again if it clears and comes back.

    python portfolio_monitor.py --alerts                 evaluate every plan, email what is new           (run nightly)
    python portfolio_monitor.py --monthly                send this month's report where it is due          (run daily, acts on the 11th to 15th)
    python portfolio_monitor.py --preview 12             build plan 12's report and alert as HTML files, send nothing, write nothing
    python portfolio_monitor.py --send-test 12 --to me@example.com [--sample-alert]
                                                         email plan 12's monthly check (and a labelled sample alert) to ME, not to its owner; writes nothing
    python portfolio_monitor.py --list                   the plans it covers (number, name, owner, funds, age, eligible?), and what it leaves out
    python portfolio_monitor.py --status                 when it last ran, what it did, and whether email is set up
    options: --portfolio N   only that plan        --today YYYY-MM-DD   pretend it is that day        --dry-run   send and write nothing
             --force         ignore the 11th-15th window and the data-ready wait (for testing)

WHAT IT NEVER DOES
    It does not tell anyone to buy, sell or switch, it carries no scores, and it sends nothing about a plan whose owner no longer has
    the subscription. Every number is a fact about what a fund has returned or held, with the date it is as at. See DEPLOY_ALERTS.md.

WHY EVERYTHING IS RECOMPUTED FROM NAVs
    "Three months running" needs the fund's position at three past month-ends. Nothing here depends on a history somebody remembered
    to store: the month-end NAVs of the fund, its category's funds and the benchmark are all in the database, so the same code answers
    for any month and a late data load cannot leave a hole that silently ends a streak.
"""

import calendar
import html as _html
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

IST = timezone(timedelta(hours=5, minutes=30))
STREAK = 3                       # month-ends in a row
REPORT_FIRST_DAY, REPORT_LAST_DAY = 11, 15
QUIET_DAYS = 14                  # a plan is left alone for this long after it is locked: the first report covers it
MAX_STALE_DAYS = 7               # a NAV older than this at a month end means the fund has stopped (merged, closed): not measured
MIN_PEERS = 8                    # a quarter of fewer than eight funds means nothing
MAX_EMAIL_ATTEMPTS = 5
DEFAULT_OVERLAP_LINE = 50.0
DEFAULT_QUARTILE_LINE = 3        # "third quarter or lower"

# The rules this checker actually evaluates. saved_portfolio_api.ALERT_RULES is the catalogue the screen shows; both read these keys.
LIVE_RULES = ("behind_benchmark", "rank_slip", "overlap_high")
QUARTER_WORDS = {1: "top quarter", 2: "second quarter", 3: "third quarter", 4: "bottom quarter"}

DISCLAIMER = ("FinChaya is a do-it-yourself (DIY) tool: the plan is yours, and nothing in this email is a recommendation by FinChaya. "
              "Educational information only, not investment advice. FinChaya is not currently registered with SEBI as an Investment Adviser or "
              "Research Analyst, does not tell you to buy, sell or switch anything, and does not execute anything. Past returns do not "
              "guarantee future returns. Mutual fund investments are subject to market risks.")


# =====================================================================
# DATES
# =====================================================================
def month_end(y, m):
    return date(y, m, calendar.monthrange(y, m)[1])


def prev_month_end(d):
    return d.replace(day=1) - timedelta(days=1)


def latest_month_end(on_or_before):
    """The last calendar month end on or before a date."""
    me = month_end(on_or_before.year, on_or_before.month)
    return me if on_or_before >= me else prev_month_end(on_or_before)


def month_ends_back(latest, n):
    out = [latest]
    while len(out) < n:
        out.append(prev_month_end(out[-1]))
    return out


def years_before(me, years):
    return month_end(me.year - years, me.month)


def today_ist():
    return datetime.now(IST).date()


def fmt_date(d):
    return d.strftime("%d %b %Y").lstrip("0")


def category_phrase(category):
    """'Flexi Cap Fund' -> 'flexi cap funds', 'ELSS' -> 'ELSS funds'."""
    c = (category or "its category").strip()
    if c.upper() == c:
        return c + " funds"
    return (c[:-5] if c.endswith(" Fund") else c).lower() + " funds"


# =====================================================================
# SMALL PURE RULES (what "three in a row" and "quarter" mean)
# =====================================================================
def streak_length(flags):
    """How many of the NEWEST readings in a row are True. `flags` is newest first; None (not measurable that month) ends the run, because a
    run that cannot be seen is not a run."""
    n = 0
    for f in flags:
        if f is True:
            n += 1
        else:
            break
    return n


def quarter_of(rank, n):
    """1 = top quarter ... 4 = bottom quarter, for rank 1..n (1 = best)."""
    return min(4, (rank - 1) * 4 // n + 1)


def rank_of(value, others):
    return 1 + sum(1 for o in others if o > value)


def pct(x, nd=1):
    return "-" if x is None else "%.*f%%" % (nd, x * 100)


def pts(x, nd=1):
    """Percentage-point gap, signed."""
    return "-" if x is None else "%+.*f points" % (nd, x * 100)


# =====================================================================
# DATA ACCESS (read-only, with small caches so one run asks each question once)
# =====================================================================
class Store:
    def __init__(self, cur):
        self.cur = cur
        self._info, self._canon, self._peers, self._cat_rets = {}, {}, {}, {}
        self._bench_last, self._bench_name, self._cat_bench = None, None, None

    def q(self, sql, params=None):
        self.cur.execute(sql, params or {})
        return self.cur.fetchall()

    # ---- funds ----
    def canonical(self, codes):
        need = [c for c in codes if c not in self._canon]
        if need:
            rows = self.q("""
                SELECT s.scheme_code, COALESCE(c.canonical_scheme_code, s.scheme_code) AS canon
                FROM mf_scheme s
                LEFT JOIN v_fund_canonical c ON c.scheme_name = s.scheme_name AND c.amc_name IS NOT DISTINCT FROM s.amc_name
                WHERE s.scheme_code = ANY(%(c)s)""", {"c": need})
            for r in rows:
                self._canon[r["scheme_code"]] = r["canon"]
            for c in need:
                self._canon.setdefault(c, c)
        return {c: self._canon[c] for c in codes}

    def category_benchmarks(self):
        """The benchmark most of a category's mapped schemes point at: the fallback for a scheme with no mapping of its own."""
        if self._cat_bench is None:
            rows = self.q("""
                SELECT c.category, m.benchmark_id, COUNT(*) AS n
                FROM mf_benchmark_map m JOIN v_scheme_category c ON c.scheme_code = m.scheme_code
                WHERE m.benchmark_id IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 3 DESC""")
            self._cat_bench = {}
            for r in rows:
                self._cat_bench.setdefault(r["category"], r["benchmark_id"])
        return self._cat_bench

    def fund_info(self, codes):
        need = [c for c in codes if c not in self._info]
        if need:
            canon = self.canonical(need)
            codes_all = sorted(set(need) | set(canon.values()))
            rows = self.q("""
                SELECT s.scheme_code, s.scheme_name, s.plan_type, s.option_type, c.category, COALESCE(c.rank_meaningful, false) AS rank_meaningful,
                       (SELECT m.benchmark_id FROM mf_benchmark_map m WHERE m.scheme_code = s.scheme_code AND m.benchmark_id IS NOT NULL LIMIT 1) AS bm_map,
                       (SELECT r.benchmark_id FROM mf_returns r WHERE r.scheme_code = s.scheme_code AND r.benchmark_id IS NOT NULL
                          ORDER BY r.as_of_date DESC LIMIT 1) AS bm_ret
                FROM mf_scheme s LEFT JOIN v_scheme_category c ON c.scheme_code = s.scheme_code
                WHERE s.scheme_code = ANY(%(c)s)""", {"c": codes_all})
            raw = {r["scheme_code"]: r for r in rows}
            cat_bench = self.category_benchmarks()
            for c in need:
                r = raw.get(c)
                if not r:
                    self._info[c] = None
                    continue
                twin = raw.get(canon[c])
                bm = r["bm_map"] or r["bm_ret"] or (twin and (twin["bm_map"] or twin["bm_ret"])) or cat_bench.get(r["category"])
                self._info[c] = {"code": c, "name": r["scheme_name"], "plan": (r["plan_type"] or "").upper() or None,
                                 "option": r["option_type"], "category": r["category"], "rank_meaningful": bool(r["rank_meaningful"]),
                                 "benchmark_id": bm}
        return {c: self._info[c] for c in codes}

    def peers(self, category, plan):
        """Codes of the funds a fund is ranked against: same category, same kind of plan, growth option, comparable."""
        key = (category, plan)
        if key not in self._peers:
            rows = self.q("""
                SELECT s.scheme_code FROM mf_scheme s JOIN v_scheme_category c ON c.scheme_code = s.scheme_code
                WHERE c.category = %(cat)s AND c.rank_meaningful AND s.plan_type = %(plan)s AND s.option_type = 'GROWTH'""",
                          {"cat": category, "plan": plan})
            self._peers[key] = [r["scheme_code"] for r in rows]
        return self._peers[key]

    # ---- NAV and index values at given dates (last value on or before, not older than MAX_STALE_DAYS) ----
    def navs(self, codes, dates):
        out = {}
        if not codes or not dates:
            return out
        rows = self.q("""
            SELECT c.code, t.d, n.nav_date, n.nav
            FROM unnest(%(codes)s::text[]) AS c(code) CROSS JOIN unnest(%(dates)s::date[]) AS t(d)
            LEFT JOIN LATERAL (SELECT nav_date, nav FROM mf_nav WHERE scheme_code = c.code AND nav_date <= t.d AND nav > 0
                               ORDER BY nav_date DESC LIMIT 1) n ON true""", {"codes": list(codes), "dates": list(dates)})
        for r in rows:
            ok = r["nav"] is not None and (r["d"] - r["nav_date"]).days <= MAX_STALE_DAYS
            out[(r["code"], r["d"])] = float(r["nav"]) if ok else None
        return out

    def bench_values(self, bid, dates):
        rows = self.q("""
            SELECT t.d, b.index_date, b.index_value
            FROM unnest(%(dates)s::date[]) AS t(d)
            LEFT JOIN LATERAL (SELECT index_date, index_value FROM benchmark_nav WHERE benchmark_id = %(b)s AND index_date <= t.d AND index_value > 0
                               ORDER BY index_date DESC LIMIT 1) b ON true""", {"dates": list(dates), "b": bid})
        out = {}
        for r in rows:
            ok = r["index_value"] is not None and (r["d"] - r["index_date"]).days <= MAX_STALE_DAYS
            out[r["d"]] = float(r["index_value"]) if ok else None
        return out

    def bench_last(self):
        if self._bench_last is None:
            self._bench_last = {r["benchmark_id"]: r["d"] for r in self.q(
                "SELECT benchmark_id, MAX(index_date) AS d FROM benchmark_nav GROUP BY 1")}
        return self._bench_last

    def bench_name(self, bid):
        if self._bench_name is None:
            self._bench_name = {r["benchmark_id"]: r["display_name"] for r in self.q("SELECT benchmark_id, display_name FROM benchmark_master")}
        return self._bench_name.get(bid) or "its benchmark"

    # ---- the whole category's 1-year returns at each month end (for the quarter) ----
    def category_returns(self, category, plan, month_ends):
        """{month_end: {code: 1-year return}} for every comparable fund in the category. Cached per run."""
        key = (category, plan, tuple(month_ends))
        if key not in self._cat_rets:
            codes = self.peers(category, plan)
            dates = sorted(set(month_ends) | {years_before(m, 1) for m in month_ends})
            navs = self.navs(codes, dates)
            out = {}
            for me in month_ends:
                ye = years_before(me, 1)
                d = {}
                for c in codes:
                    a, b = navs.get((c, me)), navs.get((c, ye))
                    if a and b:
                        d[c] = a / b - 1.0
                out[me] = d
            self._cat_rets[key] = out
        return self._cat_rets[key]

    def disclosures(self, codes):
        """Each fund's newest portfolio date."""
        rows = self.q("SELECT scheme_code, MAX(as_of_date) AS d FROM mf_holding WHERE scheme_code = ANY(%(c)s) GROUP BY 1", {"c": list(codes)})
        return {r["scheme_code"]: r["d"] for r in rows}


# =====================================================================
# ONE FUND, THREE MONTH ENDS
# =====================================================================
def assess_fund(store, code, month_ends, quarter_line=DEFAULT_QUARTILE_LINE):
    """What a fund looked like at each month end (newest first): its 1-year return, its benchmark's, the gap, and its quarter in its category.

    Returns a dict that always has a `series` and the two streaks, and says in `notes` why a measure is missing rather than leaving it blank.
    """
    info = store.fund_info([code])[code]
    out = {"code": code, "name": None, "plan": None, "category": None, "benchmark_id": None, "benchmark": None, "series": [],
           "behind_streak": 0, "slip_streak": 0, "notes": [], "bench_measured": False, "rank_measured": False, "latest": None}
    if not info:
        out["notes"].append("This fund is not in FinChaya's list.")
        return out
    out.update(name=info["name"], plan=info["plan"], category=info["category"], benchmark_id=info["benchmark_id"])
    ye = [years_before(m, 1) for m in month_ends]
    dates = sorted(set(month_ends) | set(ye))
    navs = store.navs([code], dates)

    bench = {}
    if info["benchmark_id"]:
        out["benchmark"] = store.bench_name(info["benchmark_id"])
        bench = store.bench_values(info["benchmark_id"], dates)
    else:
        out["notes"].append("No benchmark is mapped to this fund, so it is not compared with one.")

    can_rank = bool(info["rank_meaningful"] and info["plan"] in ("DIRECT", "REGULAR") and info["category"])
    cat = store.category_returns(info["category"], info["plan"], month_ends) if can_rank else {}
    if not can_rank:
        out["notes"].append("Funds in this category are not ranked against each other, so there is no quarter to report.")

    for me, y in zip(month_ends, ye):
        a, b = navs.get((code, me)), navs.get((code, y))
        fret = (a / b - 1.0) if (a and b) else None
        row = {"month_end": me, "fund": fret, "bench": None, "excess": None, "behind": None, "rank": None, "n": None, "quarter": None, "slipped": None}
        if info["benchmark_id"] and fret is not None:
            bi, bj = bench.get(me), bench.get(y)
            if bi and bj:
                row["bench"] = bi / bj - 1.0
                row["excess"] = fret - row["bench"]
                row["behind"] = row["excess"] < 0
        if can_rank and fret is not None:
            peers = dict(cat.get(me, {}))
            peers[code] = fret
            if len(peers) >= MIN_PEERS:
                row["rank"] = rank_of(fret, [v for c, v in peers.items() if c != code])
                row["n"] = len(peers)
                row["quarter"] = quarter_of(row["rank"], row["n"])
                row["slipped"] = row["quarter"] >= quarter_line
        out["series"].append(row)

    out["bench_measured"] = any(r["behind"] is not None for r in out["series"])
    out["rank_measured"] = any(r["quarter"] is not None for r in out["series"])
    if can_rank and not out["rank_measured"]:
        out["notes"].append("Too few comparable funds in this category to give a quarter.")
    out["behind_streak"] = streak_length([r["behind"] for r in out["series"]])
    out["slip_streak"] = streak_length([r["slipped"] for r in out["series"]])
    out["latest"] = out["series"][0] if out["series"] else None
    return out


def longer_windows(store, code, month_end_, bench_id):
    """The fund against its benchmark over 3 and 5 years at one month end (compound annual returns), for the report's table."""
    out = {}
    for y in (3, 5):
        d0 = years_before(month_end_, y)
        navs = store.navs([code], [month_end_, d0])
        a, b = navs.get((code, month_end_)), navs.get((code, d0))
        fund = (a / b) ** (1.0 / y) - 1.0 if (a and b) else None
        bench = None
        if bench_id:
            bv = store.bench_values(bench_id, [month_end_, d0])
            if bv.get(month_end_) and bv.get(d0):
                bench = (bv[month_end_] / bv[d0]) ** (1.0 / y) - 1.0
        out[y] = {"fund": fund, "bench": bench, "beat": (fund > bench) if (fund is not None and bench is not None) else None}
    return out


# =====================================================================
# THE PLAN AS A WHOLE
# =====================================================================
def plan_overlap(store, holdings):
    """Every pair's overlap, the same way the Plan page does it (selector.pairwise_overlap: each pair on a date BOTH funds published)."""
    import selector
    canon = store.canonical([h["code"] for h in holdings])
    by_canon = {}
    for h in holdings:
        by_canon.setdefault(canon[h["code"]], h)
    codes = sorted(by_canon)
    if len(codes) < 2:
        return []
    ov, asof = selector.pairwise_overlap(store.cur, codes)
    pairs = []
    for (a, b), v in ov.items():
        pairs.append({"a": a, "b": b, "a_name": by_canon[a]["name"], "b_name": by_canon[b]["name"], "pct": float(v),
                      "as_of": asof.get((a, b))})
    pairs.sort(key=lambda p: -p["pct"])
    return pairs


def plan_makeup(store, holdings, weights):
    """Companies, concentration and the large/mid/small split of the whole plan, from each fund's newest portfolio."""
    canon = store.canonical([h["code"] for h in holdings])
    codes = sorted(set(canon.values()))
    latest = store.disclosures(codes)
    wt = {}
    for h in holdings:
        c = canon[h["code"]]
        wt[c] = wt.get(c, 0.0) + weights[h["code"]]
    tot_w = sum(wt.values()) or 1.0
    rows = []
    for c in codes:
        d = latest.get(c)
        if not d:
            continue
        rows += store.q("""
            SELECT h.scheme_code, h.isin, h.pct_of_nav, COALESCE(cc.cap_class, 'Unclassified') AS cap
            FROM mf_holding h JOIN stock_master m ON m.isin = h.isin
            LEFT JOIN stock_cap_class cc ON cc.isin = h.isin
            WHERE h.scheme_code = %(c)s AND h.as_of_date = %(d)s AND h.pct_of_nav IS NOT NULL AND h.pct_of_nav > 0""", {"c": c, "d": d})
    by_fund = {}
    for r in rows:
        by_fund.setdefault(r["scheme_code"], []).append(r)
    stock, cap, eq_total = {}, {}, 0.0
    for c, rs in by_fund.items():
        fund_eq = sum(float(r["pct_of_nav"]) for r in rs) or 1.0
        for r in rs:
            w = (wt[c] / tot_w) * (float(r["pct_of_nav"]) / fund_eq)
            stock[r["isin"]] = stock.get(r["isin"], 0.0) + w
            cap[r["cap"]] = cap.get(r["cap"], 0.0) + w
            eq_total += w
    if not stock or eq_total <= 0:
        return None
    ws = sorted((v / eq_total for v in stock.values()), reverse=True)
    return {"companies": len(ws), "effective": round(1.0 / sum(w * w for w in ws)), "top10": round(100 * sum(ws[:10]), 1),
            "large": round(100 * cap.get("Large", 0) / eq_total, 1), "mid": round(100 * cap.get("Mid", 0) / eq_total, 1),
            "small": round(100 * cap.get("Small", 0) / eq_total, 1), "other": round(100 * (eq_total - sum(cap.get(k, 0) for k in ("Large", "Mid", "Small"))) / eq_total, 1)}


def evaluate_plan(store, plan, today, settings=None):
    """Everything the report and the alerts need for one plan, on `today`. Reads only. See _found_events for what counts as an alert."""
    settings = settings or {"rules": {}, "overlap_line": DEFAULT_OVERLAP_LINE, "quarter_line": DEFAULT_QUARTILE_LINE}
    codes = [h["code"] for h in plan["holdings"]]
    infos = store.fund_info(codes)
    limit = today - timedelta(days=1)
    target = latest_month_end(limit)                 # the month end this run is about
    bl = store.bench_last()
    bids = [infos[c]["benchmark_id"] for c in codes if infos.get(c) and infos[c]["benchmark_id"] in bl]
    if bids:
        limit = min(limit, min(bl[b] for b in bids))  # ... but a benchmark that has not reached it yet holds the reading back
    latest = latest_month_end(limit)
    month_ends = month_ends_back(latest, STREAK)

    funds = []
    for h in plan["holdings"]:
        a = assess_fund(store, h["code"], month_ends, settings["quarter_line"])
        a["amount"] = h.get("amount")
        a["windows"] = longer_windows(store, h["code"], latest, a["benchmark_id"]) if a["latest"] and a["latest"]["fund"] is not None else {}
        funds.append(a)
        h["name"] = a["name"] or h["code"]

    n = len(funds)
    known = [f["amount"] for f in funds if f.get("amount")]
    weights = {f["code"]: ((float(f["amount"]) if f.get("amount") else (sum(known) / len(known) if known else 1.0))) for f in funds}
    pairs = plan_overlap(store, plan["holdings"]) if n >= 2 else []
    makeup = plan_makeup(store, plan["holdings"], weights)
    disclosed = store.disclosures(sorted(set(store.canonical(codes).values())))
    ev = {"as_at": latest, "target": target, "month_ends": month_ends, "funds": funds, "pairs": pairs, "makeup": makeup, "disclosed": disclosed,
          "today": today, "overlap_line": settings["overlap_line"], "quarter_line": settings["quarter_line"]}
    ev["found"], ev["evaluated"] = _found_events(ev, settings)
    return ev


def _found_events(ev, settings):
    """(found, evaluated): the conditions that hold now, and the (rule, fund, other) keys we were able to judge either way. Only what was
    judged can be cleared: a fund whose benchmark data is late is neither flagged nor cleared."""
    found, judged = [], set()
    rules = settings["rules"]
    on = lambda k: rules.get(k, {}).get("enabled", True)
    for f in ev["funds"]:
        s = f["series"]
        if on("behind_benchmark") and len(s) >= STREAK and all(r["behind"] is not None for r in s[:STREAK]):
            judged.add(("behind_benchmark", f["code"], ""))
            if f["behind_streak"] >= STREAK:
                found.append({"rule_key": "behind_benchmark", "scheme_code": f["code"], "other_code": "",
                              "sentence": "%s returned less than its benchmark (%s) over the year to each of the last %d month ends."
                                          % (f["name"], f["benchmark"], STREAK),
                              "evidence": {"rows": [{"month_end": str(r["month_end"]), "fund": r["fund"], "bench": r["bench"], "excess": r["excess"]} for r in s[:STREAK]]}})
        if on("rank_slip") and len(s) >= STREAK and all(r["quarter"] is not None for r in s[:STREAK]):
            judged.add(("rank_slip", f["code"], ""))
            if f["slip_streak"] >= STREAK:
                line = QUARTER_WORDS[ev["quarter_line"]]
                found.append({"rule_key": "rank_slip", "scheme_code": f["code"], "other_code": "",
                              "sentence": "%s was in the %s or lower of the %s (1-year return) at each of the last %d month ends."
                                          % (f["name"], line, category_phrase(f["category"]), STREAK),
                              "evidence": {"rows": [{"month_end": str(r["month_end"]), "fund": r["fund"], "rank": r["rank"], "n": r["n"], "quarter": r["quarter"]} for r in s[:STREAK]],
                                           "line": ev["quarter_line"]}})
    if on("overlap_high"):
        for p in ev["pairs"]:
            a, b = sorted((p["a"], p["b"]))
            judged.add(("overlap_high", a, b))
            if p["pct"] > ev["overlap_line"]:
                found.append({"rule_key": "overlap_high", "scheme_code": a, "other_code": b,
                              "sentence": "%s and %s hold %.0f%% of the same companies (your line is %.0f%%), as at %s."
                                          % (p["a_name"], p["b_name"], p["pct"], ev["overlap_line"], fmt_date(p["as_of"]) if p["as_of"] else "their latest portfolios"),
                              "evidence": {"pct": p["pct"], "as_of": str(p["as_of"]) if p["as_of"] else None, "line": ev["overlap_line"],
                                           "a_name": p["a_name"], "b_name": p["b_name"]}})
    return found, judged


# =====================================================================
# WHO, AND WHAT THEY HAVE ASKED FOR
# =====================================================================
def load_plans(cur, only=None):
    """Locked plans of members. A PLAN is a saved portfolio that has a goal: that is what "Lock it and track it" creates, and the lock dialog is
    where the emails are explained. A portfolio somebody merely saved on the Portfolios page, with no goal, never saw that wording and is left
    alone. Distributors' client portfolios are left out too (a later phase): the owner here is the person it is about."""
    cur.execute("""
        SELECT p.portfolio_id, p.name, p.owner_user_id, p.created_at, u.email, u.full_name, u.is_active, u.is_premium, u.premium_until, u.premium_scope,
               EXISTS (SELECT 1 FROM app_admin a WHERE a.user_id = u.user_id) AS is_admin
        FROM portfolio p JOIN mf_user u ON u.user_id = p.owner_user_id
        WHERE p.client_id IS NULL
          AND EXISTS (SELECT 1 FROM portfolio_goal g WHERE g.portfolio_id = p.portfolio_id)
          AND (%(only)s::bigint IS NULL OR p.portfolio_id = %(only)s)
        ORDER BY p.portfolio_id""", {"only": only})
    plans = cur.fetchall()
    for p in plans:
        cur.execute("""
            SELECT h.scheme_code AS code, h.amount, h.plan_type FROM portfolio_holding h WHERE h.portfolio_id = %(p)s ORDER BY h.added_at, h.scheme_code""",
                    {"p": p["portfolio_id"]})
        p["holdings"] = [dict(r, amount=float(r["amount"]) if r["amount"] is not None else None) for r in cur.fetchall()]
    return plans


def why_not_a_plan(cur, pid):
    """The sentence for 'that is not a plan the checker covers', so nobody has to guess which of four reasons it is."""
    cur.execute("""SELECT p.portfolio_id, p.name, p.client_id,
                          EXISTS (SELECT 1 FROM portfolio_goal g WHERE g.portfolio_id = p.portfolio_id) AS has_goal
                   FROM portfolio p WHERE p.portfolio_id = %(p)s""", {"p": pid})
    r = cur.fetchone()
    if r is None:
        cur.execute("SELECT COUNT(*) AS n FROM portfolio_goal")
        n = cur.fetchone()["n"]
        return ("There is no portfolio numbered %s. Run --list to see the plans that exist%s." % (
            pid, "" if n else ". There are none yet: lock a plan on the site first (Plan page, then Lock it and track it)"))
    if r["client_id"] is not None:
        return "Portfolio %s (%s) is a distributor's client portfolio, which the checker does not cover yet." % (pid, r["name"])
    if not r["has_goal"]:
        return ("Portfolio %s (%s) was saved without a goal, so it is not a locked plan and gets no emails. "
                "Open it on the Portfolios page and use Plan this portfolio, or lock a new plan on the Plan page." % (pid, r["name"]))
    return "Portfolio %s is not covered by the checker." % pid


def list_plans(conn, today=None):
    """What the checker would look at, one line per plan, without sending or changing anything."""
    today = today or today_ist()
    cur = conn.cursor(row_factory=dict_row)
    plans = load_plans(cur)
    print("%d locked plan(s):" % len(plans))
    for p in plans:
        who = p["email"]
        masked = mask_email(who)
        s = plan_settings(cur, p["portfolio_id"])
        age = age_days(p, today)
        print("  #%-4d %-28s %s  %d fund(s)  locked %d day(s) ago  %s%s%s" % (
            p["portfolio_id"], (p["name"] or "")[:28], masked, len(p["holdings"]), age,
            "eligible" if entitled(p, today) else "NOT ELIGIBLE (no mutual-fund subscription)",
            "; in its first %d days, no alerts yet" % QUIET_DAYS if age < QUIET_DAYS else "",
            "" if s["monthly"] else "; monthly check switched off"))
    cur.execute("""SELECT COUNT(*) FILTER (WHERE p.client_id IS NOT NULL) AS clients,
                          COUNT(*) FILTER (WHERE p.client_id IS NULL AND NOT EXISTS (SELECT 1 FROM portfolio_goal g WHERE g.portfolio_id = p.portfolio_id)) AS nogoal
                   FROM portfolio p""")
    r = cur.fetchone()
    print("not covered: %d saved portfolio(s) without a goal, %d distributor client portfolio(s)" % (r["nogoal"], r["clients"]))


def entitled(plan, today):
    """May this person still receive these? Same test as the paywall: an active account with an MF (or all) subscription, or an admin."""
    if not plan["is_active"]:
        return False
    if plan["is_admin"]:
        return True
    if not plan["is_premium"]:
        return False
    if plan["premium_until"] is not None and plan["premium_until"] < today:
        return False
    return (plan["premium_scope"] or "all") in ("mf", "all")


def plan_settings(cur, pid):
    """What this plan's owner has chosen. The three live rules and email are ON unless a row says otherwise; the monthly report likewise."""
    rules = {k: {"enabled": True, "threshold": None} for k in LIVE_RULES}
    cur.execute("SELECT rule_key, enabled, threshold FROM portfolio_alert WHERE portfolio_id = %(p)s", {"p": pid})
    for r in cur.fetchall():
        if r["rule_key"] in rules:
            rules[r["rule_key"]] = {"enabled": bool(r["enabled"]), "threshold": float(r["threshold"]) if r["threshold"] is not None else None}
    email = True
    cur.execute("SELECT enabled FROM portfolio_alert_channel WHERE portfolio_id = %(p)s AND channel = 'email'", {"p": pid})
    row = cur.fetchone()
    if row is not None:
        email = bool(row["enabled"])
    monthly = True
    cur.execute("SELECT monthly FROM portfolio_report_pref WHERE portfolio_id = %(p)s", {"p": pid})
    row = cur.fetchone()
    if row is not None:
        monthly = bool(row["monthly"])
    ol = rules["overlap_high"]["threshold"]
    ql = rules["rank_slip"]["threshold"]
    return {"rules": rules, "email": email, "monthly": monthly,
            "overlap_line": float(ol) if ol is not None else DEFAULT_OVERLAP_LINE,
            "quarter_line": int(ql) if ql is not None and 2 <= int(ql) <= 4 else DEFAULT_QUARTILE_LINE}


def age_days(plan, today):
    return (today - plan["created_at"].astimezone(IST).date()).days


# =====================================================================
# EMAILS
# =====================================================================
def esc(s):
    return _html.escape(str(s if s is not None else ""), quote=True)


def site_url():
    import mailer
    return mailer.site_url()


def manage_url(plan):
    return "%s/portfolio#saved=%s" % (site_url(), plan["portfolio_id"])


def fund_label(f):
    plan = {"DIRECT": "Direct", "REGULAR": "Regular"}.get(f.get("plan") or "", "")
    return "%s%s" % (f.get("name") or f["code"], " (%s)" % plan if plan else "")


def _gap_sentence(f):
    r = f["latest"]
    if not r or r["excess"] is None:
        return "not compared (%s)" % ((f["notes"] or ["no data"])[0].rstrip("."))
    word = "ahead of" if r["excess"] >= 0 else "behind"
    return "%s %s its benchmark (%s vs %s)" % (pts(abs(r["excess"])).replace("+", ""), word, pct(r["fund"]), pct(r["bench"]))


def _quarter_text(r):
    if not r or r["quarter"] is None:
        return "not ranked"
    return "%s (%d of %d)" % (QUARTER_WORDS[r["quarter"]], r["rank"], r["n"])


def changes_since(ev, prev):
    """Plain sentences for what moved since the last report. Empty when there is no last report or nothing moved."""
    if not prev or not prev.get("funds"):
        return []
    out = []
    for f in ev["funds"]:
        p = prev["funds"].get(f["code"])
        r = f["latest"]
        if not p or not r:
            continue
        if p.get("quarter") and r["quarter"] and p["quarter"] != r["quarter"]:
            went = "up" if r["quarter"] < p["quarter"] else "down"
            out.append("%s moved %s, from the %s to the %s of its category." % (fund_label(f), went, QUARTER_WORDS[p["quarter"]], QUARTER_WORDS[r["quarter"]]))
        if p.get("behind") is not None and r["behind"] is not None and p["behind"] != r["behind"]:
            out.append("%s %s its benchmark over the year (last month it was %s)." % (
                fund_label(f), "is now behind" if r["behind"] else "is now ahead of", "ahead" if r["behind"] else "behind"))
    pp = prev.get("top_pair")
    if ev["pairs"] and pp is not None and abs(ev["pairs"][0]["pct"] - pp) >= 3:
        out.append("The closest pair of funds now overlaps by %.0f%% (last month %.0f%%)." % (ev["pairs"][0]["pct"], pp))
    pm = prev.get("makeup")
    if pm and ev["makeup"]:
        moved = [(k, ev["makeup"][k] - pm.get(k, 0)) for k in ("large", "mid", "small")]
        big = [(k, d) for k, d in moved if abs(d) >= 3]
        if big:
            out.append("The mix of large, mid and small companies moved: " + ", ".join("%s %+.0f points" % (k, d) for k, d in big) + ".")
    return out


def snapshot_of(ev):
    return {"as_at": str(ev["as_at"]),
            "funds": {f["code"]: {"quarter": f["latest"]["quarter"] if f["latest"] else None,
                                  "behind": f["latest"]["behind"] if f["latest"] else None,
                                  "excess": f["latest"]["excess"] if f["latest"] else None,
                                  "behind_streak": f["behind_streak"], "slip_streak": f["slip_streak"]} for f in ev["funds"]},
            "top_pair": ev["pairs"][0]["pct"] if ev["pairs"] else None, "makeup": ev["makeup"]}


STYLE = {"font": "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;", "ink": "#14251e", "mute": "#5a6d64", "green": "#0f7a56",
         "wash": "#eef5f1", "line": "#d5dfd9", "amber": "#9a6212", "amber_bg": "#fbf0d9"}


def _wrap_html(title, body_html, plan):
    s = STYLE
    return ("<!doctype html><html><body style=\"margin:0;background:#f3f6f4;%s color:%s\"><table role=\"presentation\" width=\"100%%\" cellpadding=\"0\" cellspacing=\"0\">"
            "<tr><td align=\"center\" style=\"padding:20px 10px\"><table role=\"presentation\" width=\"620\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:620px;width:100%%;background:#fff;border:1px solid %s;border-radius:10px\">"
            "<tr><td style=\"padding:22px 26px 6px\"><div style=\"font-size:20px;font-weight:700;color:%s\">Fin<span style=\"color:#c98a1a\">Chaya</span></div></td></tr>"
            "<tr><td style=\"padding:4px 26px 0\"><h1 style=\"margin:8px 0 4px;font-size:22px;line-height:1.25\">%s</h1>"
            "<div style=\"font-size:12px;color:%s;margin:0 0 6px\">A do-it-yourself (DIY) tool: information about the plan you built, not a recommendation by FinChaya.</div></td></tr>"
            "%s"
            "<tr><td style=\"padding:18px 26px 24px;border-top:1px solid %s;font-size:12px;line-height:1.55;color:%s\">"
            "You are receiving this because you locked the plan \u201c%s\u201d on FinChaya. "
            "<a href=\"%s\" style=\"color:%s\">Change what you are told, or turn this off</a>.<br><br>%s</td></tr>"
            "</table></td></tr></table></body></html>"
            % (s["font"], s["ink"], s["line"], s["ink"], esc(title), s["mute"], body_html, s["line"], s["mute"], esc(plan["name"]), esc(manage_url(plan)), s["green"], esc(DISCLAIMER)))


def _row(inner, pad="6px 26px"):
    return "<tr><td style=\"padding:%s;font-size:14px;line-height:1.55;color:%s\">%s</td></tr>" % (pad, STYLE["ink"], inner)


def build_monthly(plan, ev, prev, late=None):
    """(subject, text, html) for the monthly check."""
    s = STYLE
    month_name = ev["today"].strftime("%B %Y")
    subject = "Your %s plan: monthly check, %s" % (plan["name"], month_name)
    funds = ev["funds"]
    n = len(funds)
    measured_b = [f for f in funds if f["latest"] and f["latest"]["behind"] is not None]
    measured_q = [f for f in funds if f["latest"] and f["latest"]["quarter"] is not None]
    beating = sum(1 for f in measured_b if not f["latest"]["behind"])
    top = sum(1 for f in measured_q if f["latest"]["quarter"] == 1)
    top_pair = ev["pairs"][0] if ev["pairs"] else None
    changes = changes_since(ev, prev)
    watch = []
    for f in funds:
        if 0 < f["behind_streak"] < STREAK:
            watch.append("%s has been behind its benchmark for %d month%s in a row. An alert goes out at %d." % (fund_label(f), f["behind_streak"], "" if f["behind_streak"] == 1 else "s", STREAK))
        if 0 < f["slip_streak"] < STREAK:
            watch.append("%s has been in the %s or lower of its category for %d month%s in a row. An alert goes out at %d." % (
                fund_label(f), QUARTER_WORDS[ev["quarter_line"]], f["slip_streak"], "" if f["slip_streak"] == 1 else "s", STREAK))
    open_alerts = [e for e in ev.get("open_events", [])]

    # ---- text ----
    t = ["%s" % subject, "As at %s (fund portfolios as disclosed for that month end)." % fmt_date(ev["as_at"]), ""]
    if ev["as_at"] < ev["target"]:
        t.insert(2, "Returns and benchmarks for %s had not all arrived yet, so the figures are as at %s." % (fmt_date(ev["target"]), fmt_date(ev["as_at"])))
    t.append("WHERE THINGS STAND")
    t.append("- Beating their benchmark over the last year: %d of %d funds%s" % (beating, len(measured_b), "" if len(measured_b) == n else " (%d not compared)" % (n - len(measured_b))))
    t.append("- In the top quarter of their category (1-year): %d of %d funds%s" % (top, len(measured_q), "" if len(measured_q) == n else " (%d not ranked)" % (n - len(measured_q))))
    t.append("- Closest pair of funds overlaps by: %s" % ("%.0f%% (%s and %s)" % (top_pair["pct"], top_pair["a_name"], top_pair["b_name"]) if top_pair else "not measured"))
    t.append("")
    t.append("EACH FUND")
    for f in funds:
        r = f["latest"]
        w = f.get("windows") or {}
        t.append("%s" % fund_label(f))
        t.append("  1 year: %s; category: %s" % (_gap_sentence(f), _quarter_text(r)))
        if w:
            def long_view(y):
                if w[y]["fund"] is None:
                    return "%d years: not enough history yet" % y
                return "%d years: %s vs benchmark %s%s" % (y, pct(w[y]["fund"]), pct(w[y]["bench"]),
                                                           "" if w[y]["beat"] is None else (" (ahead)" if w[y]["beat"] else " (behind)"))
            t.append("  " + long_view(3) + "; " + long_view(5))
    t.append("")
    if changes:
        t.append("WHAT CHANGED SINCE LAST MONTH")
        t += ["- " + c for c in changes]
        t.append("")
    if watch:
        t.append("WORTH WATCHING")
        t += ["- " + w for w in watch]
        t.append("")
    if open_alerts:
        t.append("ALERTS STILL OPEN")
        t += ["- " + e["sentence"] for e in open_alerts]
        t.append("")
    t.append("IS IT STILL DIVERSIFIED")
    mk = ev["makeup"]
    if mk:
        t.append("- %d companies held; spread about as widely as %d equal-sized positions; the ten biggest are %.0f%% of the equity." % (mk["companies"], mk["effective"], mk["top10"]))
        t.append("- Large / mid / small companies: %.0f%% / %.0f%% / %.0f%%." % (mk["large"], mk["mid"], mk["small"]))
    for p in ev["pairs"][:3]:
        t.append("- %s and %s: %.0f%% of the same companies." % (p["a_name"], p["b_name"], p["pct"]))
    if late:
        t.append("")
        t.append("Not yet published for the latest month: %s. Their holdings figures are from the previous disclosure." % ", ".join(late))
    t += ["", "Change what you are told, or turn this off: " + manage_url(plan), "", DISCLAIMER]
    text = "\n".join(t)

    # ---- html ----
    def tick(r):
        return "<span style=\"color:%s;font-weight:600\">ahead</span>" % s["green"] if (r and r["behind"] is False) else (
            "<span style=\"color:#b3402a;font-weight:600\">behind</span>" if (r and r["behind"]) else "<span style=\"color:%s\">not compared</span>" % s["mute"])
    head = "".join("<td style=\"padding:6px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:%s;border-bottom:1px solid %s\">%s</td>" % (s["mute"], s["line"], h)
                   for h in ("Fund", "1 year vs benchmark", "Category (1 year)", "3 y / 5 y vs benchmark"))
    rows = ""
    for f in funds:
        r, w = f["latest"], f.get("windows") or {}
        def t35(y):
            if w and w[y]["fund"] is None:
                return "<span style=\"color:%s\">no record yet</span>" % s["mute"]
            if not w or w[y]["beat"] is None:
                return "-"
            return "<span style=\"color:%s\">%s</span>" % (s["green"] if w[y]["beat"] else "#b3402a", "ahead" if w[y]["beat"] else "behind")
        gap = "-" if not r or r["excess"] is None else "%s<br><span style=\"color:%s;font-size:12px\">%s vs %s</span>" % (tick(r), s["mute"], pct(r["fund"]), pct(r["bench"]))
        rows += ("<tr><td style=\"padding:8px;vertical-align:top;border-bottom:1px solid %s\"><b>%s</b></td><td style=\"padding:8px;vertical-align:top;border-bottom:1px solid %s\">%s</td>"
                 "<td style=\"padding:8px;vertical-align:top;border-bottom:1px solid %s\">%s</td><td style=\"padding:8px;vertical-align:top;border-bottom:1px solid %s\">%s / %s</td></tr>"
                 % (s["line"], esc(fund_label(f)), s["line"], gap, s["line"], esc(_quarter_text(r)), s["line"], t35(3), t35(5)))
    body = _row("As at <b>%s</b>. Fund portfolios are as disclosed for that month end.%s" % (
        esc(fmt_date(ev["as_at"])),
        (" Returns and benchmarks for %s had not all arrived yet, so the figures are as at %s." % (esc(fmt_date(ev["target"])), esc(fmt_date(ev["as_at"])))) if ev["as_at"] < ev["target"] else ""), "2px 26px 10px")
    body += _row("<div style=\"background:%s;border-radius:8px;padding:12px 14px\"><b>Where things stand</b><br>"
                 "Beating their benchmark over the last year: <b>%d of %d</b> funds<br>In the top quarter of their category: <b>%d of %d</b> funds<br>"
                 "Closest pair overlaps by: <b>%s</b></div>" % (
                     s["wash"], beating, len(measured_b), top, len(measured_q),
                     ("%.0f%%" % top_pair["pct"]) if top_pair else "not measured"), "4px 26px 10px")
    body += _row("<table role=\"presentation\" width=\"100%%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;font-size:13px\"><tr>%s</tr>%s</table>" % (head, rows), "4px 18px 10px")
    if changes:
        body += _row("<b>What changed since last month</b><ul style=\"margin:6px 0 0;padding-left:18px\">%s</ul>" % "".join("<li>%s</li>" % esc(c) for c in changes))
    if watch:
        body += _row("<div style=\"background:%s;border-radius:8px;padding:10px 14px\"><b>Worth watching</b><ul style=\"margin:6px 0 0;padding-left:18px\">%s</ul></div>" % (
            s["amber_bg"], "".join("<li>%s</li>" % esc(w) for w in watch)))
    if open_alerts:
        body += _row("<b>Alerts still open</b><ul style=\"margin:6px 0 0;padding-left:18px\">%s</ul>" % "".join("<li>%s</li>" % esc(e["sentence"]) for e in open_alerts))
    div = ""
    if mk:
        div += "<li>%d companies held, spread about as widely as %d equal-sized positions; the ten biggest are %.0f%% of the equity.</li>" % (mk["companies"], mk["effective"], mk["top10"])
        div += "<li>Large / mid / small companies: %.0f%% / %.0f%% / %.0f%%.</li>" % (mk["large"], mk["mid"], mk["small"])
    for p in ev["pairs"][:3]:
        div += "<li>%s and %s: <b>%.0f%%</b> of the same companies.</li>" % (esc(p["a_name"]), esc(p["b_name"]), p["pct"])
    body += _row("<b>Is it still diversified?</b><ul style=\"margin:6px 0 0;padding-left:18px\">%s</ul>" % div if div else "<b>Is it still diversified?</b> Not enough portfolio data to say.")
    if late:
        body += _row("<span style=\"color:%s;font-size:13px\">Not yet published for the latest month: %s. Their holdings figures are from the previous disclosure.</span>" % (s["mute"], esc(", ".join(late))))
    return subject, text, _wrap_html("Your monthly check", body, plan)


def _d(iso):
    try:
        return fmt_date(date.fromisoformat(str(iso)))
    except ValueError:
        return str(iso)


def build_alert(plan, events):
    """(subject, text, html) for one or more new alerts on a plan."""
    s = STYLE
    n = len(events)
    first = events[0]
    short = {"behind_benchmark": "behind its benchmark three month-ends running", "rank_slip": "in the lower half of its category three month-ends running",
             "overlap_high": "two funds overlap above your line"}[first["rule_key"]]
    subject = "FinChaya alert on %s: %s" % (plan["name"], short if n == 1 else "%d changes" % n)
    t = ["%s" % subject, "", "Something you asked to be told about has happened in your plan \u201c%s\u201d." % plan["name"], ""]
    h = ""
    for e in events:
        t.append("- " + e["sentence"])
        ev = e["evidence"] or {}
        if e["rule_key"] in ("behind_benchmark", "rank_slip"):
            for r in ev.get("rows", []):
                if e["rule_key"] == "behind_benchmark":
                    t.append("    %s: fund %s, benchmark %s" % (_d(r["month_end"]), pct(r["fund"]), pct(r["bench"])))
                else:
                    t.append("    %s: %s (%d of %d), 1-year return %s" % (_d(r["month_end"]), QUARTER_WORDS[r["quarter"]], r["rank"], r["n"], pct(r["fund"])))
        h += "<li style=\"margin-bottom:10px\">%s" % esc(e["sentence"])
        if e["rule_key"] in ("behind_benchmark", "rank_slip"):
            h += "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" style=\"margin-top:6px;font-size:13px;border-collapse:collapse\">"
            for r in ev.get("rows", []):
                if e["rule_key"] == "behind_benchmark":
                    cell = "fund %s, benchmark %s" % (pct(r["fund"]), pct(r["bench"]))
                else:
                    cell = "%s (%d of %d), 1-year return %s" % (QUARTER_WORDS[r["quarter"]], r["rank"], r["n"], pct(r["fund"]))
                h += "<tr><td style=\"padding:2px 14px 2px 0;color:%s\">%s</td><td style=\"padding:2px 0\">%s</td></tr>" % (s["mute"], esc(_d(r["month_end"])), esc(cell))
            h += "</table>"
        h += "</li>"
    t += ["", "What this does and does not mean: it is a record of what has happened, measured on month-end figures. It is not a signal to buy, sell or switch,",
          "and it does not say what will happen next. Whether to change anything stays with you.", "", "See the plan and change these alerts: " + manage_url(plan), "", DISCLAIMER]
    body = _row("Something you asked to be told about has happened in <b>%s</b>." % esc(plan["name"]), "2px 26px 8px")
    body += _row("<ul style=\"margin:0;padding-left:18px\">%s</ul>" % h, "4px 26px 6px")
    body += _row("<div style=\"background:%s;border-radius:8px;padding:12px 14px;font-size:13px\"><b>What this does and does not mean.</b> It is a record of what has happened, "
                 "measured on month-end figures. It is not a signal to buy, sell or switch, and it does not say what will happen next. Whether to change anything stays with you.</div>" % s["wash"], "6px 26px 10px")
    body += _row("<a href=\"%s\" style=\"display:inline-block;background:%s;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:600\">See the plan</a>" % (esc(manage_url(plan)), s["green"]), "6px 26px 8px")
    return subject, "\n".join(t), _wrap_html("A change in your plan", body, plan)


# =====================================================================
# RUNNING IT
# =====================================================================
def _log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def mask_email(addr):
    """'asha@example.com' -> 'a***@example.com', so a log can be pasted or shared without publishing anybody's address."""
    addr = str(addr or "")
    return (addr[:1] + "***@" + addr.split("@", 1)[1]) if "@" in addr else "?"


def _send(to, subject, text, html, sender):
    if sender is not None:
        return sender(to, subject, text, html)
    import mailer
    return mailer.send_email(to, subject, text, html=html)


def _start_run(cur, kind):
    cur.execute("INSERT INTO portfolio_monitor_run (kind) VALUES (%(k)s) RETURNING run_id", {"k": kind})
    return cur.fetchone()["run_id"]


def _finish_run(cur, run_id, checked, flagged, emailed, failed, note=None, error=None):
    cur.execute("""UPDATE portfolio_monitor_run SET finished_at = now(), checked = %s, flagged = %s, emailed = %s, failed = %s, note = %s, error = %s
                   WHERE run_id = %s""", (checked, flagged, emailed, failed, note, error, run_id))


def run_alerts(conn, today=None, sender=None, only=None, dry=False, force=False):
    """Evaluate every plan; record new and cleared conditions; email what is new. Returns a summary dict."""
    today = today or today_ist()
    cur = conn.cursor(row_factory=dict_row)
    summary = {"checked": 0, "flagged": 0, "emailed": 0, "failed": 0, "skipped": 0, "events": []}
    if not dry and not cur.execute("SELECT pg_try_advisory_lock(hashtext('portfolio_monitor_alerts')) AS ok").fetchone()["ok"]:
        _log("another alerts run is in progress; stopping")
        return summary
    run_id = None if dry else _start_run(cur, "alerts")
    store = Store(cur)
    try:
        for plan in load_plans(cur, only):
            if not plan["holdings"] or not entitled(plan, today):
                summary["skipped"] += 1
                continue
            settings = plan_settings(cur, plan["portfolio_id"])
            ev = evaluate_plan(store, plan, today, settings)
            summary["checked"] += 1
            pid = plan["portfolio_id"]
            cur.execute("""SELECT event_id, rule_key, scheme_code, other_code FROM portfolio_alert_event WHERE portfolio_id = %(p)s AND state = 'open'""", {"p": pid})
            open_ev = {(r["rule_key"], r["scheme_code"], r["other_code"]): r["event_id"] for r in cur.fetchall()}
            found_keys = set()
            for f in ev["found"]:
                key = (f["rule_key"], f["scheme_code"], f["other_code"])
                found_keys.add(key)
                if key in open_ev:
                    if not dry:
                        cur.execute("UPDATE portfolio_alert_event SET last_seen_on = %s, evidence = %s, sentence = %s WHERE event_id = %s",
                                    (today, Jsonb(f["evidence"]), f["sentence"], open_ev[key]))
                else:
                    summary["flagged"] += 1
                    summary["events"].append((pid, f["rule_key"], f["scheme_code"], f["other_code"]))
                    if not dry:
                        cur.execute("""INSERT INTO portfolio_alert_event (portfolio_id, rule_key, scheme_code, other_code, first_seen_on, last_seen_on, sentence, evidence, email_status)
                                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                    (pid, f["rule_key"], f["scheme_code"], f["other_code"], today, today, f["sentence"], Jsonb(f["evidence"]),
                                     "pending" if settings["email"] else "off"))
            # clear what was judged and no longer holds
            for key, eid in open_ev.items():
                if key not in found_keys and key in ev["evaluated"] and not dry:
                    cur.execute("UPDATE portfolio_alert_event SET state = 'cleared', cleared_on = %s WHERE event_id = %s", (today, eid))
            # a rule the member has switched off is closed, and nothing waiting under it is sent
            if not dry:
                off = [k for k, v in settings["rules"].items() if not v["enabled"]]
                if off:
                    cur.execute("""UPDATE portfolio_alert_event SET state = 'cleared', cleared_on = %s,
                                       email_status = CASE WHEN email_status IN ('pending','failed') THEN 'off' ELSE email_status END
                                   WHERE portfolio_id = %s AND rule_key = ANY(%s) AND state = 'open'""", (today, pid, off))
            # send what is pending, one email per plan, once the plan is out of its quiet period
            if age_days(plan, today) < QUIET_DAYS and not force:
                continue
            if dry:
                pend = [f for f in ev["found"] if (f["rule_key"], f["scheme_code"], f["other_code"]) not in open_ev]
            else:
                cur.execute("""SELECT event_id, rule_key, sentence, evidence FROM portfolio_alert_event
                               WHERE portfolio_id = %s AND state = 'open' AND email_status IN ('pending','failed') AND email_attempts < %s ORDER BY event_id""",
                            (pid, MAX_EMAIL_ATTEMPTS))
                pend = cur.fetchall()
            if not pend:
                continue
            subject, text, html = build_alert(plan, pend)
            if dry:
                _log("DRY RUN: would email %s: %s" % (mask_email(plan["email"]), subject))
                continue
            ok, detail = _send(plan["email"], subject, text, html, sender)
            ids = [e["event_id"] for e in pend]
            if ok:
                summary["emailed"] += 1
                cur.execute("UPDATE portfolio_alert_event SET email_status = 'sent', email_sent_at = now(), email_error = NULL, email_attempts = email_attempts + 1 WHERE event_id = ANY(%s)", (ids,))
            else:
                summary["failed"] += 1
                cur.execute("UPDATE portfolio_alert_event SET email_status = 'failed', email_error = %s, email_attempts = email_attempts + 1 WHERE event_id = ANY(%s)", (detail[:300], ids))
            conn.commit()
        if not dry:
            _finish_run(cur, run_id, summary["checked"], summary["flagged"], summary["emailed"], summary["failed"])
        conn.commit()
    except Exception as e:                                   # noqa: BLE001
        conn.rollback()
        if run_id and not dry:
            _finish_run(cur, run_id, summary["checked"], summary["flagged"], summary["emailed"], summary["failed"], error=("%s: %s" % (type(e).__name__, e))[:500])
            conn.commit()
        raise
    finally:
        if not dry:
            cur.execute("SELECT pg_advisory_unlock(hashtext('portfolio_monitor_alerts'))")
    return summary


def run_monthly(conn, today=None, sender=None, only=None, dry=False, force=False):
    """Send each due plan its monthly report. Due = the 11th to 15th, once a month, the funds' portfolios are in (or it is the 15th)."""
    today = today or today_ist()
    cur = conn.cursor(row_factory=dict_row)
    summary = {"checked": 0, "emailed": 0, "failed": 0, "skipped": 0, "waiting": 0, "reports": []}
    if not force and not (REPORT_FIRST_DAY <= today.day <= REPORT_LAST_DAY):
        _log("%s is outside the %dth to %dth: nothing to do" % (today, REPORT_FIRST_DAY, REPORT_LAST_DAY))
        return summary
    if not dry and not cur.execute("SELECT pg_try_advisory_lock(hashtext('portfolio_monitor_monthly')) AS ok").fetchone()["ok"]:
        _log("another monthly run is in progress; stopping")
        return summary
    run_id = None if dry else _start_run(cur, "monthly")
    report_month = today.replace(day=1)
    store = Store(cur)
    try:
        for plan in load_plans(cur, only):
            pid = plan["portfolio_id"]
            if not plan["holdings"] or not entitled(plan, today):
                summary["skipped"] += 1
                continue
            settings = plan_settings(cur, pid)
            if not settings["monthly"] or not settings["email"]:
                summary["skipped"] += 1
                continue
            if age_days(plan, today) < QUIET_DAYS and not force:
                summary["skipped"] += 1
                continue
            cur.execute("SELECT status, attempts FROM portfolio_report_log WHERE portfolio_id = %s AND report_month = %s", (pid, report_month))
            logged = cur.fetchone()
            if logged and (logged["status"] == "sent" or logged["attempts"] >= MAX_EMAIL_ATTEMPTS):
                summary["skipped"] += 1
                continue
            ev = evaluate_plan(store, plan, today, settings)
            summary["checked"] += 1
            # wait for the funds' portfolios of the month end, until the last day of the window
            have = ev["disclosed"]
            late = [store.fund_info([c])[c]["name"] for c in sorted(have) if have[c] < ev["target"]]
            behind = ev["as_at"] < ev["target"]      # NAVs or the benchmark have not reached the month end yet
            if (late or behind) and today.day < REPORT_LAST_DAY and not force:
                summary["waiting"] += 1
                continue
            cur.execute("SELECT snapshot FROM portfolio_report_log WHERE portfolio_id = %s AND status = 'sent' ORDER BY report_month DESC LIMIT 1", (pid,))
            row = cur.fetchone()
            prev = row["snapshot"] if row else None
            cur.execute("SELECT sentence FROM portfolio_alert_event WHERE portfolio_id = %s AND state = 'open' ORDER BY event_id", (pid,))
            ev["open_events"] = cur.fetchall()
            subject, text, html = build_monthly(plan, ev, prev, late=late or None)
            summary["reports"].append((pid, subject))
            if dry:
                _log("DRY RUN: would email %s: %s" % (mask_email(plan["email"]), subject))
                continue
            ok, detail = _send(plan["email"], subject, text, html, sender)
            snap = Jsonb(snapshot_of(ev))
            if ok:
                summary["emailed"] += 1
                cur.execute("""INSERT INTO portfolio_report_log (portfolio_id, report_month, status, detail, subject, snapshot, attempts, sent_at)
                               VALUES (%s,%s,'sent',%s,%s,%s,1,now())
                               ON CONFLICT (portfolio_id, report_month) DO UPDATE SET status = 'sent', detail = EXCLUDED.detail, subject = EXCLUDED.subject,
                                   snapshot = EXCLUDED.snapshot, attempts = portfolio_report_log.attempts + 1, sent_at = now()""",
                            (pid, report_month, detail[:300], subject, snap))
            else:
                summary["failed"] += 1
                cur.execute("""INSERT INTO portfolio_report_log (portfolio_id, report_month, status, detail, subject, snapshot, attempts)
                               VALUES (%s,%s,'failed',%s,%s,%s,1)
                               ON CONFLICT (portfolio_id, report_month) DO UPDATE SET status = 'failed', detail = EXCLUDED.detail,
                                   attempts = portfolio_report_log.attempts + 1""", (pid, report_month, detail[:300], subject, snap))
            conn.commit()
        if not dry:
            _finish_run(cur, run_id, summary["checked"], 0, summary["emailed"], summary["failed"], note="waiting: %d" % summary["waiting"])
        conn.commit()
    except Exception as e:                                   # noqa: BLE001
        conn.rollback()
        if run_id and not dry:
            _finish_run(cur, run_id, summary["checked"], 0, summary["emailed"], summary["failed"], error=("%s: %s" % (type(e).__name__, e))[:500])
            conn.commit()
        raise
    finally:
        if not dry:
            cur.execute("SELECT pg_advisory_unlock(hashtext('portfolio_monitor_monthly'))")
    return summary


def preview(conn, pid, today=None, out_dir="."):
    """Build one plan's monthly report and, if anything is flagged, its alert, as files. Reads only."""
    today = today or today_ist()
    cur = conn.cursor(row_factory=dict_row)
    plans = load_plans(cur, pid)
    if not plans:
        sys.exit(why_not_a_plan(cur, pid))
    plan = plans[0]
    store = Store(cur)
    settings = plan_settings(cur, pid)
    ev = evaluate_plan(store, plan, today, settings)
    cur.execute("SELECT snapshot FROM portfolio_report_log WHERE portfolio_id = %s AND status = 'sent' ORDER BY report_month DESC LIMIT 1", (pid,))
    row = cur.fetchone()
    cur.execute("SELECT sentence FROM portfolio_alert_event WHERE portfolio_id = %s AND state = 'open'", (pid,))
    ev["open_events"] = cur.fetchall()
    subject, text, html = build_monthly(plan, ev, row["snapshot"] if row else None)
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    base = os.path.join(out_dir, "plan%s" % pid)
    for ext, content in (("monthly.html", html), ("monthly.txt", text)):
        p = "%s_%s" % (base, ext)
        open(p, "w", encoding="utf-8").write(content)
        paths.append(p)
    if ev["found"]:
        a_sub, a_text, a_html = build_alert(plan, [{"rule_key": f["rule_key"], "sentence": f["sentence"], "evidence": f["evidence"]} for f in ev["found"]])
        for ext, content in (("alert.html", a_html), ("alert.txt", a_text)):
            p = "%s_%s" % (base, ext)
            open(p, "w", encoding="utf-8").write(content)
            paths.append(p)
    return subject, ev, paths


def send_test(conn, pid, to, today=None, sample_alert=False, sender=None):
    """Send one plan's monthly check (and, if asked, a labelled SAMPLE alert) to an address of the caller's choosing, NOT to the plan's owner,
    and write nothing: no report log, no alert events, no run ledger. This is how the first real email is tried on a real plan safely."""
    today = today or today_ist()
    cur = conn.cursor(row_factory=dict_row)
    plans = load_plans(cur, pid)
    if not plans:
        sys.exit(why_not_a_plan(cur, pid))
    plan = plans[0]
    store = Store(cur)
    ev = evaluate_plan(store, plan, today, plan_settings(cur, pid))
    cur.execute("SELECT snapshot FROM portfolio_report_log WHERE portfolio_id = %s AND status = 'sent' ORDER BY report_month DESC LIMIT 1", (pid,))
    row = cur.fetchone()
    cur.execute("SELECT sentence FROM portfolio_alert_event WHERE portfolio_id = %s AND state = 'open'", (pid,))
    ev["open_events"] = cur.fetchall()
    sent = []
    subject, text, html = build_monthly(plan, ev, row["snapshot"] if row else None)
    banner = "THIS IS A TEST: sent to you instead of the plan's owner; nothing was recorded.\n\n"
    ok, detail = _send(to, "[TEST] " + subject, banner + text, ("<p style=\"background:#fbf0d9;padding:8px 12px;font-family:sans-serif\"><b>This is a test.</b> "
                       "Sent to you instead of the plan's owner; nothing was recorded.</p>" + html), sender)
    sent.append(("monthly check", ok, detail))
    if ev["found"]:
        events = [{"rule_key": f["rule_key"], "sentence": f["sentence"], "evidence": f["evidence"]} for f in ev["found"]]
        label = "real alerts flagged now"
    elif sample_alert and ev["funds"]:
        f0 = ev["funds"][0]
        rows = [{"month_end": str(r["month_end"]), "fund": r["fund"], "bench": r["bench"], "excess": r["excess"]} for r in f0["series"][:STREAK]]
        events = [{"rule_key": "behind_benchmark", "sentence": "SAMPLE (nothing has been flagged in this plan): %s, shown as it would look if it had returned less than its benchmark (%s) "
                   "over the year to each of the last %d month ends." % (f0["name"], f0["benchmark"] or "its benchmark", STREAK), "evidence": {"rows": rows}}]
        label = "a labelled sample alert"
    else:
        events, label = [], None
    if events:
        a_sub, a_text, a_html = build_alert(plan, events)
        ok, detail = _send(to, "[TEST] " + a_sub, banner + a_text, "<p style=\"background:#fbf0d9;padding:8px 12px;font-family:sans-serif\"><b>This is a test.</b> "
                           "Sent to you instead of the plan's owner; nothing was recorded.</p>" + a_html, sender)
        sent.append((label, ok, detail))
    return sent


def status(conn):
    import mailer
    cur = conn.cursor(row_factory=dict_row)
    print("\n".join(mailer.describe()))
    for kind in ("alerts", "monthly"):
        cur.execute("SELECT * FROM portfolio_monitor_run WHERE kind = %s ORDER BY started_at DESC LIMIT 1", (kind,))
        r = cur.fetchone()
        print("%-8s last run: %s" % (kind, "never" if not r else "%s  checked %d, flagged %d, emailed %d, failed %d%s" % (
            r["started_at"].astimezone(IST).strftime("%d %b %H:%M IST"), r["checked"], r["flagged"], r["emailed"], r["failed"], ("  ERROR " + r["error"]) if r["error"] else "")))
    cur.execute("SELECT email_status, COUNT(*) AS n FROM portfolio_alert_event WHERE state = 'open' GROUP BY 1")
    print("open alerts by email status:", {r["email_status"]: r["n"] for r in cur.fetchall()})


def main(argv):
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(HERE, ".env"))
        load_dotenv("/opt/mfapi/.env")
    except Exception:                                        # noqa: BLE001
        pass
    dsn = os.environ.get("FINCHAYA_DB") or os.environ.get("DATABASE_URL") or os.environ.get("MF_DSN")
    if not dsn:
        sys.exit("No FINCHAYA_DB set.")
    arg = lambda name: argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None
    today = date.fromisoformat(arg("--today")) if arg("--today") else None
    only = int(arg("--portfolio")) if arg("--portfolio") else None
    dry, force = "--dry-run" in argv, "--force" in argv
    with psycopg.connect(dsn) as conn:
        if "--status" in argv:
            status(conn)
            return 0
        if "--list" in argv:
            list_plans(conn, today)
            return 0
        if "--send-test" in argv:
            to = arg("--to")
            import mailer
            if not to or not mailer.valid_address(to):
                sys.exit("Give the address to send the test to:  --send-test <plan number> --to you@example.com")
            for label, ok, detail in send_test(conn, int(arg("--send-test")), to, today, sample_alert="--sample-alert" in argv):
                print("%s: %s (%s)" % (label, "SENT to " + to if ok else "NOT SENT", detail))
            return 0
        if "--preview" in argv:
            pid = int(arg("--preview"))
            subject, ev, paths = preview(conn, pid, today, arg("--out") or os.path.join(HERE, "monitor_preview"))
            print(subject)
            print("flagged now:", [(f["rule_key"], f["sentence"]) for f in ev["found"]])
            print("\n".join(paths))
            return 0
        if "--alerts" in argv:
            s = run_alerts(conn, today, only=only, dry=dry, force=force)
            _log("alerts:", {k: v for k, v in s.items() if k != "events"}, "new:", s["events"])
            return 1 if s["failed"] else 0
        if "--monthly" in argv:
            s = run_monthly(conn, today, only=only, dry=dry, force=force)
            _log("monthly:", {k: v for k, v in s.items() if k != "reports"})
            return 1 if s["failed"] else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
