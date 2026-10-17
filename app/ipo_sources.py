"""
ipo_sources.py -- where the IPO facts come from, and how each source's answer is turned into plain records. No database in here.
---------------------------------------------------------------------------
THREE PUBLIC SOURCES
    NSE (nseindia.com, JSON, needs a browser-like session: a cookie from a normal page first, and a Chrome-like TLS handshake -- curl_cffi)
        /api/all-upcoming-issues?category=ipo     issues that have not opened (mainboard "EQ" and "SME"), with dates and price band
        /api/ipo-current-issue                    issues open for bidding right now
        /api/public-past-issues                   every issue since 2012 (the date and category arguments are ignored: all rows come back), with dates,
                                                  final issue price and listing date
        /api/ipo-detail?symbol=X&series=EQ|SME    issue size, price range, lot, lead managers, registrar, subscription so far, and the link to the
                                                  Red Herring Prospectus: https://nsearchives.nseindia.com/content/ipo/RHP_<SYMBOL>.zip
    SEBI (sebi.gov.in): "Draft Offer Documents filed with SEBI" -- a list (25 a page; page 1 is plain HTML, later pages come from one POST) whose rows
        link to a detail page that holds the DRHP as a PDF. Mainboard issues only: SME issues file their drafts with the exchange, not SEBI.
    The documents themselves: a PDF from SEBI, a zip (with one big PDF in it) from NSE.

EVERYTHING HERE THAT READS A RESPONSE IS A PURE FUNCTION, tested against responses saved from the live sites (ipo_demo/fixtures). If a site changes its
format the test fixtures stay valid and the daily job's --probe says which source stopped answering in the shape this file expects.

NOTHING IN THIS FILE DECIDES WHAT AN ISSUE IS WORTH. It records what the issuers and the exchanges publish.
"""

import io
import json
import os
import re
import time
import zipfile
from datetime import date, datetime

NSE_HOME = "https://www.nseindia.com"
NSE_WARM = NSE_HOME + "/market-data/all-upcoming-issues-ipo"
NSE_ARCHIVE = "https://nsearchives.nseindia.com/content/ipo/"
SEBI_LIST = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=10"
SEBI_AJAX = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NSE_PAUSE = 0.8                                   # seconds between NSE calls: it blocks a session that goes faster than a person would
MIN_PDF_BYTES = 200_000                           # a real offer document is megabytes; anything smaller is an error page


