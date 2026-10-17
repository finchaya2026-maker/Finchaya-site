"""
fetch_stock_events.py -- corporate announcements, dividends, splits,
bonuses, buybacks, rights issues and forthcoming results.
------------------------------------------------------------------------
WHY TWO BULK CALLS REPLACED THOUSANDS OF PER-STOCK ONES
    Two endpoints happen to answer "what's coming up, across the WHOLE
    market" in a single request each, instead of one request per stock:

    - NSE's corporates-corporateActions?index=equities (default view is
      "All Forthcoming") returns every upcoming dividend/bonus/split/
      rights/buyback across all ~2,500 listed companies in one call, by
      ISIN directly -- no scrip-code resolution needed -- and the
      dividend amount is right there in the text ("Dividend - Rs 2.35
      Per Share"), not something this script has to infer.
    - BSE's Corpforthresults/w returns the exchange's own curated
      calendar of every company with a board meeting currently scheduled
      to consider results, across the whole market, in one call.

    Both were confirmed live, 28 Sep 2026: real, complete data, matching
    what each exchange's own results/corporate-actions calendar page
    shows. This replaces the previous per-stock design (loop every
    stock, hit BSE's DefaultData + BoardMeeting endpoints for each one),
    which was slower, and for results specifically, was inferring an
    upcoming date by text-mining announcement wording -- unnecessary now
    that BSE publishes the calendar itself.

    NSE's corporate-actions endpoint also turned out to answer a plain
    requests.Session() with no session warm-up at all (confirmed twice).
    That contradicts what an earlier version of this script concluded
    about NSE sitting behind Akamai -- that conclusion was drawn from a
    DIFFERENT NSE endpoint (per-stock announcements), which may still be
    blocked; it was never re-tested for this one specifically. Bot
    protection on NSE's API surface is evidently inconsistent across
    endpoints, so "NSE is blocked" doesn't generalize -- each endpoint
    needs its own check.

    STILL NOT FETCHED: actual result NUMBERS (revenue/profit/EPS). NSE's
    corporates-financial-results endpoint lists every FILED result with
    a link to its XBRL filing, but reading figures out of that XBRL is
    the parsing problem flagged as a later phase in the original
    fundamentals write-up, not solved here.

WHY THE PER-STOCK ANNOUNCEMENT FEED STILL EXISTS
    The two bulk calls above cover the screener's needs (what's coming
    up), but the single-stock page's "Corporate events" tab also shows
    general company announcements and board-meeting intimations, which
    have no bulk endpoint. That part still loops per stock against BSE's
    AnnSubCategoryGetData, and still needs stock_master.bse_scrip_code
    resolved by backfill_company_profile.py first.

SAFE TO RE-RUN. Upserts on (isin, kind, event_date, source_ref).

HOW OFTEN IT RUNS (this was missing, and it is why the "coming up" lists went stale)
    Nothing used to run this script; it was run by hand and last on 28 Sep 2026, so any company that
    announced a results date, dividend or split after that was simply absent (6 of the 7 companies
    reporting on 9 Oct). Now:
      * nightly.sh runs it every night with --bulk-only (dividends, splits, bonus, rights, results);
      * result_watcher.py, which runs every 30 minutes, refreshes the results dates from the same
        calendars (sync_results_calendar), so a date announced at 11 am is on the site by noon.
    The per-stock announcement history (the long part) is the only thing still run by hand.

USAGE
    python fetch_stock_events.py --symbols RELIANCE,TCS   # spot check
    python fetch_stock_events.py --limit 50 --dry-run     # scale test
    python fetch_stock_events.py --bulk-only              # the quick part, what nightly runs
    python fetch_stock_events.py                          # full universe, with per-stock history
"""

import argparse
import hashlib
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.rows import dict_row

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv()

DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set.")

WINDOW_DAYS = 360          # BSE refuses an announcements query spanning more
                           # than 12 months -- confirmed live: 400 days back
                           # returns {"Status":false,"Message":"Date range
                           # cannot exceed 12 months."} instead of data.
