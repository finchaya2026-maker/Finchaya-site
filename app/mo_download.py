#!/usr/bin/env python3
"""
mo_download.py -- fetch Motilal Oswal month-end portfolios.

    python mo_download.py --years 2020-2026 --out C:\\finchaya\\mo --list
    python mo_download.py --years 2025-2026 --out C:\\finchaya\\mo

ONE REQUEST PER YEAR. The endpoint takes a blank month and returns the
whole year, so twelve calls cover 2015 to now.

THREE THINGS THIS FILE EXISTS TO GET RIGHT
-------------------------------------------
1. The API's `month` is the PUBLICATION month, not the portfolio month.
   Ask for aug 2025 and you get the JULY portfolio. Everything here is
   keyed on the portfolio month, taken from the title.

2. The category mixes three kinds of document: month-end portfolios,
   FORTNIGHTLY disclosures, and performance sheets. Only the first is
   wanted. A fortnightly file loaded as a month-end silently puts
   mid-month holdings on a month-end date.

3. THE FILENAME AND THE TITLE CONTRADICT EACH OTHER, in both directions.
   Two different files are both named
   scheme-portfolio-details-october-2025.xlsx -- one is September's.
   Elsewhere a file named 15072025 is titled "15th jun 2025".
   So neither is trusted: the title decides the name to save under, and
   then --verify reads the "Portfolio as on" line INSIDE the file and
   shouts if the two disagree.
"""

import argparse
import os
import re
import sys
import time

import requests

BASE = "https://www.motilaloswalmf.com"
API = BASE + "/content/aem-cloud-dept-backend-motilal-oswal/api/search-documents.json"
HEADERS = {
    "accept": "*/*",
    "referer": BASE + "/downloads/scheme-portfolio-details",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/152.0.0.0 Safari/537.36"),
}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
MONTHS.update({m[:3]: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])})
MONTHS["sept"] = 9

# Anything matching these is NOT a month-end portfolio.
SKIP = re.compile(r"fortnight|performance|factsheet\s*-|debt\s+valuation", re.I)
# A month-end portfolio calls itself one of these.
KEEP = re.compile(r"month\s*end\s*(active\s*)?portfolio|scheme\s*portfolio\s*details", re.I)


def fetch_year(year, session, retries=3):
    params = {"year": str(year), "category": "month end portfolio",
              "month": "", "type": "mf"}
    for attempt in range(retries):
        try:
            r = session.get(API, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json().get("results", [])
        except Exception as e:
            if attempt == retries - 1:
                print(f"  {year}: FAILED after {retries} tries -- {e}")
                return []
            time.sleep(2 * (attempt + 1))


def portfolio_month(title, path):
    """(year, month) of the PORTFOLIO. Title first, filename as fallback."""
    t = (title or "").strip().lower()
    m = re.search(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True))
                  + r")\b[^0-9]{0,4}(\d{4})", t)
    if m:
        return int(m.group(2)), MONTHS[m.group(1)]
    fn = os.path.basename(path).lower()
    m = re.search(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True))
                  + r")[-_ ]?(\d{4})", fn)
    if m:
        return int(m.group(2)), MONTHS[m.group(1)]
    m = re.search(r"(\d{2})(\d{2})(\d{4})", fn)      # 30062022
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(3)), int(m.group(2))
    return None


def classify(doc):
    title, path = doc.get("title", ""), doc.get("path", "")
    blob = f"{title} {os.path.basename(path)}"
    if SKIP.search(blob):
        return "skip"
    if KEEP.search(title or "") or KEEP.search(os.path.basename(path)):
        return "keep"
    return "unknown"


