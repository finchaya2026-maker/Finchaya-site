"""
backfill_company_profile.py -- description, sector taxonomy, and (as a
free byproduct) headline fundamentals ratios, per stock.
------------------------------------------------------------------------
Two independent sources, one script because both are "who is this
company" rather than "what has it done lately" (that part is
fetch_stock_events.py):

  1. BSE's own site JSON (ComHeadernew) -- sector / industry / P/E / P/B
     / EPS / ROE. Reached through the same undocumented-but-public
     pattern as everything NSE/BSE-sourced in this codebase: it is what
     bseindia.com's own pages call, not a published, supported API, so
     treat it as liable to change shape or disappear without notice.
     BSE is keyed by a numeric SCRIP CODE, not ISIN or NSE symbol, so
     each stock is first resolved to one via PeerSmartSearch (a
     type-ahead endpoint) and the code is cached in
     stock_master.bse_scrip_code so that lookup only ever happens once
     per stock.

  2. Wikipedia's REST summary API -- a plain-text description, when a
     matching page exists. FREE and requires no key, but coverage drops
     off outside large- and mid-cap names; a stock with no good match is
     left with description = NULL rather than guessed at. Getting this
     wrong -- attaching the wrong company's history to a stock -- is
     worse than leaving it blank, so a match is only accepted when the
     returned page title is close to the company name AND Wikipedia
     itself does not flag it as a disambiguation page.

pe_ratio / pb_ratio / eps / roe_pct ARE STORED BUT NOT YET SHOWN ANYWHERE.
Whether and how to surface them is a separate, open decision -- see
add_company_profile_columns.py's docstring.

SAFE TO RE-RUN. Idempotent upserts; a stock with no BSE match or no wiki
match simply keeps whatever it already had.

USAGE
    python backfill_company_profile.py --symbols RELIANCE,TCS   # spot check
    python backfill_company_profile.py --limit 50 --dry-run     # scale test
    python backfill_company_profile.py                          # full universe
"""

import argparse
import os
import re
import sys
import time

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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

PAUSE_SEC = 0.6          # between BSE calls -- a courtesy, not documented
BATCH_PAUSE_SEC = 8
BATCH_SIZE = 50

ap = argparse.ArgumentParser()
ap.add_argument("--symbols", help="comma-separated NSE symbols, for a spot check")
ap.add_argument("--limit", type=int, help="only the first N stocks (newest updated_at last)")
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()


# ---------------------------------------------------------------------
# BSE: resolve a scrip code, then read sector + fundamentals off it.
# ---------------------------------------------------------------------
bse = requests.Session()
bse.headers.update({
    "User-Agent": UA, "Accept": "application/json",
    # Both required -- confirmed live 28 Sep 2026: identical requests
    # without a same-site Referer/Origin come back 404, even though
    # nothing about the URL or params differs. Not documented anywhere;
    # if BSE ever changes how this check works, a 404 here is the symptom.
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
})

#   liclick('500325','RELIANCE INDUSTRIES LTD')  ... <a><strong>RELIANCE</strong> INDUSTRIES LTD<br /><span>...INE002A01018...
#   liclick('532540','TATA CONSULTANCY SERVICES LTD') ... <a>TATA CONSULTANCY SERVICES LTD<br /><span><strong>TCS</strong>...INE467B01029...
# BSE bolds whichever part of the row matched the search text -- the
# company name when the search was a name, the ticker when it was a
# ticker -- so a <strong> tag is NOT reliably found around the company
# name (a first version of this regex assumed it always was, and quietly
# failed to resolve every stock searched for by its ticker rather than
# its name, which given callers pass the NSE symbol was most of them).
# The company name itself is never used -- only the scrip code and the
# ISIN that confirms it -- so this reads across everything between one
# liclick(...) and the next ISIN, whatever markup sits in between.
_SEARCH_ROW = re.compile(r"liclick\('(\d+)'.*?(\bINE[0-9A-Z]{9}\b)", re.S)


def resolve_scrip_code(symbol, isin):
    """PeerSmartSearch returns an HTML fragment (as a JSON string) of every
    company whose name or symbol matches the search text. Matched on ISIN,
    never on position in the list -- a plain symbol search for "RELIANCE"
    also returns Reliance Infrastructure and Reliance Power, and taking
    row zero would silently mix up three unrelated companies."""
    try:
        r = bse.get("https://api.bseindia.com/BseIndiaAPI/api/PeerSmartSearch/w",
                     params={"Type": "SS", "text": symbol}, timeout=10)
        if not r.ok:
            return None
        for scrip_code, row_isin in _SEARCH_ROW.findall(r.text):
            if row_isin == isin:
                return scrip_code
    except requests.RequestException:
        pass
    return None


def fetch_bse_header(scrip_code):
    try:
        r = bse.get("https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w",
                     params={"scripcode": scrip_code}, timeout=10)
        if not r.ok:
            return None
        j = r.json()
        return j if j.get("SecurityId") else None
    except (requests.RequestException, ValueError):
        return None


def num(v):
    try:
        f = float(v)
        return f if f == f else None   # filters NaN
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------
# Wikipedia: a short description, only when the match looks safe.
# ---------------------------------------------------------------------
wiki = requests.Session()
wiki.headers.update({
    "User-Agent": "FinChaya/1.0 (mf.finchaya.com; contact finchaya2026@gmail.com)"})

SUFFIXES = re.compile(
    r"\b(limited|ltd\.?|private|pvt\.?|company|co\.?|corporation|corp\.?)\b\.?",
    re.I)


