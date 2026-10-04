-- ============================================================
-- returns_table.sql
-- Run once in pgAdmin against DROPLET - finchaya_mf.
--
-- One row per fund per period. Computed nightly by score_returns.py so
-- the API reads a table rather than doing arithmetic per request --
-- the same "expensive work once, cheap reads many times" shape as
-- mf_score.
-- ============================================================

CREATE TABLE IF NOT EXISTS mf_returns (
    scheme_code     varchar(20) NOT NULL REFERENCES mf_scheme(scheme_code),
    as_of_date      date        NOT NULL,
    -- '1Y','3Y','5Y','10Y', or 'SI' for since-inception
    period          varchar(4)  NOT NULL,

    -- The window actually measured. NAV dates fall on trading days, so
    -- the real start is the nearest prior date to the target, and the
    -- CAGR is computed over the elapsed time rather than the nominal
    -- period. Storing both dates makes any number reproducible.
    start_date      date        NOT NULL,
    end_date        date        NOT NULL,
    years           numeric(6,3) NOT NULL,

    start_nav       numeric(18,6),
    end_nav         numeric(18,6),
    fund_cagr       numeric(8,3),

    -- Null where the fund has no benchmark mapped, which is the honest
    -- state for sectoral funds whose index we don't hold.
    benchmark_id    integer REFERENCES benchmark_master(benchmark_id),
    bench_start     numeric(18,4),
    bench_end       numeric(18,4),
    bench_cagr      numeric(8,3),
    excess_cagr     numeric(8,3),
    -- EXACT or PROXY, copied from mf_benchmark_map so the page can
    -- disclose a substitution without a second join.
    match_type      varchar(10),

    created_at      timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scheme_code, as_of_date, period)
);

CREATE INDEX IF NOT EXISTS idx_mf_returns_lookup
    ON mf_returns (scheme_code, as_of_date);

COMMENT ON COLUMN mf_returns.period IS
  'SI is written only when the fund''s first NAV is meaningfully later '
  'than the start of the NAV data itself. Otherwise the first NAV is '
  'the edge of the backfill, not the fund''s launch, and calling it '
  'inception would be wrong for exactly the oldest funds.';

COMMENT ON COLUMN mf_returns.years IS
  'Elapsed years between the matched start and end dates, not the '
  'nominal period. A "5Y" row may measure 5.02 years; the CAGR is '
  'computed on what was actually measured.';
