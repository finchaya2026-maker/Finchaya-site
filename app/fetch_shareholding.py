"""
fetch_shareholding.py -- who owns each listed company, quarter by quarter.
------------------------------------------------------------------------
WHAT IT DOES
    1. LIST (the default). Asks NSE's shareholding-pattern list for every filing made
       in a window of days, all companies, in ONE request (about 3 MB for three
       months). For each filing of a stock in stock_master it stores the promoter,
       public and employee-trust percentages, the filing date, whether it was revised
       and the address of the filing's XBRL file, in stock_shareholding.
    2. DETAIL (--detail). Downloads the XBRL file of filings whose breakdown has not
       been read yet (newest quarters first) and stores who the public holders are:
       mutual funds, insurers, banks, other domestic institutions, foreign investors,
       resident individuals, bodies corporate, NRIs, the rest, and the share of the
       promoters' own shares that is pledged. One request per company-quarter, so it
       is capped by --detail-limit and run a few hundred at a time.

WHY THE LIST AND THE FILES ARE SEPARATE
    The list is one cheap request for the whole market and already carries the three
    headline numbers. The breakdown exists only inside each filing's XBRL file
    (0.1 - 1.3 MB each). A company-quarter is useful the moment the list has it.

WHAT WAS LEARNT FROM THE REAL DATA (9 Oct 2026; list windows of 3, 2 and 12 months;
40 XBRL files from 3 format generations, 33 companies)
    * The window filters on the day the filing was FIRST SUBMITTED. A revised filing
      keeps that day (TCS's March 2026 filing was revised on 17 Sep 2026 but still
      carries 21 Apr 2026), so a revision is only seen by a window that reaches back to
      the original submission. A run with --days 7 does not see revisions of filings
      older than a week; the nightly run uses --days 180 (one request, about 5 MB).
    * The list's isin cannot be trusted to match stock_master: 661 of 2,850 rows had a
      blank isin or an OLD one (ABB is INE117A01014 there, INE117A01022 here; ACC
      is a temporary IN9012A01015) -- a split changes the isin. The SYMBOL is used to
      find the stock and the isin only as a fallback (4 stocks are kept as SYMBOL~ISIN
      in stock_master). Across 14,000 rows the two never pointed at different stocks.
      Matching on isin alone would have lost a quarter of the market.
    * Some rows are not quarters at all: filings "as on" a date such as 1 Oct 2026
      (around a share allotment or a listing). 205 of 2,850. They are skipped and
      counted, so "latest quarter" always means a real quarter end.
    * 313 of our active stocks listed on NSE in 2026 (BSE to NSE moves). NSE has no
      filing history for them yet; their first NSE filings are the Sep 2026 ones.
      Only 3 other active stocks (ABBOTINDIA, BAYERCROP, MCX) have no NSE filing at all.
    * One company-quarter can appear twice (a re-filing, or the original beside its
      revision). The latest by broadcast time wins and the row is marked revised.
    * Promoter + public + employee trusts is 100 (+/- rounding) on every row seen.
      No blanks, no sums over 100. Rows that do not satisfy it are skipped anyway.
    * Three XBRL generations, told apart by the file itself. The oldest seen (Dec 2021):
      percentages, institutions not split domestic/foreign, no bodies-corporate or NRI
      lines (they sit in "others"). 2024 to Mar 2025: percentages, with the
      domestic/foreign split. Mid-2026 on (taxonomy dated 2025-10-31): FRACTIONS (0.7177
      means 71.77 %). The scale is read from the file's own 100 % total, never assumed
      from the date. Category names are matched on the member name of the category axis
      (case-insensitive, known spelling slips such as Rbi/RBI, Uti/UTI, Catergory
      included), never on context ids, because context ids differ between generations.
    * Pledge: stated per category as a share of that category's own shares. The row
      for "promoter and promoter group" is the one used. Old files give "pledged or
      encumbered %", new files give pledged, non-disposal and other encumbrances and a
      total. The total is stored. TCS's 2024 filings, for example, state 0.28 % of the
      promoter group's shares as encumbered.

WHAT IS REFUSED
    A downloaded file is only used when its category figures add up to 100 +/- 1.5,
    its promoter figure is within 0.2 of the list's, its date agrees with the row and
    so does its ISIN or its symbol, and it is a plain XML document (no DTD) of a
    sensible size from NSE's archive host.
    A refused file leaves detail_loaded false, writes the reason to detail_error and is
    left alone on later runs (--retry-failed tries again). A revision resets it.

USAGE
    python fetch_shareholding.py                              # filings of the last 7 days
    python fetch_shareholding.py --dry-run                    # read and parse, write nothing
    python fetch_shareholding.py --days 180 --detail --detail-limit 400    # what the nightly run does
    python fetch_shareholding.py --from 2026-07-01 --to 2026-10-09
    python fetch_shareholding.py --quarter 2026-06-30         # one quarter's filings
    python fetch_shareholding.py --history-quarters 12        # first load: one request per quarter
    python fetch_shareholding.py --symbols TCS,SBIN --company-history   # last 20 quarters of two companies
    python fetch_shareholding.py --detail-only --detail-since 2025-06-30 --detail-limit 0   # read all pending files (0 = no limit)
    python fetch_shareholding.py --detail-only --retry-failed
CRON (droplet; once a night after the market closes)
    30 23 * * * cd /opt/mfapi && venv/bin/python3 fetch_shareholding.py --days 180 --detail --detail-limit 400 >> logs/fetch_shareholding.log 2>&1

SAFE TO RE-RUN. Upserts on (isin, quarter_end); rows that did not change are not
touched; an older filing never replaces a newer one. A Postgres advisory lock stops
two runs overlapping; --dry-run takes no lock and its connection is read-only.
Needs create_stock_shareholding_table.py run once (--dry-run works without it).
"""

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

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

IST = timezone(timedelta(hours=5, minutes=30))
LIST_URL = "https://www.nseindia.com/api/corporate-share-holdings-master"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern"
FILE_HOST = "nsearchives.nseindia.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