def clean_name(name):
    return re.sub(r"\s+", " ", SUFFIXES.sub("", name)).strip(" .,-")


def norm_words(s):
    return set(re.sub(r"&", " and ", s.lower()).split())


def fetch_wiki_description(company_name):
    """Search, then confirm via the summary endpoint. Rejected rather than
    guessed at: a disambiguation page, or a title that is not EXACTLY the
    company's own name once suffixes are stripped, comes back as None.

    Exact word-set equality, not "shares most words" -- a looser check
    (originally: reject only if fewer than core_words-1 words overlap)
    let "Infosys Limited" match the Wikipedia page for "Infosys BPM
    Limited", its own BPM subsidiary, because a one-word core name
    ("infosys") made the threshold trivially easy to clear. A short,
    single-word company name is exactly where a loose match is most
    dangerous, not least -- so this trades missed matches (no
    description shown) for zero tolerance of a wrong one (the wrong
    company's history shown as this one's)."""
    core = clean_name(company_name)
    try:
        r = wiki.get("https://en.wikipedia.org/w/api.php", timeout=10, params={
            "action": "query", "list": "search", "format": "json",
            "srsearch": company_name + " company India", "srlimit": 5,
        })
        hits = r.json().get("query", {}).get("search", [])
    except (requests.RequestException, ValueError):
        return None
    if not hits:
        return None

    core_words = norm_words(core)
    for hit in hits:
        title = hit["title"]
        title_words = norm_words(clean_name(re.sub(r"\s*\(.*?\)\s*", "", title)))
        if title_words != core_words:
            continue  # anything but an exact match, once suffixes are stripped
        try:
            sr = wiki.get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/"
                + requests.utils.quote(title.replace(" ", "_")), timeout=10)
            sj = sr.json()
        except (requests.RequestException, ValueError):
            continue
        if sj.get("type") == "disambiguation" or not sj.get("extract"):
            continue
        return sj["extract"], "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
    return None


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------
UPSERT = """
UPDATE stock_master SET
    bse_scrip_code          = COALESCE(%(scrip)s, bse_scrip_code),
    sector                  = COALESCE(%(sector)s, sector),
    industry                = COALESCE(industry, %(industry)s),
    pe_ratio                = COALESCE(%(pe)s, pe_ratio),
    pb_ratio                = COALESCE(%(pb)s, pb_ratio),
    eps                     = COALESCE(%(eps)s, eps),
    roe_pct                 = COALESCE(%(roe)s, roe_pct),
    fundamentals_updated_at = CASE WHEN %(sector)s IS NOT NULL
                                   THEN now() ELSE fundamentals_updated_at END,
    description             = COALESCE(%(desc)s, description),
    description_source      = COALESCE(%(desc_src)s, description_source),
    description_updated_at  = CASE WHEN %(desc)s IS NOT NULL
                                    THEN now() ELSE description_updated_at END
WHERE isin = %(isin)s
"""

with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
    where = "WHERE is_active"
    params = {}
    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",")]
        where += " AND symbol = ANY(%(syms)s)"
        params["syms"] = syms
    cur.execute(f"""
        SELECT isin, symbol, company_name, bse_scrip_code
        FROM stock_master {where}
        ORDER BY (description IS NULL AND sector IS NULL) DESC,
                 updated_at ASC NULLS FIRST
        {"LIMIT %(limit)s" if args.limit else ""}
    """, {**params, "limit": args.limit})
    stocks = cur.fetchall()
    print(f"{len(stocks)} stocks to process.")

    done_desc = done_bse = skipped = 0
    for i, s in enumerate(stocks, 1):
        scrip = s["bse_scrip_code"]
        if not scrip:
            scrip = resolve_scrip_code(s["symbol"], s["isin"])
            time.sleep(PAUSE_SEC)

        bse_row = fetch_bse_header(scrip) if scrip else None
        time.sleep(PAUSE_SEC)
        wiki_hit = fetch_wiki_description(s["company_name"])

        if not bse_row and not wiki_hit:
            skipped += 1
            continue

        row = {
            "isin": s["isin"],
            "scrip": scrip,
            "sector": (bse_row or {}).get("Sector") or None,
            "industry": (bse_row or {}).get("IndustryNew") or None,
            "pe": num((bse_row or {}).get("PE")),
            "pb": num((bse_row or {}).get("PB")),
            "eps": num((bse_row or {}).get("EPS")),
            "roe": num((bse_row or {}).get("ROE")),
            "desc": wiki_hit[0] if wiki_hit else None,
            "desc_src": wiki_hit[1] if wiki_hit else None,
        }
        if bse_row:
            done_bse += 1
        if wiki_hit:
            done_desc += 1

        print(f"[{i}/{len(stocks)}] {s['symbol']:<15} "
              f"bse={'y' if bse_row else 'n'} wiki={'y' if wiki_hit else 'n'}")

        if not args.dry_run:
            cur.execute(UPSERT, row)
            if i % BATCH_SIZE == 0:
                conn.commit()
                time.sleep(BATCH_PAUSE_SEC)

    if not args.dry_run:
        conn.commit()

    print(f"\nDone. BSE profile: {done_bse}/{len(stocks)}   "
          f"Description: {done_desc}/{len(stocks)}   "
          f"No match at all: {skipped}/{len(stocks)}")
    if args.dry_run:
        print("--dry-run: nothing written.")
