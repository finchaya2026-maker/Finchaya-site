"""
load_benchmarks.py -- load NSE Total Return Index CSVs.
-------------------------------------------------------
Reads every .csv in a folder, works out which index each one holds from
its own IndexName column, and loads the values into benchmark_nav.

USAGE
    python load_benchmarks.py benchmarks
    python load_benchmarks.py benchmarks --dry-run

The files come from niftyindices.com -> Reports -> Historical Data, with
"Total returns Index Values" selected at the top. That selection matters:
a fund's NAV includes the dividends its holdings pay, so comparing it to
a price index would flatter every fund by around 1.5% a year. This script
refuses files that don't look like TRI.

RE-RUNNING IS SAFE. Existing (index, date) rows are left alone, so you
can drop in a fresh current-year file each month and only the new days
are added.
"""

import os
import re
import csv
import sys
import glob
import argparse
from datetime import datetime

import psycopg
from dotenv import load_dotenv

load_dotenv()
DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Check that .env exists in this folder.")


# Tidy display names. Anything not listed gets title-cased, so a new
# index still loads -- this only controls how it reads on the page.
DISPLAY = {
    "NIFTY 50":               "Nifty 50 TRI",
    "NIFTY 100":              "Nifty 100 TRI",
    "NIFTY 500":              "Nifty 500 TRI",
    "NIFTY MIDCAP 150":       "Nifty Midcap 150 TRI",
    "NIFTY SMALLCAP 250":     "Nifty Smallcap 250 TRI",
    "NIFTY LARGEMIDCAP 250":  "Nifty LargeMidcap 250 TRI",
}


