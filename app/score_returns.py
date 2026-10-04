"""
score_returns.py -- trailing returns, fund against benchmark.
--------------------------------------------------------------
Fills mf_returns: 1/3/5/10-year CAGR for every scored fund, and the same
figure for its benchmark over the identical window.

USAGE
    python score_returns.py
    python score_returns.py --date 2026-08-29
    python score_returns.py --explain "HDFC Mid Cap Fund"

THE THREE THINGS THAT MAKE THIS HONEST

1. SAME WINDOW, BOTH SIDES. The benchmark is measured between exactly
   the dates the fund was measured between. Comparing a fund's 5 years
   to an index's 5 years starting a week apart is a different number.

2. NEAREST PRIOR, WITH A LIMIT. Period boundaries land on weekends and
   holidays, so we take the last value on or before the target date. If
   the nearest value is more than MAX_GAP_DAYS away, the period is not
   computed at all -- a fund that launched later would otherwise get a
   "10 year" return measured over six.

3. TOTAL RETURN ON BOTH SIDES. Fund NAV already includes dividends
   received. The benchmark must therefore be a TRI, which is why
   benchmark_master carries is_tri and this refuses anything else.

WHAT IT DOES NOT DO
    It computes point-to-point returns, which swing on their endpoints.
    A fund measured to a market peak looks better than the same fund
    measured a month later. Rolling returns would be steadier; that is
    a later job, not this one.
"""

import os
import sys
import argparse
from datetime import date

import psycopg
from dotenv import load_dotenv

load_dotenv()
DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Check that .env exists in this folder.")

FUND_ALGO = "fund-v1"

# Standard trailing periods, in years.
PERIODS = [("1Y", 1), ("3Y", 3), ("5Y", 5), ("10Y", 10)]

# How far the nearest available date may sit from the target before we
# decline to compute. Two weeks covers any run of holidays; beyond that
# we are measuring something other than what the label claims.
MAX_GAP_DAYS = 15

# A fund whose first NAV is within this many days of the start of the
# NAV data itself is older than our data -- its first NAV is the edge of
# the backfill, not its launch. Such funds get no SI row.
INCEPTION_BUFFER_DAYS = 30


