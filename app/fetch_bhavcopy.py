"""
fetch_bhavcopy.py -- daily stock candles from NSE's own end-of-day price file.
------------------------------------------------------------------------------
Replaces the Zerodha download in fetch_technicals.py. NSE publishes ONE file
per trading day with open, high, low, close and volume for every listed stock
(the "bhavcopy", about 200 KB). This reads it and stores the candles in
stock_ohlc_daily, which is the same table the indicator code already reads.

    cd /opt/mfapi
    venv/bin/python3 fetch_bhavcopy.py --dry-run     # look, write nothing
    venv/bin/python3 fetch_bhavcopy.py               # fill every missing day
    venv/bin/python3 fetch_bhavcopy.py --from 2026-09-14 --to 2026-10-01
    venv/bin/python3 fetch_bhavcopy.py --days 5      # re-read the last 5 days

    # One-off history for stocks that have too few candles to be scored (about
    # 60 are needed). Adds what is missing, rewrites nothing that is stored:
    venv/bin/python3 fetch_bhavcopy.py --from 2024-08-01 --insert-only

WHY THIS IS SAFE TO SWITCH TO
    research/probe_nse_bhavcopy.py compared NSE's file with 2,286 stored
    Zerodha candles for 11 Sep 2026. Open, high, low, close and volume were
    identical for all 2,286. Zerodha's candles ARE this data, so the history
    already stored does not change and does not need re-checking.

NEW LISTINGS
    This only stores stocks already in stock_master. load_stock_master.py runs
    just before it in nightly.sh and adds new ones from NSE's equity list, and
    the last OVERLAP_DAYS days are always re-read, so a stock added tonight
    gets its recent candles too. The run ends by saying what the not-tracked
    rows in the newest file are (SME-board, funds/ETFs, other shares), so a
    new listing the list has missed is visible rather than silent.

WHAT "MISSING" MEANS
    The newest day with at least FULL_DAY_MIN candles. Not simply the newest
    date in the table: since 15 Sep 2026 the Zerodha job stored one stock a
    night, so the newest date (1 Oct) said nothing about what was actually
    complete (11 Sep). A day counts as done only when it holds most of the
    market.

WHAT IS STORED
    One candle per ISIN per day, only for ISINs already in stock_master (a
    stock we do not track is not added). A stock can appear under more than
    one series on a day (EQ, and BE for trade-for-trade); the main series
    wins. That is also why this finds ~230 stocks Zerodha never delivered:
    Zerodha lists the BE ones under a different symbol.

    Rows with a missing or zero open/high/low/close are skipped and counted
    rather than stored. A half-candle would corrupt ADX and Supertrend.

PRICES ARE NOT SPLIT-ADJUSTED. Neither were Zerodha's. A split still shows
as an overnight drop in the stored series, exactly as before.

HOLIDAYS AND "NOT YET PUBLISHED"
    NSE answers 404 for a day with no file. For a past weekday that is a
    market holiday and is skipped quietly. For today it usually means the
    file is not out yet (about 4:35 pm IST), and the next run picks it up.

THE RUN LOG
    Writes a FETCH_BHAVCOPY row to batch_run_log and ALWAYS closes it, even
    when something throws, so it can never be left at RUNNING. (Sixteen
    FETCH_TECHNICALS rows are stuck that way today.)

SAFE TO RE-RUN. The write is an upsert on (isin, as_of_date).
Exit code is 1 if any day failed, so nightly.sh reports it.
"""

import argparse
import csv
import io
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import psycopg
import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv()

DB = os.getenv("FINCHAYA_DB")

# A name, not a literal inside the SQL, so a test can point the whole script
# at a throwaway table without touching real candles.
TABLE = "stock_ohlc_daily"
SOURCE = "NSE"

