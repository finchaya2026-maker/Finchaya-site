#!/usr/bin/env python3
"""
fetch_ipo_ohlc.py -- find recent IPOs and store their raw daily OHLCV.

WHAT THIS IS FOR
    The IPO breakout screener needs two things the technicals pipeline
    throws away: the HIGH of a stock's first trading day, and daily
    VOLUME. fetch_technicals.py already receives both from Kite -- its
    historical_data call returns full OHLCV -- and then persists only
    close_price. This script keeps the rest.

HOW IPOs ARE IDENTIFIED, AND WHY IT NEEDS NO NSE DOWNLOAD
    Ask Kite for N years of daily candles. If the stock was already
    listed when that window opened, the first candle comes back at (or
    within a few sessions of) the window edge. If the stock listed
    DURING the window, there is nothing before its listing day for Kite
    to return -- so the first candle IS the listing day.

    That single asymmetry does the whole job. One request per stock,
    no scraping of EQUITY_L.csv, nothing to keep working when NSE
    changes its site.

    The reasoning is only valid inside the window. A stock that listed
    in 1995 gets a first candle at the window edge and is correctly
    skipped -- we cannot know its listing-day high from this source and
    we do not pretend to. That is the same trap that made the earliest
    row in stock_technical look like a listing date when it was really
    the day the nightly job first ran.

USAGE
    python fetch_ipo_ohlc.py                  # default 3-year window
    python fetch_ipo_ohlc.py --years 5
    python fetch_ipo_ohlc.py --symbols SWIGGY,OLAELEC
    python fetch_ipo_ohlc.py --fresh          # ignore the checkpoint
"""

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta

import pandas as pd
import psycopg
from dotenv import load_dotenv

from zerodha_login_with_auto import get_session

load_dotenv()
DB = os.getenv("FINCHAYA_DB")
if not DB:
    print("FINCHAYA_DB is not set. Check that .env exists in this folder.")
    sys.exit(1)

DEFAULT_YEARS = 3.0
MAX_DAYS_PER_REQUEST = 1900        # Kite's 'day' ceiling is 2000
RATE_LIMIT_PER_SEC = 3.0
BATCH_SIZE = 100
BATCH_PAUSE_SEC = 2.0
INSTRUMENT_CACHE = "instruments_nse.json"
CHECKPOINT = "ipo_ohlc_checkpoint.json"

# How close to the window edge still counts as "already listed".
#
# Not zero, and not arbitrary. A stock can miss its first few sessions in
# Kite's history for dull reasons -- a data gap, a holiday cluster, a
# symbol change. Ten trading days of slack absorbs that. Set it too tight
# and old stocks with a ragged first week get mistaken for IPOs, which is
# the failure that matters: it would invent a "listing high" out of an
# ordinary Tuesday.
EDGE_TOLERANCE_DAYS = 15


# =====================================================================
class RateLimiter:
    """Tracks when the next call is ALLOWED rather than sleeping a fixed
    amount after each one -- if a call itself takes 300ms, a flat sleep
    throws that time away."""

    def __init__(self, per_second):
        self.min_gap = 1.0 / per_second
        self.next_allowed = 0.0

    def wait(self):
        now = time.monotonic()
        if now < self.next_allowed:
            time.sleep(self.next_allowed - now)
        self.next_allowed = max(now, self.next_allowed) + self.min_gap


limiter = RateLimiter(RATE_LIMIT_PER_SEC)


def call_with_retry(fn, retries=5, **kwargs):
    """Exponential backoff. A 429 means we pushed too hard, so backing off
    is the correct response -- not failing, and not retrying immediately."""
    delay = 2.0
    for attempt in range(retries):
        limiter.wait()
        try:
            return fn(**kwargs)
        except Exception as e:
            msg = str(e).lower()
            transient = any(t in msg for t in
                            ("too many", "429", "timeout", "connection",
                             "network", "gateway", "temporarily"))
            if attempt == retries - 1 or not transient:
                raise
            print(f"        rate-limited, waiting {delay:.0f}s "
                  f"(attempt {attempt + 1}/{retries - 1})")
            time.sleep(delay)
            delay *= 2
    return None


def load_instrument_map(kite):
    """symbol -> instrument_token for NSE equities, cached for 24h.

    Same cache file fetch_technicals.py uses, so running that first
    means this script starts warm.
    """
    if os.path.exists(INSTRUMENT_CACHE):
        age = time.time() - os.path.getmtime(INSTRUMENT_CACHE)
        if age < 86400:
            with open(INSTRUMENT_CACHE) as f:
                return json.load(f)

    limiter.wait()
    instruments = kite.instruments("NSE")
    mapping = {i["tradingsymbol"]: i["instrument_token"]
               for i in instruments if i.get("segment") == "NSE"}
    with open(INSTRUMENT_CACHE, "w") as f:
        json.dump(mapping, f)
    return mapping


# =====================================================================
def fetch_window(kite, token, years):
    """Daily candles across the window, in <=1900-day chunks.

    A 3-year window is ~1096 days, so this is one request per stock in
    the default case. --years 6 makes it two.
    """
    end = date.today()
    start = end - timedelta(days=int(years * 365.25))

    frames, window_start = [], start
    while window_start < end:
        window_end = min(window_start + timedelta(days=MAX_DAYS_PER_REQUEST), end)
        candles = call_with_retry(
            kite.historical_data,
            instrument_token=token,
            from_date=window_start,
            to_date=window_end,
            interval="day",
        )
        if candles:
            frames.append(pd.DataFrame(candles))
        window_start = window_end + timedelta(days=1)

    if not frames:
        return None, start

    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.drop_duplicates(subset="date").sort_values("date").set_index("date")
    return df, start