# =====================================================================
# One statement per period, computing every fund at once.
#
# The lateral joins are the heart of it: for each fund and each end of
# the window, take the last NAV on or before the target date. The same
# pattern then runs against benchmark_nav using the fund's OWN matched
# dates, so both sides measure the identical window.
# =====================================================================
COMPUTE = """
WITH params AS (
    SELECT %(as_of)s::date AS as_of,
           (%(as_of)s::date - (%(years)s * INTERVAL '1 year'))::date AS target_start
),
funds AS (
    -- ONE ROW PER FUND, ON THE DIRECT PLAN.
    -- v_fund_canonical picks Direct/Growth where it exists and degrades to
    -- Regular where it does not. Selecting from mf_score instead would take
    -- whichever variant promote_holdings happened to land on -- Regular for
    -- 328 funds -- and a category median mixing Direct and Regular measures
    -- distributor commission, not the manager.
    --
    -- The mf_score EXISTS check keeps the old restriction to funds we score,
    -- but matches on the FUND (name + amc) rather than the scheme code, so a
    -- fund scored on its Regular variant still reports on its Direct one.
    --
    -- nav_scheme_code is the series we read: a fund's own, or its Growth
    -- sibling's when this is an IDCW scheme whose NAV has been reduced
    -- by every distribution it ever paid.
    -- EVERY PLAN OF EVERY SCORED FUND, not just the canonical one.
    --
    -- This used to return c.canonical_scheme_code alone -- Direct/Growth --
    -- which is right for a category median, because a median mixing Direct
    -- and Regular measures distributor commission rather than the manager.
    -- It is wrong for portfolio analysis: a client holds a Regular plan, and
    -- Regular runs roughly a point a year behind. A fund beating its
    -- benchmark by 0.4 in Direct is one whose Regular investors are behind
    -- it, and telling them otherwise is the worst version of the error
    -- because it is specific to their own money.
    --
    -- v_returns_source is keyed per scheme code and already matches
    -- plan_type when it substitutes a Growth sibling for an IDCW series, so
    -- a Regular IDCW fund reads its REGULAR Growth sibling, not the Direct
    -- one. That was the trap here and the view does not fall into it.
    SELECT DISTINCT tgt.scheme_code,
           COALESCE(v.nav_scheme_code, tgt.scheme_code) AS nav_scheme_code
    FROM v_fund_canonical c
    JOIN mf_scheme tgt ON tgt.scheme_name = c.scheme_name
                      AND tgt.amc_name IS NOT DISTINCT FROM c.amc_name
    LEFT JOIN v_returns_source v ON v.scheme_code = tgt.scheme_code
    WHERE EXISTS (
        SELECT 1
        FROM mf_scheme sib
        JOIN mf_score s ON s.scheme_code = sib.scheme_code
        WHERE sib.scheme_name = c.scheme_name
          AND sib.amc_name IS NOT DISTINCT FROM c.amc_name
          AND s.algo_version = %(algo)s
          AND s.as_of_date = (SELECT MAX(as_of_date) FROM mf_score
                               WHERE algo_version = %(algo)s)
    )
),
fundmap AS (
    -- The benchmark mapping sits on whichever scheme code map_benchmarks
    -- processed, which is not the canonical one. Resolve it across the
    -- fund's siblings, best match first.
    -- Keyed per scheme code now, not per canonical fund, since every plan
    -- gets a row. The mapping still resolves across the fund's siblings --
    -- map_benchmarks wrote it onto whichever code it processed -- so all
    -- plans of one fund share the benchmark, which is correct: the index a
    -- fund measures itself against does not depend on the plan.
    SELECT DISTINCT ON (tgt.scheme_code)
           tgt.scheme_code,
           m.benchmark_id, m.match_type
    FROM mf_scheme tgt
    JOIN mf_scheme sib ON sib.scheme_name = tgt.scheme_name
                      AND sib.amc_name IS NOT DISTINCT FROM tgt.amc_name
    JOIN mf_benchmark_map m ON m.scheme_code = sib.scheme_code
    WHERE m.benchmark_id IS NOT NULL
      AND m.match_type IN ('EXACT', 'PROXY', 'CATEGORY')
    ORDER BY tgt.scheme_code,
             CASE m.match_type WHEN 'EXACT' THEN 1
                               WHEN 'PROXY' THEN 2
                               ELSE 3 END,
             m.confidence DESC NULLS LAST
),
ends AS (
    SELECT f.scheme_code, f.nav_scheme_code, p.as_of, p.target_start,
           e.nav AS end_nav, e.nav_date AS end_date
    FROM funds f
    CROSS JOIN params p
    CROSS JOIN LATERAL (
        SELECT n.nav, n.nav_date FROM mf_nav n
        WHERE n.scheme_code = f.nav_scheme_code AND n.nav_date <= p.as_of
        ORDER BY n.nav_date DESC LIMIT 1
    ) e
),
windows AS (
    SELECT e.*, s.nav AS start_nav, s.nav_date AS start_date
    FROM ends e
    CROSS JOIN LATERAL (
        SELECT n.nav, n.nav_date FROM mf_nav n
        WHERE n.scheme_code = e.nav_scheme_code AND n.nav_date <= e.target_start
        ORDER BY n.nav_date DESC LIMIT 1
    ) s
    -- Reject a start date that drifted too far from the target: that
    -- means the fund did not exist yet, not that a holiday intervened.
    WHERE e.target_start - s.nav_date <= %(max_gap)s
      AND s.nav > 0
      AND e.end_date > s.nav_date
),
priced AS (
    SELECT w.*,
           (w.end_date - w.start_date) / 365.25::numeric AS yrs
    FROM windows w
),
withbench AS (
    SELECT p.*, map.benchmark_id, map.match_type,
           bs.index_value AS bench_start, be.index_value AS bench_end
    FROM priced p
    LEFT JOIN fundmap map ON map.scheme_code = p.scheme_code
    -- is_tri guard: a price index here would understate the benchmark
    -- by the dividend yield, every year, flattering every fund.
    LEFT JOIN benchmark_master bm
           ON bm.benchmark_id = map.benchmark_id AND bm.is_tri
    LEFT JOIN LATERAL (
        SELECT n.index_value FROM benchmark_nav n
        WHERE n.benchmark_id = bm.benchmark_id AND n.index_date <= p.start_date
        ORDER BY n.index_date DESC LIMIT 1
    ) bs ON TRUE
    LEFT JOIN LATERAL (
        SELECT n.index_value FROM benchmark_nav n
        WHERE n.benchmark_id = bm.benchmark_id AND n.index_date <= p.end_date
        ORDER BY n.index_date DESC LIMIT 1
    ) be ON TRUE
)
INSERT INTO mf_returns
    (scheme_code, as_of_date, period, start_date, end_date, years,
     start_nav, end_nav, fund_cagr,
     benchmark_id, bench_start, bench_end, bench_cagr, excess_cagr,
     match_type, nav_scheme_code)
SELECT
    w.scheme_code, w.as_of, %(label)s, w.start_date, w.end_date,
    ROUND(w.yrs, 3),
    w.start_nav, w.end_nav,
    ROUND(((POWER(w.end_nav / w.start_nav, 1.0 / w.yrs) - 1) * 100)::numeric, 3),
    w.benchmark_id, w.bench_start, w.bench_end,
    CASE WHEN w.bench_start > 0 AND w.bench_end IS NOT NULL THEN
        ROUND(((POWER(w.bench_end / w.bench_start, 1.0 / w.yrs) - 1) * 100)::numeric, 3)
    END,
    CASE WHEN w.bench_start > 0 AND w.bench_end IS NOT NULL THEN
        ROUND((((POWER(w.end_nav / w.start_nav, 1.0 / w.yrs) - 1)
              - (POWER(w.bench_end / w.bench_start, 1.0 / w.yrs) - 1)) * 100)::numeric, 3)
    END,
    w.match_type, w.nav_scheme_code
FROM withbench w
WHERE w.yrs > 0.5
ON CONFLICT (scheme_code, as_of_date, period) DO UPDATE SET
    start_date  = EXCLUDED.start_date,
    end_date    = EXCLUDED.end_date,
    years       = EXCLUDED.years,
    start_nav   = EXCLUDED.start_nav,
    end_nav     = EXCLUDED.end_nav,
    fund_cagr   = EXCLUDED.fund_cagr,
    benchmark_id= EXCLUDED.benchmark_id,
    bench_start = EXCLUDED.bench_start,
    bench_end   = EXCLUDED.bench_end,
    bench_cagr  = EXCLUDED.bench_cagr,
    excess_cagr = EXCLUDED.excess_cagr,
    match_type  = EXCLUDED.match_type,
    nav_scheme_code = EXCLUDED.nav_scheme_code,
    created_at  = NOW()
"""