URL = ("https://nsearchives.nseindia.com/content/cm/"
       "BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

TIMEOUT_SEC = 40
RETRIES = 3
PAUSE_BETWEEN_FILES_SEC = 1.0

# A normal trading day carries ~2,300 of our stocks. 1,000 is "most of the
# market"; the Zerodha job's one-stock nights are far below it.
FULL_DAY_MIN = 1000
# Fewer matched stocks than this in a file means something is wrong with the
# file or the match, not that the market was quiet. Refuse to store it.
MIN_MATCHED = 500
LOOKBACK_DAYS = 90
# Every run re-reads at least this many days back, on top of anything missing.
# Re-reading is harmless (an upsert of identical numbers) and it is what gives
# a stock added to stock_master tonight its candles for the last few days,
# instead of starting from tonight.
OVERLAP_DAYS = 5

# NSE's column names in the current ("UDiFF") layout, and our short names.
# Confirmed against the real file on the droplet, 5 Oct 2026 (34 columns).
COLS = {
    "isin": "ISIN", "symbol": "TckrSymb", "series": "SctySrs",
    "open": "OpnPric", "high": "HghPric", "low": "LwPric", "close": "ClsPric",
    "volume": "TtlTradgVol",
}
SHARE_SERIES = ("EQ", "BE", "BZ", "SM", "ST")
PREFERRED = {s: i for i, s in enumerate(SHARE_SERIES)}

UPSERT = """
INSERT INTO {table}
    (isin, as_of_date, open, high, low, close, volume, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (isin, as_of_date) DO UPDATE SET
    open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
    close=EXCLUDED.close, volume=EXCLUDED.volume, source=EXCLUDED.source
"""


# For a long historical backfill: add the candles we do not have and leave
# every existing one exactly as it is. Re-writing identical numbers into a
# million rows that are already right would only churn the table.
INSERT_ONLY = """
INSERT INTO {table}
    (isin, as_of_date, open, high, low, close, volume, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (isin, as_of_date) DO NOTHING
"""


class LayoutChanged(Exception):
    """NSE changed the file's columns. Stop; do not guess."""


class FetchFailed(Exception):
    """A day's file could not be fetched after retries."""


def ist_today():
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def dec(x):
    try:
        v = Decimal(str(x).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None
    return v if v.is_finite() else None


# ---------------------------------------------------------------------
# THE FILE
# ---------------------------------------------------------------------
def download_day(session, day):
    """Bytes of the day's zip, or None when NSE has no file for it (404).

    Retries only what can plausibly pass: timeouts, connection errors and
    5xx. A 403 is not retried -- it means NSE is refusing this address, and
    asking again faster does not help.
    """
    url = URL.format(d=day.strftime("%Y%m%d"))
    last = "no attempt"
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT_SEC)
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        else:
            if r.status_code == 200:
                return r.content
            if r.status_code == 404:
                return None
            last = f"HTTP {r.status_code}"
            if r.status_code < 500:
                break
        if attempt < RETRIES:
            time.sleep(2 * attempt)
    raise FetchFailed(f"{day}: {last}")


def parse_bhavcopy(zip_bytes):
    """Zip bytes -> list of dicts keyed by our short names. Raises
    LayoutChanged if a column we need is not there."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in COLS.values() if c not in header]
    if missing:
        raise LayoutChanged(f"NSE's file no longer has {missing}; "
                            f"columns now: {header}")
    out = []
    for raw in reader:
        raw = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        out.append({short: raw.get(col) for short, col in COLS.items()})
    return out


def select_rows(rows, known_isins):
    """One candle per tracked ISIN. Returns (picked, stats)."""
    best, stats = {}, {"in_file": len(rows), "not_tracked": 0,
                       "other_series": 0, "bad_prices": 0, "untracked": []}
    for r in rows:
        isin, series = r["isin"], r["series"]
        if series not in PREFERRED:
            stats["other_series"] += 1
            continue
        if isin not in known_isins:
            stats["not_tracked"] += 1
            stats["untracked"].append((r["symbol"], isin, series))
            continue
        o, h, l, c = (dec(r[k]) for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            stats["bad_prices"] += 1
            continue
        cur = best.get(isin)
        if cur is None or PREFERRED[series] < PREFERRED[cur["series"]]:
            vol = dec(r["volume"])
            best[isin] = {"series": series, "open": o, "high": h, "low": l,
                          "close": c,
                          "volume": int(vol) if vol is not None else None}
    stats["matched"] = len(best)
    return best, stats


# ---------------------------------------------------------------------
# THE DATABASE
# ---------------------------------------------------------------------
def last_complete_day(conn):
    """Newest day holding at least FULL_DAY_MIN candles, or None."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT max(d) FROM (
                SELECT as_of_date AS d FROM {TABLE}
                WHERE as_of_date >= %s
                GROUP BY as_of_date HAVING count(*) >= %s) x
        """, (ist_today() - timedelta(days=LOOKBACK_DAYS), FULL_DAY_MIN))
        return cur.fetchone()[0]


def store_day(conn, day, picked, insert_only=False):
    """Write one day's candles. Returns how many rows were written.

    With insert_only the count is the rows that were actually ADDED, worked
    out from the table before and after rather than from the driver, because
    a skipped (already stored) row does not count and the driver's rowcount
    for a batch is not something to rely on."""
    tuples = [(isin, day, p["open"], p["high"], p["low"], p["close"],
               p["volume"], SOURCE) for isin, p in picked.items()]
    with conn.cursor() as cur:
        if not insert_only:
            cur.executemany(UPSERT.format(table=TABLE), tuples)
            conn.commit()
            return len(tuples)
        cur.execute(f"SELECT count(*) FROM {TABLE} WHERE as_of_date = %s", (day,))
        before = cur.fetchone()[0]
        cur.executemany(INSERT_ONLY.format(table=TABLE), tuples)
        cur.execute(f"SELECT count(*) FROM {TABLE} WHERE as_of_date = %s", (day,))
        after = cur.fetchone()[0]
    conn.commit()
    return after - before


def open_run_log(conn):
    with conn.cursor() as cur:
        # A run that died without closing its row stays RUNNING forever and
        # then tells every later reader the job is "still going".
        cur.execute("""
            UPDATE batch_run_log
               SET status='FAILED', finished_at=now(),
                   error_message='never finished (the process died)'
             WHERE run_type='FETCH_BHAVCOPY' AND status='RUNNING'
               AND started_at < now() - interval '6 hours'""")
        cur.execute("""
            INSERT INTO batch_run_log (run_type, business_date, status)
            VALUES ('FETCH_BHAVCOPY', CURRENT_DATE, 'RUNNING')
            RETURNING run_id""")
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def close_run_log(conn, run_id, status, data_date, rows_read, rows_written, err):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE batch_run_log
               SET status=%s, finished_at=now(),
                   business_date=COALESCE(%s, business_date),
                   rows_read=%s, rows_written=%s, error_message=%s
             WHERE run_id=%s""",
                    (status, data_date, rows_read, rows_written,
                     err[:500] if err else None, run_id))
    conn.commit()


