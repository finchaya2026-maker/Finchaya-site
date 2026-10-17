"""
add_stock_financials_filed_on.py -- one-time migration. Run once, by hand.

WHAT IT ADDS
    stock_financials.filed_on (date): the day the company filed this result
    with the exchange. It is what the stock page shows as "Filed 8 Oct 2026".

WHY NOT updated_at
    updated_at is when WE stored the row. All 2,374 June-2026 rows say 8 Oct
    2026 because that is the day they were backfilled, not the day the
    companies reported. Filing dates come from BSE's own upload dates
    (Fld_CreateDate in its filing list).

HOW THE DATES GET IN
    - New results: result_watcher.py writes filed_on with the figures.
    - Results already stored: result_watcher.py --stamp-dates fills them in
      from BSE's filing list (no pages downloaded, one list call per stock).

SAFE TO RE-RUN, and safe to run before or after the new code is uploaded: the
column is nullable, nothing existing reads or writes it, and result_watcher.py
and the API both cope with it being absent.
"""

import os
import sys

import psycopg
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv()

DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set.")

with psycopg.connect(DB) as conn, conn.cursor() as cur:
    cur.execute("ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS filed_on date")
    conn.commit()
    cur.execute("""SELECT count(*) AS total, count(filed_on) AS dated
                   FROM stock_financials""")
    total, dated = cur.fetchone()
    print(f"stock_financials.filed_on: column present. {dated} of {total} rows have a filing date "
          f"(run result_watcher.py --stamp-dates to fill the rest).")
