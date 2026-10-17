"""
add_stock_financials_operating.py -- one-time migration. Run once, by hand.

WHAT IT ADDS (two nullable columns on stock_financials, in rupees like the other figures)
    operating_profit    revenue from operations minus the costs that are not interest or depreciation,
                        i.e. Revenue - (Expenses - FinanceCosts - Depreciation). Other income is NOT included.
                        Left empty for banks, finance companies and insurers, whose biggest cost IS interest or
                        claims, so a "before interest" profit means nothing for them.
    profit_before_tax   as the company reports it. Filled for every kind of company, and what the results page
                        shows instead of operating profit for the lenders and insurers.

WHY: the quarterly-results page (results.html) shows Sales, Operating profit, Net profit and EPS for the
latest quarter, the quarter before and the same quarter a year ago. Operating profit was never stored.

HOW THE FIGURES GET IN
    - New results: result_watcher.py writes them with the rest of the result, and also reads them for the two
      quarters it is compared with (the one before and the one a year earlier), so nothing needs doing by hand.
    - Results already stored: run  result_watcher.py --operating  once. It reads only the stocks whose latest
      result was filed in the last 30 days (about 3 filing pages each), and touches nothing but these two columns.

SAFE TO RE-RUN, and safe to run before or after the new code is uploaded: the columns are nullable, nothing
existing reads them, and result_watcher.py and the API both cope with them being absent.
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
    cur.execute("ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS operating_profit numeric")
    cur.execute("ALTER TABLE stock_financials ADD COLUMN IF NOT EXISTS profit_before_tax numeric")
    conn.commit()
    cur.execute("""SELECT count(*) AS total, count(operating_profit) AS op, count(profit_before_tax) AS pbt
                   FROM stock_financials""")
    total, op, pbt = cur.fetchone()
    print(f"stock_financials.operating_profit and .profit_before_tax: columns present. {op} and {pbt} of {total} rows "
          f"have them (run  result_watcher.py --operating  to fill the recent ones).")
