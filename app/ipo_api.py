"""
ipo_api.py -- the IPO section: the lists and scanner at /ipo, a page for each issue at /ipo/<name>, and their API.
---------------------------------------------------------------------------
Mounted with the one block enable_ipo.py appends to the live api.py, or by api.py itself:

    GET  /ipo                              the page (lists + scanner)
    GET  /ipo/{slug}                       the page of one issue
    GET  /api/ipo/access                   may I see it? (never an error: the page asks this first)
    GET  /api/ipo/list                     issues, filtered:  stage=upcoming|listed|filed|ended|all  board=mainboard|sme  q=  issue_type=fresh|ofs|mix
                                           use=<what the money is for>  promoters_selling=yes|no  profitable=yes|no  min_revenue_growth=  min_size_cr=
                                           max_size_cr=  min_promoters_pct=  max_promoters_pct=  risk=<topic>  gain=above|below  sort=date|size|growth|promoters|gain
    GET  /api/ipo/scanners                 the ready-made filters the page offers, and the lists of choices (what the money is for, risk topics)
    GET  /api/ipo/{slug}                   everything read for one issue, with the page number of each item in the offer document
    GET  /api/ipo/for-stock/{isin}         the same detail for a LISTED company's stock page ({"available": false} when there is none; never an error)
    GET  /api/ipo/view.js                  the script that draws one issue (the IPO page and the IPO tab of the stock page share it)
    GET  /api/ipo/status                   ADMIN: what the daily job has stored and how its last runs went

ONE PAGE PER COMPANY
    Once an issue has listed and its stock is on FinChaya, /ipo/<name> redirects to the stock page (/#/stock/<isin>/ipo): the IPO sections are an "IPO" tab there,
    beside the chart, financials and shareholding. The IPO page itself is for issues that are coming, open, waiting to list, or whose stock is not on the site.

INFORMATION ONLY. NO VERDICT.
    Nothing here says whether an issue is good or bad, cheap or dear, or what anyone should do. There is no score, rank, rating, recommendation or
    "subscribe / avoid" anywhere: the page reports what the company's own offer document and the exchanges say, with the page it said it on, and the notice
    below on every response. The ready-made scanners are descriptions of what an issue IS ("the money repays debt", "promoters are selling"), not labels of
    whether that is good.

WHO MAY SEE IT
    EVERYONE, signed in or not (the default since 12 Oct 2026). MF_IPO_ACCESS=admin | signed_in | premium narrows it; MF_IPO_ENABLED=false is a hard off.
    The lists are kept for MF_IPO_CACHE_SECONDS (default 120) because the pages are public: a busy page must not mean a database read for every visitor.

NEEDS (on the server): stock_access.py, ipo_store.py, ipo_sources.py (the stage rules), ipo_extract.py (the labels), price_adjust.py, api.py, and the tables
    from create_ipo_tables.py. Before the tables exist the endpoints answer 503 with that instruction for an administrator.
"""

import os
import re
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

import ipo_extract as X
import ipo_sources as S
import stock_access

router = APIRouter(prefix="/api/ipo", tags=["ipo"])
page_router = APIRouter()
GATE = stock_access.Gate("MF_IPO_ACCESS", "MF_IPO_ENABLED", default="everyone")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ISIN_RE = re.compile(r"[A-Z0-9]{12}")
MAX_LIST = 400
LISTED_WINDOW_DAYS = 120            # the "Listed" tab shows issues that listed within this many days
ENDED_WINDOW_DAYS = 120             # the "Closed or withdrawn" tab shows issues whose bidding closed within this many days
# "upcoming" is what is still ahead: not open yet, open now, or bidding closed in the last few days with the listing still to come (S.LISTING_WAIT_DAYS).
# An issue that was withdrawn or postponed, or whose bidding closed longer ago with no listing found, is "ended": a tab of its own, never under Upcoming.
STAGE_GROUPS = {"upcoming": ("upcoming", "open", "closed"), "listed": ("listed",), "filed": ("filed",), "ended": ("ended",),
                "all": ("filed", "upcoming", "open", "closed", "listed", "ended")}

NOTICE = ("This page only reports what the company's offer document and the stock exchanges say. FinChaya does not recommend that you apply for, buy, hold "
          "or sell any issue, does not rate or rank issues, and is not a SEBI-registered investment adviser or research analyst. The figures were read "
          "automatically from the PDF and can contain mistakes: the page number beside each item shows where to check it, and the offer document itself is "
          "the only authoritative source.")

