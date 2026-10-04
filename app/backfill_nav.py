"""
backfill_nav.py -- one-time historical NAV load.
------------------------------------------------
load_mf_schemes.py keeps mf_nav current from today onward. It cannot look
backwards: AMFI's daily file holds one day. This fills in the years behind
it, so trailing 1/3/5/10-year returns have something to stand on.

WHERE THE DATA COMES FROM
    AMFI publishes a historical NAV report that takes a date range and
    returns every scheme's NAV for those days, in the same semicolon
    format as the daily file. It is public data, served without session
    tokens or bot checks -- the source is meant to be read this way.

USAGE
    python backfill_nav.py                      last 10 years, scored funds
    python backfill_nav.py --from 2016-01        from a given month
    python backfill_nav.py --all                every scheme, not just scored
    python backfill_nav.py --dry-run            fetch and parse, write nothing

SAFE TO STOP AND RESTART. Completed months are recorded in
backfill_progress.txt; a second run skips them. Delete that file to redo.

THIS TAKES A WHILE. Ten years is 120 requests against someone else's
server, so it is deliberately unhurried -- roughly an hour. Leave it
running.
"""

import os
import re
import sys
import time
import argparse
from datetime import date, datetime, timedelta

import psycopg
import requests
from dotenv import load_dotenv

load_dotenv()
DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Check that .env exists in this folder.")

HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
PROGRESS_FILE = "backfill_progress.txt"

# Be a good guest. This is a free public service and there is no hurry.
PAUSE_SECONDS = 3
TIMEOUT = 180


# ---------------------------------------------------------------------
# Column detection, same approach as load_mf_schemes.py: match on header
# words rather than position. The historical report puts its columns in a
# different order from the daily file and adds repurchase/sale price, so
# reading by position would quietly load the wrong numbers.
# ---------------------------------------------------------------------
def build_column_map(header_line):
    names = [p.strip().lower() for p in header_line.split(";")]
    mapping = {}
    for i, name in enumerate(names):
        if "scheme code" in name:
            mapping["code"] = i
        elif "net asset value" in name or name == "nav":
            mapping["nav"] = i
        elif name == "date" or name.endswith(" date"):
            mapping["date"] = i
    return mapping, names