# Since inception. Same shape, but the start is the fund's first NAV --
# and only for funds whose first NAV is genuinely later than the start
# of the NAV data, otherwise "inception" is just where the backfill began.
COMPUTE_SI = """
WITH data_start AS (
    SELECT MIN(nav_date) AS d FROM mf_nav
),
canon AS (
    -- EVERY PLAN OF EVERY SCORED FUND, not just the canonical one.
    --
    -- This used to return c.canonical_scheme_code alone -- Direct/Growth --
    -- which is right for a category median, because a median mixing Direct
    -- and Regular measures distributor commission rather than the manager.
    -- It is wrong for portfolio analysis: a client holds a Regular plan, and
    -- Regular runs roughly a point a year behind. A fund beating its
    -- benchmark by 0.4 in Direct is one whose Regular investors are behind
    -- it, and telling them otherwise is the worst version of the error
    -- because it is specific to their own money.
    --
    -- v_returns_source is keyed per scheme code and already matches
    -- plan_type when it substitutes a Growth sibling for an IDCW series, so
    -- a Regular IDCW fund reads its REGULAR Growth sibling, not the Direct
    -- one. That was the trap here and the view does not fall into it.
    SELECT DISTINCT tgt.scheme_code,
           COALESCE(v.nav_scheme_code, tgt.scheme_code) AS nav_scheme_code
    FROM v_fund_canonical c
    JOIN mf_scheme tgt ON tgt.scheme_name = c.scheme_name
                      AND tgt.amc_name IS NOT DISTINCT FROM c.amc_name
    LEFT JOIN v_returns_source v ON v.scheme_code = tgt.scheme_code
    WHERE EXISTS (
        SELECT 1
        FROM mf_scheme sib
        JOIN mf_score s ON s.scheme_code = sib.scheme_code
        WHERE sib.scheme_name = c.scheme_name
          AND sib.amc_name IS NOT DISTINCT FROM c.amc_name
          AND s.algo_version = %(algo)s
          AND s.as_of_date = (SELECT MAX(as_of_date) FROM mf_score
                               WHERE algo_version = %(algo)s)
    )
),
fundmap AS (
    -- Keyed per scheme code now, not per canonical fund, since every plan
    -- gets a row. The mapping still resolves across the fund's siblings --
    -- map_benchmarks wrote it onto whichever code it processed -- so all
    -- plans of one fund share the benchmark, which is correct: the index a
    -- fund measures itself against does not depend on the plan.
    SELECT DISTINCT ON (tgt.scheme_code)
           tgt.scheme_code,
           m.benchmark_id, m.match_type
    FROM mf_scheme tgt
    JOIN mf_scheme sib ON sib.scheme_name = tgt.scheme_name
                      AND sib.amc_name IS NOT DISTINCT FROM tgt.amc_name
    JOIN mf_benchmark_map m ON m.scheme_code = sib.scheme_code
    WHERE m.benchmark_id IS NOT NULL
      AND m.match_type IN ('EXACT', 'PROXY', 'CATEGORY')
    ORDER BY tgt.scheme_code,
             CASE m.match_type WHEN 'EXACT' THEN 1
                               WHEN 'PROXY' THEN 2
                               ELSE 3 END,
             m.confidence DESC NULLS LAST
),
funds AS (
    SELECT c.scheme_code, c.nav_scheme_code, MIN(n.nav_date) AS first_date
    FROM canon c
    JOIN mf_nav n ON n.scheme_code = c.nav_scheme_code
    GROUP BY 1, 2
),
young AS (
    SELECT f.scheme_code, f.nav_scheme_code, f.first_date
    FROM funds f, data_start ds
    WHERE f.first_date > ds.d + %(buffer)s          -- launched after our data begins
      AND f.first_date > %(as_of)s::date - INTERVAL '10 years'  -- else 10Y covers it
),
windows AS (
    SELECT y.scheme_code, y.nav_scheme_code, y.first_date AS start_date,
           sn.nav AS start_nav, e.nav AS end_nav, e.nav_date AS end_date
    FROM young y
    JOIN LATERAL (
        SELECT nav FROM mf_nav n
        WHERE n.scheme_code = y.nav_scheme_code AND n.nav_date = y.first_date
    ) sn ON TRUE
    CROSS JOIN LATERAL (
        SELECT n.nav, n.nav_date FROM mf_nav n
        WHERE n.scheme_code = y.nav_scheme_code AND n.nav_date <= %(as_of)s::date
        ORDER BY n.nav_date DESC LIMIT 1
    ) e
    WHERE sn.nav > 0 AND e.nav_date > y.first_date
),
priced AS (
    SELECT w.*, (w.end_date - w.start_date) / 365.25::numeric AS yrs FROM windows w
),
withbench AS (
    SELECT p.*, map.benchmark_id, map.match_type,
           bs.index_value AS bench_start, be.index_value AS bench_end
    FROM priced p
    LEFT JOIN fundmap map ON map.scheme_code = p.scheme_code
    LEFT JOIN benchmark_master bm
           ON bm.benchmark_id = map.benchmark_id AND bm.is_tri
    LEFT JOIN LATERAL (
        SELECT n.index_value FROM benchmark_nav n
        WHERE n.benchmark_id = bm.benchmark_id AND n.index_date <= p.start_date
        ORDER BY n.index_date DESC LIMIT 1
    ) bs ON TRUE
    LEFT JOIN LATERAL (
        SELECT n.index_value FROM benchmark_nav n
        WHERE n.benchmark_id = bm.benchmark_id AND n.index_date <= p.end_date
        ORDER BY n.index_date DESC LIMIT 1
    ) be ON TRUE
)
INSERT INTO mf_returns
    (scheme_code, as_of_date, period, start_date, end_date, years,
     start_nav, end_nav, fund_cagr,
     benchmark_id, bench_start, bench_end, bench_cagr, excess_cagr,
     match_type, nav_scheme_code)
SELECT
    w.scheme_code, %(as_of)s::date, 'SI', w.start_date, w.end_date,
    ROUND(w.yrs, 3), w.start_nav, w.end_nav,
    ROUND(((POWER(w.end_nav / w.start_nav, 1.0 / w.yrs) - 1) * 100)::numeric, 3),
    w.benchmark_id, w.bench_start, w.bench_end,
    CASE WHEN w.bench_start > 0 AND w.bench_end IS NOT NULL THEN
        ROUND(((POWER(w.bench_end / w.bench_start, 1.0 / w.yrs) - 1) * 100)::numeric, 3)
    END,
    CASE WHEN w.bench_start > 0 AND w.bench_end IS NOT NULL THEN
        ROUND((((POWER(w.end_nav / w.start_nav, 1.0 / w.yrs) - 1)
              - (POWER(w.bench_end / w.bench_start, 1.0 / w.yrs) - 1)) * 100)::numeric, 3)
    END,
    w.match_type, w.nav_scheme_code
FROM withbench w
WHERE w.yrs > 1.0        -- under a year, an annualised figure misleads
ON CONFLICT (scheme_code, as_of_date, period) DO UPDATE SET
    start_date = EXCLUDED.start_date, end_date = EXCLUDED.end_date,
    years = EXCLUDED.years, start_nav = EXCLUDED.start_nav,
    end_nav = EXCLUDED.end_nav, fund_cagr = EXCLUDED.fund_cagr,
    benchmark_id = EXCLUDED.benchmark_id, bench_start = EXCLUDED.bench_start,
    bench_end = EXCLUDED.bench_end, bench_cagr = EXCLUDED.bench_cagr,
    excess_cagr = EXCLUDED.excess_cagr, match_type = EXCLUDED.match_type,
    nav_scheme_code = EXCLUDED.nav_scheme_code, created_at = NOW()
"""