USE_LABELS = dict(X.TAG_LABEL)
RISK_LABELS = {k: label for k, label, _ in X.RISK_TOPICS}
STAGE_LABELS = {"filed": "Draft filed with SEBI", "upcoming": "Opens soon", "open": "Open for bidding", "closed": "Bidding closed, listing awaited", "listed": "Listed",
                "ended": "Bidding closed, no listing found"}
ENDED_LABELS = {"withdrawn": "Issue withdrawn", "postponed": "Issue postponed", "cancelled": "Issue cancelled", "no_listing": "Bidding closed, no listing found"}
ENDED_NOTES = {"no_listing": "The exchange's list shows no listing date for this issue and FinChaya holds no prices for it. It may have listed only on another exchange, "
                             "or not at all; check the exchange before relying on this page.",
               "withdrawn": "The exchange lists this issue as withdrawn.", "postponed": "The exchange lists this issue as postponed.",
               "cancelled": "The exchange lists this issue as cancelled."}
TYPE_LABELS = {"fresh": "Fresh issue (the company receives the money)", "ofs": "Offer for sale (existing holders sell; the company receives nothing)",
               "mix": "Fresh issue and offer for sale"}

SCANNERS = [
    {"key": "fresh", "label": "Fresh issue only", "help": "All the shares are new shares: the company receives the money.", "params": {"issue_type": "fresh"}},
    {"key": "ofs", "label": "Offer for sale only", "help": "All the shares are sold by existing holders: the company receives nothing.", "params": {"issue_type": "ofs"}},
    {"key": "mix", "label": "Fresh issue and offer for sale", "help": "Part new shares, part shares sold by existing holders.", "params": {"issue_type": "mix"}},
    {"key": "debt", "label": "Money used to repay borrowings", "help": "One of the stated uses of the money is repaying debt.", "params": {"use": "debt_repayment"}},
    {"key": "wc", "label": "Money used for working capital", "help": "One of the stated uses is working capital.", "params": {"use": "working_capital"}},
    {"key": "capex", "label": "Money used for expansion or equipment", "help": "One of the stated uses is capital expenditure.", "params": {"use": "capex"}},
    {"key": "acq", "label": "Money used for acquisitions", "help": "One of the stated uses is an acquisition or strategic investment.", "params": {"use": "acquisition"}},
    {"key": "psell", "label": "Promoters are selling shares", "help": "A promoter is among the selling shareholders.", "params": {"promoters_selling": "yes"}},
    {"key": "loss", "label": "Made a loss in the latest full year", "help": "Profit after tax in the latest full year was below zero.", "params": {"profitable": "no"}},
    {"key": "grow", "label": "Revenue up 25% or more", "help": "Latest full-year revenue is at least 25% above the year before.", "params": {"min_revenue_growth": "25"}},
    {"key": "big", "label": "Issue of ₹1,000 crore or more", "help": "The size of the issue.", "params": {"min_size_cr": "1000"}},
    {"key": "sme", "label": "SME platform", "help": "Listed on the NSE Emerge / SME platform, not the main board.", "params": {"board": "sme"}},
    {"key": "above", "label": "Closed its first day above the issue price", "help": "Listed issues only.", "params": {"stage": "listed", "gain": "above"}},
    {"key": "below", "label": "Closed its first day below the issue price", "help": "Listed issues only.", "params": {"stage": "listed", "gain": "below"}},
]


# =====================================================================
# helpers
# =====================================================================
def _query(sql, params=None, one=False, user=None):
    from api import query
    import psycopg
    try:
        return query(sql, params, one=one)
    except psycopg.errors.UndefinedTable as e:
        print("mfapi: ipo tables missing: %s" % str(e).splitlines()[0][:160], flush=True)
        detail = "The IPO section is not set up on this server yet: its tables have not been created."
        if user is not None and stock_access.is_admin(user):
            detail += " Run `python create_ipo_tables.py --admin doadmin` on the server (DEPLOY_IPO.md), then try again."
        raise HTTPException(503, detail)


def _need(request):
    user = GATE.require(request)
    return user


def _f(v):
    return None if v is None else float(v)


def _iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else d


def _pick(params, key, allowed=None):
    v = (params.get(key) or "").strip()
    if not v:
        return None
    if allowed is not None and v not in allowed:
        raise HTTPException(400, "'%s' is not one of: %s." % (key, ", ".join(sorted(allowed))))
    return v


def _num(params, key):
    v = (params.get(key) or "").strip()
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        raise HTTPException(400, "'%s' must be a number." % key)
    if x != x or abs(x) > 1e12:
        raise HTTPException(400, "'%s' is out of range." % key)
    return x


