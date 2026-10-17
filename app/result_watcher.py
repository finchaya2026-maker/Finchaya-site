"""
result_watcher.py -- picks up company results the day they are filed.
------------------------------------------------------------------------
WHAT IT DOES, EACH TIME IT RUNS (cheap when nothing new: 1-3 calls)
    1. Asks BSE's announcements feed for every "Result" announcement filed
       today and yesterday, across the whole market, in one call.
    2. Adds companies the results calendar says are due (BSE's and NSE's).
    3. Keeps the ones in our stock list (matched on BSE scrip code).
    4. For each, reads BSE's filing list, takes filings uploaded in the
       window, fetches the readable filing page, and writes revenue /
       profit / EPS into stock_financials with the same reader and the same
       upsert as fetch_stock_financials.py.
    A company whose figures are already stored is skipped, so running it
    every half hour costs nothing extra once a result is in.

WHY THE ANNOUNCEMENTS FEED AND NOT JUST THE CALENDAR
    On 7 Oct 2026 BSE's calendar listed 3 companies for the day, none of
    them in our stock list, and 5 companies actually filed. The calendar
    is whatever boards chose to announce in advance; the feed is what was
    filed. Both are used; the feed is the one that decides.

WHY "WAITING" IS A NORMAL STATE
    The board-meeting outcome (a PDF) goes up first. The structured
    filing the figures come from is uploaded later -- minutes to a day.
    Until it appears in BSE's list the stock is reported WAITING and the
    next run tries again. Nothing is written for it, and nothing blank is
    ever written over a real number (the upsert keeps what it has).

WHY A FILING IS PICKED BY UPLOAD DATE, NOT BY GUESSING THE QUARTER
    "Which quarter is this?" depends on fiscal-year ends, half-yearly
    filers and late filers. "Uploaded in the last 2 days" does not. The
    quarter comes from the filing's own BSE period code.

WHAT IS HELD BACK
    Revenue 10x up or down on the previous stored period (what a units
    mistake looks like) is NOT written; it is listed as HELD for a human
    look. A blank is better than a wrong number. Profit bigger than revenue
    is NOT held: holding and investment companies report exactly that.

WHEN BSE FILES A RESULT UNDER THE WRONG PERIOD
    Seen on TAJGVK: a filing uploaded 31 Jul 2026 carries the code for
    Jun 2025. For a period with both a normal upload (within 270 days of the
    period end) and a much later one, the later one is ignored.

USAGE
    python result_watcher.py                       # today + yesterday (IST)
    python result_watcher.py --dry-run             # show, write nothing
    python result_watcher.py --day 2026-10-06 --days 3 --dry-run
    python result_watcher.py --symbols TCS --days 400 --dry-run   # spot check
    python result_watcher.py --backfill 2026-06-30 # fill one past quarter for
                                                   #   every stock still blank
    python result_watcher.py --stamp-dates         # fill in the "filed on" date for results already
                                                   #   stored (needs add_stock_financials_filed_on.py run
                                                   #   once first; downloads no pages, so it is quick)
    python result_watcher.py --operating           # operating profit + profit before tax, for the results page,
                                                   #   of every stock with a result filed in the last 30 days (that
                                                   #   quarter, the one before, and a year earlier). Needs
                                                   #   add_stock_financials_operating.py run once first. New results
                                                   #   get theirs automatically from then on.
    python result_watcher.py --backfill-from 2025-03-31 --shard 1/3 &   # every quarter and year since
    python result_watcher.py --backfill-from 2025-03-31 --shard 2/3 &   #   Mar 2025, in one pass, split
    python result_watcher.py --backfill-from 2025-03-31 --shard 3/3 &   #   across three runs
CRON (droplet; every 30 minutes, all day, every day). It works out IST itself, so the
server's clock zone does not matter, and an idle run is about 3 web calls:
    */30 * * * * cd /opt/mfapi && venv/bin/python3 result_watcher.py >> logs/result_watcher.log 2>&1

SAFE TO RE-RUN. Upserts on (isin, period_type, period_end); a second run
for a filing already stored is skipped. An advisory lock stops two runs
overlapping.
"""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
import requests
from psycopg.rows import dict_row

import fetch_stock_financials as fsf

DB = fsf.DB
IST = timezone(timedelta(hours=5, minutes=30))
PAUSE_SEC = 0.4
PAGE_PAUSE_SEC = 0.2
LATE_UPLOAD_DAYS = 270
MAX_CONSECUTIVE_BSE_ERRORS = 8
MAX_ANNOUNCEMENT_PAGES = 60
LOCK_KEY = 727401
RUN_TYPE = "FETCH_RESULT_WATCH"

ANN_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
LIST_URL = "https://api.bseindia.com/BseIndiaAPI/api/Corp_FinanceResult_ng_new/w"
BSE_CAL_URL = "https://api.bseindia.com/BseIndiaAPI/api/Corpforthresults/w"
NSE_CAL_URL = "https://www.nseindia.com/api/event-calendar"

api = requests.Session()
api.headers.update({
    "User-Agent": fsf.UA, "Accept": "application/json",
    "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com",
})
nse_api = requests.Session()
nse_api.headers.update({
    "User-Agent": fsf.UA, "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings",
})


def now_ist():
    return datetime.now(IST).replace(tzinfo=None)


def to_date(s):
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((s or "").strip(), fmt).date()
        except ValueError:
            pass
    return None


def to_dt(s):
    try:
        return datetime.fromisoformat(str(s)[:19])
    except ValueError:
        return None