# Rows written before the switch to canonical Direct schemes are keyed to
# the old Regular variant. The upsert cannot remove them -- it keys on
# (scheme_code, as_of_date, period), so the Direct row inserts ALONGSIDE
# the stale Regular one, and the fund is then counted twice in every
# median and every denominator.
#
# Runs after the compute and inside the same transaction: a failed compute
# rolls this back too, so a broken run never leaves the table emptier than
# it found it. Only touches the date being recomputed; earlier dates are
# history and stay untouched.
# STALE ROWS, NOT NON-CANONICAL ONES.
#
# This used to delete every row whose scheme_code was not canonical, which
# was right when only Direct/Growth was computed. Now that every plan is
# written deliberately, that rule would wipe all 4,000-odd Regular rows on
# the next nightly -- silently, because check_freshness.py only compares
# MAX(as_of_date) and the Direct rows would still be fresh.
#
# What it should remove is rows for scheme codes this run no longer covers:
# a fund that stopped being scored, or a code that left mf_scheme.
PRUNE_STALE = """
DELETE FROM mf_returns r
WHERE r.as_of_date = %(as_of)s
  AND NOT EXISTS (
      SELECT 1
      FROM mf_scheme tgt
      JOIN v_fund_canonical c ON c.scheme_name = tgt.scheme_name
                             AND c.amc_name IS NOT DISTINCT FROM tgt.amc_name
      WHERE tgt.scheme_code = r.scheme_code
        AND EXISTS (
            SELECT 1 FROM mf_scheme sib
            JOIN mf_score s ON s.scheme_code = sib.scheme_code
            WHERE sib.scheme_name = c.scheme_name
              AND sib.amc_name IS NOT DISTINCT FROM c.amc_name
              AND s.algo_version = %(algo)s
        )
  )
"""