# =====================================================================
# how a listed issue has traded since (facts from the price table; split/bonus adjusted)
# =====================================================================
def stock_isins(symbols):
    """{symbol: isin} for the symbols whose stock is on FinChaya (so it has a stock page); empty when the table cannot be read."""
    syms = sorted({s for s in symbols if s})
    if not syms:
        return {}
    try:
        return {m["symbol"]: m["isin"] for m in _query("SELECT symbol, isin FROM stock_master WHERE symbol = ANY(%(s)s) AND is_active", {"s": syms})}
    except HTTPException:
        return {}


def listing_performance(issues, floor=None, isin_of=None):
    """{issue_id: {...}} for listed issues whose prices are on file: the first day's open and close and the latest close, against the issue price. Prices from
    before the licence floor are not used; a split or bonus after listing is divided out only where the prices show it (price_adjust)."""
    import price_adjust as pa
    out = {}
    cand = [i for i in issues if i.get("symbol") and i.get("listing_date") and (not floor or i["listing_date"] >= floor)]
    if not cand:
        return out
    if isin_of is None:
        isin_of = stock_isins([i["symbol"] for i in cand])
    isin_of = {s: v for s, v in isin_of.items() if s in {i["symbol"] for i in cand}}
    if not isin_of:
        return out
    first = min(i["listing_date"] for i in cand)
    bars = _query("""SELECT isin, as_of_date AS d, open, high, low, close, volume FROM stock_ohlc_daily
                     WHERE isin = ANY(%(i)s) AND as_of_date >= %(f)s AND close IS NOT NULL AND close > 0 ORDER BY isin, as_of_date""",
                  {"i": list(isin_of.values()), "f": first})
    by_isin = {}
    for b in bars:
        by_isin.setdefault(b["isin"], []).append(b)
    try:
        events = _query("SELECT isin, kind, event_date, headline FROM stock_event WHERE kind IN ('split', 'bonus') AND isin = ANY(%(i)s) AND event_date >= %(f)s ORDER BY event_date",
                        {"i": list(isin_of.values()), "f": first})
    except HTTPException:
        events = []
    ev_of = {}
    for e in events:
        ev_of.setdefault(e["isin"], []).append(e)
    for i in cand:
        isin = isin_of.get(i["symbol"])
        rows = [b for b in by_isin.get(isin, []) if b["d"] >= i["listing_date"]]
        price = _f(i.get("issue_price"))
        if not rows or not price or rows[0]["d"] > i["listing_date"] + timedelta(days=4):
            continue                                                            # no listing-day bar on file (a bar a week late is some other day)
        raw = [{"d": b["d"], "open": _f(b["open"]), "high": _f(b["high"]), "low": _f(b["low"]), "close": _f(b["close"]), "volume": b["volume"]} for b in rows]
        applied = []
        acts = pa.collect_actions(ev_of.get(isin, []))
        if acts and len(raw) > 1:
            applied, _ = pa.confirm_actions(acts, raw)
        f = pa.adjust_bars(raw, applied)[0]["factor"] if applied else 1.0
        last = raw[-1]
        lo_c = raw[0]["close"]
        p = {"listing_open": raw[0]["open"], "listing_close": lo_c, "latest": round(last["close"] * f, 2), "latest_date": _iso(last["d"]), "adjusted": bool(applied),
             "listing_open_pct": round((raw[0]["open"] / price - 1) * 100, 1) if raw[0]["open"] else None,
             "listing_close_pct": round((lo_c / price - 1) * 100, 1),
             "latest_pct": round((last["close"] * f / price - 1) * 100, 1)}
        if applied:
            p["adjustment"] = ", ".join(a["label"] for a in applied)
        out[i["issue_id"]] = p
    return out


# =====================================================================
# one line of a list, one whole page
# =====================================================================
def _effective(rows, today=None):
    """The issues as the pages should see them today: each row with its stage, listing date and label corrected by S.effective_stage (an issue whose bidding closed weeks ago is
    not "upcoming"; one that has traded is listed even where the exchange's list has no listing date; withdrawn and postponed issues are 'ended'). Adds `_stored` (the
    stage in the table), `_label` and `_note`. Reads FinChaya's own prices for the few issues that need it."""
    import ipo_store
    today = today or date.today()
    cand = [r for r in rows if r["stage"] == "closed" and r.get("listing_date") is None]
    try:
        first = ipo_store.listing_evidence(lambda sql, p: _query(sql, p), cand) if cand else {}
    except HTTPException:
        first = {}
    out = []
    for r in rows:
        stage, listing, note = S.effective_stage(today, r["open_date"], r["close_date"], r["listing_date"], r["symbol"], r["name"], first.get(r["issue_id"]))
        if r["stage"] == "filed" and not r.get("symbol"):
            stage = "filed"
        out.append(dict(r, stage=stage, listing_date=listing, _stored=r["stage"], _note=note,
                        _label=ENDED_LABELS.get(note) if stage == "ended" else None))
    return out