# ---------------------------------------------------------------- sources
def announcements(d_from, d_to):
    """([(scrip_code, headline, time)], ok). Every 'Result' announcement filed
    between the two dates, all companies. ok=False if the feed itself failed."""
    out, page = [], 1
    while page <= MAX_ANNOUNCEMENT_PAGES:
        try:
            r = api.get(ANN_URL, params={
                "pageno": page, "strCat": "Result", "strPrevDate": d_from.strftime("%Y%m%d"),
                "strScrip": "", "strSearch": "P", "strToDate": d_to.strftime("%Y%m%d"),
                "strType": "C", "subcategory": "-1"}, timeout=40)
            if not r.ok:
                return out, False
            table = (r.json() or {}).get("Table") or []
        except (requests.RequestException, ValueError):
            return out, False
        for x in table:
            out.append((str(x.get("SCRIP_CD")), x.get("NEWSSUB") or "", to_dt(x.get("NEWS_DT"))))
        pages = int((table[0].get("TotalPageCnt") if table else 1) or 1)
        if not table or page >= pages:
            return out, True
        page += 1
        time.sleep(0.3)
    return out, True


def calendar_due(d_from, d_to):
    """({bse scrip codes}, {nse symbols}) of companies whose results meeting falls in the
    window. Best effort: either calendar failing just means fewer names."""
    scrips, symbols = set(), set()
    try:
        r = api.get(BSE_CAL_URL, timeout=25)
        for x in (r.json() if r.ok else []) or []:
            d = to_date(x.get("meeting_date"))
            if d and d_from <= d <= d_to:
                scrips.add(str(x.get("scrip_Code")))
    except (requests.RequestException, ValueError):
        pass
    try:
        r = nse_api.get(NSE_CAL_URL, params={"index": "equities"}, timeout=25)
        for x in (r.json() if r.ok else []) or []:
            d = to_date(x.get("date"))
            if d and d_from <= d <= d_to and "result" in (x.get("purpose") or "").lower():
                symbols.add((x.get("symbol") or "").strip().upper())
    except (requests.RequestException, ValueError):
        pass
    return scrips, symbols


def listing(scrip):
    """BSE's list of every filing one company has made, or None if the call failed."""
    try:
        r = api.get(LIST_URL, params={"SCRIP_CD": scrip, "FlagDur": "7", "HFQ": "",
                                      "ISUBGROUP_CODE": "", "segment": "C"}, timeout=30)
        if not r.ok:
            return None
        return (r.json() or {}).get("Table") or []
    except (requests.RequestException, ValueError):
        return None


# ------------------------------------------------------------ the pieces
def page_for(name):
    """The readable page for a file name BSE lists, or None. Ordinary companies and banks list
    the page itself (.html). Finance companies and insurers list only the raw .xml, but the same
    name ending .html sits in the same folder (checked on Aavas, Bajaj Finance, SBI Life,
    ICICI Lombard). A plain "FourOneUploadDocument/...xml" has no such twin (404)."""
    name = name or ""
    if name.endswith(".html"):
        return name
    if name.endswith(".xml") and "DuplicateUploadDocument" in name.split("/")[0]:
        return name[:-4] + ".html"
    return None


def filing_groups(rows, keep):
    """{(period_type, period_end): [(page_name, consolidated, filed_on), ...]}, consolidated first,
    for the listing rows that `keep(row, period_type, period_end)` accepts. filed_on is the day
    the result was first filed with BSE (the earliest upload for that period; the consolidated
    and standalone files go up together), or None if BSE gave no date."""
    groups = {}
    for f in rows:
        parsed = fsf.parse_quarter_code(f.get("quarter_code"))
        if not parsed:
            continue
        period_end, period_type = parsed
        if not keep(f, period_type, period_end):
            continue
        opts = groups.setdefault((period_type, period_end), [])
        created = to_dt(f.get("Fld_CreateDate"))
        filed = created.date() if created else None
        late = (filed - period_end).days if filed else None                # days from period end to upload
        if page_for(f.get("Consol_XMLName")):
            opts.append((page_for(f["Consol_XMLName"]), True, late, filed))
        if page_for(f.get("XMLName")):
            opts.append((page_for(f["XMLName"]), f.get("Fld_NatureOfReport") != "Standalone", late, filed))
    for key, opts in groups.items():
        # BSE sometimes files a result under the wrong period code (seen: TAJGVK's Jun-2026 filing,
        # uploaded 31 Jul 2026, carries the code for Jun 2025). Results are due within weeks of
        # the period end, so when a period has both a normal upload and one made far later,
        # the late one is the likelier mislabel and is left out. A period with ONLY a late
        # upload (a genuine late filer) is kept.
        if any(o[2] is not None and o[2] <= LATE_UPLOAD_DAYS for o in opts):
            opts = [o for o in opts if o[2] is None or o[2] <= LATE_UPLOAD_DAYS]
        first_filed = min((o[3] for o in opts if o[3]), default=None)
        seen, ordered = set(), []
        for name, consol, _, _ in sorted(opts, key=lambda o: not o[1]):
            if name not in seen:
                seen.add(name)
                ordered.append((name, consol, first_filed))
        groups[key] = ordered
    return {k: v for k, v in groups.items() if v}


def doubtful(row, prev_revenue):
    """Why these figures should not be written without a human look, or None.

    Only the one test that catches a units slip: revenue 10x up or down on the period before.
    Profit larger than revenue and even negative revenue are NOT errors: holding and investment
    companies report exactly that (Bajaj Holdings: revenue Rs 394 cr, profit Rs 2,708 cr, EPS 243,
    which agrees; MMTC; Essar Shipping). An earlier version held those back and blanked 28 stocks."""
    rev = row["revenue"]
    if rev and prev_revenue and not (0.1 <= rev / prev_revenue <= 10):
        return "revenue is %.1fx the previous stored period (units?)" % (rev / prev_revenue)
    return None


def crore(v):
    return "n/a" if v is None else "%s" % format(round(v / 1e7, 1), ",.1f")


def load_stored(cur, isins):
    """{isin: {(period_type, period_end): row}} of what we already hold."""
    out = {}
    cur.execute("""SELECT isin, period_type, period_end, revenue, profit, consolidated, source_ref
                   FROM stock_financials WHERE isin = ANY(%s)""", (list(isins),))
    for r in cur.fetchall():
        out.setdefault(r["isin"], {})[(r["period_type"], r["period_end"])] = r
    return out