PAUSE_SEC = 0.5                    # between any two requests to NSE (never less than 0.4)
RETRY_PAUSE_SEC = 2.0
MAX_CONSECUTIVE_FAILURES = 8
LIST_TIMEOUT = 90
FILE_TIMEOUT = 60
MAX_FILE_BYTES = 8 * 1024 * 1024   # the largest real file seen was 1.5 MB
MAX_LIST_BYTES = 128 * 1024 * 1024 # a year of filings is 8 MB
WINDOW_SPAN_DAYS = 92              # one history window: the quarter after a quarter end
LONGEST_WINDOW_DAYS = 366          # a longer --days/--from..--to range is asked for in pieces (a year worked in one go)
DEFAULT_DAYS = 7
DEFAULT_DETAIL_LIMIT = 300
LOCK_KEY = 727501
RUN_TYPE = "FETCH_SHAREHOLDING"

HUNDRED = Decimal(100)
FOUR = Decimal("0.0001")
SUM_TOLERANCE = Decimal("1.5")       # parts must add up to 100 +/- this
PROMOTER_TOLERANCE = Decimal("0.2")  # file vs list
ROUNDING_SLACK = Decimal("0.3")      # sub-totals are rounded one by one

QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))
MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}

nse = requests.Session()
nse.headers.update({
    "User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": REFERER,
})


# ------------------------------------------------------------------ small helpers
def now_ist():
    return datetime.now(IST).replace(tzinfo=None)


def parse_nse_date(s):
    """'30-SEP-2026' -> date, or None. Does not depend on the machine's locale."""
    m = re.fullmatch(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s*", s or "")
    if not m or m.group(2).upper() not in MONTHS:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(2).upper()], int(m.group(1)))
    except ValueError:
        return None


def parse_nse_datetime(s):
    """'09-OCT-2026 00:51:14' -> datetime; a bare date gives midnight; anything else None."""
    m = re.fullmatch(r"\s*(\d{1,2}-[A-Za-z]{3}-\d{4})(?:\s+(\d{1,2}):(\d{2}):(\d{2}))?\s*", s or "")
    if not m:
        return None
    d = parse_nse_date(m.group(1))
    if d is None:
        return None
    try:
        return datetime(d.year, d.month, d.day, int(m.group(2) or 0), int(m.group(3) or 0), int(m.group(4) or 0))
    except ValueError:
        return None


def is_quarter_end(d):
    return (d.month, d.day) in QUARTER_ENDS


def quarter_ends_back(today, n):
    """The n most recent calendar quarter ends on or before `today`, newest first."""
    out, year = [], today.year
    while len(out) < n:
        for month, day in reversed(QUARTER_ENDS):
            q = date(year, month, day)
            if q <= today:
                out.append(q)
                if len(out) >= n:
                    break
        year -= 1
    return out


def list_pct(v):
    """Decimal 0-100 (4 places) from a field of the list, or None if blank, not a number or out of range."""
    s = str(v).strip().replace(",", "") if v is not None else ""
    if s in ("", "-"):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 0 or d > HUNDRED:
        return None
    return d.quantize(FOUR)


def file_seq(url):
    """The filing's running number inside NSE's file name (SHP_<n>_<yyyymmddhhmmss>_WEB.xml; the older
    names carry one more number in front), or None. A bigger number is a later filing."""
    m = re.search(r"SHP_(?:\d+_)?(\d+)_(\d{14})_WEB\.xml$", url or "")
    return int(m.group(1)) if m else None


def clean_file_url(url):
    """The XBRL address if it is a plain .xml file on NSE's archive host, else None (blank, '-', anything else)."""
    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return None
    if u.scheme != "https" or (u.hostname or "").lower() != FILE_HOST:
        return None
    if not u.path.lower().endswith(".xml") or u.path.endswith("/-"):
        return None
    return u.geturl()


class Counter(dict):
    """A dict of counts: a key never seen reads as 0."""

    def __missing__(self, key):
        return 0


def new_stats():
    return Counter(_not_ours_ids=set())


def new_result():
    return dict(rows_read=0, written=0, detail_written=0, marked=0, errors=0, aborted=None,
                stats=new_stats(), tally=Counter(), refusals=Counter())


# ------------------------------------------------------------------------ HTTP
class Tripwire:
    """Counts failures in a row; trips at the limit. A success starts the count again."""

    def __init__(self, limit=MAX_CONSECUTIVE_FAILURES):
        self.limit, self.n = limit, 0

    def ok(self):
        self.n = 0

    def fail(self):
        self.n += 1

    @property
    def tripped(self):
        return self.n >= self.limit


def http_get(url, params=None, timeout=60, max_bytes=None):
    """(status, body, problem). problem is None only for a complete 200 reply. One retry after a pause
    for a timeout, a dropped connection or a 5xx/429 reply; never for any other error code."""
    status, problem = None, "no attempt"
    for attempt in (1, 2):
        try:
            r = nse.get(url, params=params, timeout=timeout, stream=True)
            status = r.status_code
            if status != 200:
                problem = "HTTP %s" % status
                r.close()
            else:
                chunks, size = [], 0
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if max_bytes and size > max_bytes:
                        r.close()
                        return status, None, "reply larger than %d bytes" % max_bytes
                    chunks.append(chunk)
                return status, b"".join(chunks), None
        except requests.RequestException as e:
            status, problem = None, "%s" % type(e).__name__
        if attempt == 1 and (status is None or status >= 500 or status == 429):
            time.sleep(RETRY_PAUSE_SEC)
            continue
        break
    return status, None, problem


def fetch_list(d_from=None, d_to=None, symbol=None):
    """(rows, problem). The filings NSE lists for a window of submission days (all companies),
    or for one symbol (that company's last 20 quarters). rows is None when the call failed."""
    params = {"index": "equities"}
    if symbol:
        params["symbol"] = symbol
    else:
        params["from_date"] = d_from.strftime("%d-%m-%Y")
        params["to_date"] = d_to.strftime("%d-%m-%Y")
    status, body, problem = http_get(LIST_URL, params, LIST_TIMEOUT, MAX_LIST_BYTES)
    if problem:
        return None, problem
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None, "reply is not JSON"
    if not isinstance(data, list):
        return None, "reply is not a list"
    return [x for x in data if isinstance(x, dict)], None


def fetch_file(url):
    """(status, body, problem) for one XBRL file. Only NSE's archive host is ever contacted."""
    if clean_file_url(url) is None:
        return None, None, "address is not an NSE archive .xml file"
    return http_get(url, None, FILE_TIMEOUT, MAX_FILE_BYTES)


