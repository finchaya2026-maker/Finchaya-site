"""
add_ohlc_date_index.py -- OPTIONAL one-time migration: an index on stock_ohlc_daily.as_of_date alone.
------------------------------------------------------------------------
WHY
    stock_ohlc_daily is indexed on (isin, as_of_date). Anything that asks "every stock's close on these dates"
    or "every stock since this date" (the Change in Dow Theory screen reads about a year of days, two of weeks
    and four of months for all 2,500 stocks) cannot use it, so Postgres reads the whole 4.3 million rows each
    time: 7 to 10 seconds from the laptop. An index on the date alone lets it go straight to the rows it needs.
    The screen caches what it reads, so this only shortens the first read after a restart or a new trading day.
    The screen works without it. Not measured on the production table yet: run the screen once before and once
    after and compare the "first call" time.

WHAT IT DOES
    CREATE INDEX CONCURRENTLY IF NOT EXISTS: it does not lock the table against reads or writes, so the nightly
    price job and the site carry on while it builds (about a minute; roughly 100 MB of disk). Safe to re-run.
    If the database login does not own the table, run it as the owner (the other create_*/add_* scripts take
    --admin for the same reason).

    python add_ohlc_date_index.py
"""

import os
import sys
import time

import psycopg
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv()

DDL = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ohlc_date ON stock_ohlc_daily (as_of_date)"


def main():
    db = os.getenv("FINCHAYA_DB")
    if not db:
        sys.exit("FINCHAYA_DB is not set.")
    # CONCURRENTLY cannot run inside a transaction, so the connection is in autocommit mode.
    with psycopg.connect(db, autocommit=True) as conn:
        started = time.time()
        conn.execute(DDL)
        n = conn.execute("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_ohlc_date'").fetchone()[0]
    print("ix_ohlc_date is %s (%.0fs)." % ("in place" if n else "MISSING", time.time() - started))


if __name__ == "__main__":
    main()