def has_numbers(r):
    return bool(r) and (r["revenue"] is not None or r["profit"] is not None)


def prev_revenue(stored, period_type, period_end, consolidated):
    """Revenue of the period just before this one, same kind (quarter vs year, consolidated vs
    standalone), if we hold it and it is recent enough to compare. Our stored history stops at
    Dec 2024 for most stocks, and a 15-month-old figure says nothing about this quarter."""
    window = timedelta(days=130 if period_type == "quarterly" else 400)
    best = None
    for (ptype, pend), r in stored.items():
        if (ptype == period_type and period_end - window <= pend < period_end and r["revenue"]
                and r["consolidated"] == consolidated and (best is None or pend > best[0])):
            best = (pend, r["revenue"])
    return float(best[1]) if best else None     # the column is numeric (Decimal); the parsed figure is a float


# Same rule as fetch_stock_financials.UPSERT: a failed read must never blank a real number, and a missing
# filing date must never blank a known one. The four variants exist so the watcher keeps working whichever of
# the two migrations (filed_on; operating_profit + profit_before_tax) has been run.
UPSERT_DATED = fsf.upsert_sql(dated=True)


def choose_upsert(cur):
    """The right statement for the columns the table has now."""
    return fsf.upsert_sql(dated=has_filed_on(cur), operating=has_operating(cur))


def has_operating(cur):
    """Have operating_profit and profit_before_tax been added (add_stock_financials_operating.py)?"""
    cur.execute("""SELECT count(*) AS n FROM pg_attribute WHERE attrelid = to_regclass('stock_financials')
                   AND attname IN ('operating_profit', 'profit_before_tax') AND NOT attisdropped""")
    return cur.fetchone()["n"] == 2


def has_filed_on(cur):
    """Does stock_financials have the filing-date column yet? (add_stock_financials_filed_on.py adds
    it.) Looked up on whichever table the name resolves to, so this watcher keeps working, without
    dates, if the code is uploaded before the migration is run."""
    cur.execute("""SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('stock_financials')
                   AND attname = 'filed_on' AND NOT attisdropped""")
    return cur.fetchone() is not None


def has_balance_sheet(cur):
    """Has create_stock_balance_sheet.py been run? Same idea as has_filed_on: results keep being read, without
    balance sheets, if this code is uploaded before the table exists."""
    cur.execute("SELECT to_regclass('stock_balance_sheet') IS NOT NULL AS ok")
    return cur.fetchone()["ok"]


def bs_period(ptype, pend):
    """Annual filings and the March and September quarters carry a balance sheet; June and December ones do not."""
    return ptype == "yearly" or pend.month in (3, 9)


def store_balance_sheet(cur, isin, ptype, pend, consol, filed, name, html, dry=False):
    """Read the balance sheet out of an already-downloaded filing page and store it. True if the page had one.
    Runs inside a savepoint: a problem here must never undo the results that were just written beside it."""
    row = fsf.balance_sheet_row(isin, pend, fsf.parse_facts(html), consol, name,
                                fsf.ref_hash(isin, "bs", pend, name), filed, annual=(ptype == "yearly"))
    if row is None:
        return False
    if not dry:
        with cur.connection.transaction():
            cur.execute(fsf.BS_UPSERT, row)
    return True


def month_end_shift(d, months):
    """The last day of the month `months` months away from d (negative = earlier)."""
    y, mth = divmod(d.year * 12 + d.month - 1 + months, 12)
    mth += 1
    nxt = date(y + (mth == 12), mth % 12 + 1, 1)
    return nxt - timedelta(days=1)


def fill_operating_for(cur, m, rows, wanted, pages, dry):
    """Read operating profit and profit before tax for results we ALREADY hold, from the filing pages.
    wanted: quarter-end dates. Only stored quarterly rows that have figures and no profit_before_tax yet are
    read, each from the filing of the SAME basis (consolidated against consolidated), and only those two
    columns are written: nothing else about the row changes. A page whose revenue differs from the stored
    revenue by more than half a per cent is a different version of the result and is left alone.
    Returns [(outcome, text)]: DONE, NO_FIGURES, NO_FILING, FETCH_FAILED, CHANGED."""
    out = []
    cur.execute("""SELECT period_end, consolidated, revenue FROM stock_financials
                   WHERE isin = %s AND period_type = 'quarterly' AND period_end = ANY(%s)
                     AND profit_before_tax IS NULL AND (revenue IS NOT NULL OR profit IS NOT NULL)""",
                (m["isin"], list(wanted)))
    need = {r["period_end"]: r for r in cur.fetchall()}
    if not need:
        return out
    groups = filing_groups(rows, lambda f, ptype, pend: ptype == "quarterly" and pend in need)
    for pend, old in sorted(need.items()):
        label = fsf.fiscal_quarter_label(pend)
        opts = [o for o in groups.get(("quarterly", pend), []) if o[1] == old["consolidated"]]
        if not opts:
            out.append(("NO_FILING", "%s: no %s filing in BSE's list" % (label, "consolidated" if old["consolidated"] else "standalone")))
            continue
        name = opts[0][0]
        if name not in pages:
            pages[name] = fsf.fetch_bse_xbrl_html(name)
            time.sleep(PAGE_PAUSE_SEC)
        if not pages[name]:
            out.append(("FETCH_FAILED", "%s: filing page would not download (will retry)" % label))
            continue
        row = fsf.build_financial_row(m["isin"], "quarterly", pend, fsf.parse_facts(pages[name]), old["consolidated"],
                                      "BSE", None, page_name=name)
        if row is None or (row["operating_profit"] is None and row["profit_before_tax"] is None):
            out.append(("NO_FIGURES", "%s: the page gives neither operating profit nor profit before tax" % label))
            continue
        if old["revenue"] and row["revenue"] and abs(float(row["revenue"]) / float(old["revenue"]) - 1) > 0.005:
            out.append(("CHANGED", "%s: the page's revenue differs from the stored revenue (a revised result?) -- left alone" % label))
            continue
        if not dry:
            cur.execute("""UPDATE stock_financials SET operating_profit = %s, profit_before_tax = %s
                           WHERE isin = %s AND period_type = 'quarterly' AND period_end = %s AND consolidated = %s""",
                        (row["operating_profit"], row["profit_before_tax"], m["isin"], pend, old["consolidated"]))
        out.append(("DONE", "%s: operating profit Rs %s cr, profit before tax Rs %s cr"
                    % (label, crore(row["operating_profit"]), crore(row["profit_before_tax"]))))
    return out


