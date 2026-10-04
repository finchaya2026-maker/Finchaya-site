"""
load_stock_master.py
--------------------
Downloads the NSE equity list and loads it into the stock_master table.

    /opt/mfapi/venv/bin/python3 /opt/mfapi/load_stock_master.py
    /opt/mfapi/venv/bin/python3 /opt/mfapi/load_stock_master.py --dry-run
    /opt/mfapi/venv/bin/python3 /opt/mfapi/load_stock_master.py --file /tmp/EQUITY_L.csv

Safe to run repeatedly: existing stocks are UPDATED, new ones INSERTED.
Nothing is ever duplicated.

WHERE THIS SITS IN THE MONTHLY RUN
    It must come BEFORE promote_holdings.py, not after. mf_holding has a
    foreign key to stock_master and promote uses JOIN, not LEFT JOIN, so
    any holding whose ISIN is unknown is DROPPED -- silently, and in a way
    that makes coverage_pct read HIGHER than the truth, because the lost
    position is not even in the denominator.

        parse_holdings -> load_holdings_raw -> THIS -> promote_holdings
        -> score_stocks -> score_funds

WHY THERE IS NO "LISTED IN THE LAST N DAYS" FILTER
    Because a window keyed on the run cadence has no catch-up. Skip one
    month -- a failed job, a blocked download, a busy week -- and every
    stock that listed inside the gap falls outside the window forever,
    with nothing to say so.

    The full list is ~2,100 rows. Fetching all of it and letting
    ON CONFLICT sort out what is new costs nothing and is self-healing: a
    missed run just means the next run catches up. The set difference IS
    the filter.

WHAT IT WILL NEVER FIX
    AMCs legitimately hold UNLISTED companies -- pre-IPO stakes in the
    likes of Symbiotec Pharmalab, Tempsens Instruments, Skyways Air
    Services. They are not on NSE, have no price history, and cannot be
    scored. Expect a permanent residue of such names in
    missing_stocks.py. The residue should be STABLE; a sudden jump means
    this download broke, not that funds bought 200 new companies.

CHANGES FROM THE LAPTOP VERSION
    1. .env is read by absolute path. A bare load_dotenv() reads the
       CURRENT directory, so under cron (cwd=/root) DB came back None.
       This is the single change that makes it a server job.
    2. The hand-download fallback path is no longer C:\\finchaya, and
       --file lets you point at a file you fetched any other way.
    3. A row-count floor. raise_for_status() catches a 403, but NOT a
       200 that returns a truncated file -- that would upsert 40 rows,
       log SUCCESS, and look exactly like a good run.
    4. It reports WHAT IS NEW, and which of the new names the current
       holdings are actually waiting on. "Loaded 2,143 stocks" is the
       same message whether 0 or 40 of them are new.
    5. --dry-run, so the monthly job can be rehearsed against the live
       database without writing to it.
"""

import argparse
import csv
import io
import os
import sys
from datetime import datetime

import psycopg
import requests
from dotenv import load_dotenv

# ABSOLUTE PATH, DELIBERATELY. See note 1 above.
load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")

NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

# If the download fails, put the file here by hand and re-run, or pass
# --file. On the droplet /tmp is the sane place; the laptop copy of this
# script pointed at C:\finchaya, which is meaningless here.
LOCAL_FALLBACK = "/tmp/EQUITY_L.csv"

# NSE 'series' codes. EQ = normal trading. BE = trade-for-trade (usually
# smaller / more volatile names). Everything else is rights, warrants,
# partly-paid shares etc., which are not what you want to score.
KEEP_SERIES = {"EQ", "BE"}

# NSE has listed well over 2,000 EQ+BE names for years. Anything under
# this is a truncated or error response wearing a CSV's clothes, and
# must not be allowed to reach the database. See note 3 above.
MIN_EXPECTED_ROWS = 1500


# ---------------------------------------------------------------------
# 1. GET THE FILE
# ---------------------------------------------------------------------
def download_nse_csv():
    """NSE blocks plain scripts. We pretend to be a browser: first visit
    the homepage so the server hands us cookies, then request the file
    using those same cookies."""

    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36"),
        "Accept": "text/csv,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })

    # Warm-up call: we throw away the response, we only want the cookies.
    session.get("https://www.nseindia.com/", timeout=20)

    response = session.get(NSE_URL, timeout=30)
    response.raise_for_status()
    return response.content.decode("utf-8-sig")