def verify(fp):
    """Read the 'Portfolio as on' line from inside the file."""
    try:
        rows = []
        if fp.lower().endswith(".xlsx"):
            import openpyxl
            wb = openpyxl.load_workbook(fp, read_only=True)
            ws = wb[wb.sheetnames[0]]
            for i, r in enumerate(ws.iter_rows(max_row=12, values_only=True)):
                rows += [str(c) for c in r if c]
                if i > 12:
                    break
            wb.close()
        else:
            import xlrd
            sh = xlrd.open_workbook(fp).sheet_by_index(0)
            for i in range(min(12, sh.nrows)):
                rows += [str(c) for c in sh.row_values(i) if c]
        blob = " ".join(rows).lower()
        m = re.search(r"as on\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True))
                      + r")\w*\s+\d{1,2},?\s*(\d{4})", blob)
        if m:
            return int(m.group(2)), MONTHS[m.group(1)]
        m = re.search(r"as on\s+\d{1,2}[-/ ]+(" +
                      "|".join(sorted(MONTHS, key=len, reverse=True)) +
                      r")\w*[-/ ]+(\d{4})", blob)
        if m:
            return int(m.group(2)), MONTHS[m.group(1)]
    except Exception as e:
        print(f"      could not read {os.path.basename(fp)}: {e}")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2020-2026", help="e.g. 2020-2026")
    ap.add_argument("--out", default="mo_downloads")
    ap.add_argument("--list", action="store_true",
                    help="show what would be downloaded, download nothing")
    ap.add_argument("--verify", action="store_true", default=True,
                    help="read the portfolio date from inside each file")
    a = ap.parse_args()

    y0, _, y1 = a.years.partition("-")
    years = range(int(y0), int(y1 or y0) + 1)
    os.makedirs(a.out, exist_ok=True)
    session = requests.Session()

    wanted, skipped, unknown = {}, 0, []
    for y in years:
        docs = fetch_year(y, session)
        print(f"{y}: {len(docs)} documents")
        for d in docs:
            kind = classify(d)
            if kind == "skip":
                skipped += 1
                continue
            pm = portfolio_month(d.get("title"), d.get("path"))
            if kind == "unknown":
                unknown.append((d.get("title", "").strip(), d.get("path")))
                continue
            if not pm:
                unknown.append((d.get("title", "").strip(), d.get("path")))
                continue
            # Later publishDate wins for the same portfolio month -- MO
            # occasionally republishes a corrected file.
            prev = wanted.get(pm)
            if prev is None or d.get("publishDate", "") > prev.get("publishDate", ""):
                wanted[pm] = d

    print(f"\n{len(wanted)} month-end portfolios, {skipped} fortnightly/"
          f"performance skipped, {len(unknown)} unclassified")

    if unknown:
        print("\nUNCLASSIFIED -- look at these by hand:")
        for t, p in unknown:
            print(f"   {t or '(no title)'}  <-  {os.path.basename(p)}")

    print(f"\n{'portfolio':10s} {'title':46s} file")
    for pm in sorted(wanted):
        d = wanted[pm]
        print(f"  {pm[0]}-{pm[1]:02d}   {d['title'].strip()[:44]:46s} "
              f"{os.path.basename(d['path'])[:46]}")

    have = set(wanted)
    allm = [(y, m) for y in years for m in range(1, 13)]
    gaps = [k for k in allm if k not in have and k <= max(have)]
    print(f"\nmonths with no month-end portfolio on the site: "
          f"{', '.join(f'{y}-{m:02d}' for y, m in gaps) or 'none'}")

    if a.list:
        print("\n--list given, nothing downloaded.")
        return

    print()
    mismatches = []
    for pm in sorted(wanted):
        d = wanted[pm]
        ext = os.path.splitext(d["path"])[1] or ".xlsx"
        dest = os.path.join(a.out, f"MO_{pm[0]}-{pm[1]:02d}{ext}")
        if os.path.exists(dest):
            print(f"  {os.path.basename(dest)} already here, skipping")
            continue
        url = BASE + d["path"]
        try:
            r = session.get(url, headers=HEADERS, timeout=120)
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
            print(f"  {os.path.basename(dest)}  ({len(r.content)//1024} KB)")
        except Exception as e:
            print(f"  FAILED {pm[0]}-{pm[1]:02d}: {e}")
            continue
        if a.verify:
            got = verify(dest)
            if got and got != pm:
                mismatches.append((dest, pm, got))
                print(f"     MISMATCH: saved as {pm[0]}-{pm[1]:02d} but the "
                      f"file says {got[0]}-{got[1]:02d}")
        time.sleep(1)          # be polite to their server

    if mismatches:
        print("\nFILES WHOSE CONTENTS DISAGREE WITH THEIR TITLE -- rename these "
              "before loading, and trust the CONTENTS:")
        for dest, pm, got in mismatches:
            print(f"   {os.path.basename(dest)}  is really "
                  f"{got[0]}-{got[1]:02d}")
    else:
        print("\nEvery downloaded file's contents matched its title.")


if __name__ == "__main__":
    main()
