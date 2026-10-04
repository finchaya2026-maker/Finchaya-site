"""
backfill_ema.py -- fill the EMA columns on rows that already exist.
-------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/backfill_ema.py
    --limit 20        only the first N stocks (a trial run)
    --symbols TCS,INFY
    --dry-run         compute and report, write nothing
    --only-missing    skip stocks whose EMAs are already filled (default on)
    --force           recompute even where they are filled

WHY NOT JUST RE-RUN fetch_technicals.py
    Because that would rewrite every column -- macd, adx, supertrend, the
    lot -- on 1.6M rows, and those columns feed stock scores that are
    already written. Recomputing them SHOULD be a no-op, but "should be a
    no-op" is how a quiet change to every score in the database gets made.
    This touches the seven new columns and nothing else.

    It also needs no Kite calls. The candles are in stock_ohlc_daily
    already, so this is CPU and one UPDATE per stock.

WHY IT REBUILDS FROM THE FULL CANDLE HISTORY EVERY TIME
    An EMA at a given bar depends on every bar before it. You cannot fill
    in a missing month by looking at that month -- the whole series has to
    be walked from its seed. So each stock is read in full, all three
    timeframes are computed, and the rows are updated together.

THE HONEST PART OF THE OUTPUT
    A 200-period EMA needs 200 bars of that timeframe. Monthly bars start
    in 2014, so a 200-month EMA needs sixteen years and essentially
    nothing has it -- those stay NULL, for ever, and that is correct. The
    summary at the end says how many values each period actually got, per
    timeframe, so an empty column is visibly a fact about the data rather
    than a suspected bug in this script.
"""

import argparse
import os
import sys
import time
from collections import defaultdict

import pandas as pd
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

sys.path.insert(0, "/opt/mfapi")
import indicators as ind                                    # noqa: E402

# NOT imported from fetch_technicals: that module cannot load without the
# Kite client, and this job touches no broker. The period list is the one
# thing it would need, and a list of seven integers is not worth a
# dependency on a login.
EMA_PERIODS = (5, 10, 20, 26, 50, 100, 200)
to_weekly_real_days = ind.to_weekly_real_days


def safe(value):
    """NaN -> None, so Postgres stores a real NULL rather than 'NaN'."""
    if value is None or pd.isna(value):
        return None
    return float(value)

load_dotenv("/opt/mfapi/.env")
DB = (os.environ.get("FINCHAYA_DB") or os.environ.get("DATABASE_URL")
      or os.environ.get("MF_DSN"))

TARGETS = """
SELECT isin, symbol FROM stock_master WHERE is_active ORDER BY symbol
"""

CANDLES = """
SELECT as_of_date, open, high, low, close, volume
FROM stock_ohlc_daily
WHERE isin = %(isin)s
ORDER BY as_of_date
"""

# Which stocks still need it, so a re-run after an interruption is cheap.
FILLED = """
SELECT DISTINCT isin FROM stock_technical WHERE ema_200 IS NOT NULL
"""

# ONE STATEMENT PER STOCK PER TIMEFRAME, NOT ONE PER ROW.
#
# The obvious shape is an UPDATE per bar, and it is what this did first.
# The arithmetic kills it: roughly 1,790 daily + 376 weekly + 84 monthly
# bars per stock, times 2,165 stocks, is 4.9 MILLION round trips to a
# managed database across the network. At a conservative millisecond each
# that is an hour and a half of latency doing nothing.
#
# UPDATE ... FROM (VALUES ...) sends the whole series for one stock and
# one timeframe in a single statement -- 6,500 statements for the
# universe instead of 4.9 million. The work is identical; only the number
# of times it asks permission changes.
#
# Chunked at CHUNK_ROWS because a statement carries one parameter per
# value and Postgres stops at 65,535 of them. Eight columns times 1,000
# rows is 8,000, comfortably inside it.
CHUNK_ROWS = 1000