def handle_stock(cur, m, rows, keep, stored, dry, upsert=fsf.UPSERT, force=frozenset(), bs=False, op=False):
    """Returns a list of (outcome, text). Outcomes: DONE ALREADY WAITING UNREADABLE HELD FETCH_FAILED.
    bs=True: when a results filing carrying a balance sheet is written (annual, March, September), the balance
    sheet is stored from the same page, at no extra download.
    op=True: when a QUARTERLY result is written, the two quarters the results page compares it with (the one
    before and the same quarter a year earlier) get their operating profit read too, if they do not have it
    yet (two more page downloads, once per stock)."""
    out = []
    groups = filing_groups(rows, keep)
    if not groups:
        return [("WAITING", "no readable filing in BSE's list yet")]
    pages = {}      # a March quarter and its full year are usually the same page: download it once
    # Oldest first, so each period is checked against the one before it, which has just been stored.
    for (ptype, pend), opts in sorted(groups.items(), key=lambda kv: kv[0][1]):
        label = fsf.fiscal_quarter_label(pend) if ptype == "quarterly" else fsf.fiscal_year_label(pend)
        old = stored.get((ptype, pend))
        status = "WAITING"
        for name, consol, filed in opts:
            ref = fsf.ref_hash(m["isin"], ptype, pend, name)
            if ((ptype, pend) not in force and has_numbers(old)
                    and (old["source_ref"] == ref or (old["consolidated"] and not consol))):
                status, text = "ALREADY", "%s already stored" % label
                break
            if name not in pages:
                pages[name] = fsf.fetch_bse_xbrl_html(name)
                time.sleep(PAGE_PAUSE_SEC)       # several backfill slices at once: stay gentle on BSE
            html = pages[name]
            if not html:
                status, text = "FETCH_FAILED", "%s: filing page would not download (will retry)" % label
                continue
            row = fsf.build_financial_row(m["isin"], ptype, pend, fsf.parse_facts(html), consol, "BSE", ref, page_name=name)
            if row is None or (row["revenue"] is None and row["profit"] is None):
                status, text = "UNREADABLE", "%s: page downloaded but no revenue/profit found in it" % label
                continue
            why = doubtful(row, prev_revenue(stored, ptype, pend, consol))
            figures = "revenue Rs %s cr, profit Rs %s cr, EPS %s (%s)" % (
                crore(row["revenue"]), crore(row["profit"]), row["eps"], "consolidated" if consol else "standalone")
            if why:
                status, text = "HELD", "%s: %s -- NOT written (%s)" % (label, figures, why)
                break
            row["filed_on"] = filed
            if not dry:
                cur.execute(upsert, row)
            stored[(ptype, pend)] = {"revenue": row["revenue"], "profit": row["profit"],
                                     "consolidated": consol, "source_ref": ref}
            status, text = "DONE", "%s: %s" % (label, figures)
            if bs and bs_period(ptype, pend):
                try:
                    if store_balance_sheet(cur, m["isin"], ptype, pend, consol, filed, name, html, dry):
                        text += " + balance sheet"
                except Exception as e:                  # noqa: BLE001  the results above are already safe
                    text += " (balance sheet not stored: %s)" % str(e)[:80]
            if op and ptype == "quarterly":
                try:
                    with cur.connection.transaction():          # a problem here must never undo the result just written
                        extra = fill_operating_for(cur, m, rows, [month_end_shift(pend, -3), month_end_shift(pend, -12)], pages, dry)
                    done = [t for s, t in extra if s == "DONE"]
                    if done:
                        text += " + operating profit for %d earlier quarter(s)" % len(done)
                except Exception as e:                  # noqa: BLE001
                    text += " (operating profit of earlier quarters not read: %s)" % str(e)[:80]
            break
        out.append((status, text))
    return out


def refresh_results_calendar(conn, cur, master, today):
    """Keep the 'results due' rows of the events table (the Corporate events tab and the stock screener's
    'upcoming results' list) current. This run already reads both exchanges' calendars to know who is due
    today; the same two calls refresh the table, so it costs nothing extra and a date announced at 11 am
    is on the site by noon. It must never get in the way of reading results, so any trouble is reported
    and swallowed. Prints only when something actually changed, to keep a 48-runs-a-day log readable."""
    try:
        import fetch_stock_events as fse          # imported here: a problem in that module cannot stop this one
        out = fse.sync_results_calendar(cur, master, today)
        conn.commit()
        if out["new"] or out["rescheduled_removed"]:
            print("Results calendar: %d new date(s) added, %d old date(s) removed because the meeting moved."
                  % (out["new"], out["rescheduled_removed"]))
        elif not out["ok"]:
            print("Results calendar: neither exchange answered; the events table was left as it was.")
        return out
    except Exception as e:                        # noqa: BLE001
        conn.rollback()
        print("Results calendar not refreshed (%s: %s); results reading carries on." % (type(e).__name__, str(e)[:120]))
        return None


