"""
load_stock_master.py
--------------------
Downloads the NSE equity list and loads it into the stock_master table.

Run this whenever you want to refresh the stock list (weekly is plenty --
companies don't list and delist very often).

Safe to run repeatedly: existing stocks are UPDATED, new ones INSERTED.
Nothing is ever duplicated.
"""

import csv
import io
import os
import sys
from datetime import datetime

import psycopg
import requests

# ---------------------------------------------------------------------
# SETTINGS -- change the password to yours
# ---------------------------------------------------------------------
# DB = "host=localhost port=5432 dbname=finchaya user=postgres password=..."
from dotenv import load_dotenv
load_dotenv()
DB = os.getenv("FINCHAYA_DB")
NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

# If the download fails, put the file here by hand and re-run.
LOCAL_FALLBACK = r"C:\finchaya\EQUITY_L.csv"

# NSE 'series' codes. EQ = normal trading. BE = trade-for-trade (usually
# smaller / more volatile names). Everything else is rights, warrants,
# partly-paid shares etc., which are not what you want to score.
KEEP_SERIES = {"EQ", "BE"}


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


def read_local_csv():
    with open(LOCAL_FALLBACK, "r", encoding="utf-8-sig") as f:
        return f.read()


def get_csv_text():
    try:
        print("Downloading from NSE...")
        text = download_nse_csv()
        print("Download OK.")
        return text
    except Exception as e:
        print(f"Download failed: {e}")
        print(f"Trying local file: {LOCAL_FALLBACK}")
        try:
            text = read_local_csv()
            print("Local file loaded OK.")
            return text
        except FileNotFoundError:
            print("\nNo local file either.")
            print("FIX: open this URL in Chrome, save the file to")
            print(f"     {LOCAL_FALLBACK}, then run this script again:")
            print(f"     {NSE_URL}")
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
    reader.fieldnames = [name.strip() for name in reader.fieldnames]

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

    print(f"Parsed {len(rows)} stocks "
          f"(skipped {skipped_series} non-EQ/BE, {skipped_no_isin} missing ISIN)")
    return rows


# ---------------------------------------------------------------------
# 3. WRITE TO THE DATABASE
# ---------------------------------------------------------------------
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


def main():
    rows = parse_rows(get_csv_text())

    if not rows:
        print("Nothing to load. Stopping without touching the database.")
        sys.exit(1)

    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:

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

                print(f"Loaded {len(rows)} stocks. run_id = {run_id}")

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
                print(f"FAILED: {e}")
                sys.exit(1)

        # Show what's actually in the table now
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_master")
            print(f"stock_master now holds {cur.fetchone()[0]} rows")

            cur.execute("""
                SELECT symbol, company_name, isin, listing_date
                FROM stock_master
                ORDER BY symbol
                LIMIT 5
            """)
            print("\nSample:")
            for r in cur.fetchall():
                print(" ", r)


if __name__ == "__main__":
    main()