def _item(r, perf=None, isin=None):
    c = r.get("card") or {}
    lot, hi = r.get("lot_size"), _f(r.get("price_high")) or _f(r.get("issue_price"))
    uses = [USE_LABELS.get(t, t) for t in (c.get("uses") or [])]
    name, _status = S.status_note(r["name"])
    return {
        "slug": r["slug"], "name": name or r["name"], "symbol": r["symbol"], "board": r["board"], "stage": r["stage"], "stage_label": r.get("_label") or STAGE_LABELS[r["stage"]],
        "stage_note": ENDED_NOTES.get(r.get("_note")) if r["stage"] == "ended" else None,
        "open_date": _iso(r["open_date"]), "close_date": _iso(r["close_date"]), "listing_date": _iso(r["listing_date"]), "filed_on": _iso(r["sebi_filed_on"]),
        "price_low": _f(r["price_low"]), "price_high": _f(r["price_high"]), "issue_price": _f(r["issue_price"]), "lot_size": lot,
        "min_investment": round(lot * hi) if lot and hi else None,
        "read": bool(c.get("read")), "doc_kind": c.get("doc_kind"), "doc_date": c.get("doc_date"),
        "issue_type": c.get("issue_type"), "issue_type_label": TYPE_LABELS.get(c.get("issue_type")), "fresh_pct": c.get("fresh_pct"),
        "size_cr": c.get("size_cr"), "size_basis": c.get("size_basis"),
        "uses": uses, "use_keys": c.get("uses") or [], "promoters": c.get("promoters") or [], "promoters_pct": c.get("promoters_pct"),
        "promoters_selling": c.get("promoters_selling"), "sellers": c.get("sellers"),
        "revenue_cr": c.get("revenue_cr"), "revenue_growth": c.get("revenue_growth"), "pat_cr": c.get("pat_cr"), "pat_margin": c.get("pat_margin"),
        "loss_making": c.get("loss_making"), "fin_period": c.get("fin_period"), "risk_count": c.get("risk_count"),
        "risk_keys": c.get("risk_topics") or [], "perf": perf, "isin": isin,
    }


def _date_key(it):
    return it["open_date"] or it["filed_on"] or it["listing_date"] or "0000-00-00"


def _filter(items, p):
    stage = _pick(p, "stage", set(STAGE_GROUPS)) or "all"
    board = _pick(p, "board", {"mainboard", "sme"})
    itype = _pick(p, "issue_type", {"fresh", "ofs", "mix"})
    use = _pick(p, "use", set(USE_LABELS))
    risk = _pick(p, "risk", set(RISK_LABELS))
    psell = _pick(p, "promoters_selling", {"yes", "no"})
    profitable = _pick(p, "profitable", {"yes", "no"})
    gain = _pick(p, "gain", {"above", "below"})
    q = (p.get("q") or "").strip().lower()[:60]
    mg, mnsz, mxsz = _num(p, "min_revenue_growth"), _num(p, "min_size_cr"), _num(p, "max_size_cr")
    mnpp, mxpp = _num(p, "min_promoters_pct"), _num(p, "max_promoters_pct")
    out = []
    for it in items:
        if it["stage"] not in STAGE_GROUPS[stage]:
            continue
        if board and it["board"] != board:
            continue
        if q and q not in it["name"].lower() and q not in (it["symbol"] or "").lower():
            continue
        if itype and it["issue_type"] != itype:
            continue
        if use and use not in it["use_keys"]:
            continue
        if risk and risk not in it["risk_keys"]:
            continue
        if psell == "yes" and not it["promoters_selling"]:
            continue
        if psell == "no" and it["promoters_selling"] is not False:
            continue
        if profitable == "yes" and it["loss_making"] is not False:
            continue
        if profitable == "no" and it["loss_making"] is not True:
            continue
        if mg is not None and (it["revenue_growth"] is None or it["revenue_growth"] < mg):
            continue
        if mnsz is not None and (it["size_cr"] is None or it["size_cr"] < mnsz):
            continue
        if mxsz is not None and (it["size_cr"] is None or it["size_cr"] > mxsz):
            continue
        if mnpp is not None and (it["promoters_pct"] is None or it["promoters_pct"] < mnpp):
            continue
        if mxpp is not None and (it["promoters_pct"] is None or it["promoters_pct"] > mxpp):
            continue
        if gain:
            pc = (it["perf"] or {}).get("listing_close_pct")
            if pc is None or (gain == "above" and pc <= 0) or (gain == "below" and pc >= 0):
                continue
        out.append(it)
    return out