# --------------------------------------------------------- the list: rows -> filings
def normalise_row(raw, by_isin, by_symbol, stats, wanted_symbols=None, wanted_quarter=None):
    """One row of NSE's list -> a filing dict, or None (the reason is counted in `stats`).
    by_isin {isin: symbol} and by_symbol {symbol: isin} describe our active stocks."""
    list_isin = (raw.get("isin") or "").strip().upper()
    symbol = (raw.get("symbol") or "").strip().upper()
    # The SYMBOL identifies the stock; the list's isin is only a fallback. In the list of 9 Oct 2026, 661 of
    # 2,850 rows carried a blank isin (82) or an old/temporary one (579: ABB INE117A01014 for our
    # INE117A01022, ACC IN9012A01015 for INE012A01025), and none of them disagreed with the symbol.
    by_sym = by_symbol.get(symbol)
    by_id = list_isin if list_isin in by_isin else None
    if by_sym and by_id and by_sym != by_id:
        stats["id_conflict"] += 1                 # symbol and isin point at different stocks: do not guess
        return None
    isin = by_sym or by_id
    if not isin:
        stats["not_ours"] += 1
        stats["_not_ours_ids"].add(list_isin or symbol)
        return None
    if list_isin != isin:
        stats["matched_by_symbol"] += 1
    if wanted_symbols and symbol not in wanted_symbols and by_isin[isin] not in wanted_symbols:
        stats["other_symbols"] += 1
        return None
    quarter_end = parse_nse_date(raw.get("date"))
    if quarter_end is None:
        stats["bad_date"] += 1
        return None
    if not is_quarter_end(quarter_end):
        stats["off_cycle"] += 1                   # "as on" a date that is not a quarter end
        return None
    if wanted_quarter and quarter_end != wanted_quarter:
        stats["other_quarter"] += 1
        return None
    promoter, public = list_pct(raw.get("pr_and_prgrp")), list_pct(raw.get("public_val"))
    trusts_raw = raw.get("employeeTrusts")
    blank = trusts_raw is None or str(trusts_raw).strip() in ("", "-")
    trusts = Decimal("0.0000") if blank else list_pct(trusts_raw)
    if promoter is None or public is None or trusts is None:
        stats["bad_values"] += 1                  # blank, not a number, negative or over 100
        return None
    if abs(promoter + public - HUNDRED) > SUM_TOLERANCE and abs(promoter + public + trusts - HUNDRED) > SUM_TOLERANCE:
        stats["sum_off"] += 1                     # the parts do not make a whole
        return None
    revised = "revised" in ((raw.get("revisedStatus") or "") + " " + (raw.get("revisedData") or "")).lower()
    url = clean_file_url(raw.get("xbrl"))
    return dict(
        isin=isin, symbol=by_isin[isin], quarter_end=quarter_end,
        promoter_pct=promoter, public_pct=public, employee_trust_pct=trusts,
        submitted_on=parse_nse_date(raw.get("submissionDate")), revised=revised, xbrl_url=url,
        broadcast=parse_nse_datetime(raw.get("broadcastDate")) or datetime.min, seq=file_seq(url))


def merge_filings(filings, stats=None):
    """{(isin, quarter_end): filing}. Several filings for one company-quarter (a re-filing, or the
    original beside its revision) become one: the latest by broadcast time, then by NSE's running
    number. The result is marked revised when any of them was, or when there was more than one."""
    groups = {}
    for f in filings:
        group = groups.setdefault((f["isin"], f["quarter_end"]), {})
        group[f["xbrl_url"] or id(f)] = f              # the same file listed twice is one filing
    out = {}
    for key, by_url in groups.items():
        fs = list(by_url.values())
        best = dict(max(fs, key=lambda f: (f["broadcast"], f["seq"] or 0, f["submitted_on"] or date.min)))
        best["revised"] = any(f["revised"] for f in fs) or len(fs) > 1
        if len(fs) > 1 and stats is not None:
            stats["merged"] += len(fs) - 1
        out[key] = best
    return out


def plan_changes(existing, chosen):
    """Compare what we hold with what the list says -> dict(insert, update, same, older).
    `existing` is {(isin, quarter_end): row}. A filing numbered lower than the stored one never
    replaces it (a narrow window can show only the first of two filings)."""
    insert, update, same, older = [], [], 0, 0
    for key, f in sorted(chosen.items(), key=lambda kv: (kv[0][1], kv[0][0]), reverse=True):
        old = existing.get(key)
        if old is None:
            insert.append(f)
            continue
        new_seq, old_seq = file_seq(f["xbrl_url"]), file_seq(old["xbrl_url"])
        if f["xbrl_url"] and old["xbrl_url"] and f["xbrl_url"] != old["xbrl_url"] \
                and new_seq is not None and old_seq is not None and new_seq < old_seq:
            older += 1
            continue
        unchanged = (f["promoter_pct"] == old["promoter_pct"] and f["public_pct"] == old["public_pct"]
                     and f["employee_trust_pct"] == old["employee_trust_pct"]
                     and (f["xbrl_url"] is None or f["xbrl_url"] == old["xbrl_url"])
                     and (f["submitted_on"] is None or f["submitted_on"] == old["submitted_on"])
                     and (old["revised"] or not f["revised"]))
        if unchanged:
            same += 1
        else:
            update.append(f)
    return dict(insert=insert, update=update, same=same, older=older)


# A different file than the one stored means a revised filing: the breakdown read from the old
# file no longer belongs to it, so it is cleared and the file goes back in the queue.
_SAME_FILE = "(EXCLUDED.xbrl_url IS NULL OR EXCLUDED.xbrl_url IS NOT DISTINCT FROM stock_shareholding.xbrl_url)"
DETAIL_COLUMNS = ("mf_pct", "insurance_pct", "bank_pct", "other_dii_pct", "fii_pct", "retail_pct",
                  "bodies_corporate_pct", "nri_pct", "other_public_pct", "promoter_pledged_pct")