def stamp_dates(conn, cur, master, args):
    """Fill in filed_on for results we already hold, from BSE's own upload dates. One filing list
    per stock and no pages downloaded, so it is much quicker than a backfill. Only rows that have
    figures and no date yet are touched, and nothing else about them changes (not even updated_at)."""
    if not has_filed_on(cur):
        print("stock_financials has no filed_on column yet. Run add_stock_financials_filed_on.py first.")
        return 0, {}, "filed_on column missing"
    since = args.stamp_dates
    wanted = {x.strip().upper() for x in args.symbols.split(",")} if args.symbols else None
    items = sorted((m for m in master if m["bse_scrip_code"] and (wanted is None or m["symbol"] in wanted)),
                   key=lambda m: m["symbol"])
    if args.shard:
        k, n = args.shard
        items = [m for idx, m in enumerate(items) if idx % n == k - 1]
        print("Slice %d of %d: this run takes every %d-th stock." % (k, n, n))
    if args.limit:
        items = items[:args.limit]
    keep = lambda f, ptype, pend: pend >= since
    print("Filling in filing dates for results ended on or after %s: %d stock(s)%s.\n"
          % (since, len(items), " (dry run: nothing will be written)" if args.dry_run else ""))
    where = ("WHERE isin = %s AND period_type = %s AND period_end = %s AND filed_on IS NULL "
             "AND (revenue IS NOT NULL OR profit IS NOT NULL)")
    tally, stamped, errors, aborted = {}, 0, 0, None
    for i, m in enumerate(items, 1):
        rows = listing(m["bse_scrip_code"])
        if rows is None:
            errors += 1
            tally["BSE_ERROR"] = tally.get("BSE_ERROR", 0) + 1
            print("  BSE_ERROR   %-12s BSE's filing list would not load" % m["symbol"])
            if errors >= MAX_CONSECUTIVE_BSE_ERRORS:
                aborted = "BSE failed %d times in a row -- stopping" % errors
                break
            time.sleep(PAUSE_SEC)
            continue
        errors = 0
        for (ptype, pend), opts in filing_groups(rows, keep).items():
            filed = opts[0][2]
            if filed is None:
                continue
            if args.dry_run:
                cur.execute("SELECT 1 FROM stock_financials " + where, (m["isin"], ptype, pend))
                hit = cur.fetchone() is not None
            else:
                cur.execute("UPDATE stock_financials SET filed_on = %s " + where, (filed, m["isin"], ptype, pend))
                hit = cur.rowcount > 0
            if hit:
                stamped += 1
                tally["STAMPED"] = tally.get("STAMPED", 0) + 1
        conn.commit()
        if i % 100 == 0:
            print("  ... %d/%d checked, %d dates filled in" % (i, len(items), stamped))
        time.sleep(PAUSE_SEC)
    print("\nSummary:", ", ".join("%s %d" % kv for kv in sorted(tally.items())) or "nothing to fill in",
          "| filled in %d" % stamped)
    if aborted:
        print("PROBLEM:", aborted)
    return stamped, tally, aborted


def balance_sheets(conn, cur, master, args):
    """Read the balance sheets of the annual, March and September filings since a date, for P/B, return on
    equity and debt to equity. One filing list per stock, and a page download only for a filing whose balance
    sheet is not stored yet (the March quarter and the annual result are usually one page: downloaded once).
    Results are not touched. Pages that carry no balance sheet are simply skipped."""
    if not has_balance_sheet(cur):
        print("stock_balance_sheet does not exist yet. Run create_stock_balance_sheet.py first.")
        return 0, {}, "stock_balance_sheet table missing"
    since = args.balance_sheet
    wanted = {x.strip().upper() for x in args.symbols.split(",")} if args.symbols else None
    items = sorted((m for m in master if m["bse_scrip_code"] and (wanted is None or m["symbol"] in wanted)),
                   key=lambda m: m["symbol"])
    if args.shard:
        k, n = args.shard
        items = [m for idx, m in enumerate(items) if idx % n == k - 1]
        print("Slice %d of %d: this run takes every %d-th stock." % (k, n, n))
    if args.limit:
        items = items[:args.limit]
    cur.execute("SELECT isin, period_end, period_type, source_ref FROM stock_balance_sheet WHERE period_end >= %s", (since,))
    stored = {(r["isin"], r["period_end"]): r for r in cur.fetchall()}
    keep = lambda f, ptype, pend: pend >= since and bs_period(ptype, pend)
    print("Reading balance sheets for filings ended on or after %s: %d stock(s)%s.\n"
          % (since, len(items), " (dry run: nothing will be written)" if args.dry_run else ""))
    tally, written, errors, aborted = {}, 0, 0, None
    for i, m in enumerate(items, 1):
        rows = listing(m["bse_scrip_code"])
        if rows is None:
            errors += 1
            tally["BSE_ERROR"] = tally.get("BSE_ERROR", 0) + 1
            print("  BSE_ERROR   %-12s BSE's filing list would not load" % m["symbol"])
            if errors >= MAX_CONSECUTIVE_BSE_ERRORS:
                aborted = "BSE failed %d times in a row -- stopping" % errors
                break
            time.sleep(PAUSE_SEC)
            continue
        errors = 0
        pages = {}
        covered = set()          # dates whose balance sheet is stored (or was already)
        # Oldest first. Within one date the ANNUAL filing goes first: it carries the balance sheet AND the
        # full-year profit. The March quarter's own page then adds nothing and is skipped; it is only a fallback
        # for an annual page that turns out to have no balance sheet. (Otherwise, where the two are different
        # pages, every run would download and rewrite both.)
        for (ptype, pend), opts in sorted(filing_groups(rows, keep).items(), key=lambda kv: (kv[0][1], kv[0][0] != "yearly")):
            if ptype == "quarterly" and pend in covered:
                continue
            status, text = "NO_BALANCE_SHEET", ""
            for name, consol, filed in opts:
                old = stored.get((m["isin"], pend))
                ref = fsf.ref_hash(m["isin"], "bs", pend, name)
                if (not args.force and old and old["source_ref"] == ref
                        and (ptype != "yearly" or old["period_type"] == "yearly")):
                    status = "ALREADY"
                    break
                if name not in pages:
                    pages[name] = fsf.fetch_bse_xbrl_html(name)
                    time.sleep(PAGE_PAUSE_SEC)
                if not pages[name]:
                    status, text = "FETCH_FAILED", "filing page would not download (will retry)"
                    continue
                try:
                    ok = store_balance_sheet(cur, m["isin"], ptype, pend, consol, filed, name, pages[name], args.dry_run)
                except Exception as e:                      # noqa: BLE001  one bad page must not stop the rest
                    conn.rollback()
                    status, text = "ERROR", "%s: %s" % (type(e).__name__, str(e)[:120])
                    break
                if ok:
                    status = "DONE"
                    text = "%s %s balance sheet (%s)" % (pend, ptype, "consolidated" if consol else "standalone")
                    stored[(m["isin"], pend)] = {"source_ref": ref, "period_type": "yearly" if ptype == "yearly"
                                                 else (old["period_type"] if old else "interim")}
                    break
            if status in ("DONE", "ALREADY"):
                covered.add(pend)
            tally[status] = tally.get(status, 0) + 1
            if status == "DONE" and not args.dry_run:
                written += 1
            if status in ("DONE", "FETCH_FAILED", "ERROR") or (args.verbose and status != "ALREADY"):
                print("  %-11s %-12s %s" % (status, m["symbol"], text))
        conn.commit()
        if i % 100 == 0:
            print("  ... %d/%d checked, %d balance sheets written" % (i, len(items), written))
        time.sleep(PAUSE_SEC)
    print("\nSummary:", ", ".join("%s %d" % kv for kv in sorted(tally.items())) or "nothing to check",
          "| written %d" % written)
    if aborted:
        print("PROBLEM:", aborted)
    return written, tally, aborted