def untracked_summary(untracked):
    """What the not-tracked rows ARE, so the big number is not a mystery.

    SME-board shares and funds/ETFs are left out on purpose (the stock list is
    NSE's main-board EQ and BE equities). What is left -- company shares in a
    main-board series -- is the set that could be a new listing the stock list
    has not caught up with yet, and that is the one worth reading."""
    sme = [u for u in untracked if u[2] in ("SM", "ST")]
    funds = [u for u in untracked if u[2] not in ("SM", "ST")
             and u[1].startswith("INF")]
    shares = [u for u in untracked if u[2] not in ("SM", "ST")
              and not u[1].startswith("INF")]
    return len(sme), len(funds), sorted(shares)


def candidate_days(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:          # Saturday and Sunday never have a file
            yield d
        d += timedelta(days=1)


# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="start", help="first day, YYYY-MM-DD")
    ap.add_argument("--to", dest="end", help="last day (default: today, IST)")
    ap.add_argument("--days", type=int,
                    help="re-read this many days back from the end day")
    ap.add_argument("--dry-run", action="store_true",
                    help="download and check, but write nothing")
    ap.add_argument("--insert-only", action="store_true",
                    help="add only candles not stored yet; never rewrite an "
                         "existing one (for a long historical backfill)")
    ap.add_argument("--force", action="store_true",
                    help=f"store a day even if fewer than {MIN_MATCHED} "
                         "stocks match")
    args = ap.parse_args()

    if not DB:
        print("FINCHAYA_DB is not set. Check that .env exists in this folder.")
        return 1

    end = (datetime.strptime(args.end, "%Y-%m-%d").date()
           if args.end else ist_today())

    with psycopg.connect(DB) as conn:
        if args.start:
            start = datetime.strptime(args.start, "%Y-%m-%d").date()
        elif args.days:
            start = end - timedelta(days=args.days)
        else:
            done = last_complete_day(conn)
            if done is None:
                print(f"No complete day in the last {LOOKBACK_DAYS} days to "
                      "continue from. Give --from YYYY-MM-DD.")
                return 1
            # Whatever is missing, but never less than the last few days.
            start = min(done + timedelta(days=1),
                        end - timedelta(days=OVERLAP_DAYS))
            print(f"Last complete day in {TABLE}: {done}. Reading from "
                  f"{start} (everything after it, and at least the last "
                  f"{OVERLAP_DAYS} days).")

        if start > end:
            print(f"Nothing to do: {TABLE} is complete up to {end}.")
            return 0

        with conn.cursor() as cur:
            cur.execute("SELECT isin FROM stock_master")
            known = {r[0] for r in cur.fetchall()}
        print(f"{len(known):,} stocks tracked. Days: {start} to {end}"
              f"{'  (DRY RUN, nothing is written)' if args.dry_run else ''}\n")

        run_id = None if args.dry_run else open_run_log(conn)
        session = requests.Session()
        session.headers.update({"User-Agent": UA,
                                "Referer": "https://www.nseindia.com/"})

        stored = read = 0
        newest, newest_untracked = None, []
        failures = []
        # Set BEFORE the try, so the log row is closed with something true
        # whatever happens inside it, including a crash.
        status, err = "FAILED", "stopped before finishing"
        try:
            for day in candidate_days(start, end):
                try:
                    blob = download_day(session, day)
                    if blob is None:
                        why = ("not published yet" if day >= ist_today()
                               else "no file (market holiday)")
                        print(f"{day}  {why}")
                        continue
                    picked, st = select_rows(parse_bhavcopy(blob), known)
                except LayoutChanged:
                    raise            # no point trying the next day
                except (FetchFailed, zipfile.BadZipFile) as e:
                    failures.append(str(e))
                    print(f"{day}  FAILED: {e}")
                    continue

                read += st["in_file"]
                line = (f"{day}  file {st['in_file']:,} rows | matched "
                        f"{st['matched']:,} | not tracked {st['not_tracked']:,}"
                        f" | other series {st['other_series']:,}"
                        f" | bad prices {st['bad_prices']:,}")
                if st["matched"] < MIN_MATCHED and not args.force:
                    failures.append(f"{day}: only {st['matched']} stocks matched")
                    print(line + "\n          REFUSED: too few matched to be a "
                                 "real day (use --force to store anyway)")
                    continue
                if args.dry_run:
                    print(line + "   [dry run]")
                else:
                    n = store_day(conn, day, picked, args.insert_only)
                    stored += n
                    print(line + (f"   added {n:,}" if args.insert_only
                                  else f"   stored {n:,}"))
                if newest is None or day > newest:
                    newest, newest_untracked = day, st["untracked"]
                time.sleep(PAUSE_BETWEEN_FILES_SEC)

            if newest is not None:
                n_sme, n_funds, shares = untracked_summary(newest_untracked)
                print(f"\nNot in stock_master, {newest} file: "
                      f"{len(newest_untracked):,} rows = {n_sme:,} SME-board, "
                      f"{n_funds:,} funds/ETFs, {len(shares):,} other shares.")
                if shares:
                    print("  The other shares could be new listings the stock "
                          "list has not caught up with (load_stock_master.py "
                          "adds them):")
                    for sym, isin, ser in shares[:25]:
                        print(f"    {sym:<14} {isin}  {ser}")
                    if len(shares) > 25:
                        print(f"    ... and {len(shares) - 25} more")

            status = ("SUCCESS" if not failures
                      else "PARTIAL" if stored else "FAILED")
            err = "; ".join(failures)
        except LayoutChanged as e:
            status, err = "FAILED", f"layout changed: {e}"
            print(f"\nSTOPPED: {e}")
        except Exception as e:           # noqa: BLE001 -- logged and re-raised
            status, err = "FAILED", f"{type(e).__name__}: {e}"
            raise
        finally:
            if run_id is not None:
                try:
                    conn.rollback()
                    close_run_log(conn, run_id, status, newest,
                                  read, stored, err)
                except Exception as e:   # noqa: BLE001
                    print(f"(could not close run log {run_id}: {e})")

        print(f"\n{'=' * 60}")
        print(f"{status}. Stored {stored:,} candles"
              f"{f' up to {newest}' if newest else ''}."
              f"{'  (dry run: nothing written)' if args.dry_run else ''}")
        if failures:
            print("Problems:")
            for f in failures:
                print("  ", f)
        return 0 if status == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