SORTS = {
    "date": (lambda it: _date_key(it), True),
    "size": (lambda it: it["size_cr"], True),
    "growth": (lambda it: it["revenue_growth"], True),
    "promoters": (lambda it: it["promoters_pct"], True),
    "gain": (lambda it: (it["perf"] or {}).get("listing_close_pct"), True),
}


def _sort(items, key):
    fn, desc = SORTS[key]
    have = [i for i in items if fn(i) is not None]
    lack = [i for i in items if fn(i) is None]
    have.sort(key=fn, reverse=desc)
    return have + lack                                               # items with no figure go last, never first


def last_run():
    rows = _query("""SELECT status, finished_at, rows_read, rows_written, error_message FROM batch_run_log WHERE run_type = 'IPO_REFRESH'
                     ORDER BY run_id DESC LIMIT 1""") if _has_log() else []
    return rows[0] if rows else None


def _has_log():
    try:
        from api import query
        return bool(query("SELECT to_regclass('batch_run_log') IS NOT NULL AS t", one=True)["t"])
    except Exception:                                                # noqa: BLE001
        return False


# =====================================================================
# endpoints
# =====================================================================
@page_router.get("/ipo", include_in_schema=False)
def page():
    path = os.path.join(BASE_DIR, "ipo.html")
    if not os.path.exists(path):
        raise HTTPException(404, "ipo.html is missing from %s" % BASE_DIR)
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})


def stock_page_url(slug):
    """Where a LISTED issue lives once its stock is on FinChaya: the IPO tab of the stock page. None when it has no stock page (or its offer document has not been
    read, so there would be no IPO tab), or when the IPO section is not open to everyone (a redirect to a page without the tab would lead nowhere)."""
    if not (GATE.enabled() and GATE.mode() == "everyone"):
        return None
    try:
        from api import query
        r = query("""SELECT m.isin FROM ipo_issue i JOIN stock_master m ON m.symbol = i.symbol AND m.is_active
                     WHERE i.slug = %(s)s AND i.stage = 'listed' AND (i.card->>'read')::boolean IS TRUE LIMIT 1""", {"s": slug}, one=True)
    except Exception:                                                # noqa: BLE001  (tables missing, database busy ...: show the IPO page rather than fail)
        return None
    return "/#/stock/%s/ipo" % r["isin"] if r else None


@page_router.get("/ipo/{slug}", include_in_schema=False)
def issue_page(slug: str):
    if not re.fullmatch(r"[a-z0-9-]{1,90}", slug):
        raise HTTPException(404, "No such issue.")
    target = stock_page_url(slug)
    if target:                                                       # one page per company: the stock page has the IPO as a tab
        return RedirectResponse(target, status_code=302)
    path = os.path.join(BASE_DIR, "ipo_issue.html")
    if not os.path.exists(path):
        raise HTTPException(404, "ipo_issue.html is missing from %s" % BASE_DIR)
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})


@router.get("/access")
def access(request: Request):
    return GATE.access(request)


@router.get("/scanners")
def scanners(request: Request):
    _need(request)
    return {"scanners": SCANNERS, "uses": [{"key": k, "label": v} for k, v in USE_LABELS.items() if k != "other"],
            "risks": [{"key": k, "label": v} for k, v in RISK_LABELS.items()], "types": [{"key": k, "label": v} for k, v in TYPE_LABELS.items()],
            "stages": [{"key": "upcoming", "label": "Upcoming and open"}, {"key": "listed", "label": "Listed"}, {"key": "filed", "label": "Draft filed with SEBI"},
                       {"key": "ended", "label": "Closed or withdrawn"}, {"key": "all", "label": "All"}], "notice": NOTICE}


_CACHE = {"at": 0.0, "day": None, "value": None}


def _cache_seconds():
    try:
        return max(0.0, float(os.getenv("MF_IPO_CACHE_SECONDS", "120")))
    except ValueError:
        return 120.0


