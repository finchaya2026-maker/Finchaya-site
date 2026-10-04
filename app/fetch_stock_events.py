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

USAGE
    python fetch_stock_events.py --symbols RELIANCE,TCS   # spot check
    python fetch_stock_events.py --limit 50 --dry-run     # scale test
    python fetch_stock_events.py                          # full universe
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


def fetch_nse_corporate_actions():
    try:
        r = nse.get("https://www.nseindia.com/api/corporates-corporateActions",
                     params={"index": "equities"}, timeout=20)
        if not r.ok:
            return []
        return r.json() or []
    except (requests.RequestException, ValueError):
        return []


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


def result_rows_from_bse(rows, isin_by_symbol):
    out = []
    for r in rows:
        symbol = (r.get("short_name") or "").strip().upper()
        isin = isin_by_symbol.get(symbol)
        if not isin:
            continue
        d = parse_date(r.get("meeting_date"))
        if not d:
            continue
        out.append(dict(
            isin=isin, kind="result", event_date=d,
            headline="Board meeting to consider results", detail=None,
            period=None, attachment_url=r.get("URL"), source="BSE",
            source_ref=ref_hash(isin, d, "results"),
        ))
    return out


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
        result_rows = result_rows_from_bse(fetch_bse_forthcoming_results(), isin_by_symbol)
        print(f"NSE corporate actions (bulk, dividends/bonus/split/rights/buyback): "
              f"{len(action_rows)} rows in scope.")
        print(f"BSE forthcoming results (bulk): {len(result_rows)} rows in scope.")

        if not args.dry_run:
            for row in action_rows + result_rows:
                cur.execute(UPSERT, row)
            conn.commit()

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
              f"{len(result_rows)} forthcoming results (BSE, bulk) + "
              f"{total_ann} announcements across {ok} stocks "
              f"({empty} returned no announcements).")
        if args.dry_run:
            print("--dry-run: nothing written.")


if __name__ == "__main__":
    main()