def parse_date(value):
    value = (value or "").strip()
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_value(value):
    value = (value or "").replace(",", "").strip()
    if not value or value in ("-", "NA", "N.A."):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def read_file(path):
    """Return (index_name, [(date, value)]) or (None, reason)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return None, "empty file"

        # Match on header words, not position -- NSE has changed the
        # column order before and would do it silently.
        cols = {}
        for name in reader.fieldnames:
            low = (name or "").strip().lower()
            if "indexname" in low.replace(" ", ""):
                cols["name"] = name
            elif low == "date":
                cols["date"] = name
            elif "total returns" in low:
                cols["value"] = name
            elif "closing index value" in low or low == "close":
                # A price-return file. Accepting it would understate the
                # benchmark by roughly the dividend yield, every year,
                # in the direction that flatters every fund. Refuse it.
                cols["price_only"] = name

        if "value" not in cols:
            if "price_only" in cols:
                return None, ("PRICE RETURN file, not Total Return -- "
                              "re-download with 'Total returns Index Values' "
                              "selected at the top of the NSE page")
            return None, "no Total Returns column. headers: %s" % reader.fieldnames
        if "date" not in cols or "name" not in cols:
            return None, "missing Date or IndexName: %s" % reader.fieldnames

        names, rows, bad = set(), {}, 0
        for r in reader:
            idx = (r.get(cols["name"]) or "").strip().upper()
            d = parse_date(r.get(cols["date"]))
            v = parse_value(r.get(cols["value"]))
            if not idx or d is None or v is None:
                bad += 1
                continue
            names.add(idx)
            rows[d] = v            # last write wins within one file

        if not rows:
            return None, "no readable rows (%d unparsed)" % bad
        if len(names) > 1:
            return None, "file mixes several indices: %s" % sorted(names)

        return names.pop(), sorted(rows.items())


def ensure_benchmark(cur, index_name):
    cur.execute("SELECT benchmark_id FROM benchmark_master WHERE index_name = %s",
                (index_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    display = DISPLAY.get(index_name, index_name.title() + " TRI")
    cur.execute("""
        INSERT INTO benchmark_master (index_name, display_name, provider, is_tri)
        VALUES (%s, %s, 'NSE', TRUE) RETURNING benchmark_id
    """, (index_name, display))
    print("   + new index registered: %s" % display)
    return cur.fetchone()[0]


def insert_values(cur, benchmark_id, rows):
    cur.execute("""
        CREATE TEMP TABLE IF NOT EXISTS bm_stage
            (index_date date, index_value numeric) ON COMMIT DROP
    """)
    cur.execute("TRUNCATE bm_stage")
    with cur.copy("COPY bm_stage (index_date, index_value) FROM STDIN") as cp:
        for d, v in rows:
            cp.write_row((d, v))
    cur.execute("""
        INSERT INTO benchmark_nav (benchmark_id, index_date, index_value)
        SELECT %s, index_date, index_value FROM bm_stage
        ON CONFLICT (benchmark_id, index_date) DO NOTHING
    """, (benchmark_id,))
    return cur.rowcount


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="folder holding the NSE CSVs")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.folder, "**", "*.csv"),
                             recursive=True))
    if not paths:
        sys.exit("No .csv files found under %s" % args.folder)
    print("Found %d file(s) under %s\n" % (len(paths), args.folder))

    parsed, failures = [], []
    for path in paths:
        name, result = read_file(path)
        if name is None:
            failures.append((os.path.basename(path), result))
            print("  SKIP %-58s %s" % (os.path.basename(path)[:58], result))
        else:
            parsed.append((name, result))

    if not parsed:
        sys.exit("\nNothing readable. Stopping.")

    # Group by index so each one is a single insert and a single summary.
    by_index = {}
    for name, rows in parsed:
        by_index.setdefault(name, {}).update(dict(rows))

    print()
    if args.dry_run:
        for name, rows in sorted(by_index.items()):
            days = sorted(rows)
            print("%-26s %5d days  %s .. %s (not written)"
                  % (name, len(days), days[0], days[-1]))
        print("\nDRY RUN -- nothing written.")
        return

    total_new = 0
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            for name, rowmap in sorted(by_index.items()):
                rows = sorted(rowmap.items())
                bid = ensure_benchmark(cur, name)
                new = insert_values(cur, bid, rows)
                total_new += new

                cur.execute("""
                    UPDATE benchmark_master SET
                      first_date = (SELECT MIN(index_date) FROM benchmark_nav
                                     WHERE benchmark_id = %(id)s),
                      last_date  = (SELECT MAX(index_date) FROM benchmark_nav
                                     WHERE benchmark_id = %(id)s),
                      day_count  = (SELECT COUNT(*) FROM benchmark_nav
                                     WHERE benchmark_id = %(id)s),
                      updated_at = NOW()
                    WHERE benchmark_id = %(id)s
                """, {"id": bid})

                print("%-26s %5d days in files, %5d new  (%s .. %s)"
                      % (name, len(rows), new, rows[0][0], rows[-1][0]))
            conn.commit()

        print("\n%d new rows added.\n" % total_new)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT display_name, day_count, first_date, last_date
                FROM benchmark_master ORDER BY display_name
            """)
            print("benchmark_master now holds:")
            for display, days, first, last in cur.fetchall():
                print("  %-28s %5s days  %s .. %s" % (display, days, first, last))

            # A year with far fewer than ~245 trading days means a file
            # is missing. Silent gaps break any period that spans them.
            cur.execute("""
                SELECT m.display_name, EXTRACT(YEAR FROM n.index_date)::int AS yr,
                       COUNT(*) AS days
                FROM benchmark_nav n JOIN benchmark_master m USING (benchmark_id)
                GROUP BY 1, 2 HAVING COUNT(*) < 200
                ORDER BY 1, 2
            """)
            thin = cur.fetchall()
            if thin:
                print("\nYEARS THAT LOOK INCOMPLETE (expect ~245 trading days):")
                for display, yr, days in thin:
                    print("  %-28s %d  only %d days" % (display, yr, days))
                print("  A current or partial year is fine. Any other year "
                      "means a missing file.")

    if failures:
        print("\n%d file(s) skipped:" % len(failures))
        for fname, why in failures:
            print("  %-58s %s" % (fname[:58], why))


if __name__ == "__main__":
    main()