def _load_items(user=None):
    """Every issue the lists can show, as list items with the stock link and the trading figures, plus the counts per tab and when the job last ran. The same for
    every visitor (nothing here is personal), so it is kept for MF_IPO_CACHE_SECONDS: a public page must not read the price table for each visitor."""
    import time
    ttl = _cache_seconds()
    today = date.today()
    if ttl and _CACHE["value"] is not None and _CACHE["day"] == today and time.monotonic() - _CACHE["at"] < ttl:
        return _CACHE["value"]
    rows = _query("""SELECT issue_id, slug, name, symbol, board, stage, open_date, close_date, listing_date, price_low, price_high, issue_price, lot_size,
                            issue_size_shares, sebi_filed_on, card FROM ipo_issue
                     WHERE (stage <> 'listed' OR listing_date >= %(since)s)
                       AND (stage <> 'closed' OR close_date IS NULL OR close_date >= %(ended_since)s)
                       AND (stage <> 'filed' OR sebi_filed_on >= %(filed_since)s)
                     ORDER BY coalesce(open_date, sebi_filed_on) DESC NULLS LAST""",
                  {"since": today - timedelta(days=LISTED_WINDOW_DAYS), "filed_since": today - timedelta(days=150), "ended_since": today - timedelta(days=ENDED_WINDOW_DAYS)}, user=user)
    rows = _effective(rows, today)
    # an issue the table still calls 'closed' that has traded counts as listed here too, but only once it has a listing date in the table does it have a stock-page tab
    rows = [r for r in rows if r["stage"] != "listed" or (r["listing_date"] and r["listing_date"] >= today - timedelta(days=LISTED_WINDOW_DAYS))]
    counts = {}
    for r in rows:
        counts[r["stage"]] = counts.get(r["stage"], 0) + 1
    from api import _chart_floor
    listed = [r for r in rows if r["stage"] == "listed"]
    isin_of = stock_isins([r["symbol"] for r in listed])
    perf = listing_performance(listed, _chart_floor(), isin_of)
    items = [_item(r, perf.get(r["issue_id"]), isin_of.get(r["symbol"]) if r["stage"] == "listed" and r["_stored"] == "listed" else None) for r in rows]
    run = last_run()
    value = {"items": items, "counts": {"upcoming": sum(counts.get(s, 0) for s in STAGE_GROUPS["upcoming"]), "listed": counts.get("listed", 0), "filed": counts.get("filed", 0),
                                         "ended": counts.get("ended", 0)},
             "updated": _iso(run["finished_at"]) if run else None}
    _CACHE.update(at=time.monotonic(), day=today, value=value)
    return value


@router.get("/list")
def list_issues(request: Request):
    user = _need(request)
    p = dict(request.query_params)
    _pick(p, "stage", set(STAGE_GROUPS))
    sort = _pick(p, "sort", set(SORTS)) or "date"
    try:
        limit = max(1, min(int(p.get("limit") or 200), MAX_LIST))
    except ValueError:
        raise HTTPException(400, "'limit' must be a whole number.")
    data = _load_items(user)
    shown = _sort(_filter(data["items"], p), sort)
    return {"issues": shown[:limit], "matched": len(shown), "counts": data["counts"], "updated": data["updated"], "notice": NOTICE}


@router.get("/status")
def status(request: Request):
    user = _need(request)
    if not stock_access.is_admin(user):
        raise HTTPException(403, "This is for administrators.")
    stages = _query("SELECT stage, count(*) AS n FROM ipo_issue GROUP BY stage ORDER BY stage", user=user)
    docs = _query("SELECT kind, status, count(*) AS n FROM ipo_document GROUP BY kind, status ORDER BY kind, status", user=user)
    failed = _query("""SELECT i.name, d.kind, d.attempts, left(d.error, 200) AS error FROM ipo_document d JOIN ipo_issue i USING (issue_id)
                       WHERE d.status = 'failed' ORDER BY d.doc_id DESC LIMIT 15""", user=user)
    runs = _query("""SELECT status, started_at, finished_at, rows_read, rows_written, left(coalesce(error_message, ''), 300) AS error FROM batch_run_log
                     WHERE run_type = 'IPO_REFRESH' ORDER BY run_id DESC LIMIT 10""") if _has_log() else []
    return {"stages": stages, "documents": docs, "failed": failed, "runs": [{**r, "started_at": _iso(r["started_at"]), "finished_at": _iso(r["finished_at"])} for r in runs]}


