"""
create_stock_financials_table.py -- one-time migration. Run once, by hand.

WHY period_type + period_end IS THE KEY, NOT A SEQUENCE NUMBER
    NSE's own bulk financial-results listing already says which of the two
    filing types a given result is ("Quarterly" or "Annual") and which
    period it covers (toDate) -- that's authoritative and doesn't need to
    be re-derived, so each stock has at most one row per (period_type,
    period_end): a re-fetch upserts onto itself instead of duplicating.

WHY consolidated IS A COLUMN, NOT PART OF THE KEY
    A company can file both Consolidated and Non-Consolidated results for
    the same period; fetch_stock_financials.py picks whichever it prefers
    (Consolidated when available) before writing, so only one row per
    period ever exists here -- this table stores the fetcher's chosen
    view, not both.

SAFE TO RE-RUN.
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

DDL = """
CREATE TABLE IF NOT EXISTS stock_financials (
    isin          text        NOT NULL,
    period_type   text        NOT NULL,   -- 'quarterly' | 'yearly'
    period_end    date        NOT NULL,
    period_label  text        NOT NULL,   -- e.g. "Q3 FY25" or "FY 2024-25"
    revenue       numeric,
    profit        numeric,
    eps           numeric,
    debt          numeric,                -- yearly only (total borrowings)
    consolidated  boolean     NOT NULL,
    source        text        NOT NULL,   -- 'NSE'
    source_ref    text        NOT NULL,   -- the filing's own seqNumber
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (isin, period_type, period_end)
);

CREATE INDEX IF NOT EXISTS ix_stock_financials_isin
    ON stock_financials (isin, period_end DESC);
"""

with psycopg.connect(DB) as conn, conn.cursor() as cur:
    cur.execute(DDL)
    conn.commit()
    print("stock_financials: table present.")
