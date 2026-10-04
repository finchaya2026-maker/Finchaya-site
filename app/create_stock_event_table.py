"""
create_stock_event_table.py -- one-time migration. Run once, by hand.

WHY ONE TABLE FOR ANNOUNCEMENTS, BOARD MEETINGS, DIVIDENDS/SPLITS/BONUS
AND RESULT FILINGS, RATHER THAN FOUR
    NSE exposes these as four separate endpoints, but to a reader on the
    stock page they are the same question asked four ways: "what has this
    company told the exchange, and when." One table with a `kind` column
    -- the same shape finchaya.js already uses for fund/stock ownership
    changes (CH_LABEL / CH_TONE) -- means one feed, one sort order, one
    render path, instead of four lists that would need merging by date
    every time the page draws them anyway.

WHY (isin, kind, event_date, source_ref) IS THE KEY
    NSE gives every announcement a seq_id but corporate actions and board
    meetings don't carry one -- so source_ref falls back to a hash of the
    fields that make a row unique when no ID exists. Re-running the
    fetch script is then always safe: an unchanged row upserts onto
    itself instead of duplicating.

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
CREATE TABLE IF NOT EXISTS stock_event (
    isin          text        NOT NULL,
    kind          text        NOT NULL,   -- announcement | board_meeting |
                                           -- dividend | split | bonus |
                                           -- rights | buyback |
                                           -- corp_action_other | result
    event_date    date        NOT NULL,   -- ex-date / meeting date / filing
                                           -- date, whichever the kind uses
    headline      text        NOT NULL,
    detail        text,
    period        text,                   -- results only: e.g. "Q2 FY26"
    attachment_url text,
    source        text        NOT NULL,   -- 'NSE' or 'BSE'
    source_ref    text        NOT NULL,   -- dedup key: their id, or a hash
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (isin, kind, event_date, source_ref)
);

CREATE INDEX IF NOT EXISTS ix_stock_event_isin_date
    ON stock_event (isin, event_date DESC);

-- For the screener: "every dividend/result/split/bonus/rights row due in
-- the next N days", scanned across every stock rather than one at a time.
CREATE INDEX IF NOT EXISTS ix_stock_event_kind_date
    ON stock_event (kind, event_date);
"""

with psycopg.connect(DB) as conn, conn.cursor() as cur:
    cur.execute(DDL)
    conn.commit()
    print("stock_event: table present.")