# =====================================================================
# CATEGORY AGGREGATE
#
# MEDIAN, NOT MEAN. One outlier drags a mean, and a four-fund category has
# no defence against it. "Did this fund beat the typical fund in its
# category" is a median question.
#
# The peer set comes from v_scheme_category, never raw sub_category --
# otherwise Kotak Mid Cap and HSBC Midcap land in different buckets and
# ELSS splits across two labels, and a rank on a split category is wrong
# in a way nobody notices.
#
# rank_meaningful comes from category_alias. Categories where a rank says
# nothing (a bucket of unlike funds, or too few of them) are aggregated
# but not ranked.
#
# The count is PER PERIOD, not per category: a fund launched three years
# ago has no 5Y row, so the 5Y peer set is smaller than the 1Y one. The
# page must show "9th of 22" and "14th of 31" on the same fund rather
# than one denominator for the whole block.
# =====================================================================
ENSURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS mf_category_return (
    category           VARCHAR(100) NOT NULL,
    as_of_date         DATE         NOT NULL,
    period             VARCHAR(4)   NOT NULL,
    fund_count         INTEGER      NOT NULL,
    median_cagr        NUMERIC(8,3),
    p25_cagr           NUMERIC(8,3),
    p75_cagr           NUMERIC(8,3),
    best_cagr          NUMERIC(8,3),
    worst_cagr         NUMERIC(8,3),
    median_bench_cagr  NUMERIC(8,3),
    bench_count        INTEGER      NOT NULL DEFAULT 0,
    rank_meaningful    BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (category, as_of_date, period)
);

ALTER TABLE mf_returns ADD COLUMN IF NOT EXISTS category       VARCHAR(100);
ALTER TABLE mf_returns ADD COLUMN IF NOT EXISTS category_rank  INTEGER;
ALTER TABLE mf_returns ADD COLUMN IF NOT EXISTS category_count INTEGER;
"""

CATEGORY_MEDIANS = """
INSERT INTO mf_category_return
    (category, as_of_date, period, fund_count,
     median_cagr, p25_cagr, p75_cagr, best_cagr, worst_cagr,
     median_bench_cagr, bench_count, rank_meaningful, created_at)
SELECT vc.category, r.as_of_date, r.period,
       COUNT(*),
       ROUND(percentile_cont(0.5)  WITHIN GROUP (ORDER BY r.fund_cagr)::numeric, 3),
       ROUND(percentile_cont(0.25) WITHIN GROUP (ORDER BY r.fund_cagr)::numeric, 3),
       ROUND(percentile_cont(0.75) WITHIN GROUP (ORDER BY r.fund_cagr)::numeric, 3),
       ROUND(MAX(r.fund_cagr), 3),
       ROUND(MIN(r.fund_cagr), 3),
       ROUND(percentile_cont(0.5)
             WITHIN GROUP (ORDER BY r.bench_cagr)::numeric, 3),
       COUNT(r.bench_cagr),
       bool_and(vc.rank_meaningful),
       NOW()
FROM mf_returns r
JOIN v_scheme_category vc USING (scheme_code)
WHERE r.as_of_date = %(as_of)s
  AND r.fund_cagr IS NOT NULL
  -- CANONICAL ROWS ONLY, and this one is not optional.
  -- mf_category_return is keyed on (category, as_of_date, period) with no
  -- plan column, so once every plan is computed the median would be taken
  -- over Direct AND Regular together -- dragging it down by roughly the
  -- expense difference and making every Direct fund look better against a
  -- median it is not really being compared to. One plan per fund keeps this
  -- table meaning what it has always meant.
  AND EXISTS (
      SELECT 1 FROM v_fund_canonical c
      WHERE c.canonical_scheme_code = r.scheme_code
  )