UPSERT = """
INSERT INTO stock_shareholding
    (isin, quarter_end, promoter_pct, public_pct, employee_trust_pct, submitted_on, revised,
     xbrl_url, source, updated_at)
VALUES
    (%(isin)s, %(quarter_end)s, %(promoter_pct)s, %(public_pct)s, %(employee_trust_pct)s,
     %(submitted_on)s, %(revised)s, %(xbrl_url)s, 'NSE', now())
ON CONFLICT (isin, quarter_end) DO UPDATE SET
    promoter_pct = EXCLUDED.promoter_pct,
    public_pct = EXCLUDED.public_pct,
    employee_trust_pct = EXCLUDED.employee_trust_pct,
    submitted_on = COALESCE(EXCLUDED.submitted_on, stock_shareholding.submitted_on),
    revised = EXCLUDED.revised OR stock_shareholding.revised,
    xbrl_url = COALESCE(EXCLUDED.xbrl_url, stock_shareholding.xbrl_url),
""" + "".join("    %s = CASE WHEN %s THEN stock_shareholding.%s END,\n" % (c, _SAME_FILE, c) for c in DETAIL_COLUMNS) + """
    detail_loaded = stock_shareholding.detail_loaded AND %s,
    detail_error = CASE WHEN %s THEN stock_shareholding.detail_error END,
    updated_at = now()
""" % (_SAME_FILE, _SAME_FILE)

DETAIL_UPDATE = """
UPDATE stock_shareholding SET
""" + "".join("    %s = %%(%s)s,\n" % (c, c) for c in DETAIL_COLUMNS) + """
    detail_loaded = true, detail_error = NULL, updated_at = now()
WHERE isin = %(isin)s AND quarter_end = %(quarter_end)s AND xbrl_url IS NOT DISTINCT FROM %(xbrl_url)s
"""

DETAIL_REFUSED = """
UPDATE stock_shareholding SET detail_error = %(reason)s
WHERE isin = %(isin)s AND quarter_end = %(quarter_end)s AND xbrl_url IS NOT DISTINCT FROM %(xbrl_url)s
"""


def table_exists(cur):
    cur.execute("SELECT to_regclass('stock_shareholding') IS NOT NULL AS ok")
    return cur.fetchone()["ok"]


def load_existing(cur, isins, oldest):
    cur.execute("""SELECT isin, quarter_end, promoter_pct, public_pct, employee_trust_pct, submitted_on,
                          revised, xbrl_url, detail_loaded
                   FROM stock_shareholding WHERE isin = ANY(%s) AND quarter_end >= %s""", (list(isins), oldest))
    return {(r["isin"], r["quarter_end"]): r for r in cur.fetchall()}


# ------------------------------------------------------- the XBRL file: category figures
PCT_TAG = "ShareholdingAsAPercentageOfTotalNumberOfShares"
PLEDGE_TOTAL_TAGS = ("EncumberedSharesHeldAsPercentageOfTotalNumberOfShares",
                     "PledgedOrEncumberedSharesHeldAsPercentageOfTotalNumberOfShares")
PLEDGE_PART_TAGS = ("EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares",
                    "EncumberedShareUnderNonDisposalUndertakingAsPercentageOfTotalNumberOfShares",
                    "EncumberedShareUnderOtherEncumbrancesAsPercentageOfTotalNumberOfShares")
PLEDGE_COUNT_TAGS = ("NumberOfSharesEncumbered", "PledgedOrEncumberedNumberOfShares")
COUNT_TAG = "NumberOfShares"
WANTED_TAGS = frozenset((PCT_TAG, COUNT_TAG) + PLEDGE_TOTAL_TAGS + PLEDGE_PART_TAGS + PLEDGE_COUNT_TAGS)

# Category-axis member names, lower case, without the trailing "member". Spelling slips that exist in
# real filings (Rbi/RBI, Uti/UTI, Catergory, Goverments) are covered by lower-casing and the lists.
M_PROMOTER = "shareholdingofpromoterandpromotergroup"
M_PUBLIC = "publicshareholding"
M_TOTAL = "shareholdingpattern"
M_EMPLOYEE = ("employeebenefitstrusts", "employeetrusts")
M_MF = "mutualfundsoruti"
M_INSURANCE = "insurancecompanies"
M_BANK = ("banks", "financialinstitutionorbanks")
M_DOMESTIC_TOTAL = "institutionsdomestic"
M_DOMESTIC_PARTS = ("venturecapitalfunds", "alternativeinvestmentfunds", "providentfundsorpensionfunds",
                    "assetreconstructioncompanies", "sovereignwealthfundsdomestic", "nbfcsregisteredwithrbi",
                    "otherfinancialinstitutions", "otherinstitutionsdomestic", "otherinstitutions")
M_FOREIGN_TOTAL = "institutionsforeign"
M_FOREIGN_PARTS = ("foreigndirectinvestment", "foreignventurecapitalinvestors", "sovereignwealthfundsforeign",
                   "overseasdepositories", "otherinstitutionsforeign")