def parse_date(value):
    value = (value or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_nav(value):
    value = (value or "").strip()
    if not value or value.upper() in ("N.A.", "NA", "-", "NULL", "#N/A"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_rows(text, wanted_codes):
    """Return [(code, nav_date, nav)] for codes we care about.

    wanted_codes is a set; None means keep everything.
    """
    rows = {}
    cols = None
    seen = skipped_unknown = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line or ";" not in line:
            continue                      # AMC and category heading lines

        parts = [p.strip() for p in line.split(";")]

        if cols is None and not parts[0].isdigit():
            cols, names = build_column_map(line)
            missing = {"code", "nav", "date"} - set(cols)
            if missing:
                print("   ! header found but columns missing: %s" % missing)
                print("     header was: %s" % names)
                return None, 0, 0
            continue

        if cols is None or not parts[0].isdigit():
            continue
        if len(parts) <= max(cols.values()):
            continue                      # ragged line

        code = parts[cols["code"]]
        seen += 1

        if wanted_codes is not None and code not in wanted_codes:
            skipped_unknown += 1
            continue

        nav = parse_nav(parts[cols["nav"]])
        nav_date = parse_date(parts[cols["date"]])
        if nav is None or nav_date is None:
            continue

        # Same key as the table, so duplicates inside one file collapse here
        # rather than being sent to the database to be rejected.
        rows[(code, nav_date)] = (code, nav_date, nav)

    return list(rows.values()), seen, skipped_unknown


# ---------------------------------------------------------------------
def month_chunks(start, end):
    """Yield (first_day, last_day) for each month from start to end.

    AMFI limits how wide a range it will serve, so ask month by month.
    A month is also a natural resume point.
    """
    cur = date(start.year, start.month, 1)
    while cur <= end:
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        yield cur, min(nxt - timedelta(days=1), end)
        cur = nxt


def amfi_fmt(d):
    return d.strftime("%d-%b-%Y")


def fetch_month(first, last, session):
    params = {"frmdt": amfi_fmt(first), "todt": amfi_fmt(last)}
    r = session.get(HISTORY_URL, params=params, timeout=TIMEOUT,
                    headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------
def load_progress():
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE) as f:
        return {line.strip() for line in f if line.strip()}


def mark_done(tag):
    with open(PROGRESS_FILE, "a") as f:
        f.write(tag + "\n")


# ---------------------------------------------------------------------
# Insert via a TEMP table + COPY. executemany for a few million rows takes
# hours over a network; COPY takes seconds. The INSERT..SELECT afterwards
# is what applies ON CONFLICT, which COPY cannot do on its own.
# ---------------------------------------------------------------------
def insert_rows(conn, rows):
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS nav_stage
              (scheme_code varchar, nav_date date, nav numeric)
            ON COMMIT DROP
        """)
        cur.execute("TRUNCATE nav_stage")

        with cur.copy("COPY nav_stage (scheme_code, nav_date, nav) FROM STDIN") as cp:
            for row in rows:
                cp.write_row(row)

        cur.execute("""
            INSERT INTO mf_nav (scheme_code, nav_date, nav)
            SELECT s.scheme_code, s.nav_date, s.nav
            FROM nav_stage s
            JOIN mf_scheme m ON m.scheme_code = s.scheme_code
            ON CONFLICT (scheme_code, nav_date) DO NOTHING
        """)
        return cur.rowcount


def known_codes(conn, everything):
    """Which schemes to keep.

    Default is the funds we actually score -- those with promoted holdings.
    Ten years of all 14,000 schemes is tens of millions of rows for data
    no page will ever read. --all overrides that.
    """
    with conn.cursor() as cur:
        if everything:
            cur.execute("SELECT scheme_code FROM mf_scheme")
        else:
            cur.execute("""
                SELECT DISTINCT scheme_code FROM mf_scheme_map
                UNION
                SELECT DISTINCT scheme_code FROM mf_score
            """)
        return {r[0] for r in cur.fetchall()}


# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", help="start month, YYYY-MM")
    ap.add_argument("--to", dest="to", help="end month, YYYY-MM")
    ap.add_argument("--all", action="store_true",
                    help="every scheme, not just the ones we score")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and parse, write nothing")
    args = ap.parse_args()

    today = date.today()
    start = (datetime.strptime(args.frm, "%Y-%m").date() if args.frm
             else date(today.year - 10, today.month, 1))
    end = (datetime.strptime(args.to, "%Y-%m").date() if args.to else today)

    print("Backfilling NAV from %s to %s" % (start.strftime("%b %Y"),
                                             end.strftime("%b %Y")))
    if args.dry_run:
        print("DRY RUN -- nothing will be written\n")

    done = load_progress()
    if done:
        print("Resuming: %d month(s) already done\n" % len(done))

    with psycopg.connect(DB) as conn:
        codes = None if args.all else known_codes(conn, args.all)
        if codes is not None:
            print("Keeping %d scheme codes (use --all for every scheme)\n"
                  % len(codes))
            if not codes:
                sys.exit("No scored schemes found. Promote some holdings first, "
                         "or run with --all.")

        session = requests.Session()
        total_written = 0
        chunks = list(month_chunks(start, end))

        for n, (first, last) in enumerate(chunks, 1):
            tag = first.strftime("%Y-%m")
            label = "[%3d/%d] %s" % (n, len(chunks), first.strftime("%b %Y"))

            if tag in done:
                print("%s  already done, skipping" % label)
                continue

            try:
                text = fetch_month(first, last, session)
            except Exception as e:
                print("%s  FETCH FAILED: %s" % (label, e))
                print("        stopping here -- rerun to resume from this month")
                break

            parsed = parse_rows(text, codes)
            if parsed[0] is None:
                print("%s  could not read the file, stopping" % label)
                break
            rows, seen, skipped = parsed

            if not rows:
                # Genuinely empty months exist: AMFI has no data before a
                # scheme launched, and very old ranges may return nothing.
                print("%s  %6d lines, no rows kept" % (label, seen))
                if not args.dry_run:
                    mark_done(tag)
                time.sleep(PAUSE_SECONDS)
                continue

            if args.dry_run:
                print("%s  %6d lines -> %6d rows (not written)"
                      % (label, seen, len(rows)))
            else:
                written = insert_rows(conn, rows)
                conn.commit()
                total_written += written
                mark_done(tag)
                print("%s  %6d lines -> %6d rows, %6d new  (total %d)"
                      % (label, seen, len(rows), written, total_written))

            time.sleep(PAUSE_SECONDS)

        print("\nDone. %d rows added." % total_written)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), MIN(nav_date), MAX(nav_date),
                       COUNT(DISTINCT scheme_code)
                FROM mf_nav
            """)
            rows, mn, mx, schemes = cur.fetchone()
            print("mf_nav now: %d rows, %s to %s, %d schemes"
                  % (rows, mn, mx, schemes))

            # A fund with only a handful of dates cannot produce a trailing
            # return. Worth knowing before the returns job runs.
            cur.execute("""
                SELECT COUNT(*) FROM (
                    SELECT scheme_code FROM mf_nav
                    GROUP BY scheme_code HAVING COUNT(*) >= 250
                ) t
            """)
            print("schemes with 250+ days of history: %d" % cur.fetchone()[0])


if __name__ == "__main__":
    main()