GROUP BY 1, 2, 3
ON CONFLICT (category, as_of_date, period) DO UPDATE SET
    fund_count        = EXCLUDED.fund_count,
    median_cagr       = EXCLUDED.median_cagr,
    p25_cagr          = EXCLUDED.p25_cagr,
    p75_cagr          = EXCLUDED.p75_cagr,
    best_cagr         = EXCLUDED.best_cagr,
    worst_cagr        = EXCLUDED.worst_cagr,
    median_bench_cagr = EXCLUDED.median_bench_cagr,
    bench_count       = EXCLUDED.bench_count,
    rank_meaningful   = EXCLUDED.rank_meaningful,
    created_at        = NOW()
"""

# Rank is written back onto mf_returns so the fund page reads one row.
# Ranked highest CAGR first; ties share a rank, and the next rank skips --
# two funds at 2nd means no 3rd, which is what a reader expects.
CATEGORY_RANKS = """
WITH ranked AS (
    -- PARTITIONED BY PLAN AS WELL AS CATEGORY.
    -- Without plan in the partition, adding every Regular code would rank
    -- Direct and Regular against each other and double every category
    -- count -- a fund would read "12th of 58" where 29 of those 58 are the
    -- same funds in a different plan. Regular is ranked against Regular,
    -- which is what a client holding a Regular plan is actually among.
    -- OPTION IS IN THE PARTITION TOO, and it has to be.
    -- Plan alone left Growth and IDCW in one partition. v_returns_source
    -- reads the Growth NAV for BOTH, so the IDCW row carries an identical
    -- CAGR -- every fund tied with itself, the count doubled, and ranks
    -- advancing in twos. Mid Cap read "31st of 57" for 29 real funds: the
    -- 16th fund, counted among plan-and-option variants rather than funds.
    -- Two rows holding the same number are not two competitors.
    -- Growth and IDCW still report the same RETURN, which is correct --
    -- same portfolio, same manager. They are no longer counted twice.
    SELECT r.scheme_code, r.period, vc.category, vc.rank_meaningful,
           RANK() OVER (PARTITION BY vc.category, r.period,
                                     COALESCE(m.plan_type, 'UNKNOWN'),
                                     COALESCE(m.option_type, 'UNKNOWN')
                        ORDER BY r.fund_cagr DESC)  AS rnk,
           COUNT(*) OVER (PARTITION BY vc.category, r.period,
                                       COALESCE(m.plan_type, 'UNKNOWN'),
                                       COALESCE(m.option_type, 'UNKNOWN')) AS cnt
    FROM mf_returns r
    JOIN v_scheme_category vc USING (scheme_code)
    JOIN mf_scheme m USING (scheme_code)
    WHERE r.as_of_date = %(as_of)s
      AND r.fund_cagr IS NOT NULL
)
UPDATE mf_returns r
   SET category       = k.category,
       category_rank  = CASE WHEN k.rank_meaningful THEN k.rnk END,
       category_count = k.cnt
FROM ranked k
WHERE r.scheme_code = k.scheme_code
  AND r.period      = k.period
  AND r.as_of_date  = %(as_of)s