def operating_profits(conn, cur, master, args):
    """--operating [DAYS]: fill operating profit and profit before tax for the results the results page shows.
    For every stock whose latest quarterly result was filed within DAYS days (default 30) this reads those two
    figures for that quarter, the quarter before it and the same quarter a year earlier, from the filing
    pages. One filing list per stock; pages only for rows that still lack them; nothing else is rewritten."""
    if not has_operating(cur):
        print("stock_financials has no operating_profit / profit_before_tax columns yet. "
              "Run add_stock_financials_operating.py first.")
        return 0, {}, "operating_profit columns missing"
    if not has_filed_on(cur):
        print("stock_financials has no filed_on column yet. Run add_stock_financials_filed_on.py first.")
        return 0, {}, "filed_on column missing"
    days = args.operating
    wanted = {x.strip().upper() for x in args.symbols.split(",")} if args.symbols else None
    today = args.day or now_ist().date()
    cur.execute("""SELECT isin, max(period_end) AS latest FROM stock_financials
                   WHERE period_type = 'quarterly' AND filed_on >= %s AND (revenue IS NOT NULL OR profit IS NOT NULL)
                   GROUP BY isin""", (today - timedelta(days=days),))
    latest = {r["isin"]: r["latest"] for r in cur.fetchall()}
    items = sorted((m for m in master if m["bse_scrip_code"] and m["isin"] in latest
                    and (wanted is None or m["symbol"] in wanted)), key=lambda m: m["symbol"])
    if args.shard:
        k, n = args.shard
        items = [m for idx, m in enumerate(items) if idx % n == k - 1]
        print("Slice %d of %d: this run takes every %d-th stock." % (k, n, n))
    if args.limit:
        items = items[:args.limit]
    print("Reading operating profit for %d stock(s) with a result filed in the last %d days%s.\n"
          % (len(items), days, " (dry run: nothing will be written)" if args.dry_run else ""))
    tally, written, errors, aborted = {}, 0, 0, None
    for i, m in enumerate(items, 1):
        rows = listing(m["bse_scrip_code"])
        if rows is None:
            errors += 1
            tally["BSE_ERROR"] = tally.get("BSE_ERROR", 0) + 1
            print("  BSE_ERROR   %-12s BSE's filing list would not load" % m["symbol"])
            if errors >= MAX_CONSECUTIVE_BSE_ERRORS:
                aborted = "BSE failed %d times in a row -- stopping" % errors
                break
            time.sleep(PAUSE_SEC)
            continue
        errors = 0
        pend = latest[m["isin"]]
        try:
            results = fill_operating_for(cur, m, rows, [pend, month_end_shift(pend, -3), month_end_shift(pend, -12)], {}, args.dry_run)
            conn.commit()
        except Exception as e:                      # noqa: BLE001  one bad page must not stop the rest
            conn.rollback()
            results = [("ERROR", "%s: %s" % (type(e).__name__, str(e)[:120]))]
        for status, text in results:
            tally[status] = tally.get(status, 0) + 1
            if status == "DONE" and not args.dry_run:
                written += 1
            if status != "ALREADY" or args.verbose:
                print("  %-11s %-12s %s" % (status, m["symbol"], text))
        if i % 100 == 0:
            print("  ... %d/%d checked, %d written" % (i, len(items), written))
        time.sleep(PAUSE_SEC)
    print("\nSummary:", ", ".join("%s %d" % kv for kv in sorted(tally.items())) or "nothing to read",
          "| written %d" % written)
    if aborted:
        print("PROBLEM:", aborted)
    return written, tally, aborted