# ---------------------------------------------------------------------------------------------------------------------------------
# small readers
# ---------------------------------------------------------------------------------------------------------------------------------
def parse_date(s):
    """'13-Oct-2026', '09-OCT-2026', 'Oct 07, 2026', '2026-10-13' -> date; '-' / '' / None -> None."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if s in ("-", "--", "NA", "N.A.", "null"):
        return None
    for fmt in ("%d-%b-%Y", "%b %d, %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s.title() if fmt in ("%d-%b-%Y", "%d-%B-%Y") else s, fmt).date()
        except ValueError:
            continue
    return None


def money(s):
    """The first rupee figure in 'Rs.258 to Rs.271', '   272', 'Rs. 2,00,000', '402' -> float, else None."""
    if s is None:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(s))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def price_band(s):
    """'Rs.258 to Rs.271' -> (258.0, 271.0); 'Rs.1000' or '402' -> (1000.0, 1000.0); anything else -> (None, None)."""
    if not s:
        return (None, None)
    nums = re.findall(r"\d[\d,]*(?:\.\d+)?", str(s))
    vals = []
    for n in nums[:2]:
        try:
            vals.append(float(n.replace(",", "")))
        except ValueError:
            pass
    if not vals:
        return (None, None)
    lo, hi = vals[0], vals[-1]
    return (min(lo, hi), max(lo, hi))


def to_int(s):
    if s is None:
        return None
    m = re.search(r"\d[\d,]*", str(s))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


_SUFFIX = re.compile(r"\b(?:limited|ltd|private|pvt|public|the|company)\b\.?", re.I)


def company_key(name):
    """The same company however each source spells it: lower case, 'and' for '&', no punctuation, no Limited/Pvt/Ltd, no '(India)' / '(I)' tail.
    'Pentacle Consultants (I) Limited' == 'Pentacle Consultants India Limited' == 'PENTACLE CONSULTANTS I LIMITED'."""
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"\((?:india|i)\)", " ", s)
    s = _SUFFIX.sub(" ", s)
    s = re.sub(r"\bindia\b|\bi\b(?=\s*$)", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def slugify(name):
    """'HD Fire Protect Limited' -> 'hd-fire-protect-limited' (the page address)."""
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80].strip("-") or "issue"


_LOWER_WORDS = {"and", "of", "the", "for", "in", "&"}
_KEEP_UPPER = {"LLP", "LTD", "PVT", "NNT", "PCI", "PES", "SAI", "BS", "AI", "IT", "HD", "ABC", "II", "III", "IV", "USA", "UK", "I"}


def _title_if_shouting(name):
    """SEBI's list prints some names in capitals ('INTEGRUM ENERGY INFRASTRUCTURE LIMITED'); the page shows them in ordinary capitals. Names that already
    have lower-case letters are left exactly as written. A word of three letters or fewer stays in capitals (it is usually an acronym)."""
    if re.search(r"[a-z]", name):
        return name
    out = []
    for w in name.split(" "):
        letters = re.sub(r"[^A-Za-z]", "", w)
        if w.lower() in _LOWER_WORDS and out:
            out.append(w.lower())
        elif letters.upper() in ("LTD", "PVT"):
            out.append(re.sub(r"[A-Za-z]+", lambda m: m.group(0)[:1] + m.group(0)[1:].lower(), w))
        else:                                                                       # each run of letters: three or fewer stay as they are, longer ones are capitalised
            out.append(re.sub(r"[A-Za-z]+", lambda m: m.group(0) if len(m.group(0)) <= 3 else m.group(0)[:1] + m.group(0)[1:].lower(), w))
    return " ".join(out)


def tidy_name(name):
    """A company name as the page shows it: single spaces, no trailing ' - DRHP', no stray punctuation, capitals only where a name has them."""
    s = re.sub(r"<[^>]+>", " ", name or "")
    s = re.sub(r"\s*[-–—]\s*(?:DRHP|Draft Red Herring Prospectus|Addendum.*|RHP)\s*$", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -–—")
    return _title_if_shouting(s)


# ---------------------------------------------------------------------------------------------------------------------------------
# NSE: the lists
# ---------------------------------------------------------------------------------------------------------------------------------
def board_of(series):
    return "sme" if str(series or "").upper() == "SME" else "mainboard"


def norm_upcoming(rec):
    """One row of /api/all-upcoming-issues or /api/ipo-current-issue -> the record the job stores."""
    lo, hi = price_band(rec.get("priceBand") or rec.get("issuePrice"))
    return {
        "symbol": (rec.get("symbol") or "").strip().upper() or None,
        "name": tidy_name(rec.get("companyName") or rec.get("company")),
        "board": board_of(rec.get("series") or rec.get("securityType")),
        "open_date": parse_date(rec.get("issueStartDate")),
        "close_date": parse_date(rec.get("issueEndDate")),
        "listing_date": None,
        "price_low": lo, "price_high": hi, "issue_price": None,
        "lot_size": to_int(rec.get("lotSize")),
        "issue_size_shares": to_int(rec.get("issueSize")),
        "nse_status": rec.get("status"),
    }


def norm_past(rec):
    """One row of /api/public-past-issues. Only equity issues (EQ mainboard, SME) are kept; debt, rights, InvITs and the rest return None."""
    kind = str(rec.get("securityType") or "").upper()
    if kind not in ("EQ", "SME"):
        return None
    lo, hi = price_band(rec.get("priceRange"))
    return {
        "symbol": (rec.get("symbol") or "").strip().upper() or None,
        "name": tidy_name(rec.get("company") or rec.get("companyName")),
        "board": board_of(kind),
        "open_date": parse_date(rec.get("ipoStartDate")),
        "close_date": parse_date(rec.get("ipoEndDate")),
        "listing_date": parse_date(rec.get("listingDate")),
        "price_low": lo, "price_high": hi,
        "issue_price": money(rec.get("issuePrice")),
        "lot_size": None, "issue_size_shares": None, "nse_status": None,
    }


def stage_for(today, open_date, close_date, listing_date, have_nse=True):
    """Where an issue is: filed (draft with the regulator only), upcoming, open, closed (bidding over, not yet trading), listed."""
    if not have_nse or (open_date is None and close_date is None and listing_date is None):
        return "filed"
    if listing_date is not None and listing_date <= today:
        return "listed"
    if open_date is not None and today < open_date:
        return "upcoming"
    if close_date is not None and today > close_date:
        return "closed"
    if open_date is not None and (close_date is None or today <= close_date):
        return "open"
    return "upcoming"


# An issue lists about three working days after its bidding closes. Past this many calendar days (a long weekend and a holiday on top) with no listing, "bidding closed,
# not yet listed" is no longer true of it: it is not coming up any more.
LISTING_WAIT_DAYS = 6
LISTING_EVIDENCE_DAYS = 14        # our own first price must be within this many days of the close for it to count as the listing day
# The NSE writes what happened to an issue into its name: "X Limited-Issue Withdrawn", "Y Limited-Issue postponed", "Z Limited- Withdrawal Window".
_STATUS_TAIL = re.compile(r"\s*[-–—]*\s*(?:issue\s+)?(withdrawn|postponed|cancell?ed|withdrawal\s+window)\s*$", re.I)
_STATUS_KEY = {"withdrawn": "withdrawn", "postponed": "postponed", "cancelled": "cancelled", "canceled": "cancelled"}


def status_note(name):
    """(the name without the NSE's status words, 'withdrawn' | 'postponed' | 'cancelled' | None).  'IC Electricals Limited-Issue postponed' -> ('IC Electricals Limited', 'postponed')."""
    m = _STATUS_TAIL.search(name or "")
    if not m:
        return (name or "").strip(), None
    word = re.sub(r"\s+", " ", m.group(1).lower())
    return (name[:m.start()]).strip(" -–—"), ("withdrawn" if word.startswith("withdrawal") else _STATUS_KEY.get(word, "withdrawn"))


def effective_stage(today, open_date, close_date, listing_date, symbol, name, first_trade=None):
    """(stage, listing_date, note) as of `today`: the stage_for() answer corrected for what stage_for cannot know.
         * 'ended'   the issue was withdrawn / postponed / cancelled (the NSE says so in its name), or its bidding closed more than LISTING_WAIT_DAYS ago and nothing shows it
                     listed. It is not "coming up", and it is not listed. note: 'withdrawn' | 'postponed' | 'cancelled' | 'no_listing'
         * 'listed'  its bidding closed and it has TRADED since (first_trade: the first price FinChaya holds after the close), although the exchange's list gave no listing date
         * 'closed'  only for the first LISTING_WAIT_DAYS days after the close, while the listing is still to come
       Everything else is stage_for()."""
    _, status = status_note(name)
    if status:
        return "ended", listing_date, status
    stage = stage_for(today, open_date, close_date, listing_date, have_nse=symbol is not None)
    if stage == "closed":
        if first_trade is not None:
            return "listed", first_trade, None
        if close_date is not None and listing_date is None and (today - close_date).days > LISTING_WAIT_DAYS:     # (a listing date still ahead keeps it coming up)
            return "ended", listing_date, "no_listing"
    return stage, listing_date, None


# ---------------------------------------------------------------------------------------------------------------------------------
# NSE: one issue's detail
# ---------------------------------------------------------------------------------------------------------------------------------
def _unquote(v):
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return v.strip()


def _clean_html(v):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v or "")).strip()