"""


def explain(conn, name, as_of):
    with conn.cursor() as cur:
        # Resolve through the canonical view, so --explain shows the same
        # scheme code the returns were actually computed on.
        cur.execute("""
            SELECT c.canonical_scheme_code, c.scheme_name,
                   c.plan_type, c.option_type
            FROM v_fund_canonical c
            WHERE c.scheme_name ILIKE %s
            ORDER BY c.scheme_name LIMIT 1
        """, (f"%{name}%",))
        row = cur.fetchone()
        if not row:
            print(f"No scored fund matching '{name}'")
            return
        code, sname, plan, opt = row
        print(f"\n{sname}  [{code}]  {plan or ''} {opt or ''}\n")

        cur.execute("""
            SELECT r.period, r.start_date, r.end_date, r.years,
                   r.fund_cagr, r.bench_cagr, r.excess_cagr,
                   b.display_name, r.match_type,
                   r.category_rank, r.category_count, cr.median_cagr
            FROM mf_returns r
            LEFT JOIN benchmark_master b USING (benchmark_id)
            LEFT JOIN mf_category_return cr
                   ON cr.category = r.category
                  AND cr.as_of_date = r.as_of_date
                  AND cr.period = r.period
            WHERE r.scheme_code = %s AND r.as_of_date = %s
            ORDER BY r.years
        """, (code, as_of))
        rows = cur.fetchall()

    if not rows:
        print("  (no returns computed -- not enough NAV history)")
        return

    print(f"  {'period':<8}{'window':<25}{'fund':>8}{'bench':>8}{'diff':>8}"
          f"{'cat med':>9}{'rank':>10}   benchmark")
    for p, sd, ed, yrs, fc, bc, ex, bname, mt, rnk, cnt, med in rows:
        window = f"{sd} to {ed}"
        b = f"{bc:>7.2f}%" if bc is not None else f"{'--':>8}"
        e = f"{ex:>+7.2f}%" if ex is not None else f"{'--':>8}"
        m = f"{med:>8.2f}%" if med is not None else f"{'--':>9}"
        r = f"{rnk} of {cnt}" if rnk is not None else (f"-- of {cnt}"
                                                      if cnt else "--")
        # match_type travels with the label: a category benchmark is not
        # the one the fund states, and the page must say so.
        suffix = {"PROXY": " (proxy)", "CATEGORY": " (category)"}.get(mt, "")
        label = f"{bname or ''}{suffix}"
        print(f"  {p:<8}{window:<25}{float(fc):>7.2f}%{b}{e}{m}{r:>10}   {label}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--explain")
    args = ap.parse_args()

    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            # CLAMPED TO TODAY, and the clamp is the point.
            #
            # This was a bare MAX(nav_date). On 12 September 2026, 662
            # bad feed rows carried 14 September, so this line returned
            # a day that had not happened -- and every one of the 12,415
            # rows written below was stamped with it. The whole returns
            # table, and every trailing return on the site, was labelled
            # "as at" a future date.
            #
            # Nothing downstream could catch that: check_freshness.py
            # only asked whether dates were too OLD. One bad row in a
            # feed should never be able to move an entire table into
            # next week, so the bound lives here, at the point the date
            # is chosen, rather than in whatever reads it afterwards.
            #
            # The count is fetched in the same pass so the bad rows are
            # reported rather than silently skipped -- ignoring them
            # quietly would trade one invisible problem for another.
            cur.execute("""
                SELECT MAX(nav_date) FILTER (WHERE nav_date <= CURRENT_DATE),
                       count(*)      FILTER (WHERE nav_date >  CURRENT_DATE)
                FROM mf_nav
            """)
            latest_nav, future_rows = cur.fetchone()

        if future_rows:
            print(f"WARNING: mf_nav holds {future_rows:,} row(s) dated after "
                  f"today.\n         They are ignored here, but they are "
                  f"wrong at the source.\n         Run nav_future.py to see "
                  f"which schemes.\n")

        # An explicit --date is a human being deliberate, so it is not
        # clamped. It is still worth saying out loud when that date has
        # not happened yet.
        as_of = date.fromisoformat(args.date) if args.date else latest_nav
        if not as_of:
            sys.exit("mf_nav has no rows dated today or earlier. "
                     "Run the NAV loader first.")
        if args.date and as_of > date.today():
            print(f"NOTE: --date {as_of} is in the future. Computing anyway, "
                  f"because you asked explicitly.\n")

        # Idempotent, cheap, and needed by --explain too -- which returns
        # before the compute block, so this cannot live down there.
        with conn.cursor() as cur:
            cur.execute(ENSURE_SCHEMA)
            conn.commit()

        if args.explain:
            explain(conn, args.explain, as_of)
            return

        print(f"Computing returns as at {as_of}\n")

        with conn.cursor() as cur:
            cur.execute("""INSERT INTO batch_run_log (run_type, business_date, status)
                           VALUES ('SCORE_RETURNS', %s, 'RUNNING') RETURNING run_id""",
                        (as_of,))
            run_id = cur.fetchone()[0]
            conn.commit()

            written = 0
            try:
                for label, years in PERIODS:
                    cur.execute(COMPUTE, {"as_of": as_of, "years": years,
                                          "label": label, "algo": FUND_ALGO,
                                          "max_gap": MAX_GAP_DAYS})
                    print(f"  {label:<4} {cur.rowcount:>5} funds")
                    written += cur.rowcount

                cur.execute(COMPUTE_SI, {"as_of": as_of, "algo": FUND_ALGO,
                                         "buffer": INCEPTION_BUFFER_DAYS})
                print(f"  SI   {cur.rowcount:>5} funds (younger than the data)")
                written += cur.rowcount

                # Must precede the category pass: medians and ranks read
                # every row in mf_returns for this date.
                cur.execute(PRUNE_STALE, {"as_of": as_of, "algo": FUND_ALGO})
                if cur.rowcount:
                    print(f"\n  pruned {cur.rowcount} row(s) for funds this run "
                          f"no longer covers")

                # Category aggregates run AFTER every period is written --
                # they are computed from mf_returns, not alongside it.
                cur.execute(CATEGORY_MEDIANS, {"as_of": as_of})
                print(f"\n  category medians  {cur.rowcount:>5} category/period rows")
                cur.execute(CATEGORY_RANKS, {"as_of": as_of})
                print(f"  category ranks    {cur.rowcount:>5} fund rows updated")

                cur.execute("""UPDATE batch_run_log SET status='SUCCESS',
                               finished_at=NOW(), rows_written=%s WHERE run_id=%s""",
                            (written, run_id))
                conn.commit()
            except Exception as e:
                conn.rollback()
                with conn.cursor() as c2:
                    c2.execute("""UPDATE batch_run_log SET status='FAILED',
                                  finished_at=NOW(), error_message=%s WHERE run_id=%s""",
                               (str(e)[:2000], run_id))
                conn.commit()
                print(f"\nFAILED: {e}")
                sys.exit(1)

        print(f"\n{written} rows written. run_id = {run_id}")

        with conn.cursor() as cur:
            cur.execute("""
                SELECT period,
                       COUNT(*) AS funds,
                       COUNT(bench_cagr) AS with_benchmark,
                       ROUND(AVG(fund_cagr), 2) AS avg_fund,
                       ROUND(AVG(bench_cagr), 2) AS avg_bench,
                       COUNT(*) FILTER (WHERE excess_cagr > 0) AS beat_benchmark
                FROM mf_returns WHERE as_of_date = %s
                GROUP BY 1 ORDER BY MIN(years)
            """, (as_of,))
            print(f"\n{'period':<8}{'funds':>7}{'w/bench':>9}{'avg fund':>10}"
                  f"{'avg bench':>11}{'beat':>10}")
            for p, n, wb, af, ab, beat in cur.fetchall():
                pct = f"{beat}/{wb}" if wb else "--"
                print(f"{p:<8}{n:>7}{wb:>9}{str(af):>10}{str(ab or '--'):>11}{pct:>10}")

            # A CAGR outside this band is almost always a data problem,
            # not a fund -- a bad NAV, or a window that is not what its
            # label says. Surface it rather than let it reach the page.
            cur.execute("""
                SELECT m.scheme_name, r.period, r.fund_cagr, r.years,
                       r.start_date, r.start_nav, r.end_nav
                FROM mf_returns r JOIN mf_scheme m USING (scheme_code)
                WHERE r.as_of_date = %s
                  AND (r.fund_cagr > 60 OR r.fund_cagr < -40)
                ORDER BY ABS(r.fund_cagr) DESC LIMIT 10
            """, (as_of,))
            odd = cur.fetchall()

            # Every fund should now be on one plan. A stray REGULAR is
            # either a fund with no direct variant (expected, ~5 equity
            # funds) or a canonical view that did not resolve.
            cur.execute("""
                SELECT s.plan_type, COUNT(DISTINCT r.scheme_code)
                FROM mf_returns r JOIN mf_scheme s USING (scheme_code)
                WHERE r.as_of_date = %s
                GROUP BY 1 ORDER BY 2 DESC
            """, (as_of,))
            print("\nplan mix (expect overwhelmingly DIRECT):")
            for plan, n in cur.fetchall():
                print(f"  {str(plan or '(none)'):<10}{n:>6}")

            cur.execute("""
                SELECT category, period, fund_count, median_cagr,
                       worst_cagr, best_cagr, rank_meaningful
                FROM mf_category_return
                WHERE as_of_date = %s AND period = '3Y'
                ORDER BY fund_count DESC LIMIT 15
            """, (as_of,))
            cat = cur.fetchall()
            if cat:
                print(f"\n3Y category medians (top 15 by size):")
                print(f"  {'category':<28}{'funds':>6}{'median':>9}"
                      f"{'worst':>9}{'best':>9}  ranked")
                for c, _p, n, med, lo, hi, rk in cat:
                    print(f"  {c[:28]:<28}{n:>6}{float(med):>8.2f}%"
                          f"{float(lo):>8.2f}%{float(hi):>8.2f}%  "
                          f"{'yes' if rk else 'no'}")

            cur.execute("""
                SELECT COUNT(DISTINCT scheme_code) FROM mf_returns
                WHERE as_of_date = %s AND nav_scheme_code <> scheme_code
            """, (as_of,))
            subbed = cur.fetchone()[0]
            if subbed:
                print("\n%d IDCW fund(s) measured on their Growth sibling's NAV "
                      "(their own is reduced by every payout)." % subbed)
            if odd:
                print("\nCHECK -- returns outside -40%..+60%, likely a data issue:")
                for sname, p, cagr, yrs, sd, sn, en in odd:
                    print(f"  {sname[:40]:<40} {p:<4} {float(cagr):>8.1f}%  "
                          f"{float(yrs):.2f}y  {sd}  {sn} -> {en}")


if __name__ == "__main__":
    main()