FPI_PREFIX = "institutionsforeignportfolioinvestor"     # category one, category two, and the old single line
M_BODIES = "bodiescorporate"
M_NRI = ("nonresidentindians", "foreignnationals")
RETAIL_MARK = "nominalsharecapital"                     # "...holding nominal share capital up to / in excess of Rs 2 lakh"
NONPROMOTER_NONPUBLIC = re.compile(r"nonpromoter.*nonpublic")
CUSTODIAN = re.compile(r"custodian|drholder")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _decimal(text):
    try:
        d = Decimal((text or "").strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _iso_date(text):
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def _q4(d):
    return d.quantize(FOUR)


def read_instance(raw):
    """-> (facts, scalars, flags, instants, problem). facts is {(category, tag): [(Decimal, instant)]} for
    the facts that sit on a context whose ONLY dimension is the shareholder-category axis."""
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        return None, None, None, None, "file declares a DTD/entity (not a plain XBRL instance)"
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return None, None, None, None, "not valid XML (%s)" % str(e)[:60]
    if _local(root.tag) != "xbrl":
        return None, None, None, None, "not an XBRL instance"
    contexts = {}
    for el in root:
        if _local(el.tag) != "context":
            continue
        members, typed, instant = [], False, None
        for sub in el.iter():
            name = _local(sub.tag)
            if name == "explicitMember":
                members.append(((sub.get("dimension") or "").rsplit(":", 1)[-1], (sub.text or "").strip().rsplit(":", 1)[-1]))
            elif name == "typedMember":
                typed = True
            elif name == "instant":
                instant = _iso_date(sub.text)
        category = None
        if not typed and len(members) == 1 and members[0][0] == "CategoryOfShareholdersAxis":
            category = re.sub(r"member$", "", members[0][1].lower())
        contexts[el.get("id")] = (category, instant)
    facts, scalars, flags, instants = {}, {}, {}, {}
    for el in root:
        name = _local(el.tag)
        if name in ("context", "unit", "schemaRef"):
            continue
        text = (el.text or "").strip()
        if name == "ISIN":
            scalars.setdefault("isin", text.upper())
        elif name == "Symbol":
            scalars.setdefault("symbol", text.upper())
        elif name == "DateOfReport":
            scalars.setdefault("date", _iso_date(text))
        elif name.startswith("WhetherAnySharesHeldByPromoters"):
            flags[name] = text.lower()
        elif name in WANTED_TAGS:
            category, instant = contexts.get(el.get("contextRef"), (None, None))
            value = _decimal(text)
            if category is not None and value is not None:
                facts.setdefault((category, name), []).append((value, instant))
                if instant is not None:
                    instants[instant] = instants.get(instant, 0) + 1
    return facts, scalars, flags, instants, None


def parse_shp_xml(raw, quarter_end=None, isin=None, expected_promoter=None, symbol=None):
    """Read one shareholding-pattern XBRL file -> (figures, None) or (None, why_refused).
    figures holds promoter_pct, public_pct, employee_trust_pct and the detail columns, all 0-100.
    A category the file does not mention counts as 0; a category this code does not know falls into
    other_public_pct, so the parts still add up. The file is refused when:
      * it is not a plain XBRL document, or has no category figures at all;
      * its date is not `quarter_end`, or it names neither our ISIN nor our symbol (an ISIN that changed
        after a split is common, so ONE of the two agreeing is enough);
      * a scale cannot be told, or the figures conflict with themselves;
      * the listed public categories exceed the public total, or all parts do not add up to 100 +/- 1.5;
      * the promoter figure differs from `expected_promoter` (the list's) by more than 0.2."""
    facts, scalars, flags, instants, problem = read_instance(raw)
    if problem:
        return None, problem
    if not facts:
        return None, "no shareholder-category figures in the file"
    if (isin or symbol) and (scalars.get("isin") or scalars.get("symbol")):
        same_isin = bool(isin) and scalars.get("isin") == isin.upper()
        same_symbol = bool(symbol) and scalars.get("symbol") == symbol.upper()
        if not same_isin and not same_symbol:
            return None, "file is for %s / %s, expected %s / %s" % (
                scalars.get("isin"), scalars.get("symbol"), isin, symbol)
    file_date = scalars.get("date") or (max(instants.items(), key=lambda kv: kv[1])[0] if instants else None)
    if quarter_end and file_date and file_date != quarter_end:
        return None, "file is dated %s, expected %s" % (file_date, quarter_end)

    conflicts = []

    def raw_value(category, tag):
        found = facts.get((category, tag))
        if not found:
            return None
        found = [x for x in found if x[1] == quarter_end] or found      # prefer the figure dated at the quarter end
        if len({v for v, _ in found}) > 1:
            conflicts.append("%s/%s" % (category, tag))
        return found[0][0]

    # The scale: this category's value in the file's own 100 % total.
    total_raw = raw_value(M_TOTAL, PCT_TAG)
    promoter_raw, public_raw = raw_value(M_PROMOTER, PCT_TAG), raw_value(M_PUBLIC, PCT_TAG)
    if total_raw is not None:
        probe = total_raw
    elif public_raw is not None:
        probe = public_raw + (promoter_raw or 0)
    else:
        return None, "no public-shareholding total in the file"
    if 0 < probe <= Decimal("1.5"):
        scale = HUNDRED                      # newest format: 0.7177 means 71.77 %
    elif probe >= 50:
        scale = Decimal(1)                   # older formats: already percent
    else:
        return None, "cannot tell the scale (the 100 %% total reads %s)" % probe

    def pct(category):
        v = raw_value(category, PCT_TAG)
        return None if v is None else v * scale

    def first(*categories):
        for c in categories:
            v = pct(c)
            if v is not None:
                return v
        return None

    def total_of(names):
        return sum((pct(n) or Decimal(0) for n in names), Decimal(0))

    promoter = pct(M_PROMOTER) or Decimal(0)         # a bank with no promoter has no such line
    public = pct(M_PUBLIC)
    if public is None:
        return None, "no public-shareholding total in the file"
    trusts = first(*M_EMPLOYEE) or Decimal(0)
    mf = pct(M_MF) or Decimal(0)
    insurance = pct(M_INSURANCE) or Decimal(0)
    bank = first(*M_BANK) or Decimal(0)
    domestic_total = pct(M_DOMESTIC_TOTAL)
    if domestic_total is not None:
        other_dii = domestic_total - mf - insurance - bank
        if other_dii < -ROUNDING_SLACK:
            return None, "domestic institutions %s are less than their own parts" % domestic_total
        other_dii = max(other_dii, Decimal(0))
    else:
        other_dii = total_of(M_DOMESTIC_PARTS)
    foreign_total = pct(M_FOREIGN_TOTAL)
    if foreign_total is not None:
        fii = foreign_total
    else:
        fii = total_of(M_FOREIGN_PARTS) + sum((pct(c) or Decimal(0) for (c, t) in facts if t == PCT_TAG and c.startswith(FPI_PREFIX)), Decimal(0))
    retail = sum((pct(c) or Decimal(0) for (c, t) in facts if t == PCT_TAG and RETAIL_MARK in c), Decimal(0))
    bodies = pct(M_BODIES) or Decimal(0)
    nri = total_of(M_NRI)

    # Category C (employee trusts, and depository-receipt custodians if a filing has them).
    c_total = None
    for (c, t) in facts:
        if t == PCT_TAG and NONPROMOTER_NONPUBLIC.search(c):
            c_total = pct(c)
    if c_total is not None:
        outside = max(c_total - trusts, Decimal(0))
    else:
        outside = sum((pct(c) or Decimal(0) for (c, t) in facts if t == PCT_TAG and CUSTODIAN.search(c)), Decimal(0))

    named = mf + insurance + bank + other_dii + fii + retail + bodies + nri
    rest = public - named
    if rest < -ROUNDING_SLACK:
        return None, "named public categories (%s) exceed the public total (%s)" % (_q4(named), _q4(public))
    other_public = max(rest, Decimal(0)) + outside
    parts = promoter + named + other_public + trusts
    if abs(parts - HUNDRED) > SUM_TOLERANCE:
        return None, "parts add up to %s, not 100" % _q4(parts)
    if conflicts:
        return None, "the file states different values for the same figure (%s)" % ", ".join(conflicts[:2])
    if expected_promoter is not None:
        listed = Decimal(str(expected_promoter))
        if abs(promoter - listed) > PROMOTER_TOLERANCE:
            return None, "promoter %s in the file, %s in the list" % (_q4(promoter), listed)

    out = dict(
        promoter_pct=_q4(promoter), public_pct=_q4(public), employee_trust_pct=_q4(trusts),
        mf_pct=_q4(mf), insurance_pct=_q4(insurance), bank_pct=_q4(bank), other_dii_pct=_q4(other_dii),
        fii_pct=_q4(fii), retail_pct=_q4(retail), bodies_corporate_pct=_q4(bodies), nri_pct=_q4(nri),
        other_public_pct=_q4(other_public), promoter_pledged_pct=pledged_share(facts, flags, scale, promoter, raw_value))
    return out, None


def pledged_share(facts, flags, scale, promoter, raw_value):
    """The share (0-100) of the promoter group's own shares that is pledged or otherwise encumbered, as
    the filing states it; 0 when the filing says nothing is; None when it does not say or there is no
    promoter. Order: the stated total, else the sum of the stated kinds, else shares encumbered /
    shares held, else the yes/no questions."""
    if promoter <= 0:
        return None
    stated = None
    for tag in PLEDGE_TOTAL_TAGS:
        v = raw_value(M_PROMOTER, tag)
        if v is not None:
            stated = v * scale
            break
    if stated is None:
        parts = [raw_value(M_PROMOTER, tag) for tag in PLEDGE_PART_TAGS]
        parts = [p for p in parts if p is not None]
        if parts:
            stated = sum(parts) * scale
    if stated is None:
        held = raw_value(M_PROMOTER, COUNT_TAG)
        for tag in PLEDGE_COUNT_TAGS:
            n = raw_value(M_PROMOTER, tag)
            if n is not None and held:
                stated = n / held * HUNDRED
                break
    if stated is not None:
        return _q4(min(max(stated, Decimal(0)), HUNDRED))
    asked = [v for k, v in flags.items() if ("Pledge" in k or "Encumber" in k)
             and "ForPublic" not in k and "ForNonPromoter" not in k]
    if asked and all(v == "false" for v in asked):
        return Decimal("0.0000")
    return None


# ------------------------------------------------------------------------ the run
def split_range(label, lo, hi):
    """[(label, lo, hi, None)] -- one window, or several if the range is longer than a year."""
    out = []
    while lo <= hi:
        end = min(hi, lo + timedelta(days=LONGEST_WINDOW_DAYS - 1))
        out.append((label if not out and end == hi else "%s (%s to %s)" % (label, lo, end), lo, end, None))
        lo = end + timedelta(days=1)
    return out


def build_windows(args, today):
    """[(label, d_from, d_to, symbol)]: the list requests this run makes."""
    if args.company_history:
        return [("%s (last 20 quarters)" % s, None, None, s) for s in args.symbol_list]
    if args.history_quarters:
        out = []
        for q in quarter_ends_back(today, args.history_quarters):
            lo, hi = q + timedelta(days=1), min(today, q + timedelta(days=WINDOW_SPAN_DAYS))
            if lo <= hi:
                out.append(("filings after %s" % q, lo, hi, None))
        return out
    if args.date_from or args.date_to:
        return split_range("filings %s to %s" % (args.date_from or "?", args.date_to or today),
                           args.date_from or (args.date_to - timedelta(days=DEFAULT_DAYS)), args.date_to or today)
    if args.quarter:
        return [("filings after %s" % args.quarter, args.quarter + timedelta(days=1),
                 min(today, args.quarter + timedelta(days=WINDOW_SPAN_DAYS)), None)]
    return split_range("last %d days" % args.days, today - timedelta(days=args.days), today)


def short(v):
    """A percentage for the log: 71.77, 0, 100, n/a."""
    if v is None:
        return "n/a"
    return "{:f}".format(v.normalize()) if v else "0"


def say_filing(kind, f):
    print("  %-7s %-12s %s  promoter %s  public %s  employee trusts %s  filed %s%s" % (
        kind, f["symbol"], f["quarter_end"], short(f["promoter_pct"]), short(f["public_pct"]),
        short(f["employee_trust_pct"]), f["submitted_on"] or "?", "  (revised)" if f["revised"] else ""))


def list_stage(conn, cur, args, today, master, table_ok, res, trip):
    """Read the list, decide, write. Returns the {(isin, quarter_end): filing} that the list stage
    inserted or updated (what a dry run would write); empty when the run was aborted."""
    by_isin = {m["isin"]: m["symbol"] for m in master}
    by_symbol = {m["symbol"]: m["isin"] for m in master}
    wanted = set(args.symbol_list) or None
    stats = res["stats"]
    filings = []
    windows = build_windows(args, today)
    print("%d list request(s): %s." % (len(windows), "; ".join(w[0] for w in windows[:4]) + (" ..." if len(windows) > 4 else "")))
    for i, (label, d_from, d_to, symbol) in enumerate(windows):
        if i:
            time.sleep(PAUSE_SEC)
        rows, problem = fetch_list(d_from, d_to, symbol)
        if rows is None:
            trip.fail()
            res["errors"] += 1
            print("  LIST FAILED  %s: %s" % (label, problem))
            if trip.tripped:
                res["aborted"] = "NSE failed %d times in a row -- stopping" % trip.n
                break
            continue
        trip.ok()
        res["rows_read"] += len(rows)
        stats["list_rows"] += len(rows)
        before = len(filings)
        for raw in rows:
            f = normalise_row(raw, by_isin, by_symbol, stats, wanted if not args.company_history else None, args.quarter)
            if f:
                filings.append(f)
        print("  %s: %d rows, %d of them our filings" % (label, len(rows), len(filings) - before))
    chosen = merge_filings(filings, stats)
    if args.limit:
        keep = sorted({k[0] for k in chosen}, key=lambda i: by_isin[i])[:args.limit]
        chosen = {k: v for k, v in chosen.items() if k[0] in keep}
    stats["filings"] = len(chosen)
    if not chosen:
        return {}
    existing = load_existing(cur, {k[0] for k in chosen}, min(k[1] for k in chosen)) if table_ok else {}
    plan = plan_changes(existing, chosen)
    stats["insert"], stats["update"], stats["same"], stats["older_kept"] = (
        len(plan["insert"]), len(plan["update"]), plan["same"], plan["older"])
    for kind, items in (("INSERT", plan["insert"]), ("UPDATE", plan["update"])):
        for f in items[:(8 if args.dry_run or args.verbose else 0)]:
            say_filing(kind, f)
    if not args.dry_run:
        for f in plan["insert"] + plan["update"]:
            cur.execute(UPSERT, f)
            res["written"] += 1
        conn.commit()
    return {(f["isin"], f["quarter_end"]): f for f in plan["insert"] + plan["update"]}


def detail_queue(cur, args, table_ok, master, planned):
    """The filings whose XBRL still has to be read: [{isin, symbol, quarter_end, promoter_pct, xbrl_url}],
    newest quarter first. In a dry run, the filings the list stage would have written join the queue."""
    names = {m["isin"]: m["symbol"] for m in master}
    queue = {}
    if table_ok:
        sql = ["SELECT isin, quarter_end, promoter_pct, xbrl_url FROM stock_shareholding",
               "WHERE NOT detail_loaded AND xbrl_url IS NOT NULL"]
        params = []
        if not args.retry_failed:
            sql.append("AND detail_error IS NULL")
        if args.detail_since:
            sql.append("AND quarter_end >= %s")
            params.append(args.detail_since)
        if args.quarter:
            sql.append("AND quarter_end = %s")
            params.append(args.quarter)
        if args.symbol_list:
            sql.append("AND isin = ANY(%s)")
            params.append([m["isin"] for m in master if m["symbol"] in args.symbol_list])
        cur.execute(" ".join(sql), params)
        for r in cur.fetchall():
            if r["isin"] in names:
                queue[(r["isin"], r["quarter_end"])] = dict(r, symbol=names[r["isin"]])
    for key, f in planned.items():
        if f["xbrl_url"] and (not args.detail_since or f["quarter_end"] >= args.detail_since):
            queue[key] = dict(isin=f["isin"], quarter_end=f["quarter_end"], promoter_pct=f["promoter_pct"],
                              xbrl_url=f["xbrl_url"], symbol=f["symbol"])
    items = sorted(queue.values(), key=lambda q: q["symbol"])
    items.sort(key=lambda q: q["quarter_end"], reverse=True)       # newest quarter first (the sort is stable)
    limit = args.detail_limit
    return items[:limit] if limit else items


def say_detail(item, d):
    print("  DETAIL OK   %-12s %s  promoter %s | MF %s insurance %s bank %s other DII %s FII %s | retail %s bodies %s NRI %s other %s | pledged %s" % (
        item["symbol"], item["quarter_end"], short(d["promoter_pct"]), short(d["mf_pct"]), short(d["insurance_pct"]),
        short(d["bank_pct"]), short(d["other_dii_pct"]), short(d["fii_pct"]), short(d["retail_pct"]),
        short(d["bodies_corporate_pct"]), short(d["nri_pct"]), short(d["other_public_pct"]), short(d["promoter_pledged_pct"])))


def detail_stage(conn, cur, args, queue, res, trip, first_call):
    tally = res["tally"]
    print("\n%d filing(s) to read%s." % (len(queue), " (dry run: nothing will be written)" if args.dry_run else ""))
    for n, item in enumerate(queue, 1):
        if not first_call or n > 1:
            time.sleep(PAUSE_SEC)
        status, body, problem = fetch_file(item["xbrl_url"])
        key = dict(isin=item["isin"], quarter_end=item["quarter_end"], xbrl_url=item["xbrl_url"])
        if problem and status == 404:
            reason, d = "file not found on NSE (HTTP 404)", None
            trip.ok()
        elif problem:
            trip.fail()
            res["errors"] += 1
            tally["DETAIL_DOWNLOAD_FAILED"] += 1
            print("  DETAIL DOWNLOAD FAILED  %-12s %s: %s" % (item["symbol"], item["quarter_end"], problem))
            if trip.tripped:
                res["aborted"] = "NSE failed %d times in a row -- stopping" % trip.n
                break
            continue
        else:
            trip.ok()
            res["rows_read"] += 1
            d, reason = parse_shp_xml(body, item["quarter_end"], item["isin"], item["promoter_pct"],
                                      item["symbol"].split("~")[0])      # a few symbols are kept as SYMBOL~ISIN
        if d is None:
            tally["DETAIL_REFUSED"] += 1
            res["refusals"][reason] += 1
            print("  DETAIL REFUSED %-12s %s: %s" % (item["symbol"], item["quarter_end"], reason))
            if not args.dry_run:
                cur.execute(DETAIL_REFUSED, dict(key, reason=reason[:300]))
                res["marked"] += 1
        else:
            tally["DETAIL_OK"] += 1
            if args.dry_run or args.verbose:
                say_detail(item, d)
            if not args.dry_run:
                cur.execute(DETAIL_UPDATE, dict(d, **key))
                res["detail_written"] += 1
        if not args.dry_run and n % 25 == 0:
            conn.commit()
    if not args.dry_run:
        conn.commit()


def run(conn, args):
    """One run. Returns a dict of counters (see make_result)."""
    res = new_result()
    cur = conn.cursor()
    today = args.today or now_ist().date()
    cur.execute("SELECT isin, symbol FROM stock_master WHERE is_active")
    master = cur.fetchall()
    table_ok = table_exists(cur)
    if not table_ok and not args.dry_run:
        res["aborted"] = "stock_shareholding does not exist: run create_stock_shareholding_table.py first"
        print("PROBLEM:", res["aborted"])
        return res
    if not table_ok:
        print("(stock_shareholding does not exist yet: everything below is what a first run would write)")
    trip = Tripwire()
    planned = {}
    list_calls = 0
    if not args.detail_only:
        planned = list_stage(conn, cur, args, today, master, table_ok, res, trip)
        list_calls = 1
    if (args.detail or args.detail_only) and not res["aborted"]:
        queue = detail_queue(cur, args, table_ok, master, planned if args.dry_run else {})
        detail_stage(conn, cur, args, queue, res, trip, first_call=not list_calls)
    summarise(res, args)
    return res


def summarise(res, args):
    s, t = res["stats"], res["tally"]
    print("\nSummary:")
    if not args.detail_only:
        print("  list rows read %d | not in our stock list %d (%d companies) | off-cycle dates skipped %d | "
              "odd values skipped %d | parts do not add up %d | symbol and isin disagree %d | "
              "matched by symbol (list isin blank or old) %d" % (
                  s["list_rows"], s["not_ours"], len(s["_not_ours_ids"]), s["off_cycle"],
                  s["bad_values"] + s["bad_date"], s["sum_off"], s["id_conflict"], s["matched_by_symbol"]))
        print("  company-quarters %d (several filings merged: %d) | new %d | changed %d | unchanged %d | "
              "older filing ignored %d%s" % (s["filings"], s["merged"], s["insert"], s["update"], s["same"],
                                              s["older_kept"], " | WOULD be written (dry run)" if args.dry_run else ""))
    if t:
        print("  detail: read OK %d | refused %d | download failed %d" % (
            t["DETAIL_OK"], t["DETAIL_REFUSED"], t["DETAIL_DOWNLOAD_FAILED"]))
        for reason, n in sorted(res["refusals"].items(), key=lambda kv: -kv[1])[:6]:
            print("    %3d x %s" % (n, re.sub(r"[-+]?\d+(\.\d+)?", "#", reason)[:100]))
    if res["aborted"]:
        print("PROBLEM:", res["aborted"])
    if args.dry_run:
        print("--dry-run: nothing written.")


# -------------------------------------------------------------------- locking, logging
def take_lock(cur, key=LOCK_KEY):
    cur.execute("SELECT pg_try_advisory_lock(%s) AS got", (key,))
    return cur.fetchone()["got"]


def log_run(cur, res, started, run_type=RUN_TYPE):
    """One row in batch_run_log. Only called when something was written or went wrong."""
    status = "FAILED" if res["aborted"] else ("PARTIAL" if res["errors"] else "SUCCESS")
    s, t = res["stats"], res["tally"]
    note = res["aborted"] or ("list rows %d, new %d, changed %d, unchanged %d; detail ok %d, refused %d, download failed %d" % (
        s["list_rows"], s["insert"], s["update"], s["same"], t["DETAIL_OK"], t["DETAIL_REFUSED"], t["DETAIL_DOWNLOAD_FAILED"]))
    cur.execute("""INSERT INTO batch_run_log (run_type, business_date, status, started_at, finished_at,
                                              rows_read, rows_written, error_message)
                   VALUES (%s, CURRENT_DATE, %s, %s, now(), %s, %s, %s)""",
                (run_type, status, started, res["rows_read"],
                 res["written"] + res["detail_written"] + res["marked"], note[:500]))


def parse_day(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Load shareholding patterns from NSE (see the top of this file).")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="filings submitted in the last N days (default %d)" % DEFAULT_DAYS)
    ap.add_argument("--from", dest="date_from", type=parse_day, help="first submission day, YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", type=parse_day, help="last submission day, YYYY-MM-DD (default today)")
    ap.add_argument("--quarter", type=parse_day, help="only this quarter end (YYYY-MM-DD, e.g. 2026-06-30)")
    ap.add_argument("--history-quarters", type=int, metavar="N",
                    help="walk back N quarters, one request per quarter (first load)")
    ap.add_argument("--symbols", help="comma-separated NSE symbols")
    ap.add_argument("--company-history", action="store_true",
                    help="with --symbols: each company's last 20 quarters, one request per company")
    ap.add_argument("--limit", type=int, help="at most this many companies (testing)")
    ap.add_argument("--detail", action="store_true", help="after the list, read the XBRL files not yet read")
    ap.add_argument("--detail-only", action="store_true", help="only read XBRL files; skip the list")
    ap.add_argument("--detail-limit", type=int, default=DEFAULT_DETAIL_LIMIT,
                    help="most files to read (default %d, 0 = no limit)" % DEFAULT_DETAIL_LIMIT)
    ap.add_argument("--detail-since", type=parse_day, help="only files for quarters ended on or after this day")
    ap.add_argument("--retry-failed", action="store_true", help="also try files refused on an earlier run")
    ap.add_argument("--dry-run", action="store_true", help="read and parse, write nothing")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--today", type=parse_day, help=argparse.SUPPRESS)       # for tests
    args = ap.parse_args(argv)
    args.symbol_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else []
    if args.company_history and not args.symbol_list:
        ap.error("--company-history needs --symbols")
    if args.quarter and not is_quarter_end(args.quarter):
        ap.error("--quarter must be a quarter end (31 Mar, 30 Jun, 30 Sep or 31 Dec)")
    if args.history_quarters is not None and args.history_quarters < 1:
        ap.error("--history-quarters must be at least 1")
    if args.days < 1:
        ap.error("--days must be at least 1")
    if args.date_from and args.date_to and args.date_from > args.date_to:
        ap.error("--from is after --to")
    return args


def main(argv=None):
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)      # lines reach a redirected log as they happen
    started = datetime.now(timezone.utc)
    with psycopg.connect(DB, row_factory=dict_row) as conn:
        if args.dry_run:
            conn.read_only = True          # a dry run cannot write, whatever the code does
        cur = conn.cursor()
        if not args.dry_run:
            if not take_lock(cur):
                print("Another shareholding run is still going; skipping this one.")
                return 0
            conn.commit()
        res = run(conn, args)
        if not args.dry_run and (res["written"] or res["detail_written"] or res["marked"] or res["aborted"] or res["errors"]):
            log_run(cur, res, started)
            conn.commit()
    return 1 if res["aborted"] else 0


if __name__ == "__main__":
    sys.exit(main())