def norm_detail(d):
    """/api/ipo-detail -> {fields: {title: value}, brlms, registrar, rhp_url, issue_size_text, face_value, lot_size, price_range, issue_period, subscription}.
    Values keep the exchange's words; nothing is computed."""
    out = {"fields": {}, "brlms": [], "registrar": None, "rhp_url": None, "issue_size_text": None, "face_value": None, "lot_size": None,
           "price_range": None, "issue_period": None, "issue_type": None, "subscription": []}
    items = ((d or {}).get("issueInfo") or {}).get("dataList") or []
    for it in items:
        title = (it.get("title") or "").strip()
        val = _unquote(it.get("value"))
        if not title or not val or len(title) > 120:
            continue
        out["fields"][title] = val[:600]
        t = title.lower()
        if t.startswith("book running lead manager"):
            out["brlms"] = [x.strip() for x in re.split(r",\s*|\s+and\s+|;\s*", val) if x.strip()][:8]
        elif t.startswith("name of the registrar"):
            out["registrar"] = val[:160]
        elif t.startswith("red herring prospectus"):
            m = re.search(r"https?://[^\s\"'<>]+", val)
            out["rhp_url"] = m.group(0) if m else None
        elif t == "issue size":
            out["issue_size_text"] = val[:300]
        elif t == "face value":
            out["face_value"] = val[:60]
        elif t in ("bid lot", "lot size"):
            out["lot_size"] = to_int(val)
        elif t == "price range":
            out["price_range"] = val[:120]
        elif t == "issue period":
            out["issue_period"] = val[:80]
        elif t == "issue type":
            out["issue_type"] = val[:60]
    # subscription so far: category -> times subscribed (the exchange's own table; empty before bidding starts)
    for b in (d or {}).get("bidDetails") or []:
        cat = (b.get("category") or "").strip()
        times = b.get("noOfTime")
        if cat and times not in (None, "", "0.00", "0"):
            try:
                out["subscription"].append({"category": cat, "times": float(times)})
            except (TypeError, ValueError):
                pass
    return out