def read_local_csv(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def get_csv_text(explicit_path):
    if explicit_path:
        print("Reading %s" % explicit_path)
        return read_local_csv(explicit_path)
    try:
        print("Downloading from NSE...")
        text = download_nse_csv()
        print("Download OK.")
        return text
    except Exception as e:
        print("Download failed: %s" % e)
        print("Trying local file: %s" % LOCAL_FALLBACK)
        try:
            text = read_local_csv(LOCAL_FALLBACK)
            print("Local file loaded OK.")
            return text
        except FileNotFoundError:
            print("\nNo local file either.")
            print("FIX: open this URL in a browser, save the file, copy it")
            print("     to the server as %s, then re-run:" % LOCAL_FALLBACK)
            print("     %s" % NSE_URL)
            sys.exit(1)


# ---------------------------------------------------------------------
# 2. PARSE IT
# ---------------------------------------------------------------------
def parse_date(value):
    """NSE writes dates like '06-OCT-2008'. Returns None if unparseable."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%b-%Y").date()
    except ValueError:
        return None


def parse_number(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_rows(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text))

    # NSE puts a space after each comma in the header row, so the column
    # names arrive as ' SERIES', ' ISIN NUMBER' etc. Strip them.
    if not reader.fieldnames:
        sys.exit("The file has no header row. It is not EQUITY_L.csv.")
    reader.fieldnames = [name.strip() for name in reader.fieldnames]

    # Fail on the SHAPE, not on the row count alone. An HTML error page
    # parses as a CSV with one nonsense column and zero usable rows, and
    # the message "0 stocks parsed" does not tell you that is what
    # happened.
    needed = {"SYMBOL", "ISIN NUMBER", "SERIES"}
    missing = needed - set(reader.fieldnames)
    if missing:
        print("Columns found: %s" % reader.fieldnames)
        sys.exit("Not an NSE equity list -- missing %s. NSE probably "
                 "returned an error page with a 200 status." % sorted(missing))

    rows = []
    skipped_series = 0
    skipped_no_isin = 0

    # Dedupe as we go. A stock can appear under more than one series, and
    # our table allows only one row per ISIN and one per symbol.
    seen_isin = set()
    seen_symbol = set()

    for raw in reader:
        clean = {k: (v.strip() if isinstance(v, str) else v)
                 for k, v in raw.items()}

        series = clean.get("SERIES", "")
        if series not in KEEP_SERIES:
            skipped_series += 1
            continue

        isin = clean.get("ISIN NUMBER", "")
        symbol = clean.get("SYMBOL", "")

        if not isin or not symbol:
            skipped_no_isin += 1
            continue

        if isin in seen_isin or symbol in seen_symbol:
            continue

        seen_isin.add(isin)
        seen_symbol.add(symbol)

        rows.append((
            isin,
            symbol,
            clean.get("NAME OF COMPANY", ""),
            "NSE",
            parse_date(clean.get("DATE OF LISTING")),
            parse_number(clean.get("FACE VALUE")),
            parse_number(clean.get("PAID UP VALUE")),
        ))

    print("Parsed %d stocks (skipped %d non-EQ/BE, %d missing ISIN)"
          % (len(rows), skipped_series, skipped_no_isin))

    if len(rows) < MIN_EXPECTED_ROWS:
        sys.exit("Only %d stocks parsed, expected at least %d. Refusing to "
                 "load a truncated file -- a partial upsert would log "
                 "SUCCESS and look like a good run."
                 % (len(rows), MIN_EXPECTED_ROWS))

    return rows


# ---------------------------------------------------------------------
# 3. WRITE TO THE DATABASE
# ---------------------------------------------------------------------
# industry is deliberately NOT in the update list. backfill_industry.py
# curates it from recent AMC filings and handles reclassified companies
# (Century Textiles -> Aditya Birla Real Estate); overwriting it here
# every month would quietly undo that work.
UPSERT = """
INSERT INTO stock_master
    (isin, symbol, company_name, exchange, listing_date,
     face_value, paid_up_value, is_active, updated_at)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, TRUE, NOW())