# ------------------------------------------------------------------- main
def run(conn, args):
    day = args.day or now_ist().date()
    d_from = day - timedelta(days=args.days - 1)
    since_dt = datetime.combine(d_from, datetime.min.time())
    cur = conn.cursor()
    cur.execute("SELECT isin, symbol, bse_scrip_code FROM stock_master WHERE is_active")
    master = cur.fetchall()
    by_scrip = {m["bse_scrip_code"]: m for m in master if m["bse_scrip_code"]}
    by_symbol = {m["symbol"]: m for m in master}
    if args.stamp_dates:
        return stamp_dates(conn, cur, master, args)
    if args.balance_sheet:
        return balance_sheets(conn, cur, master, args)
    if args.operating:
        return operating_profits(conn, cur, master, args)
    upsert = choose_upsert(cur)
    with_bs = has_balance_sheet(cur)
    with_op = has_operating(cur)
    tally = {}
    candidates = {}           # isin -> (master row, why)
    aborted = None

    range_mode = bool(args.backfill or args.backfill_from or args.fix_eps)
    fix_bad = {}              # --fix-eps: isin -> the (type, period end) pairs to re-read
    fix_now = {"bad": frozenset()}
    if range_mode:
        if args.fix_eps:
            # A loss is shown in brackets and some filings leave the minus sign off the tag. Before 9 Oct
            # 2026 the reader dropped some of those EPS lines and took others as positive, so a loss-making
            # period was stored with NO EPS (2,100+ rows) or a POSITIVE one (74 rows). This re-reads exactly
            # those periods with the corrected reader and nothing else.
            cur.execute("""SELECT isin, period_type, period_end FROM stock_financials
                           WHERE period_end >= %s AND profit < 0 AND (eps IS NULL OR eps > 0)""", (args.fix_eps,))
            for r in cur.fetchall():
                fix_bad.setdefault(r["isin"], set()).add((r["period_type"], r["period_end"]))
            print("Re-reading %d loss-making period(s) of %d stock(s) stored with no EPS or a positive one."
                  % (sum(len(v) for v in fix_bad.values()), len(fix_bad)))
            keep = lambda f, ptype, pend: (ptype, pend) in fix_now["bad"]
        elif args.backfill_from:
            lo = args.backfill_from
            print("Backfill of every quarter and year ended on or after %s, for every active stock with a BSE code." % lo)
            keep = lambda f, ptype, pend: pend >= lo
        else:
            target = args.backfill
            print("Backfill of the quarter/year ended %s for every active stock with a BSE code." % target)
            keep = lambda f, ptype, pend: pend == target
        wanted = {x.strip().upper() for x in args.symbols.split(",")} if args.symbols else None
        for m in master:
            if (m["bse_scrip_code"] and (wanted is None or m["symbol"] in wanted)
                    and (not args.fix_eps or m["isin"] in fix_bad)):
                candidates[m["isin"]] = (m, "fix eps" if args.fix_eps else "backfill")
    else:
        keep = lambda f, ptype, pend: (to_dt(f.get("Fld_CreateDate")) or datetime.min) >= since_dt
        if args.symbols:
            for s in [x.strip().upper() for x in args.symbols.split(",")]:
                if s in by_symbol and by_symbol[s]["bse_scrip_code"]:
                    candidates[by_symbol[s]["isin"]] = (by_symbol[s], "asked for")
                else:
                    print("  %s: not in our active list or has no BSE scrip code" % s)
        else:
            anns, ok = announcements(d_from, day)
            if not ok:
                aborted = "BSE announcements feed failed"
            ours = outside = 0
            for scrip, headline, when in anns:
                m = by_scrip.get(scrip)
                if m:
                    ours += 1
                    candidates.setdefault(m["isin"], (m, "filed %s" % (when.strftime("%d %b %H:%M") if when else "?")))
                else:
                    outside += 1
            due_scrips, due_symbols = calendar_due(d_from, day)
            for scrip in due_scrips:
                if scrip in by_scrip:
                    candidates.setdefault(by_scrip[scrip]["isin"], (by_scrip[scrip], "due on calendar"))
            for sym in due_symbols:
                if sym in by_symbol and by_symbol[sym]["bse_scrip_code"]:
                    candidates.setdefault(by_symbol[sym]["isin"], (by_symbol[sym], "due on calendar"))
            print("Window %s to %s (IST). Result announcements on BSE: %d | in our stock list: %d | not in our list: %d | "
                  "also due per calendars: %d" % (d_from, day, len(anns), ours, outside,
                                                  len({s for s in due_scrips if s in by_scrip} | {s for s in due_symbols if s in by_symbol})))
            if not args.dry_run:
                refresh_results_calendar(conn, cur, master, day)

    stored = load_stored(cur, candidates.keys()) if candidates else {}
    if args.backfill and not args.backfill_from:
        before = len(candidates)
        candidates = {i: c for i, c in candidates.items()
                      if args.force or not any(has_numbers(r) for (pt, pe), r in stored.get(i, {}).items() if pe == args.backfill)}
        print("%d of %d stocks have no figures for that period yet." % (len(candidates), before))
    items = sorted(candidates.values(), key=lambda c: c[0]["symbol"])
    if args.shard:
        k, n = args.shard
        items = [it for idx, it in enumerate(items) if idx % n == k - 1]
        print("Slice %d of %d: this run takes every %d-th stock." % (k, n, n))
    if args.limit:
        items = items[:args.limit]
    print("%d stock(s) to check%s.\n" % (len(items), " (dry run: nothing will be written)" if args.dry_run else ""))

    written = errors = 0
    for i, (m, why) in enumerate(items, 1):
        rows = listing(m["bse_scrip_code"])
        if rows is None:
            errors += 1
            tally["BSE_ERROR"] = tally.get("BSE_ERROR", 0) + 1
            print("  BSE_ERROR   %-12s BSE's filing list would not load" % m["symbol"])
            if errors >= MAX_CONSECUTIVE_BSE_ERRORS:
                aborted = "BSE failed %d times in a row -- stopping" % errors
                break
            time.sleep(PAUSE_SEC)
            continue
        errors = 0
        try:
            fix_now["bad"] = frozenset(fix_bad.get(m["isin"], ()))
            results = handle_stock(cur, m, rows, keep, stored.get(m["isin"], {}), args.dry_run, upsert,
                                   force=fix_now["bad"], bs=with_bs, op=with_op)
            conn.commit()
        except Exception as e:           # one bad filing must not stop the rest
            conn.rollback()
            results = [("ERROR", "%s: %s" % (type(e).__name__, str(e)[:150]))]
        for status, text in results:
            if status == "WAITING" and range_mode:
                text = "BSE lists no readable filing for that period (not filed, half-yearly filer, or a format we cannot read)"
            tally[status] = tally.get(status, 0) + 1
            if status == "DONE" and not args.dry_run:
                written += 1
            if status != "ALREADY" or args.verbose:
                print("  %-11s %-12s %s   [%s]" % (status, m["symbol"], text, why))
        if range_mode and i % 100 == 0:
            print("  ... %d/%d checked, %d written" % (i, len(items), written))
        time.sleep(PAUSE_SEC)

    print("\nSummary:", ", ".join("%s %d" % kv for kv in sorted(tally.items())) or "nothing to check",
          "| written %d" % written)
    if aborted:
        print("PROBLEM:", aborted)
    return written, tally, aborted