# ---------------------------------------------------------------------------------------------------------------------------------
# SEBI
# ---------------------------------------------------------------------------------------------------------------------------------
# the link's own attributes can hold '>' inside a quoted title ("... DRHP <br><a href=...>Abridged Prospectus</a>"), so its attributes are read quote by quote
_SEBI_ROW = re.compile(r"<tr[^>]*>\s*<td[^>]*>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>\s*<td[^>]*>\s*<a\s+href\s*=\s*['\"]([^'\"]+)['\"]"
                       r"(?:\s+[A-Za-z-]+\s*(?:=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*\s*>(.*?)</a>", re.S)


def sebi_kind(url, title):
    s = ((url or "") + " " + (title or "")).lower()
    if "addendum" in s or "corrigendum" in s:
        return "ADDENDUM"
    if re.search(r"drhp|draft[- ]red[- ]herring|draft[- ]offer", s):
        return "DRHP"
    if re.search(r"(?<![a-z])rhp(?![a-z])|red[- ]herring", s):
        return "RHP"
    if "prospectus" in s:
        return "PROSPECTUS"
    return "OTHER"


def sebi_rows(html):
    """The rows of one page of SEBI's list -> [{date, url, title, company, kind, sebi_id}] (newest first, as printed)."""
    out = []
    for m in _SEBI_ROW.finditer(html or ""):
        d = parse_date(m.group(1))
        url = m.group(2).strip()
        if url.startswith("/"):
            url = "https://www.sebi.gov.in" + url
        label = m.group(3).split("<br", 1)[0]
        title = tidy_name(label)
        sid = re.search(r"_(\d+)\.html", url)
        out.append({"date": d, "url": url, "title": title, "company": tidy_name(re.sub(r"[-–]\s*(?:DRHP|RHP).*$", "", label)),
                    "kind": sebi_kind(url, label), "sebi_id": int(sid.group(1)) if sid else None})
    return out


def sebi_total(html):
    """(first, last, total) from '26 to 50 of 2265 records', else None."""
    m = re.search(r"(\d+) to (\d+) of (\d+) records", html or "")
    return tuple(int(x) for x in m.groups()) if m else None


def sebi_pdf_url(detail_html):
    """The PDF behind a SEBI detail page (an iframe: ...?file=https://www.sebi.gov.in/sebi_data/attachdocs/.../x.pdf)."""
    m = re.search(r"file=(https?://[^'\"\s]+?\.pdf)", detail_html or "", re.I)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------------------------------------------------------------