def _section_pages(h):
    labels = {"risk": "Risk factors", "offer": "The offer", "objects": "Objects of the offer", "promoters": "Our promoters and promoter group", "business": "Our business",
              "summary_fin": "Summary of financial information", "mdna": "Management's discussion and analysis", "litigation": "Outstanding litigation",
              "related_party": "Related party transactions", "contingent": "Contingent liabilities", "basis": "Basis for the offer price", "capital": "Capital structure",
              "industry": "Industry overview"}
    secs = h.get("sections") or {}
    return [{"label": labels[k], "page": secs[k]["page"]} for k in labels if k in secs]


def _detail(r, docs, exch):
    best = None
    for d in docs:
        if d["status"] == "done" and d.get("highlights"):
            if best is None or ((d["kind"] != "DRHP"), d["doc_date"] or date.min) > ((best["kind"] != "DRHP"), best["doc_date"] or date.min):
                best = d
    h = best["highlights"] if best else {}
    o, ob, pr, bz, fin, rk = (h.get("offer") or {}), (h.get("objects") or {}), (h.get("promoters") or {}), (h.get("business") or {}), (h.get("financials") or {}), (h.get("risks") or {})
    c = r.get("card") or {}
    item = _item(r)
    unit_f = 0.1                                                      # stored in rupees million; the page shows crore
    uses = [{"text": it["text"], "amount_cr": None if it.get("amount_mn") is None else round(it["amount_mn"] * unit_f, 2), "tags": it["tags"],
             "tag_labels": [USE_LABELS.get(t, t) for t in it["tags"]]} for it in (ob.get("items") or [])]
    page_of = lambda sec: ((h.get("sections") or {}).get(sec) or {}).get("page")
    rows = {k: {"label": v["label"], "values": v["values"], "page": v["page"]} for k, v in ((fin.get("rows") or {}).items())}
    return {
        "issue": item,
        "exchange": {"lead_managers": (exch or {}).get("brlms") or [], "registrar": (exch or {}).get("registrar"), "issue_size_text": (exch or {}).get("issue_size_text"),
                     "face_value": (exch or {}).get("face_value"), "price_range": (exch or {}).get("price_range"), "issue_period": (exch or {}).get("issue_period"),
                     "issue_type": (exch or {}).get("issue_type"), "subscription": (exch or {}).get("subscription") or []},
        "document": ({"kind": best["kind"], "source": best["source"], "date": _iso(best["doc_date"]) if best["kind"] == "DRHP" else None, "pages": best["pages"],
                      "url": best["pdf_url"] or best["source_ref"], "read_on": _iso(best["extracted_at"])} if best else None),
        "documents": [{"kind": d["kind"], "source": d["source"], "date": _iso(d["doc_date"]) if d["kind"] == "DRHP" else None, "status": d["status"],
                       "url": d["pdf_url"] or d["source_ref"]} for d in docs],
        "offer": {"fresh_shares": o.get("fresh_shares"), "ofs_shares": o.get("ofs_shares"), "total_shares": o.get("total_shares"), "face_value": o.get("face_value"),
                  "shares_before": o.get("shares_before"), "shares_after": o.get("shares_after"), "fresh_amount_cr": _crore(o.get("fresh_amount_mn")),
                  "ofs_amount_cr": _crore(o.get("ofs_amount_mn")), "total_amount_cr": _crore(o.get("total_amount_mn")), "no_proceeds_to_company": o.get("no_proceeds_to_company"),
                  "employee_reservation": o.get("employee_reservation_text"),
                  "selling_shareholders": [{"name": s["name"], "shares": s.get("shares"), "amount_cr": _crore(s.get("amount_mn"))} for s in (o.get("selling_shareholders") or [])],
                  "issue_type": c.get("issue_type"),
                  "issue_type_label": TYPE_LABELS.get(c.get("issue_type")), "fresh_pct": c.get("fresh_pct"), "page": o.get("page"), "size_cr": c.get("size_cr"), "size_basis": c.get("size_basis")},
        "money": {"intro": ob.get("intro"), "items": uses, "total_cr": _crore(ob.get("total_mn")), "no_proceeds_to_company": ob.get("no_proceeds_to_company"),
                  "page": ob.get("table_page") or ob.get("page"), "use_pct": {USE_LABELS.get(k, k): v for k, v in (c.get("use_pct") or {}).items()}},
        "promoters": {"names": pr.get("names") or [], "intro": pr.get("intro"), "holding_shares": pr.get("holding_shares"), "holding_pct": pr.get("holding_pct"),
                      "profiles": pr.get("profiles") or [], "selling": c.get("promoters_selling"), "page": pr.get("page")},
        "business": {"overview": bz.get("overview"), "page": bz.get("page")},
        "financials": {"unit": "₹ crore", "periods": fin.get("periods") or [], "basis": fin.get("basis"), "rows": rows, "page": fin.get("page"), "auditor": h.get("auditor"),
                       "revenue_growth": c.get("revenue_growth"), "pat_margin": c.get("pat_margin"), "fin_period": c.get("fin_period")},
        "risks": {"count": rk.get("count") or 0, "page": rk.get("page"), "topics": [{"key": t["key"], "label": t["label"], "count": t["count"], "items": t["items"][:4]} for t in (rk.get("topics") or [])],
                  "headings": [{"n": x["n"], "text": x["text"][:220], "page": x["page"]} for x in (rk.get("headings") or [])]},
        "where_to_read": _section_pages(h),
        "notes": h.get("notes") or [],
        "notice": NOTICE,
    }


