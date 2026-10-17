"""
load_stock_master.py
--------------------
Keeps stock_master in step with NSE's list of listed equities (EQUITY_L.csv).

    /opt/mfapi/venv/bin/python3 /opt/mfapi/load_stock_master.py
    /opt/mfapi/venv/bin/python3 /opt/mfapi/load_stock_master.py --dry-run
    /opt/mfapi/venv/bin/python3 /opt/mfapi/load_stock_master.py --file /tmp/EQUITY_L.csv

RUNS NIGHTLY (nightly.sh, before the candle fetch) AND MONTHLY (monthly.sh).
A stock that lists tomorrow is in the table the same night, and the candle
fetch that follows picks it up. Nothing has to be remembered or run by hand.

Safe to run repeatedly: existing stocks are UPDATED, new ones INSERTED.
Nothing is ever duplicated.

WHAT WENT WRONG BEFORE, AND WHAT THIS DOES ABOUT IT  (5 Oct 2026)
    The last run, on 11 Sep 2026, FAILED:

        duplicate key value violates unique constraint
        "ux_stock_symbol_exchange"  Key (symbol, exchange)=(TCC, NSE)

    TCC Concept is still trading under its old ISIN (INE887D01016), but NSE's
    list now carries a different ISIN under the same symbol -- what a share
    split or face-value change produces, since the company is issued a new
    ISIN. The table allows one row per symbol, the write was one big
    statement, and so that ONE row rolled back EVERYTHING. Nothing new has
    been loaded since 25 Aug 2026 while 161 stocks listed in the last 60 days.

    A job that fails whole on a single awkward row is no good run nightly. It
    now sorts the rows into three piles BEFORE writing:

      CLEAN       nothing in the way. Written.
      SUPERSEDED  the symbol is held by an ISIN that is NOT in NSE's list
                  today, so that row is the stale one (an ISIN replaced by a
                  split, or a delisting whose symbol was reused). It is set
                  inactive and its symbol renamed "SYM~ISIN" to free the
                  symbol; its candles and history stay under its own ISIN.
                  The new ISIN is then written.
      CLASHING    the symbol is held by an ISIN that IS still in NSE's list
                  (a symbol swap between two live stocks). Not guessed at:
                  skipped, reported, and picked up on a later night once the
                  other row has moved.

    One awkward row can no longer stop the other hundred being loaded.

WHAT IT DOES NOT DO
    It never deactivates a stock just because it is missing from NSE's list.
    It reports those (a stock can drop off the list for reasons that are not a
    delisting). A superseded row above is the one exception, and only because
    its symbol is wanted by a live stock.

    It does not fill sector, industry or cap class for a new stock. Those come
    from backfill_industry.py and the AMFI cap-class load, so a new listing
    shows without a sector until they next run.

WHERE THIS SITS IN THE MONTHLY RUN
    It must come BEFORE promote_holdings.py, not after. mf_holding has a
    foreign key to stock_master and promote uses JOIN, not LEFT JOIN, so
    any holding whose ISIN is unknown is DROPPED -- silently, and in a way
    that makes coverage_pct read HIGHER than the truth, because the lost
    position is not even in the denominator.

        parse_holdings -> load_holdings_raw -> THIS -> promote_holdings
        -> score_stocks -> score_funds

WHY THERE IS NO "LISTED IN THE LAST N DAYS" FILTER
    Because a window keyed on the run cadence has no catch-up. Skip a run --
    a failed job, a blocked download, a busy week -- and every stock that
    listed inside the gap falls outside the window forever, with nothing to
    say so. The full list is ~2,500 rows. Fetching all of it and letting the
    comparison sort out what is new costs nothing and is self-healing.

WHAT IT WILL NEVER FIX
    AMCs legitimately hold UNLISTED companies -- pre-IPO stakes in the
    likes of Symbiotec Pharmalab, Tempsens Instruments, Skyways Air
    Services. They are not on NSE, have no price history, and cannot be
    scored. Expect a permanent residue of such names in
    missing_stocks.py. The residue should be STABLE; a sudden jump means
    this download broke, not that funds bought 200 new companies.

THE RUN LOG
    Writes a LOAD_STOCK_MASTER row to batch_run_log and always closes it,
    SUCCESS / PARTIAL (something was skipped) / FAILED. Exit code is 0 only
    for SUCCESS, so nightly.sh reports anything else.
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

# Both, deliberately. Under cron the working directory is not this folder, so
# the absolute path is what makes it a server job; the relative one is what
# lets the same file be tested from a laptop.
HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv("/opt/mfapi/.env")
load_dotenv(os.path.join(HERE, ".env"))
DB = os.getenv("FINCHAYA_DB")

NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

# If the download fails, put the file here by hand and re-run, or pass
# --file. On the droplet /tmp is the sane place.
LOCAL_FALLBACK = "/tmp/EQUITY_L.csv"

# NSE 'series' codes. EQ = normal trading. BE = trade-for-trade (usually
# smaller / more volatile names). Everything else is rights, warrants,
# partly-paid shares etc., which are not what you want to score.
KEEP_SERIES = {"EQ", "BE"}

# NSE has listed well over 2,000 EQ+BE names for years. Anything under
# this is a truncated or error response wearing a CSV's clothes, and
# must not be allowed to reach the database.
MIN_EXPECTED_ROWS = 1500

EXCHANGE = "NSE"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------
# 1. GET THE FILE
# ---------------------------------------------------------------------
def download_nse_csv():
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/csv,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })

    # nsearchives serves this file with no cookies -- the daily price file
    # sits on the same host and loads the same way from the droplet. So ask
    # directly first. The homepage visit below is the fallback, not the first
    # move: it is a second request to a slower, more guarded host, and when
    # it timed out it used to take the whole download down with it.
    try:
        r = session.get(NSE_URL, timeout=30)
        if r.status_code == 200:
            return r.content.decode("utf-8-sig")
        print("Direct download answered HTTP %s; trying with a browser "
              "session." % r.status_code)
    except requests.RequestException as e:
        print("Direct download failed (%s); trying with a browser session."
              % type(e).__name__)

    session.get("https://www.nseindia.com/", timeout=20)   # cookies only
    r = session.get(NSE_URL, timeout=30)
    r.raise_for_status()
    return r.content.decode("utf-8-sig")


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
    except Exception as e:                                   # noqa: BLE001
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
    # the message "0 stocks parsed" does not tell you that is what happened.
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
            EXCHANGE,
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
# 3. DECIDE WHAT TO WRITE
# ---------------------------------------------------------------------
def plan_writes(existing, rows):
    """Sort rows into clean / superseded / clashing BEFORE touching the table.

    existing: [(isin, symbol)] currently in stock_master for this exchange.
    rows:     parsed rows from NSE's list.

    Returns (clean_rows, retire, clashing) where
      clean_rows  rows safe to write (includes those whose symbol is freed
                  by a retirement)
      retire      [(old_isin, old_symbol, new_isin)] rows to deactivate and
                  rename so their symbol can be reused
      clashing    [(row, holder_isin)] rows skipped because the holder is
                  itself still listed
    """
    holder_of = {sym: isin for isin, sym in existing}
    listed = {r[0] for r in rows}

    clean, retire, clashing = [], [], []
    for row in rows:
        isin, symbol = row[0], row[1]
        holder = holder_of.get(symbol)
        if holder is None or holder == isin:
            clean.append(row)
        elif holder not in listed:
            # The holder is not in NSE's list today: it is the stale one.
            retire.append((holder, symbol, isin))
            clean.append(row)
        else:
            clashing.append((row, holder))
    return clean, retire, clashing


def retired_symbol(symbol, isin):
    """The name a superseded row is parked under. The ISIN makes it unique;
    the symbol part is cut so the whole fits the 30-character column."""
    return "%s~%s" % (symbol[:16], isin)


# ---------------------------------------------------------------------
# 4. WRITE TO THE DATABASE
# ---------------------------------------------------------------------
# industry is deliberately NOT in the update list. backfill_industry.py
# curates it from recent AMC filings and handles reclassified companies
# (Century Textiles -> Aditya Birla Real Estate); overwriting it here
# every run would quietly undo that work.
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

RETIRE = """
UPDATE stock_master
   SET is_active = FALSE, symbol = %s, updated_at = NOW()
 WHERE isin = %s AND exchange = %s
