"""
create_stock_shareholding_table.py -- one-time migration. Run once, by hand.

WHAT THE TABLE HOLDS
    One row per company per quarter: who owns the company. The three headline
    figures (promoter, public, employee trusts) come from NSE's market-wide list
    of shareholding-pattern filings. The breakdown of the public part (mutual
    funds, insurers, banks, foreign investors, retail, ...) is read out of each
    filing's XBRL file by fetch_shareholding.py --detail, which is slower (one
    file per company-quarter), so it is filled in separately and flagged by
    detail_loaded. A row is useful before its detail arrives.

WHY percentages are 0-100, NOT fractions
    The new filing format stores 0.7177 for 71.77 %, the older ones store 71.77.
    The loader converts both to 0-100 so the site never has to guess which one
    it is looking at.

WHY (isin, quarter_end) IS THE KEY
    A company files once per quarter. A revised filing replaces the first one
    (same company, same quarter): the loader upserts onto the same row and sets
    revised = true instead of creating a second row.

WHY detail_error EXISTS
    A filing whose XBRL cannot be read (parts that do not add up, promoter figure
    that disagrees with the list, file not found) must not be retried every night
    for ever, nor block the files behind it. The reason is kept here; the loader
    skips such rows unless told --retry-failed. A revision resets it.

WHY promoter_pledged_pct IS ALLOWED TO BE EMPTY
    It is the share of the PROMOTER GROUP'S OWN shares that are pledged or
    otherwise encumbered, as the filing states it (0 when the filing says none,
    empty when the filing does not say or there is no promoter).

SAFE TO RE-RUN. Nothing runs when this file is imported (the tests import DDL).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Every percentage is 0-100. The detail columns stay empty until detail_loaded.
DDL = """
CREATE TABLE IF NOT EXISTS stock_shareholding (
    isin                  text         NOT NULL,
    quarter_end           date         NOT NULL,
    promoter_pct          numeric(7,4) NOT NULL,   -- promoter + promoter group
    public_pct            numeric(7,4) NOT NULL,   -- public shareholders (category B)
    employee_trust_pct    numeric(7,4) NOT NULL DEFAULT 0,  -- employee benefit trusts (category C)
    mf_pct                numeric(7,4),            -- mutual funds / UTI
    insurance_pct         numeric(7,4),            -- insurance companies
    bank_pct              numeric(7,4),            -- banks
    other_dii_pct         numeric(7,4),            -- AIFs, VC funds, provident/pension funds, NBFCs, domestic sovereign funds, other domestic institutions
    fii_pct               numeric(7,4),            -- all foreign portfolio investors and other foreign institutions
    retail_pct            numeric(7,4),            -- resident individuals (up to and above Rs 2 lakh)
    bodies_corporate_pct  numeric(7,4),            -- bodies corporate (public)
    nri_pct               numeric(7,4),            -- non-resident Indians and foreign nationals
    other_public_pct      numeric(7,4),            -- everything else, so the parts add up
    promoter_pledged_pct  numeric(7,4),            -- % of the promoter group's own shares pledged/encumbered
    submitted_on          date,                    -- day the company filed it with NSE
    revised               boolean      NOT NULL DEFAULT false,
    xbrl_url              text,                    -- the filing's XBRL file on NSE's archive
    detail_loaded         boolean      NOT NULL DEFAULT false,
    detail_error          text,                    -- why the XBRL could not be used (empty when fine or not tried)
    source                text         NOT NULL DEFAULT 'NSE',
    updated_at            timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (isin, quarter_end)
);

CREATE INDEX IF NOT EXISTS ix_stock_shareholding_isin
    ON stock_shareholding (isin, quarter_end DESC);

-- the queue of filings whose XBRL still has to be read (newest quarter first)
CREATE INDEX IF NOT EXISTS ix_stock_shareholding_pending
    ON stock_shareholding (quarter_end DESC) WHERE NOT detail_loaded;
"""


def main():
    import psycopg
    from dotenv import load_dotenv

    load_dotenv(os.path.join(HERE, ".env"))
    load_dotenv()
    db = os.getenv("FINCHAYA_DB")
    if not db:
        sys.exit("FINCHAYA_DB is not set.")
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()
        print("stock_shareholding: table present.")


if __name__ == "__main__":
    main()