ON CONFLICT (isin) DO UPDATE SET
    symbol        = EXCLUDED.symbol,
    company_name  = EXCLUDED.company_name,
    listing_date  = EXCLUDED.listing_date,
    face_value    = EXCLUDED.face_value,
    paid_up_value = EXCLUDED.paid_up_value,
    is_active     = TRUE,
    updated_at    = NOW()
"""


def report_new(cur, rows):
    """Which of these ISINs the table does not have yet -- and which of
    THOSE the current holdings are actually waiting on.

    Read before the upsert, so it describes the change rather than the
    result. "Loaded 2,143 stocks" is the same sentence whether 0 or 40
    of them are new, which makes a monthly job impossible to judge."""

    cur.execute("SELECT isin FROM stock_master")
    have = {r[0] for r in cur.fetchall()}
    new = [r for r in rows if r[0] not in have]

    print("\n%d stocks in the file are NOT yet in stock_master." % len(new))
    if not new:
        return new

    new_isins = [r[0] for r in new]

    # Does anything actually hold them? A new listing nobody owns can
    # wait; one that 59 funds hold is a hole in 59 portfolios.
    cur.execute("""
        SELECT r.isin, count(DISTINCT r.amc_fund_name) AS funds
        FROM mf_holding_raw r
        WHERE r.isin = ANY(%s)
          AND r.as_of_date = (SELECT max(as_of_date) FROM mf_holding_raw)
        GROUP BY 1
    """, (new_isins,))
    held = dict(cur.fetchall())

    new.sort(key=lambda r: (-held.get(r[0], 0), r[4] or datetime(1900, 1, 1).date()))

    print("\n%-14s %-12s %-40s %-11s %s"
          % ("isin", "symbol", "company", "listed", "funds holding"))
    for r in new[:40]:
        print("%-14s %-12s %-40s %-11s %s"
              % (r[0], r[1][:12], (r[2] or "")[:40], r[4] or "-",
                 held.get(r[0], "")))
    if len(new) > 40:
        print("... and %d more." % (len(new) - 40))

    wanted = sum(1 for r in new if r[0] in held)
    if wanted:
        print("\n%d of them are held in the latest staged portfolios. Those "
              "positions are being dropped by promote_holdings.py right now."
              % wanted)
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    ap.add_argument("--file", help="read this CSV instead of downloading")
    args = ap.parse_args()

    if not DB:
        sys.exit("FINCHAYA_DB is not set. Is /opt/mfapi/.env present?")

    rows = parse_rows(get_csv_text(args.file))

    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            new = report_new(cur, rows)

            if args.dry_run:
                print("\n--dry-run: nothing was written.")
                return

            # Open a run-log entry so a silent failure leaves a trace
            cur.execute("""
                INSERT INTO batch_run_log (run_type, business_date, status)
                VALUES ('LOAD_STOCK_MASTER', CURRENT_DATE, 'RUNNING')
                RETURNING run_id
            """)
            run_id = cur.fetchone()[0]
            conn.commit()

            try:
                cur.executemany(UPSERT, rows)

                cur.execute("""
                    UPDATE batch_run_log
                       SET status = 'SUCCESS',
                           finished_at = NOW(),
                           rows_read = %s,
                           rows_written = %s
                     WHERE run_id = %s
                """, (len(rows), len(rows), run_id))
                conn.commit()

                print("\nLoaded %d stocks (%d new). run_id = %d"
                      % (len(rows), len(new), run_id))

            except Exception as e:
                conn.rollback()
                cur.execute("""
                    UPDATE batch_run_log
                       SET status = 'FAILED',
                           finished_at = NOW(),
                           error_message = %s
                     WHERE run_id = %s
                """, (str(e)[:2000], run_id))
                conn.commit()
                print("FAILED: %s" % e)
                sys.exit(1)

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_master")
            print("stock_master now holds %d rows" % cur.fetchone()[0])

    if new:
        print("\nNEXT: re-run promote_holdings.py for the current month so "
              "the newly known positions are picked up, then score_stocks.py "
              "and score_funds.py.")


if __name__ == "__main__":
    main()