"""


def write_rows(conn, clean, retire):
    """Retire the superseded rows, then upsert. Returns (written, failed)
    where failed is [(row, reason)] for anything the table still refused.

    The bulk write is tried first. If the table rejects it anyway -- a
    constraint nobody planned for -- it falls back to one row at a time in
    their own savepoints, so a single refusal still cannot take the rest down.
    """
    with conn.cursor() as cur:
        for old_isin, old_symbol, _new in retire:
            cur.execute(RETIRE, (retired_symbol(old_symbol, old_isin),
                                 old_isin, EXCHANGE))
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(UPSERT, clean)
        return len(clean), []
    except psycopg.errors.IntegrityError as e:
        print("Bulk write refused (%s). Retrying one row at a time."
              % str(e).splitlines()[0])

    written, failed = 0, []
    for row in clean:
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(UPSERT, row)
            written += 1
        except psycopg.errors.IntegrityError as e:
            failed.append((row, str(e).splitlines()[0]))
    return written, failed


# ---------------------------------------------------------------------
# 5. REPORT
# ---------------------------------------------------------------------
def report_new(cur, rows):
    """Which of these ISINs the table does not have yet -- and which of
    THOSE the current holdings are actually waiting on.

    Read before the upsert, so it describes the change rather than the
    result. "Loaded 2,143 stocks" is the same sentence whether 0 or 40
    of them are new, which makes a run impossible to judge."""

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

    new.sort(key=lambda r: (-held.get(r[0], 0),
                            r[4] or datetime(1900, 1, 1).date()))

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


def report_missing_from_list(existing_full, rows):
    """ACTIVE stocks the table holds that NSE's list does not. Report only.

    existing_full: [(isin, symbol, is_active)]."""
    listed = {r[0] for r in rows}
    gone = sorted(sym for isin, sym, active in existing_full
                  if active and isin not in listed)
    print("\n%d stocks in stock_master are not in NSE's list today%s"
          % (len(gone), "." if not gone else ":"))
    if gone:
        print("   " + ", ".join(gone[:40]) + (" ..." if len(gone) > 40 else ""))
        print("   (reported only; nothing is deactivated for this alone)")
    return gone


# ---------------------------------------------------------------------
def open_run_log(conn):
    with conn.cursor() as cur:
        # A run that died without closing its row stays RUNNING forever.
        cur.execute("""
            UPDATE batch_run_log
               SET status='FAILED', finished_at=NOW(),
                   error_message='never finished (the process died)'
             WHERE run_type='LOAD_STOCK_MASTER' AND status='RUNNING'
               AND started_at < NOW() - INTERVAL '6 hours'""")
        cur.execute("""
            INSERT INTO batch_run_log (run_type, business_date, status)
            VALUES ('LOAD_STOCK_MASTER', CURRENT_DATE, 'RUNNING')
            RETURNING run_id""")
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def close_run_log(conn, run_id, status, rows_read, rows_written, err):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE batch_run_log
               SET status=%s, finished_at=NOW(), rows_read=%s,
                   rows_written=%s, error_message=%s
             WHERE run_id=%s""",
                    (status, rows_read, rows_written,
                     err[:1500] if err else None, run_id))
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    ap.add_argument("--file", help="read this CSV instead of downloading")
    args = ap.parse_args()

    if not DB:
        print("FINCHAYA_DB is not set. Is /opt/mfapi/.env present?")
        return 1

    with psycopg.connect(DB) as conn:
        run_id = None if args.dry_run else open_run_log(conn)
        status, err = "FAILED", "stopped before finishing"
        n_read = n_written = 0
        try:
            rows = parse_rows(get_csv_text(args.file))
            n_read = len(rows)

            with conn.cursor() as cur:
                new = report_new(cur, rows)
                cur.execute("SELECT isin, symbol, is_active FROM stock_master "
                            "WHERE exchange = %s", (EXCHANGE,))
                existing_full = cur.fetchall()

            # Inactive rows count too: the unique rule covers them as well.
            clean, retire, clashing = plan_writes(
                [(i, s) for i, s, _ in existing_full], rows)
            report_missing_from_list(existing_full, rows)

            if retire:
                print("\n%d superseded row(s): the symbol now belongs to a "
                      "different ISIN in NSE's list, and the old ISIN is not "
                      "listed any more. Old row deactivated and renamed:"
                      % len(retire))
                for old_isin, sym, new_isin in retire:
                    print("   %-12s %s  ->  %s  (new ISIN %s)"
                          % (sym, old_isin, retired_symbol(sym, old_isin),
                             new_isin))
            if clashing:
                print("\n%d row(s) SKIPPED: the symbol is held by an ISIN "
                      "that is still in NSE's list. Not guessed at:"
                      % len(clashing))
                for row, holder in clashing:
                    print("   %-12s new %s  vs  existing %s"
                          % (row[1], row[0], holder))

            if args.dry_run:
                print("\n--dry-run: nothing was written. Would write %d "
                      "rows (%d new), retire %d, skip %d."
                      % (len(clean), len(new), len(retire), len(clashing)))
                return 0

            n_written, failed = write_rows(conn, clean, retire)
            conn.commit()

            for row, why in failed:
                print("   REFUSED by the table: %s %s -- %s"
                      % (row[1], row[0], why))
            problems = ["%s clashes with %s" % (r[1], h) for r, h in clashing]
            problems += ["%s refused: %s" % (r[1], w) for r, w in failed]
            notes = (["%d superseded: %s" % (
                len(retire), ", ".join(s for _, s, _ in retire))]
                if retire else [])
            status = "PARTIAL" if problems else "SUCCESS"
            err = "; ".join(notes + problems) or None

            print("\nLoaded %d stocks (%d new, %d superseded, %d skipped). "
                  "run_id = %s"
                  % (n_written, len(new), len(retire),
                     len(problems), run_id))
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM stock_master")
                print("stock_master now holds %d rows" % cur.fetchone()[0])
            if new:
                print("\nNEXT: the candle fetch that follows picks the new "
                      "stocks up. For the monthly run, re-run "
                      "promote_holdings.py so the newly known positions "
                      "count, then score_stocks.py and score_funds.py.")
            return 0 if status == "SUCCESS" else 1
        except SystemExit as e:
            err = str(e.code) if e.code not in (None, 0, 1) else err
            raise
        except Exception as e:                               # noqa: BLE001
            conn.rollback()
            err = "%s: %s" % (type(e).__name__, e)
            print("FAILED: %s" % err)
            raise
        finally:
            if run_id is not None:
                try:
                    conn.rollback()
                    close_run_log(conn, run_id, status, n_read, n_written, err)
                except Exception as e:                       # noqa: BLE001
                    print("(could not close run log %s: %s)" % (run_id, e))


if __name__ == "__main__":
    sys.exit(main())