# the network (everything above is pure; everything below talks to the sites)
# ---------------------------------------------------------------------------------------------------------------------------------
class SourceError(Exception):
    pass


def _session():
    try:
        from curl_cffi import requests as cr
        return cr.Session(impersonate="chrome")
    except Exception:                                                             # noqa: BLE001  (not installed, or the impersonation target is unknown)
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        return s


class NSE:
    """A browser-like session on nseindia.com. One cookie fetch, then JSON calls a second apart."""

    def __init__(self, session=None):
        self.s = session or _session()
        self.warmed = False
        self.last = 0.0

    def warm(self):
        r = self.s.get(NSE_WARM, timeout=30)
        if r.status_code != 200:
            raise SourceError("NSE did not answer the first page (HTTP %s)" % r.status_code)
        self.warmed = True

    def _wait(self):
        gap = NSE_PAUSE - (time.monotonic() - self.last)
        if gap > 0:
            time.sleep(gap)
        self.last = time.monotonic()

    def get_json(self, path, tries=3):
        if not self.warmed:
            self.warm()
        H = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9", "Referer": NSE_WARM}
        last = None
        for k in range(tries):
            self._wait()
            try:
                r = self.s.get(NSE_HOME + path, headers=H, timeout=60)
                if r.status_code == 200:
                    return json.loads(r.text)
                last = "HTTP %s" % r.status_code
                if r.status_code in (401, 403):
                    self.warm()
            except Exception as e:                                                 # noqa: BLE001
                last = "%s: %s" % (type(e).__name__, str(e)[:100])
            time.sleep(1.5 * (k + 1))
        raise SourceError("NSE %s failed: %s" % (path.split("?")[0], last))

    def upcoming(self):
        rows = self.get_json("/api/all-upcoming-issues?category=ipo")
        return [norm_upcoming(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def current(self):
        rows = self.get_json("/api/ipo-current-issue")
        return [norm_upcoming(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def past(self, since):
        """Equity issues that opened on or after `since`."""
        rows = self.get_json("/api/public-past-issues?start_date=01-01-2012&end_date=%s&category=ipo&type=ipo" % date.today().strftime("%d-%m-%Y"))
        out = []
        for r in rows if isinstance(rows, list) else []:
            n = norm_past(r) if isinstance(r, dict) else None
            if n and n["symbol"] and n["open_date"] and n["open_date"] >= since:
                out.append(n)
        return out

    def detail(self, symbol, board):
        d = self.get_json("/api/ipo-detail?symbol=%s&series=%s" % (symbol, "SME" if board == "sme" else "EQ"))
        return norm_detail(d)

    def download_zip_pdf(self, url, dest):
        """Download an NSE archive zip and write its biggest PDF to dest. Returns the PDF size in bytes."""
        H = {"Referer": NSE_HOME + "/", "Accept": "*/*"}
        last = None
        for k in range(3):
            self._wait()
            try:
                r = self.s.get(url, headers=H, timeout=180)
                if r.status_code != 200:
                    last = "HTTP %s" % r.status_code
                else:
                    z = zipfile.ZipFile(io.BytesIO(r.content))
                    pdfs = sorted([i for i in z.infolist() if i.filename.lower().endswith(".pdf")], key=lambda i: -i.file_size)
                    if not pdfs:
                        raise SourceError("the zip has no PDF in it")
                    data = z.read(pdfs[0])
                    if len(data) < MIN_PDF_BYTES:
                        raise SourceError("the PDF in the zip is only %d bytes" % len(data))
                    with open(dest, "wb") as f:
                        f.write(data)
                    return len(data)
            except SourceError:
                raise
            except Exception as e:                                                 # noqa: BLE001
                last = "%s: %s" % (type(e).__name__, str(e)[:100])
            time.sleep(2 * (k + 1))
        raise SourceError("download of %s failed: %s" % (url.rsplit("/", 1)[-1], last))


class SEBI:
    def __init__(self, session=None):
        self.s = session or _session()
        self.listed = False

    def first_page(self):
        r = self.s.get(SEBI_LIST, timeout=60)
        if r.status_code != 200:
            raise SourceError("SEBI list answered HTTP %s" % r.status_code)
        self.listed = True
        return r.text

    def page(self, n):
        """Page n (1 = newest). Page 1 is the plain list; later pages come from the list's own ajax call."""
        if n <= 1:
            return self.first_page()
        if not self.listed:
            self.first_page()
        data = {"nextValue": "1", "next": "n", "search": "", "fromDate": "", "toDate": "", "fromYear": "", "toYear": "", "deptId": "-1", "sid": "3",
                "ssid": "15", "smid": "10", "ssidhidden": "15", "smidhidden": "10", "intmid": "-1", "sText": "Filings", "ssText": "Public Issues",
                "smText": "Draft Offer Documents filed with SEBI", "doDirect": str(n - 1)}
        H = {"Referer": SEBI_LIST, "X-Requested-With": "XMLHttpRequest", "Accept": "text/html, */*; q=0.01", "Origin": "https://www.sebi.gov.in"}
        for k in range(3):
            try:
                r = self.s.post(SEBI_AJAX, data=data, headers=H, timeout=60)
                if r.status_code == 200 and (sebi_rows(r.text) or sebi_total(r.text) or re.search(r"no\s+records?", r.text, re.I)):
                    return r.text                                                  # an empty page past the end of the list is an answer, not a failure
            except Exception:                                                      # noqa: BLE001
                pass
            time.sleep(2 * (k + 1))
        raise SourceError("SEBI list page %d could not be read" % n)

    def filings_since(self, since, max_pages=120):
        """Every list row dated on or after `since`, newest first. The list is in date order, so it stops at the first page that reaches an older row."""
        out = []
        for n in range(1, max_pages + 1):
            rows = sebi_rows(self.page(n))
            if not rows:
                break
            out.extend(r for r in rows if r["date"] and r["date"] >= since)
            if any(r["date"] and r["date"] < since for r in rows):
                break
            time.sleep(0.6)
        return out

    def pdf_url(self, detail_url):
        r = self.s.get(detail_url, timeout=60)
        if r.status_code != 200:
            raise SourceError("SEBI detail page answered HTTP %s" % r.status_code)
        u = sebi_pdf_url(r.text)
        if not u:
            raise SourceError("no PDF link on the SEBI page")
        return u


def download_pdf(url, dest, tries=4):
    """A PDF over plain HTTPS. SEBI's server closes long downloads now and then, so this retries and checks the size it was promised."""
    import requests
    H = {"User-Agent": UA, "Referer": "https://www.sebi.gov.in/"}
    last = None
    for k in range(tries):
        try:
            r = requests.get(url, headers=H, timeout=(20, 300), stream=True)
            if r.status_code != 200:
                last = "HTTP %s" % r.status_code
            else:
                n = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                        n += len(chunk)
                promised = r.headers.get("content-length")
                if promised and promised.isdigit() and n != int(promised):
                    last = "got %d of %s bytes" % (n, promised)
                elif n < MIN_PDF_BYTES:
                    raise SourceError("the file is only %d bytes: not an offer document" % n)
                else:
                    return n
        except SourceError:
            raise
        except Exception as e:                                                     # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, str(e)[:100])
        time.sleep(3 * (k + 1))
    raise SourceError("download failed after %d tries: %s" % (tries, last))


def read_pdf_pages(path, want_layout_pages=True):
    """(pages, layout) for ipo_extract.extract: the text of every page, and a function giving one page's text in layout mode (kept open on the reader)."""
    import logging
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)                             # "Ignoring wrong pointing object ..." is noise from a damaged cross-reference table
    rd = PdfReader(path)
    pages = []
    for p in rd.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:                                                          # noqa: BLE001  (a page pypdf cannot read is an empty page)
            pages.append("")
    layout = (lambda i: rd.pages[i].extract_text(extraction_mode="layout")) if want_layout_pages else None
    return pages, layout
