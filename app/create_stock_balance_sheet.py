"""
create_stock_balance_sheet.py -- one-time migration: the table behind P/B, return on equity,
debt to equity, book value per share and market cap.
------------------------------------------------------------------------
One row per balance-sheet date per stock (the March year-end and the September half-year are the
ones companies file; see fetch_stock_financials.balance_sheet_figures). All money is in RUPEES as
the filings give it, not crores. Written by result_watcher.py (--balance-sheet for history, and
automatically as new annual and September results are read).

    net_worth          what belongs to the company's own shareholders (owners' equity; for a bank, capital +
                       reserves; for an insurer, share capital + reserves excluding revaluation)
    total_equity       the same including minority holders (used for debt to equity)
    paid_up, face_value, shares   share capital at that date; shares = paid_up / face_value (None for insurers)
    debt               corporate: borrowings; NBFC: borrowings + debt securities + subordinated + deposits;
                       bank: None (not meaningful); insurer: borrowings
    profit_owners, profit_before_tax, finance_costs, depreciation   the FULL YEAR's, only on rows from an
                       annual filing (period_type 'yearly'); None on a September ('interim') row
    kind               corporate | nbfc | bank | insurer

SAFE TO RE-RUN (CREATE ... IF NOT EXISTS). Touches nothing else.

    python create_stock_balance_sheet.py
"""

import os

import psycopg
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv()

DDL = """
CREATE TABLE IF NOT EXISTS stock_balance_sheet (
    isin              varchar     NOT NULL,
    period_end        date        NOT NULL,
    period_type       text        NOT NULL CHECK (period_type IN ('yearly', 'interim')),
    kind              text        NOT NULL CHECK (kind IN ('corporate', 'nbfc', 'bank', 'insurer')),
    consolidated      boolean     NOT NULL,
    net_worth         numeric,
    total_equity      numeric,
    paid_up           numeric,
    face_value        numeric,
    shares            numeric,
    debt              numeric,
    total_assets      numeric,
    profit_owners     numeric,
    profit_before_tax numeric,
    finance_costs     numeric,
    depreciation      numeric,
    filed_on          date,
    source_ref        text,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (isin, period_end)
);
CREATE INDEX IF NOT EXISTS stock_balance_sheet_latest ON stock_balance_sheet (isin, period_end DESC);
"""


def main():
    db = os.getenv("FINCHAYA_DB")
    if not db:
        raise SystemExit("FINCHAYA_DB is not set")
    with psycopg.connect(db) as conn:
        conn.execute(DDL)
        conn.commit()
        n = conn.execute("SELECT count(*) FROM stock_balance_sheet").fetchone()[0]
    print("stock_balance_sheet is ready (%d rows)." % n)


if __name__ == "__main__":
    main()