def _crore(mn):
    return None if mn is None else round(mn / 10.0, 2)


def _stock_of(r):
    """{"isin", "symbol", "name"} when this issue's company is on FinChaya as a stock (so it has a stock page), else None."""
    if not r.get("symbol"):
        return None
    got = stock_isins([r["symbol"]])
    return {"isin": got[r["symbol"]], "symbol": r["symbol"], "name": r["name"]} if r["symbol"] in got else None


def _full_detail(r, user=None):
    r = _effective([r])[0] if r.get("_stored") is None else r         # the stage as it is TODAY (a row from the table has not been through this yet)
    docs = _query("""SELECT doc_id, kind, source, source_ref, doc_date, pdf_url, status, pages, extracted_at, highlights FROM ipo_document WHERE issue_id = %(i)s
                     ORDER BY doc_date DESC NULLS LAST, doc_id DESC""", {"i": r["issue_id"]}, user=user)
    out = _detail(r, docs, r.get("exchange_detail"))
    stock = _stock_of(r)
    out["stock"] = stock
    out["issue"]["isin"] = stock["isin"] if stock else None
    # the stock page has an IPO tab only for an issue the TABLE calls listed whose document has been read; the page redirects there only then
    out["stock_tab"] = bool(stock and r["_stored"] == "listed" and (r.get("card") or {}).get("read"))
    if r["stage"] == "listed":
        from api import _chart_floor
        out["issue"]["perf"] = listing_performance([r], _chart_floor(), {r["symbol"]: stock["isin"]} if stock else {}).get(r["issue_id"])
    return out


@router.get("/view.js", include_in_schema=False)
def view_script():
    """The script that draws one issue: shared by the IPO page and the IPO tab of the stock page, so the two cannot drift apart."""
    path = os.path.join(BASE_DIR, "ipo_view.js")
    if not os.path.exists(path):
        raise HTTPException(404, "ipo_view.js is missing from %s" % BASE_DIR)
    return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@router.get("/for-stock/{isin}")
def for_stock(isin: str, request: Request):
    """The IPO of a listed company, for the IPO tab on its stock page. Never an error: {"available": false} when this stock has no IPO on file that has been read,
    when the section is switched off, or when the visitor may not see it, so a stock page without an IPO is quiet."""
    from api import current_user
    isin = (isin or "").strip().upper()
    if not ISIN_RE.fullmatch(isin):
        return {"available": False}
    user = current_user(request)
    if not GATE.allowed(user):
        return {"available": False}
    try:
        m = _query("SELECT symbol FROM stock_master WHERE isin = %(i)s", {"i": isin}, one=True)
        if not m or not m.get("symbol"):
            return {"available": False}
        r = _query("""SELECT * FROM ipo_issue WHERE symbol = %(s)s AND stage = 'listed' AND (card->>'read')::boolean IS TRUE
                      ORDER BY listing_date DESC NULLS LAST LIMIT 1""", {"s": m["symbol"]}, one=True)
    except HTTPException:
        return {"available": False}
    if not r:
        return {"available": False}
    return {"available": True, "slug": r["slug"], "detail": _full_detail(r, user)}


@router.get("/{slug}")
def issue(slug: str, request: Request):
    user = _need(request)
    if not re.fullmatch(r"[a-z0-9-]{1,90}", slug):
        raise HTTPException(404, "No such issue.")
    r = _query("SELECT * FROM ipo_issue WHERE slug = %(s)s", {"s": slug}, one=True, user=user)
    if not r:
        raise HTTPException(404, "No such issue.")
    return _full_detail(r, user)


def mount(app):
    """Add the API and the pages to a running app. This is what enable_ipo.py appends to the server's api.py, so the section can go live without
    uploading the laptop's api.py (which carries every other pending change)."""
    app.include_router(router)
    app.include_router(page_router)