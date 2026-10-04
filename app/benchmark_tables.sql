-- ============================================================
-- benchmark_tables.sql
-- Run once, in pgAdmin, against DROPLET - finchaya_mf.
--
-- THREE TABLES
--   benchmark_master    the indices themselves, one row each
--   benchmark_nav       the daily TRI series
--   mf_benchmark_map    which index a fund is compared against
--
-- WHY THE MAP IS ITS OWN TABLE rather than a column on mf_scheme:
-- a mapping carries more than an id. It carries how confident we are,
-- what the fund's own documents say, and whether a human has checked
-- it. Those belong beside the mapping, not scattered.
-- ============================================================

CREATE TABLE IF NOT EXISTS benchmark_master (
    benchmark_id    serial PRIMARY KEY,
    -- exactly as it appears in the IndexName column of the NSE CSVs,
    -- so the loader can find its own row without a lookup table
    index_name      varchar(120) NOT NULL UNIQUE,
    display_name    varchar(120) NOT NULL,
    provider        varchar(20)  NOT NULL DEFAULT 'NSE',
    is_tri          boolean      NOT NULL DEFAULT TRUE,
    first_date      date,
    last_date       date,
    day_count       integer,
    updated_at      timestamptz  NOT NULL DEFAULT NOW()
);

COMMENT ON COLUMN benchmark_master.is_tri IS
  'Total Return Index. Fund NAV includes dividends, so a price index '
  'would understate the benchmark by roughly 1.5%/yr. Never compare '
  'a NAV series against is_tri = FALSE.';


CREATE TABLE IF NOT EXISTS benchmark_nav (
    benchmark_id    integer NOT NULL REFERENCES benchmark_master(benchmark_id),
    index_date      date    NOT NULL,
    index_value     numeric(18,4) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (benchmark_id, index_date)
);

-- Returns look up "the value on or before date X" constantly, because
-- period boundaries land on weekends and holidays.
CREATE INDEX IF NOT EXISTS idx_benchmark_nav_lookup
    ON benchmark_nav (benchmark_id, index_date DESC);


CREATE TABLE IF NOT EXISTS mf_benchmark_map (
    scheme_code       varchar(20) PRIMARY KEY
                      REFERENCES mf_scheme(scheme_code),
    benchmark_id      integer REFERENCES benchmark_master(benchmark_id),

    -- What the fund's own documents name, verbatim. Kept even when we
    -- substitute, so the page can say "shown against X; the fund states Y".
    stated_benchmark  varchar(200),

    -- EXACT  we hold the index the fund actually states
    -- PROXY  a near-equivalent (BSE 500 shown against Nifty 500)
    -- NONE   no defensible comparison; show no benchmark
    match_type        varchar(10) NOT NULL DEFAULT 'NONE',
    confidence        numeric(4,3) NOT NULL DEFAULT 0,

    note              text,
    reviewed          boolean NOT NULL DEFAULT FALSE,
    updated_at        timestamptz NOT NULL DEFAULT NOW()
);

COMMENT ON COLUMN mf_benchmark_map.reviewed IS
  'FALSE until a human has confirmed it. The site should withhold a '
  'benchmark comparison for anything unreviewed below PROXY confidence.';

CREATE INDEX IF NOT EXISTS idx_mf_benchmark_map_review
    ON mf_benchmark_map (match_type, reviewed);