def parse_shard(s):
    try:
        k, n = (int(x) for x in s.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError("use K/N, for example 2/3")
    if not (1 <= k <= n <= 8):
        raise argparse.ArgumentTypeError("K must be between 1 and N, and N at most 8")
    return k, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), help="treat this IST date as today")
    ap.add_argument("--days", type=int, default=2, help="how many days back to look (today counts as 1)")
    ap.add_argument("--symbols", help="comma-separated NSE symbols to check regardless of announcements")
    ap.add_argument("--backfill", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    help="period end date (e.g. 2026-06-30): fill that period for every stock still blank")
    ap.add_argument("--backfill-from", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    help="period end date (e.g. 2025-03-31): fill EVERY quarter and year ended on or after it, "
                         "in one pass (one filing list per stock, not one per quarter)")
    ap.add_argument("--stamp-dates", nargs="?", const=date(2025, 3, 31),
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    help="fill in the filing date (filed_on) for results already stored, from BSE's upload dates; "
                         "optional start date, default 2025-03-31. No pages are downloaded.")
    ap.add_argument("--balance-sheet", nargs="?", const=date(2024, 3, 31),
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    help="read the balance sheets (for P/B, return on equity, debt to equity) of the annual, March and "
                         "September filings since a date, default 2024-03-31. Needs create_stock_balance_sheet.py run "
                         "once first. Results are not touched.")
    ap.add_argument("--operating", nargs="?", const=30, type=int, metavar="DAYS",
                    help="read operating profit and profit before tax for the results page: for every stock whose latest "
                         "quarterly result was filed in the last DAYS days (default 30), that quarter, the one before it "
                         "and the same quarter a year earlier. Needs add_stock_financials_operating.py run once first. "
                         "Nothing else in stock_financials is touched.")
    ap.add_argument("--shard", type=parse_shard, help="K/N, e.g. 2/3: with a backfill, take only every N-th stock "
                    "starting at the K-th, so N runs can share the work")
    ap.add_argument("--fix-eps", nargs="?", const=date(2025, 3, 31), type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    help="re-read loss-making periods (optionally from a date, default 2025-03-31) that were stored "
                         "with no EPS or a positive EPS, using the corrected reader; touches nothing else")
    ap.add_argument("--force", action="store_true", help="with --backfill: also re-read stocks that already have figures")
    ap.add_argument("--limit", type=int, help="check at most this many stocks")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="also list stocks already stored")
    args = ap.parse_args()
    range_mode = bool(args.backfill or args.backfill_from or args.stamp_dates or args.fix_eps or args.balance_sheet
                      or args.operating)
    if args.shard and not range_mode:
        ap.error("--shard only makes sense with --backfill, --backfill-from, --stamp-dates, --fix-eps, --balance-sheet or --operating")

    # Lines appear in a redirected log as they happen, not when a buffer fills (a backfill
    # run with nohup would otherwise look empty for a long time).
    sys.stdout.reconfigure(line_buffering=True)
    started = datetime.now(timezone.utc)
    with psycopg.connect(DB, row_factory=dict_row) as conn:
        cur = conn.cursor()
        # A long backfill must not block the live half-hourly run, so each mode has its own lock.
        # A dry run writes nothing, so it takes no lock and can run any time.
        # Each slice of a sliced backfill has its own lock too, so the slices run side by side.
        if not args.dry_run:
            slice_no = args.shard[0] - 1 if args.shard else 0
            key = (LOCK_KEY + 10 + slice_no if args.stamp_dates         # filling in dates has its own locks
                   else LOCK_KEY + 20 + slice_no if args.fix_eps        # and so does the EPS repair
                   else LOCK_KEY + 30 + slice_no if args.balance_sheet  # and the balance-sheet read
                   else LOCK_KEY + 40 + slice_no if args.operating      # and the operating-profit read
                   else LOCK_KEY + 1 + slice_no if range_mode else LOCK_KEY)
            cur.execute("SELECT pg_try_advisory_lock(%s) AS got", (key,))
            if not cur.fetchone()["got"]:
                print("Another %s run is still going; skipping this one." % ("backfill" if range_mode else "live"))
                return 0
            conn.commit()
        written, tally, aborted = run(conn, args)
        # Log only when something happened: a run every 30 minutes would bury the log.
        if not args.dry_run and (written or aborted or tally.get("ERROR") or range_mode):
            status = "FAILED" if aborted else ("PARTIAL" if tally.get("ERROR") else "SUCCESS")
            cur.execute("""INSERT INTO batch_run_log (run_type, business_date, status, started_at, finished_at,
                                                      rows_read, rows_written, error_message)
                           VALUES (%s, CURRENT_DATE, %s, %s, now(), %s, %s, %s)""",
                        (RUN_TYPE, status, started, sum(tally.values()), written,
                         aborted or ("; ".join("%s %d" % kv for kv in sorted(tally.items())))[:500]))
            conn.commit()
    return 1 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
