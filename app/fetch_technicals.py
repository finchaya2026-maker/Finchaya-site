"""
fetch_technicals.py  (v3 -- incremental, candle-backed)
-------------------------------------------------------
Fetches daily OHLC from Zerodha Kite, KEEPS it in stock_ohlc_daily,
derives WEEKLY and MONTHLY candles, computes MACD / RSI / ADX /
Bollinger / SMA / Supertrend on all three, and writes stock_technical.

INDICATOR MATHS LIVES IN indicators.py -- untouched, imported as-is.
Only Bollinger Bands and SMA are added here, since they weren't in it.

WHAT CHANGED FROM v2, AND WHY IT MATTERS
    v2 downloaded roughly 2,965 candles per stock, computed three
    indicator rows, and discarded the candles. The next night it
    downloaded the same 2,965 again for the sake of one new day. A full
    universe run took 64 minutes and re-fetched about eleven years of
    history 2,150 times over.

    v3 stores the candles. Each night it asks the database what the
    newest stored date is for a stock and fetches only from there --
    normally a single day. Same number of Kite requests, but each one is
    small, so the run drops to roughly ten to fifteen minutes.

    Three things this buys beyond speed:

      1. A FAILED NIGHT IS RECOVERABLE. Miss Tuesday and Wednesday's run
         fetches both days. Under v2 a missed day was simply gone,
         because nothing kept it.

      2. NEW INDICATORS COST NOTHING. Adding RSI on a different period,
         or a volume filter, used to mean re-downloading eleven years
         for 2,150 stocks. Now it is a query against data already held.

      3. VOLUME SURVIVES. Kite always sent it; v2 dropped it on the
         floor. It is now stored, and to_monthly / to_weekly already
         aggregate it when present.

    The catch worth stating plainly: incremental fetching only works
    because history is kept locally. A 200-day SMA needs 200 days. We
    fetch one day and compute over 200 because the other 199 are on
    disk -- not because the indicator suddenly needs less.

FIRST RUN IS STILL SLOW. There is nothing stored yet, so the first run
after this upgrade downloads full history exactly like v2 did -- about
an hour. Every run after that is the short one. Run create_ohlc_table.py
before the first one.

BATCHING -- three independent brakes, unchanged from v2:
    1. Rate limiter  -- 2.5 req/sec, under Kite's documented 3/sec
    2. Batch pause   -- a real pause every N stocks
    3. Backoff       -- on a 429, wait progressively longer and retry

USAGE
  python fetch_technicals.py --symbols RELIANCE,TCS,INFY     # spot check
  python fetch_technicals.py --limit 100                     # scale test
  python fetch_technicals.py                                 # full universe
  python fetch_technicals.py --rebuild-ohlc                  # ignore stored
  python fetch_technicals.py --batch-size 25 --batch-pause 45   # extra gentle

Interrupt-safe: checkpoints after every batch, resumes on re-run.
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

import indicators as ind
from zerodha_login_with_auto import get_session

# Anchored to this file rather than the current directory: cron does not
# start where you think it does, and a checkpoint written to the wrong
# folder silently turns every run into a fresh one.
HERE = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(HERE, ".env"))
load_dotenv()

DB = os.getenv("FINCHAYA_DB")
if not DB:
    print("FINCHAYA_DB is not set. Check that .env exists in this folder.")
    sys.exit(1)

RATE_LIMIT_PER_SEC = 2.5        # Kite allows 3/sec for historical
MAX_DAYS_PER_REQUEST = 1900     # Kite's 'day' ceiling is 2000
DEFAULT_YEARS = 12              # monthly ADX needs 42 bars = 3.5 yrs minimum

BATCH_SIZE = 50                 # stocks between pauses
BATCH_PAUSE_SEC = 30            # length of that pause

INSTRUMENT_CACHE = os.path.join(HERE, "kite_instruments.json")
CHECKPOINT_FILE = os.path.join(HERE, "fetch_checkpoint.json")
FLOOR_FILE = os.path.join(HERE, "ohlc_floor.json")

# How close to the requested window start counts as "we already have
# everything there is". A stock that listed in 2019 will never have a
# 2014 candle, and without this slack the job would ask for the missing
# five years every single night, forever, and get nothing back every
# time. Fifteen days also absorbs a ragged first week of trading.
HEAD_SLACK_DAYS = 15

# Indicator parameters -- recorded in the DB so future-you knows what these were
RSI_PERIOD = 14
ADX_PERIOD = 14
BB_PERIOD, BB_STDDEV = 20, 2.0
ST_PERIOD, ST_MULT = 10, 3.0
SMA_PERIOD = 50

# The standard set. 26 is here because it is MACD's slow leg -- having it
# as a plottable line makes the MACD reading explainable rather than
# magic. 5/10/20 are the short trio, 50/100/200 the long one.
#
# A period longer than the bars available comes back NULL rather than
# approximated, and that is not rare: MONTHLY bars start in 2014, so a
# 200-month EMA needs 16 years and nothing has it. The per-timeframe
# report in backfill_ema.py says which are empty and why.
EMA_PERIODS = (5, 10, 20, 26, 50, 100, 200)

PARAM_SET = (f"M12-26-9_R{RSI_PERIOD}_A{ADX_PERIOD}"
             f"_BB{BB_PERIOD}-{BB_STDDEV:g}_ST{ST_PERIOD}-{ST_MULT:g}_S{SMA_PERIOD}"
             f"_E{'-'.join(str(p) for p in EMA_PERIODS)}")


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

# Counted rather than estimated. The whole point of this version is
# fewer and smaller requests, and a number printed at the end is how you
# know it actually happened.
REQUESTS = {"n": 0}


def call_with_retry(fn, retries=5, **kwargs):
    """Exponential backoff. A 429 means we pushed too hard, so backing off
    is the correct response -- not failing, and not retrying immediately."""
    delay = 2.0
    for attempt in range(retries):
        limiter.wait()
        try:
            REQUESTS["n"] += 1
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


# =====================================================================
def load_instrument_map(kite):
    """symbol -> instrument_token for NSE equities, cached for 24h."""
    if os.path.exists(INSTRUMENT_CACHE):
        age_h = (time.time() - os.path.getmtime(INSTRUMENT_CACHE)) / 3600
        if age_h < 24:
            with open(INSTRUMENT_CACHE) as f:
                m = json.load(f)
            print(f"Instrument cache: {len(m)} symbols ({age_h:.1f}h old)")
            return m

    print("Downloading instrument list from Kite...")
    limiter.wait()
    instruments = kite.instruments("NSE")
    m = {i["tradingsymbol"]: i["instrument_token"] for i in instruments
         if i.get("instrument_type") == "EQ" and i.get("segment") == "NSE"}
    with open(INSTRUMENT_CACHE, "w") as f:
        json.dump(m, f)
    print(f"Cached {len(m)} NSE equity symbols")
    return m


def fetch_range(kite, token, start, end):
    """Daily candles between two dates, in <=1900-day chunks.

    Returns a DataFrame indexed by date, or None when the range is empty
    or the stock has nothing there (which is the normal answer for days
    before a stock listed).
    """
    if start > end:
        return None

    frames, window_start = [], start
    while window_start <= end:
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
        return None

    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.drop_duplicates(subset="date").sort_values("date").set_index("date")
    return df


# ---------------------------------------------------------------------
# THE CANDLE STORE
# ---------------------------------------------------------------------
OHLC_UPSERT = """
INSERT INTO stock_ohlc_daily
    (isin, as_of_date, open, high, low, close, volume, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (isin, as_of_date) DO UPDATE SET
    open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
    close=EXCLUDED.close, volume=EXCLUDED.volume, source=EXCLUDED.source
"""


def ohlc_table_exists(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('stock_ohlc_daily') IS NOT NULL")
        return cur.fetchone()[0]


def load_stored_bounds(conn):
    """isin -> (earliest, latest, rows), for every stock we hold candles for.

    ONE query for the whole universe rather than one per stock. This is
    only metadata -- the candles themselves are read per stock, and only
    for stocks actually being processed.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT isin, MIN(as_of_date), MAX(as_of_date), COUNT(*)
            FROM stock_ohlc_daily GROUP BY isin
        """)
        return {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}


def load_stored_daily(conn, isin, since):
    """Stored candles from `since` onward, shaped exactly like Kite's."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT as_of_date, open, high, low, close, volume
            FROM stock_ohlc_daily
            WHERE isin = %s AND as_of_date >= %s
            ORDER BY as_of_date
        """, (isin, since))
        rows = cur.fetchall()

    if not rows:
        return None

    # Postgres NUMERIC arrives as Decimal. pandas will happily hold
    # Decimals and then fail deep inside an ewm() call with a type error
    # that names neither the column nor the stock, so convert here.
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low",
                                     "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    return df.set_index("date").sort_index()


def store_daily(conn, isin, df):
    """Persist fetched candles. Returns how many rows were written."""
    if df is None or df.empty:
        return 0
    rows = []
    for ts, r in df.iterrows():
        vol = r.get("volume")
        rows.append((
            isin, ts.date(),
            safe(r.get("open")), safe(r.get("high")),
            safe(r.get("low")), safe(r.get("close")),
            None if vol is None or pd.isna(vol) else int(vol),
            "KITE",
        ))
    with conn.cursor() as cur:
        cur.executemany(OHLC_UPSERT, rows)
    conn.commit()
    return len(rows)


def load_floor():
    """isin -> the earliest date we have ever ASKED Kite for.

    Not the same as the earliest candle we hold. A stock that listed in
    2019 has no 2014 data and never will; without remembering that we
    asked, the job would request those missing years every night and get
    an empty answer every night. Asking once and recording it turns a
    permanent daily cost into a single request.
    """
    if os.path.exists(FLOOR_FILE):
        try:
            with open(FLOOR_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_floor(floor):
    try:
        with open(FLOOR_FILE, "w") as f:
            json.dump(floor, f)
    except Exception as e:
        print(f"  (could not save {FLOOR_FILE}: {e})")


# =====================================================================
# WEEKLY, corrected
#
# indicators.to_monthly labels each bar with the LAST TRADING DAY in the
# month -- correct, and your own comment explains why. to_weekly still
# uses the calendar Friday, so a Wednesday run labels the current bar with
# a Friday that hasn't happened. Same fix, applied here so your module
# stays untouched.
# =====================================================================
# Moved into indicators.py so a backfill can resample stored candles
# without importing this module -- which cannot be imported without a
# Kite client. Re-exported here under its old name so nothing that
# already calls it has to change.
to_weekly_real_days = ind.to_weekly_real_days


def add_bollinger_and_sma(df):
    """The two indicators not in indicators.py.

    ddof=0 (population stddev) is what TradingView and most charting
    platforms use. ddof=1 gives slightly wider bands that won't match
    the chart you'll inevitably check this against.
    """
    out = df.copy()
    mid = out["close"].rolling(BB_PERIOD).mean()
    sd = out["close"].rolling(BB_PERIOD).std(ddof=0)
    out["bb_middle"] = mid
    out["bb_upper"] = mid + BB_STDDEV * sd
    out["bb_lower"] = mid - BB_STDDEV * sd
    out["sma"] = out["close"].rolling(SMA_PERIOD).mean()
    return out


def compute_all(frame):
    x = ind.macd(frame)
    x = ind.rsi(x, period=RSI_PERIOD)
    x = ind.adx(x, period=ADX_PERIOD)
    x = ind.supertrend(x, period=ST_PERIOD, multiplier=ST_MULT)
    x = ind.ema(x, periods=EMA_PERIODS)
    return add_bollinger_and_sma(x)


def safe(value):
    """NaN -> None, so Postgres stores a real NULL rather than 'NaN'."""
    if value is None or pd.isna(value):
        return None
    return float(value)


def build_rows(isin, daily, history_bars):
    rows = []
    timeframes = {
        "DAILY": daily,
        "WEEKLY": to_weekly_real_days(daily),
        "MONTHLY": ind.to_monthly(daily),
    }

    for timeframe, frame in timeframes.items():
        n = len(frame)
        # ADX is the strictest warm-up (smoothed twice), so it gates all.
        if not ind.enough_bars_for_adx(n, ADX_PERIOD):
            continue
        if n < max(BB_PERIOD, SMA_PERIOD):
            continue

        computed = compute_all(frame)

        for ts, r in computed.tail(history_bars).iterrows():
            rows.append((
                isin, ts.date(), timeframe,
                safe(r.get("macd_line")), safe(r.get("macd_signal")),
                safe(r.get("macd_hist")),
                safe(r.get("adx")), safe(r.get("plus_di")), safe(r.get("minus_di")),
                safe(r.get("rsi")),
                safe(r.get("bb_upper")), safe(r.get("bb_middle")), safe(r.get("bb_lower")),
                safe(r.get("sma")),
                safe(r.get("supertrend")),
                "UP" if bool(r.get("st_bullish")) else "DOWN",
                safe(r.get("close")),
                *[safe(r.get(f"ema_{p}")) for p in EMA_PERIODS],
                PARAM_SET, "KITE",
            ))
    return rows


# =====================================================================
# Built from EMA_PERIODS rather than typed out, so adding a period means
# editing one tuple instead of three lists that must agree. The first
# version of this file had the column list, the VALUES placeholders and
# the row tuple maintained by hand in three places; with seven new columns
# that is three chances to put ema_100 where ema_50 belongs and no error
# when you do -- just wrong numbers.
_EMA_COLS = ", ".join(f"ema_{p}" for p in EMA_PERIODS)
_EMA_SET = ", ".join(f"ema_{p}=EXCLUDED.ema_{p}" for p in EMA_PERIODS)
_EMA_PLACEHOLDERS = ",".join(["%s"] * len(EMA_PERIODS))

UPSERT = f"""
INSERT INTO stock_technical
    (isin, as_of_date, timeframe,
     macd_line, macd_signal, macd_histogram,
     adx, plus_di, minus_di, rsi,
     bb_upper, bb_middle, bb_lower, sma,
     supertrend_value, supertrend_dir,
     close_price,
     {_EMA_COLS},
     param_set, source)
VALUES (%s,%s,%s, %s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s, %s,
        {_EMA_PLACEHOLDERS}, %s,%s)
ON CONFLICT (isin, as_of_date, timeframe) DO UPDATE SET
    macd_line=EXCLUDED.macd_line, macd_signal=EXCLUDED.macd_signal,
    macd_histogram=EXCLUDED.macd_histogram,
    adx=EXCLUDED.adx, plus_di=EXCLUDED.plus_di, minus_di=EXCLUDED.minus_di,
    rsi=EXCLUDED.rsi,
    bb_upper=EXCLUDED.bb_upper, bb_middle=EXCLUDED.bb_middle,
    bb_lower=EXCLUDED.bb_lower, sma=EXCLUDED.sma,
    supertrend_value=EXCLUDED.supertrend_value,
    supertrend_dir=EXCLUDED.supertrend_dir,
    close_price=EXCLUDED.close_price,
    {_EMA_SET},
    param_set=EXCLUDED.param_set, source=EXCLUDED.source
"""


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
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return set(json.load(f))
    return set()


def save_checkpoint(done):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(sorted(done), f)


# =====================================================================
def gaps_to_fetch(bounds, floor_date, want_start, today, rebuild):
    """What is MISSING for this stock -- the whole point of v3.

    Returns a list of (start, end) ranges to ask Kite for. Empty means
    everything needed is already stored, and the stock costs zero
    requests tonight.

    Two possible gaps:
      TAIL -- days after the newest stored candle. This is the normal
              one, and normally a single day.
      HEAD -- days before the oldest stored candle, which only appears
              when --years is widened after candles were already stored,
              or on a stock whose history was partially fetched.
    """
    if rebuild or not bounds:
        return [(want_start, today)], True

    first, last, _ = bounds
    ranges = []
    asked_head = False

    # HEAD: only when the stored history genuinely starts later than we
    # want, and only if we have not already asked this far back once.
    if first > want_start + timedelta(days=HEAD_SLACK_DAYS):
        already = (floor_date is not None and floor_date <= want_start)
        if not already:
            ranges.append((want_start, first - timedelta(days=1)))
            asked_head = True

    # TAIL: everything after the newest stored candle.
    if last < today:
        ranges.append((last + timedelta(days=1), today))

    return ranges, asked_head


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--symbols")
    ap.add_argument("--years", type=float, default=DEFAULT_YEARS)
    ap.add_argument("--history", type=int, default=1,
                    help="bars stored per timeframe (default: latest only)")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--batch-pause", type=float, default=BATCH_PAUSE_SEC)
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the checkpoint and revisit every stock")
    ap.add_argument("--rebuild-ohlc", action="store_true",
                    help="re-download full history and overwrite stored candles")
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",")]
               if args.symbols else None)

    today = date.today()
    want_start = today - timedelta(days=int(args.years * 365.25))

    kite = get_session(auto_login=True)
    if not kite:
        print("Could not authenticate with Kite.")
        sys.exit(1)

    instrument_map = load_instrument_map(kite)

    with psycopg.connect(DB) as conn:

        # The candle store has to exist before anything can be
        # incremental. Checked here rather than discovered as a
        # permission error 400 stocks into an unattended run.
        if not ohlc_table_exists(conn):
            print("\n" + "=" * 66)
            print("stock_ohlc_daily does not exist.")
            print("Without it there is nothing to store candles in, so every")
            print("run would re-download full history -- the slow behaviour")
            print("this version exists to remove.")
            print("\n  /opt/mfapi/venv/bin/python3 /opt/mfapi/create_ohlc_table.py "
                  "--admin doadmin")
            print("=" * 66 + "\n")
            sys.exit(1)

        targets = load_targets(conn, args.limit, symbols)
        if not targets:
            print("No stocks selected.")
            sys.exit(1)

        done = set() if args.fresh else load_checkpoint()
        todo = [(i, s) for i, s in targets if s not in done]

        bounds_map = load_stored_bounds(conn)
        floor = load_floor()

        have = sum(1 for i, _ in todo if i in bounds_map)
        first_run = have == 0 or args.rebuild_ohlc

        n_batches = (len(todo) + args.batch_size - 1) // max(args.batch_size, 1)
        if first_run:
            reqs = max(1, int(args.years * 365.25 / MAX_DAYS_PER_REQUEST) + 1)
        else:
            reqs = 1
        est_sec = (len(todo) * reqs / RATE_LIMIT_PER_SEC
                   + max(0, n_batches - 1) * args.batch_pause)

        print(f"\n{len(targets)} selected | {len(done)} done | {len(todo)} to fetch")
        print(f"{have} of them already have stored candles")
        print(f"window: {want_start} to {today} ({args.years:g} years)")
        if first_run:
            print("\nFIRST RUN -- nothing stored yet, so this one downloads full")
            print("history and will take about as long as the old version did.")
            print("Every run after this fetches only the missing days.\n")
        print(f"~{reqs} request(s)/stock, batches of {args.batch_size}, "
              f"{args.batch_pause:.0f}s pause between")
        print(f"Estimated: ~{est_sec / 60:.1f} minutes\n")

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO batch_run_log (run_type, business_date, status)
                VALUES ('FETCH_TECHNICALS', CURRENT_DATE, 'RUNNING')
                RETURNING run_id
            """)
            run_id = cur.fetchone()[0]
            conn.commit()

        written, candles_stored, skipped = 0, 0, []
        no_fetch_needed = 0
        data_date = None
        started = time.monotonic()

        for i, (isin, symbol) in enumerate(todo, 1):
            token = instrument_map.get(symbol)
            if not token:
                skipped.append((symbol, "no instrument token"))
                continue

            try:
                floor_raw = floor.get(isin)
                floor_date = (date.fromisoformat(floor_raw)
                              if floor_raw else None)

                ranges, asked_head = gaps_to_fetch(
                    bounds_map.get(isin), floor_date,
                    want_start, today, args.rebuild_ohlc)

                if not ranges:
                    no_fetch_needed += 1

                # ---- fetch only what is missing, and keep it ----
                new_rows = 0
                for start, end in ranges:
                    got = fetch_range(kite, token, start, end)
                    new_rows += store_daily(conn, isin, got)
                candles_stored += new_rows

                # Record how far back we have now ASKED, so a stock that
                # simply has no older history is not re-asked nightly.
                if asked_head or args.rebuild_ohlc or not bounds_map.get(isin):
                    floor[isin] = want_start.isoformat()

                # ---- compute from the STORE, not from the download ----
                # One source of truth. Whether tonight fetched one day or
                # eleven years, the indicators see the same series.
                daily = load_stored_daily(conn, isin, want_start)

                if daily is None or len(daily) < 60:
                    n = 0 if daily is None else len(daily)
                    skipped.append((symbol, f"only {n} candles"))
                    continue

                rows = build_rows(isin, daily, args.history)
                if not rows:
                    skipped.append((symbol, "insufficient history for any timeframe"))
                    continue

                with conn.cursor() as cur:
                    cur.executemany(UPSERT, rows)
                conn.commit()

                written += len(rows)
                done.add(symbol)

                for r in rows:
                    if r[2] == "DAILY" and (data_date is None or r[1] > data_date):
                        data_date = r[1]

                tfs = len({r[2] for r in rows})
                note = ("up to date" if new_rows == 0
                        else f"+{new_rows} new candle{'s' if new_rows != 1 else ''}")
                print(f"[{i}/{len(todo)}] {symbol:<14} {len(daily):>5} stored  "
                      f"{note:<20} -> {len(rows)} rows / {tfs} timeframes")

            except Exception as e:
                skipped.append((symbol, str(e)[:90]))
                print(f"[{i}/{len(todo)}] {symbol:<14} FAILED: {str(e)[:70]}")

            # ---- batch boundary: checkpoint, then genuinely pause ----
            if i % args.batch_size == 0 and i < len(todo):
                save_checkpoint(done)
                save_floor(floor)
                elapsed = time.monotonic() - started
                remaining = (elapsed / i) * (len(todo) - i)
                print(f"\n  -- batch done ({i}/{len(todo)}), pausing "
                      f"{args.batch_pause:.0f}s | ~{remaining / 60:.0f} min left --\n")
                time.sleep(args.batch_pause)

        save_checkpoint(done)
        save_floor(floor)

        # business_date is the date of the DATA, not the date the job
        # happened to run. A run that starts at 11:30pm Friday and
        # finishes after midnight was still working on Friday's market,
        # and a log that says Saturday makes every later investigation
        # start from a wrong fact.
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE batch_run_log
                   SET status=%s, finished_at=NOW(),
                       business_date=COALESCE(%s, business_date),
                       rows_read=%s, rows_written=%s, error_message=%s
                 WHERE run_id=%s
            """, ('PARTIAL' if skipped else 'SUCCESS', data_date,
                  len(todo), written,
                  f"{len(skipped)} skipped" if skipped else None, run_id))
            conn.commit()

        mins = (time.monotonic() - started) / 60
        print(f"\n{'=' * 66}")
        print(f"Wrote {written} indicator rows. run_id = {run_id} ({mins:.1f} min)")
        print(f"Stored {candles_stored:,} new daily candles.")
        print(f"Kite requests: {REQUESTS['n']:,}"
              f"   |  {no_fetch_needed} stock(s) needed no download at all")
        if data_date:
            print(f"Data date: {data_date}")

        if skipped:
            print(f"\nSkipped {len(skipped)}:")
            for s, why in skipped[:15]:
                print(f"  {s:<14} {why}")
            if len(skipped) > 15:
                print(f"  ... and {len(skipped) - 15} more")

        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), COUNT(DISTINCT isin),
                       MIN(as_of_date), MAX(as_of_date)
                FROM stock_ohlc_daily
            """)
            n, stocks, lo, hi = cur.fetchone()
            print(f"\nstock_ohlc_daily:  {n:,} candles  {stocks:,} stocks  "
                  f"{lo} to {hi}")

            cur.execute("""
                SELECT timeframe, COUNT(*), COUNT(DISTINCT isin), MAX(as_of_date)
                FROM stock_technical GROUP BY 1 ORDER BY 1
            """)
            print("\nstock_technical:")
            for tf, c, stocks, latest in cur.fetchall():
                print(f"  {tf:<9} {c:>7} rows  {stocks:>5} stocks  latest {latest}")

            cur.execute("""
                SELECT m.symbol, t.timeframe,
                       ROUND(t.rsi,1), ROUND(t.adx,1),
                       ROUND(t.plus_di,1), ROUND(t.minus_di,1),
                       t.supertrend_dir,
                       ROUND(t.close_price,1), ROUND(t.bb_lower,1), ROUND(t.bb_upper,1)
                FROM stock_technical t JOIN stock_master m USING (isin)
                WHERE t.as_of_date = (SELECT MAX(as_of_date) FROM stock_technical
                                      WHERE timeframe = t.timeframe)
                ORDER BY m.symbol, t.timeframe LIMIT 12
            """)
            print("\nSample (symbol, tf, rsi, adx, +di, -di, st, close, bb_lo, bb_hi):")
            for r in cur.fetchall():
                print("  ", r)


if __name__ == "__main__":
    main()