def flush(cur, isin, timeframe, rows):
    """Write one stock's EMA series for one timeframe, in one statement.

    `rows` is a list of (date, ema_5, ema_10, ... ema_200).

    Casts are explicit on the VALUES side. Without them Postgres sees a
    column of untyped parameters and refuses to compare it with a date,
    which is the kind of error that only appears against the real table.
    """
    written = 0
    cols = "d, " + ", ".join("e%d" % p for p in EMA_PERIODS)
    sets = ", ".join("ema_%d = v.e%d::numeric" % (p, p) for p in EMA_PERIODS)
    width = 1 + len(EMA_PERIODS)
    one = "(" + ",".join(["%s"] * width) + ")"

    for i in range(0, len(rows), CHUNK_ROWS):
        chunk = rows[i:i + CHUNK_ROWS]
        sql = """
            UPDATE stock_technical t SET {sets}
            FROM (VALUES {values}) AS v({cols})
            WHERE t.isin = %s AND t.timeframe = %s
              AND t.as_of_date = v.d::date
        """.format(sets=sets, values=",".join([one] * len(chunk)), cols=cols)
        params = []
        for r in chunk:
            params.extend(r)
        params.extend([isin, timeframe])
        cur.execute(sql, params)
        written += cur.rowcount
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--symbols")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()


    started = time.time()
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) AS n FROM information_schema.columns
            WHERE table_name = 'stock_technical' AND column_name = 'ema_200'
        """)
        if not cur.fetchone()["n"]:
            sys.exit("stock_technical has no ema_200 column. "
                     "Run add_ema_columns.py first.")

        cur.execute(TARGETS)
        targets = cur.fetchall()
        if args.symbols:
            want = {s.strip().upper() for s in args.symbols.split(",")}
            targets = [t for t in targets if t["symbol"].upper() in want]
        if not args.force:
            cur.execute(FILLED)
            done = {r["isin"] for r in cur.fetchall()}
            before = len(targets)
            targets = [t for t in targets if t["isin"] not in done]
            if before != len(targets):
                print("already filled: %d stock(s) skipped (--force to redo)"
                      % (before - len(targets)))
        if args.limit:
            targets = targets[:args.limit]

        print("stocks to process : %d" % len(targets))
        print("periods           : %s" % ", ".join(str(p) for p in EMA_PERIODS))
        print()
        if not targets:
            print("Nothing to do.")
            return

        # filled[timeframe][period] = how many values were written
        filled = defaultdict(lambda: defaultdict(int))
        rows_touched = 0
        no_candles = []

        for i, t in enumerate(targets, 1):
            cur.execute(CANDLES, {"isin": t["isin"]})
            candles = cur.fetchall()
            if len(candles) < min(EMA_PERIODS):
                no_candles.append(t["symbol"])
                continue

            daily = pd.DataFrame(candles)
            daily["as_of_date"] = pd.to_datetime(daily["as_of_date"])
            daily = daily.set_index("as_of_date").astype(float)
            daily.index.name = "date"

            frames = {
                "DAILY": daily,
                "WEEKLY": to_weekly_real_days(daily),
                "MONTHLY": ind.to_monthly(daily),
            }

            for tf, frame in frames.items():
                if frame.empty:
                    continue
                out = ind.ema(frame, periods=EMA_PERIODS)
                batch = []
                for ts, r in out.iterrows():
                    vals = [safe(r.get("ema_%d" % p)) for p in EMA_PERIODS]
                    if all(v is None for v in vals):
                        continue          # nothing to write for this bar
                    for p, v in zip(EMA_PERIODS, vals):
                        if v is not None:
                            filled[tf][p] += 1
                    batch.append([ts.date()] + vals)
                if batch and not args.dry_run:
                    rows_touched += flush(cur, t["isin"], tf, batch)

            if not args.dry_run:
                conn.commit()
            if i % 100 == 0 or i == len(targets):
                print("  %d/%d  %s  (%.0fs)"
                      % (i, len(targets), t["symbol"][:16],
                         time.time() - started))

    print()
    print("=" * 62)
    print("VALUES COMPUTED, BY TIMEFRAME AND PERIOD")
    print("=" * 62)
    print("Computed from the CANDLES. Not all of them land: an EMA is")
    print("written only where stock_technical already has a row for that")
    print("bar, and that table is far sparser than the candle history.")
    print("The coverage line below says how much of it was reachable.")
    print()
    for tf in ("DAILY", "WEEKLY", "MONTHLY"):
        if tf not in filled:
            continue
        parts = []
        for p in EMA_PERIODS:
            n = filled[tf][p]
            parts.append("%d:%s" % (p, "{:,}".format(n) if n else "none"))
        print("  %-8s %s" % (tf, "  ".join(parts)))
    print()

    empty = [(tf, p) for tf in filled for p in EMA_PERIODS if not filled[tf][p]]
    if empty:
        print("Empty, and correctly so -- not enough bars of that timeframe")
        print("exist for the period. A 200-month EMA needs sixteen years of")
        print("monthly candles; the oldest here start in 2014.")
        for tf, p in empty:
            print("  %s ema_%d" % (tf, p))
        print()

    if no_candles:
        print("no candles stored: %d stock(s)%s"
              % (len(no_candles),
                 " -- " + ", ".join(no_candles[:8]) if no_candles else ""))
        print()

    if args.dry_run:
        print("--dry-run: nothing written.")
    else:
        print("rows updated : %s" % "{:,}".format(rows_touched))

        # COVERAGE, because "13,668 rows updated" next to "34,285 values
        # computed" reads like a bug and is not one.
        #
        # stock_technical is written one bar per timeframe per nightly run
        # (fetch_technicals --history defaults to 1), so it holds roughly
        # one date per run -- a few hundred -- while stock_ohlc_daily holds
        # every trading day back to 2014. The EMAs are computed over the
        # full candle history, which is the only correct way to compute
        # them, and then land on the subset of bars the indicator table
        # actually has rows for.
        with psycopg.connect(DB, row_factory=dict_row) as c2, c2.cursor() as k:
            k.execute("""
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE ema_5 IS NOT NULL) AS filled
                FROM stock_technical WHERE isin = ANY(%(codes)s)
            """, {"codes": [t["isin"] for t in targets]})
            cov = k.fetchone()
        if cov["total"]:
            print("coverage     : %s of %s rows for these stocks now carry EMAs"
                  " (%.0f%%)"
                  % ("{:,}".format(cov["filled"]), "{:,}".format(cov["total"]),
                     100.0 * cov["filled"] / cov["total"]))
            print()
            print("             A row without an EMA is a bar too early in")
            print("             the stock's history for even a 5-period one.")
    print("elapsed      : %.1f min" % ((time.time() - started) / 60))


if __name__ == "__main__":
    main()