def looks_like_a_listing(daily, window_start):
    """Did this stock list INSIDE the window, or was it already trading?

    Returns the listing date, or None if the first candle sits close
    enough to the window edge that we cannot tell the difference.
    """
    if daily is None or daily.empty:
        return None
    first = daily.index[0].date()
    if (first - window_start).days <= EDGE_TOLERANCE_DAYS:
        return None            # already listed when the window opened
    return first


def safe(v):
    """None for anything pandas calls missing, so NULL lands in Postgres
    rather than the string 'nan'."""
    if v is None or pd.isna(v):
        return None
    return float(v)


UPSERT = """
INSERT INTO stock_ohlc_daily
    (isin, as_of_date, open, high, low, close, volume, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (isin, as_of_date) DO UPDATE SET
    open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
    close=EXCLUDED.close, volume=EXCLUDED.volume, source=EXCLUDED.source
"""

SET_LISTING = """
UPDATE stock_master
   SET listing_date = %s, listing_date_source = 'KITE-FIRST-CANDLE'
 WHERE isin = %s
"""


def build_rows(isin, daily):
    rows = []
    for ts, r in daily.iterrows():
        vol = r.get("volume")
        rows.append((
            isin, ts.date(),
            safe(r.get("open")), safe(r.get("high")),
            safe(r.get("low")), safe(r.get("close")),
            None if vol is None or pd.isna(vol) else int(vol),
            "KITE",
        ))
    return rows


# =====================================================================
def load_targets(conn, limit, symbols):
    with conn.cursor() as cur:
        if symbols:
            cur.execute("SELECT isin, symbol FROM stock_master "
                        "WHERE symbol = ANY(%s) ORDER BY symbol", (symbols,))
        elif limit:
            cur.execute("SELECT isin, symbol FROM stock_master "
                        "WHERE is_active ORDER BY symbol LIMIT %s", (limit,))
        else:
            cur.execute("SELECT isin, symbol FROM stock_master "
                        "WHERE is_active ORDER BY symbol")
        return cur.fetchall()


def load_checkpoint():
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT) as f:
            return set(json.load(f))
    return set()


def save_checkpoint(done):
    with open(CHECKPOINT, "w") as f:
        json.dump(sorted(done), f)


# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--symbols")
    ap.add_argument("--years", type=float, default=DEFAULT_YEARS,
                    help="how far back to look for listings (default 3)")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--batch-pause", type=float, default=BATCH_PAUSE_SEC)
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",")]
               if args.symbols else None)

    kite = get_session(auto_login=True)
    if not kite:
        print("Could not authenticate with Kite.")
        sys.exit(1)

    instrument_map = load_instrument_map(kite)

    with psycopg.connect(DB) as conn:
        targets = load_targets(conn, args.limit, symbols)
        if not targets:
            print("No stocks selected.")
            sys.exit(1)

        done = set() if args.fresh else load_checkpoint()
        todo = [(i, s) for i, s in targets if s not in done]

        reqs = max(1, int(args.years * 365.25 / MAX_DAYS_PER_REQUEST) + 1)
        est = len(todo) * reqs / RATE_LIMIT_PER_SEC
        print(f"{len(todo)} stocks to scan, ~{reqs} request(s) each, "
              f"roughly {est / 60:.0f} min.")
        print(f"Window: listings on or after "
              f"{date.today() - timedelta(days=int(args.years * 365.25))}\n")

        ipos = skipped = missing = failed = 0

        for n, (isin, symbol) in enumerate(todo, 1):
            token = instrument_map.get(symbol)
            if not token:
                missing += 1
                done.add(symbol)
                continue

            try:
                daily, window_start = fetch_window(kite, token, args.years)
                listing = looks_like_a_listing(daily, window_start)

                if listing is None:
                    # Already listed before the window, or no data at all.
                    # Storing its bars would be harmless but pointless --
                    # the screener cannot use a stock whose listing high
                    # is unknown, so we save the space and the write.
                    skipped += 1
                else:
                    rows = build_rows(isin, daily)
                    with conn.cursor() as cur:
                        cur.executemany(UPSERT, rows)
                        cur.execute(SET_LISTING, (listing, isin))
                    conn.commit()
                    ipos += 1
                    print(f"  [{n}/{len(todo)}] {symbol:<14} listed "
                          f"{listing}  {len(rows)} bars  "
                          f"first-day high {daily.iloc[0]['high']:.2f}")

                done.add(symbol)

            except Exception as e:
                failed += 1
                print(f"  [{n}/{len(todo)}] {symbol:<14} FAILED: {e}")

            if n % args.batch_size == 0:
                save_checkpoint(done)
                print(f"    ... {n}/{len(todo)} scanned "
                      f"({ipos} IPOs found), pausing")
                time.sleep(args.batch_pause)

        save_checkpoint(done)

    print(f"\nDone. {ipos} IPOs stored, {skipped} already listed before the "
          f"window, {missing} not on NSE, {failed} failed.")
    if ipos == 0:
        print("No IPOs found. If that is a surprise, widen --years.")


if __name__ == "__main__":
    main()