PAUSE_SEC = 0.6
BATCH_SIZE = 50
BATCH_PAUSE_SEC = 8

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

bse = requests.Session()
bse.headers.update({
    "User-Agent": UA, "Accept": "application/json",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
})

nse = requests.Session()
nse.headers.update({
    "User-Agent": UA, "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
})


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%d %b %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.split(".")[0] if "T" not in s and "." in s
                                      else s, fmt).date()
        except ValueError:
            continue
    return None


def ref_hash(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


ACTION_KIND = [
    (re.compile(r"buy.?back", re.I), "buyback"),
    (re.compile(r"bonus", re.I), "bonus"),
    (re.compile(r"split|sub.?division", re.I), "split"),
    (re.compile(r"rights?\s+issue|right\s+issue|^rights\b", re.I), "rights"),
    (re.compile(r"dividend", re.I), "dividend"),
]


def classify_action(purpose):
    for pat, kind in ACTION_KIND:
        if pat.search(purpose or ""):
            return kind
    return "corp_action_other"


def classify_announcement(text):
    if re.search(r"board meeting", text, re.I):
        return "board_meeting"
    return "announcement"


def fetch_nse_corporate_actions(from_date=None, to_date=None):
    """NSE's corporate actions: with no dates, the forthcoming ones; with a date window, every action
    whose ex-date falls inside it (verified 9 Oct 2026: 5,430 rows for 1 Aug 2024 .. 9 Oct 2026 in one call,
    including 118 splits, 118 bonuses, 33 demergers and 4,101 dividends)."""
    params = {"index": "equities"}
    if from_date and to_date:
        params.update(from_date=from_date.strftime("%d-%m-%Y"), to_date=to_date.strftime("%d-%m-%Y"))
    try:
        r = nse.get("https://www.nseindia.com/api/corporates-corporateActions",
                     params=params, timeout=60 if from_date else 20)
        if not r.ok:
            return []
        return r.json() or []
    except (requests.RequestException, ValueError):
        return []


def ingest_action_history(cur, known_isins, since, today=None, dry_run=False, window_days=365):
    """Load the history of dividends, splits, bonuses and demergers from `since` to now (a year per call).
    The price chart needs the splits and bonuses to stop a split looking like a crash, the valuation block
    needs the dividends, and the old per-stock announcement feed only ever reached back about a year.
    Returns (rows found in our list, rows written)."""
    today = today or date.today()
    lo, found, written = since, 0, 0
    while lo <= today:
        hi = min(lo + timedelta(days=window_days - 1), today + timedelta(days=60))
        rows = action_rows_from_nse(fetch_nse_corporate_actions(lo, hi), known_isins)
        found += len(rows)
        if not dry_run:
            for row in rows:
                cur.execute(UPSERT, row)
                written += 1
        lo = hi + timedelta(days=1)
        time.sleep(1.0)
    return found, written


def fetch_bse_forthcoming_results():
    try:
        r = bse.get("https://api.bseindia.com/BseIndiaAPI/api/Corpforthresults/w",
                     timeout=20)
        if not r.ok:
            return []
        return r.json() or []
    except (requests.RequestException, ValueError):
        return []


def fetch_announcements(scrip_code, since):
    try:
        r = bse.get("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w",
                     params={"strCat": "-1", "strPrevDate": since.strftime("%Y%m%d"),
                             "strScrip": scrip_code, "strSearch": "P",
                             "strToDate": date.today().strftime("%Y%m%d"),
                             "strType": "C"}, timeout=15)
        if not r.ok:
            return []
        j = r.json()
        return (j or {}).get("Table") or []
    except (requests.RequestException, ValueError):
        return []


def action_rows_from_nse(rows, known_isins):
    out = []
    for a in rows:
        isin = a.get("isin")
        if not isin or isin not in known_isins:
            continue
        d = parse_date(a.get("exDate")) or parse_date(a.get("recDate"))
        if not d:
            continue
        subject = (a.get("subject") or "").strip()
        out.append(dict(
            isin=isin, kind=classify_action(subject), event_date=d,
            headline=subject or "Corporate action", detail=a.get("series"),
            period=None, attachment_url=None, source="NSE",
            source_ref=ref_hash(isin, d, subject),
        ))
    return out


NSE_CALENDAR_URL = "https://www.nseindia.com/api/event-calendar"
NSE_CALENDAR_REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"

# A calendar that comes back with fewer BSE rows than this is treated as a bad answer, not as "nothing
# is scheduled": it never triggers the clean-up of rescheduled meetings below. (BSE lists ~200-300.)
MIN_CALENDAR_ROWS_FOR_CLEANUP = 50


def fetch_nse_results_calendar():
    try:
        r = nse.get(NSE_CALENDAR_URL, params={"index": "equities"}, timeout=25,
                    headers={"Referer": NSE_CALENDAR_REFERER})
        if not r.ok:
            return []
        return r.json() or []
    except (requests.RequestException, ValueError):
        return []


def results_calendar_rows(bse_rows, nse_rows, stocks):
    """The 'board meeting to consider results' dates of our stocks, from BOTH exchanges' calendars.
    Returns (rows, unmatched_bse, unmatched_nse). No network, no database.

    WHY BOTH, AND WHY BY SCRIP CODE. This used to read BSE's calendar only and match a row to a
    stock only when BSE's short name equalled the NSE symbol, which misses any company whose two
    short names differ. A BSE row is now matched on its scrip code first (the key we hold for
    every BSE-listed stock), then on the short name; an NSE row on its symbol. A company on both
    calendars on the same day is one row."""
    by_scrip = {s["bse_scrip_code"]: s["isin"] for s in stocks if s.get("bse_scrip_code")}
    by_symbol = {s["symbol"]: s["isin"] for s in stocks}
    found = {}                                   # (isin, date) -> row
    unmatched_bse = unmatched_nse = 0
    for r in bse_rows:
        d = parse_date(r.get("meeting_date"))
        if not d:
            continue
        isin = by_scrip.get(str(r.get("scrip_Code"))) or by_symbol.get((r.get("short_name") or "").strip().upper())
        if not isin:
            unmatched_bse += 1
            continue
        found[(isin, d)] = dict(isin=isin, kind="result", event_date=d,
                                headline="Board meeting to consider results", detail=None, period=None,
                                attachment_url=r.get("URL"), source="BSE",
                                source_ref=ref_hash(isin, d, "results"))
    for r in nse_rows:
        if "result" not in (r.get("purpose") or "").lower():
            continue                              # the same calendar also lists dividends, AGMs, fund raising...
        d = parse_date(r.get("date"))
        isin = by_symbol.get((r.get("symbol") or "").strip().upper())
        if not d:
            continue
        if not isin:
            unmatched_nse += 1
            continue
        found.setdefault((isin, d), dict(isin=isin, kind="result", event_date=d,
                                         headline="Board meeting to consider results",
                                         detail=(r.get("bm_desc") or None), period=None,
                                         attachment_url=None, source="NSE",
                                         source_ref=ref_hash(isin, d, "results")))
    return list(found.values()), unmatched_bse, unmatched_nse


RESULT_UPSERT = """
INSERT INTO stock_event
    (isin, kind, event_date, headline, detail, period, attachment_url, source, source_ref, updated_at)
VALUES
    (%(isin)s, %(kind)s, %(event_date)s, %(headline)s, %(detail)s, %(period)s, %(attachment_url)s,
     %(source)s, %(source_ref)s, now())
ON CONFLICT (isin, kind, event_date, source_ref) DO UPDATE SET
    headline = EXCLUDED.headline, attachment_url = COALESCE(EXCLUDED.attachment_url, stock_event.attachment_url),
    updated_at = now()
RETURNING (xmax = 0) AS inserted
"""


def sync_results_calendar(cur, stocks, today=None, dry_run=False):
    """Bring the 'results due' rows in stock_event up to date with both exchanges' calendars.
    Returns {'meetings', 'new', 'rescheduled_removed', 'unmatched_bse', 'unmatched_nse', 'ok'}.

    A meeting that has been MOVED leaves its old row behind (rows were only ever added), so the
    company would show 'results due' on the old date as well as the new one. For every company that
    is on today's calendar, any future calendar-sourced result row on a DIFFERENT date is removed.
    Only done when BSE's list came back at a believable size; a failed or empty download never
    deletes anything."""
    today = today or date.today()
    bse_rows, nse_rows = fetch_bse_forthcoming_results(), fetch_nse_results_calendar()
    if not bse_rows and not nse_rows:
        return dict(meetings=0, new=0, rescheduled_removed=0, unmatched_bse=0, unmatched_nse=0, ok=False)
    rows, un_bse, un_nse = results_calendar_rows(bse_rows, nse_rows, stocks)
    # The cursor must return dict rows (both callers use psycopg's dict_row).
    new = 0
    if not dry_run:
        for row in rows:
            cur.execute(RESULT_UPSERT, row)
            new += 1 if cur.fetchone()["inserted"] else 0
    else:
        keys = {(r["isin"], r["event_date"]) for r in rows}
        cur.execute("SELECT isin, event_date FROM stock_event WHERE kind='result' AND isin = ANY(%s)", ([r["isin"] for r in rows],))
        new = len(keys - {(r["isin"], r["event_date"]) for r in cur.fetchall()})

    removed = 0
    if len(bse_rows) >= MIN_CALENDAR_ROWS_FOR_CLEANUP and rows:
        fresh = {}
        for r in rows:
            fresh.setdefault(r["isin"], set()).add(r["event_date"])
        cur.execute("""SELECT isin, event_date FROM stock_event
                       WHERE kind = 'result' AND event_date >= %s AND source IN ('BSE', 'NSE') AND isin = ANY(%s)""",
                    (today, list(fresh)))
        stale = [(r["isin"], r["event_date"]) for r in cur.fetchall() if r["event_date"] not in fresh[r["isin"]]]
        if not dry_run:
            for i, d in stale:
                cur.execute("DELETE FROM stock_event WHERE isin = %s AND kind = 'result' AND event_date = %s "
                            "AND source IN ('BSE', 'NSE')", (i, d))
        removed = len(stale)
    return dict(meetings=len(rows), new=new, rescheduled_removed=removed,
                unmatched_bse=un_bse, unmatched_nse=un_nse, ok=True)


def announcement_rows_for_stock(isin, scrip_code, since):
    out = []
    for a in fetch_announcements(scrip_code, since):
        d = parse_date(a.get("NEWS_DT") or a.get("DT_TM"))
        if not d or d < since:
            continue
        headline = a.get("HEADLINE") or a.get("NEWSSUB") or "Announcement"
        category = a.get("CATEGORYNAME")
        att = a.get("ATTACHMENTNAME")
        out.append(dict(
            isin=isin, kind=classify_announcement(f"{headline} {category or ''}"),
            event_date=d, headline=headline, detail=category, period=None,
            attachment_url=("https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
                             + att) if att else None,
            source="BSE",
            source_ref=a.get("NEWSID") or ref_hash(isin, d, headline),
        ))
    return out


UPSERT = """
INSERT INTO stock_event
    (isin, kind, event_date, headline, detail, period, attachment_url,
     source, source_ref, updated_at)
VALUES
    (%(isin)s, %(kind)s, %(event_date)s, %(headline)s, %(detail)s,
     %(period)s, %(attachment_url)s, %(source)s, %(source_ref)s, now())
ON CONFLICT (isin, kind, event_date, source_ref) DO UPDATE SET
    headline = EXCLUDED.headline, detail = EXCLUDED.detail,
    period = EXCLUDED.period, attachment_url = EXCLUDED.attachment_url,
    updated_at = now()
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma-separated NSE symbols, for a spot check")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--actions-history-from", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    help="also load every dividend, split, bonus and demerger from this date on (a one-off, about a "
                         "minute per year of history), e.g. 2024-08-01. Needed for the price chart's split "
                         "adjustment and for dividend yield.")
    ap.add_argument("--bulk-only", action="store_true",
                    help="only the market-wide calendars (results dates, dividends, splits, bonus, rights): "
                         "a few requests, about a minute. Skips the per-stock announcement history.")
    args = ap.parse_args()

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        where = "WHERE is_active"
        params = {}
        if args.symbols:
            syms = [s.strip().upper() for s in args.symbols.split(",")]
            where += " AND symbol = ANY(%(syms)s)"
            params["syms"] = syms
        cur.execute(f"""
            SELECT isin, symbol, bse_scrip_code FROM stock_master {where}
            ORDER BY symbol
            {"LIMIT %(limit)s" if args.limit else ""}
        """, {**params, "limit": args.limit})
        stocks = cur.fetchall()
        isin_by_symbol = {s["symbol"]: s["isin"] for s in stocks}
        known_isins = set(isin_by_symbol.values())
        print(f"{len(stocks)} stocks in scope.")

        action_rows = action_rows_from_nse(fetch_nse_corporate_actions(), known_isins)
        print(f"NSE corporate actions (bulk, dividends/bonus/split/rights/buyback): "
              f"{len(action_rows)} rows in scope.")
        if not args.dry_run:
            for row in action_rows:
                cur.execute(UPSERT, row)
        if args.actions_history_from:
            found, wrote = ingest_action_history(cur, known_isins, args.actions_history_from, dry_run=args.dry_run)
            print(f"Corporate action history since {args.actions_history_from}: {found} rows for our stocks, {wrote} written.")
            if not args.dry_run:
                conn.commit()

        cal = sync_results_calendar(cur, stocks, dry_run=args.dry_run)
        if cal["ok"]:
            print(f"Results calendar (BSE + NSE): {cal['meetings']} meetings in scope, {cal['new']} new, "
                  f"{cal['rescheduled_removed']} old dates removed because the meeting moved; "
                  f"{cal['unmatched_bse']} BSE and {cal['unmatched_nse']} NSE rows are for companies not in our list.")
        else:
            print("Results calendar: neither exchange answered; left as it was.")
        if not args.dry_run:
            conn.commit()

        if args.bulk_only:
            print("\n--bulk-only: per-stock announcements skipped.")
            if args.dry_run:
                print("--dry-run: nothing written.")
            return

        with_scrip = [s for s in stocks if s["bse_scrip_code"]]
        since = date.today() - timedelta(days=WINDOW_DAYS)
        print(f"{len(with_scrip)} of those have a resolved BSE scrip code -- "
              f"fetching their announcement history too. "
              f"(Run backfill_company_profile.py first if this is 0.)")

        total_ann = ok = empty = 0
        for i, s in enumerate(with_scrip, 1):
            rows = announcement_rows_for_stock(s["isin"], s["bse_scrip_code"], since)
            print(f"[{i}/{len(with_scrip)}] {s['symbol']:<15} {len(rows)} announcements")
            total_ann += len(rows)
            if rows:
                ok += 1
            else:
                empty += 1

            if not args.dry_run:
                for row in rows:
                    cur.execute(UPSERT, row)
                if i % BATCH_SIZE == 0:
                    conn.commit()
                    time.sleep(BATCH_PAUSE_SEC)
            time.sleep(PAUSE_SEC)

        if not args.dry_run:
            conn.commit()

        print(f"\nDone. {len(action_rows)} corporate actions (NSE, bulk) + "
              f"{cal['meetings']} results dates (BSE + NSE calendars) + "
              f"{total_ann} announcements across {ok} stocks "
              f"({empty} returned no announcements).")
        if args.dry_run:
            print("--dry-run: nothing written.")


if __name__ == "__main__":
    main()
