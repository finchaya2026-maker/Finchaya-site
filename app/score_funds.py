"""
score_funds.py
--------------
Turns stock scores into a MUTUAL FUND HEALTH SCORE.

THE MODEL
    health_score = SUM(weight x stock_score) / SUM(weight)

    ...where weight is pct_of_nav and the sum runs ONLY over holdings that
    have a stock score. Cash, TREPS, debt and unmatched ISINs are excluded
    from both the numerator and the denominator -- your decision, and the
    right one: a fund holding 10% cash shouldn't be scored as though that
    10% were a terrible stock.

    The cost of that choice is that a score says nothing about how much of
    the fund it covers. So coverage_pct is stored beside every score and
    should be shown with it. A 24.0 on 95% coverage and a 24.0 on 45%
    coverage are not the same claim.

    Range matches stock scores: -8 to +32, plus health_score_100 rescaled
    to 0-100 for display.

SUB-SCORES (stored so a score can be explained, not just asserted)
    strength_score : weighted ADX      -- is there a trend at all
    momentum_score : weighted MACD+RSI -- is it accelerating
    trend_score    : weighted ST+BB    -- which way, and is it widening

CATEGORY RANK
    Momentum indicators fire harder on volatile stocks, so small-cap funds
    score structurally higher than large-cap ones. Ranking within category
    compares like with like. Use the rank for presentation; the raw score
    across categories is not an apples-to-apples number.

    Categories come from v_scheme_category, NOT from mf_scheme directly.
    AMFI ships several spellings of the same category at once ("ELSS" and
    "ELSS- Tax Saver Fund"), which split one peer group across buckets and
    made a fund look 3rd of 12 when it was 8th of 35. The view merges them.

    The view also carries rank_meaningful. Sectoral/Thematic, ETFs and
    Index Funds are groups by wrapper, not by objective -- a pharma fund
    and a PSU fund are not competitors -- so those get NULL rank rather
    than a league position that reads as one.

USAGE
    python score_funds.py
    python score_funds.py --min-coverage 60
    python score_funds.py --explain "SBI Contra Fund"
"""

import argparse
import os
import sys
from datetime import date

import psycopg
from dotenv import load_dotenv

load_dotenv()

DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Check that .env exists in this folder.")

STOCK_ALGO = "stock-v3"
FUND_ALGO = "fund-v2"
# The pre-gate pair. fund-v1 was built on stock-v2, whose ADX rule paid for a
# rising ADX without checking +DI/-DI -- so it rewarded hardening DOWNtrends.
# Kept runnable under --v1 so the old series survives for comparison; the site
# should read fund-v2 once this has run.
PREGATE_STOCK_ALGO = "stock-v2"
PREGATE_FUND_ALGO = "fund-v1"

# THE LEGACY ENGINE, under its own algo_version.
# mf_score is keyed on (scheme_code, as_of_date, algo_version), so these rows
# sit alongside fund-v1 rather than replacing it. Nothing the site reads
# changes, and both engines stay queryable on identical holdings.
LEGACY_STOCK_ALGO = "stock-v2l"
LEGACY_FUND_ALGO = "fund-v1l"

# MACD LINE ONLY -- same top-50-pct selection and raw weighted sum as the
# legacy engine, but each stock scores 4 or 0 on one rule. Max 4 per stock
# against 32, so the raw sums are roughly an eighth of the legacy ones and
# must not be compared against them directly; the normalised column can be.
MACD_STOCK_ALGO = "stock-v2m"
MACD_FUND_ALGO = "fund-v1m"

# Score only the largest holdings until their combined weight reaches this
# much of NAV. The holding that CROSSES the target is included, so coverage
# lands slightly above it -- matching include_boundary_holding=True in the
# original engine.
#
# Worth being clear about what this does and does not do. Weighting by
# pct_of_nav ALREADY makes a 5 pct position count five times a 1 pct one; that
# is not what the cut buys. What it does is set the tail to ZERO weight rather
# than small weight. The argument for it is that technicals on a 0.4 pct
# smallcap position are noisy and the tail is where ISIN mismatches hide -- not
# that big positions matter more, which the weighting handles already.
COVERAGE_TARGET_PCT = 50.0

# The floor moved with the ADX gate: the component can now reach -4 where it
# previously bottomed at -1, so the worst possible stock total is -8.
#
# THIS IS AN AXIS CHANGE, NOT A SIGNAL CHANGE. health_score_100 is a rescale
# of the raw score onto these bounds, so widening the floor by 3 points shifts
# every displayed number down by roughly 8 even for a fund whose holdings and
# raw score did not move at all. Anyone comparing a fund-v1 100-scale number
# to a fund-v2 one is reading the axis, not the fund.
STOCK_MIN, STOCK_MAX = -8.0, 32.0
STOCK_MIN_PREGATE = -5.0
# Legacy ADX floors at -2 rather than -1, so its total floors one lower.
STOCK_MIN_LEGACY = -6.0
STOCK_MIN_MACD, STOCK_MAX_MACD = 0.0, 4.0
DEFAULT_MIN_COVERAGE = 50.0     # below this a "health score" is not meaningful


# =====================================================================
AGGREGATE_ALL = """
WITH latest_holdings AS (
    -- Portfolios are disclosed monthly; use each fund's newest snapshot.
    SELECT scheme_code, MAX(as_of_date) AS h_date
    FROM mf_holding
    WHERE as_of_date <= %(as_of)s
    GROUP BY scheme_code
),
latest_scores AS (
    -- Newest score per stock at or before the scoring date.
    SELECT DISTINCT ON (isin)
           isin, total_score, adx_score, macd_score, rsi_score,
           bb_score, supertrend_score
    FROM stock_score
    WHERE algo_version = %(stock_algo)s AND as_of_date <= %(as_of)s
    ORDER BY isin, as_of_date DESC
)
SELECT
    h.scheme_code,
    lh.h_date                                              AS holdings_date,
    COUNT(*)                                               AS total_holdings,
    COUNT(*) FILTER (WHERE s.isin IS NOT NULL)             AS scored_holdings,
    SUM(h.pct_of_nav)                                      AS total_weight,
    SUM(h.pct_of_nav) FILTER (WHERE s.isin IS NOT NULL)    AS scored_weight,
    SUM(h.pct_of_nav * s.total_score)                      AS w_total,
    SUM(h.pct_of_nav * s.adx_score)                        AS w_adx,
    SUM(h.pct_of_nav * (s.macd_score + s.rsi_score))       AS w_momentum,
    SUM(h.pct_of_nav * (s.supertrend_score + s.bb_score))  AS w_trend
FROM mf_holding h
JOIN latest_holdings lh
  ON lh.scheme_code = h.scheme_code AND lh.h_date = h.as_of_date
LEFT JOIN latest_scores s ON s.isin = h.isin
WHERE h.pct_of_nav IS NOT NULL
GROUP BY h.scheme_code, lh.h_date
"""

# Same aggregate, but each fund's holdings are ranked by weight and cut once
# the running total crosses COVERAGE_TARGET_PCT. cum_before is the total
# EXCLUDING the current row, so "< target" keeps the row that crosses it.
AGGREGATE_TOP = """
WITH latest_holdings AS (
    SELECT scheme_code, MAX(as_of_date) AS h_date
    FROM mf_holding
    WHERE as_of_date <= %(as_of)s
    GROUP BY scheme_code
),
latest_scores AS (
    SELECT DISTINCT ON (isin)
           isin, total_score, adx_score, macd_score, rsi_score,
           bb_score, supertrend_score
    FROM stock_score
    WHERE algo_version = %(stock_algo)s AND as_of_date <= %(as_of)s
    ORDER BY isin, as_of_date DESC
),
ranked AS (
    SELECT h.scheme_code, lh.h_date, h.pct_of_nav, s.isin AS scored_isin,
           s.total_score, s.adx_score, s.macd_score, s.rsi_score,
           s.bb_score, s.supertrend_score,
           COALESCE(SUM(h.pct_of_nav) OVER (
               PARTITION BY h.scheme_code
               ORDER BY h.pct_of_nav DESC, h.isin
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS cum_before
    FROM mf_holding h
    JOIN latest_holdings lh
      ON lh.scheme_code = h.scheme_code AND lh.h_date = h.as_of_date
    LEFT JOIN latest_scores s ON s.isin = h.isin
    WHERE h.pct_of_nav IS NOT NULL
)
SELECT
    scheme_code,
    h_date                                              AS holdings_date,
    COUNT(*)                                            AS total_holdings,
    COUNT(*) FILTER (WHERE scored_isin IS NOT NULL)     AS scored_holdings,
    SUM(pct_of_nav)                                     AS total_weight,
    SUM(pct_of_nav) FILTER (WHERE scored_isin IS NOT NULL) AS scored_weight,
    SUM(pct_of_nav * total_score)                       AS w_total,
    SUM(pct_of_nav * adx_score)                         AS w_adx,
    SUM(pct_of_nav * (macd_score + rsi_score))          AS w_momentum,
    SUM(pct_of_nav * (supertrend_score + bb_score))     AS w_trend
FROM ranked
WHERE cum_before < %(target)s
GROUP BY scheme_code, h_date
"""

UPSERT = """
INSERT INTO mf_score
    (scheme_code, as_of_date, health_score, health_score_100,
     avg_stock_score, holdings_as_of_date, coverage_pct,
     scored_holdings, total_holdings,
     momentum_score, trend_score, strength_score,
     algo_version, run_id)
VALUES (%s,%s,%s,%s, %s,%s,%s, %s,%s, %s,%s,%s, %s,%s)
ON CONFLICT (scheme_code, as_of_date, algo_version) DO UPDATE SET
    health_score=EXCLUDED.health_score,
    health_score_100=EXCLUDED.health_score_100,
    avg_stock_score=EXCLUDED.avg_stock_score,
    holdings_as_of_date=EXCLUDED.holdings_as_of_date,
    coverage_pct=EXCLUDED.coverage_pct,
    scored_holdings=EXCLUDED.scored_holdings,
    total_holdings=EXCLUDED.total_holdings,
    momentum_score=EXCLUDED.momentum_score,
    trend_score=EXCLUDED.trend_score,
    strength_score=EXCLUDED.strength_score,
    run_id=EXCLUDED.run_id,
    created_at=NOW()
"""

# Rank within canonical category, so a large-cap fund is compared against
# large-cap funds rather than against small-caps -- and against ALL of its
# peers, not just those filed under the same spelling.
#
# Where rank_meaningful is false the rank is left NULL. Writing a number
# there would be arithmetic without meaning, and the site would show it as
# though it were a standing.
RANK = """
WITH ranked AS (
    SELECT s.scheme_code,
           CASE WHEN c.rank_meaningful THEN
               RANK() OVER (PARTITION BY c.category ORDER BY s.health_score DESC)
           END AS r
    FROM mf_score s
    JOIN v_scheme_category c USING (scheme_code)
    WHERE s.as_of_date = %s AND s.algo_version = %s
)
UPDATE mf_score t
   SET category_rank = ranked.r
  FROM ranked
 WHERE t.scheme_code = ranked.scheme_code
   AND t.as_of_date = %s AND t.algo_version = %s
"""


def to_100(raw):
    """Rescale -8..32 onto 0..100 for display."""
    return round((raw - STOCK_MIN) / (STOCK_MAX - STOCK_MIN) * 100.0, 2)


def to_100_pregate(raw):
    """Same, on the ungated rule's own floor of -5.

    Rescaling a stock-v2 score against -8 would move every pre-gate number
    down by about 8 points and make an axis change look like a signal
    change -- the same trap to_100_legacy exists to avoid.
    """
    return round((raw - STOCK_MIN_PREGATE) / (STOCK_MAX - STOCK_MIN_PREGATE) * 100.0, 2)


def to_100_macd(raw):
    """0..4 onto 0..100 -- the MACD-only rule's own bounds."""
    return round((raw - STOCK_MIN_MACD) / (STOCK_MAX_MACD - STOCK_MIN_MACD) * 100.0, 2)


def to_100_legacy(raw):
    """Same, on the legacy ADX rule's own floor of -6.

    Using -5 here would shift every legacy number by about 2.7 points against
    fund-v1 and look like a difference in the signal rather than in the axis.
    """
    return round((raw - STOCK_MIN_LEGACY) / (STOCK_MAX - STOCK_MIN_LEGACY) * 100.0, 2)


def f(x):
    return float(x) if x is not None else None


# =====================================================================
def explain(conn, name, as_of):
    with conn.cursor() as cur:
        # A fund exists under several scheme_codes (Direct/Regular x
        # Growth/IDCW). Only ONE of them is the code mf_scheme_map chose,
        # and only that one carries holdings. Prefer it over the lowest
        # code, or --explain silently looks up an empty variant.
        cur.execute("""
            SELECT m.scheme_code, m.scheme_name, c.category, c.rank_meaningful
            FROM mf_scheme m
            JOIN v_scheme_category c USING (scheme_code)
            WHERE m.scheme_name ILIKE %s
            ORDER BY
                EXISTS (SELECT 1 FROM mf_holding h
                        WHERE h.scheme_code = m.scheme_code) DESC,
                m.scheme_code
            LIMIT 1
        """, (f"%{name}%",))
        row = cur.fetchone()
    if not row:
        print(f"No scheme matching '{name}'")
        return
    code, scheme_name, category, rankable = row
    print(f"\n{scheme_name}  [{code}]  {category}"
          f"{'' if rankable else '  (not ranked -- see rank_meaningful)'}")

    with conn.cursor() as cur:
        cur.execute("""
            WITH lh AS (SELECT MAX(as_of_date) d FROM mf_holding WHERE scheme_code=%s),
            ls AS (SELECT DISTINCT ON (isin) isin, total_score
                   FROM stock_score WHERE algo_version=%s
                   ORDER BY isin, as_of_date DESC)
            SELECT COALESCE(m.symbol, h.instrument_name), h.pct_of_nav, s.total_score
            FROM mf_holding h
            CROSS JOIN lh
            LEFT JOIN stock_master m ON m.isin = h.isin
            LEFT JOIN ls s ON s.isin = h.isin
            WHERE h.scheme_code=%s AND h.as_of_date = lh.d
            ORDER BY h.pct_of_nav DESC NULLS LAST LIMIT 15
        """, (code, STOCK_ALGO, code))
        print(f"\n  {'holding':<22}{'weight':>8}{'score':>8}{'contrib':>10}")
        for sym, pct, score in cur.fetchall():
            pct = f(pct) or 0
            if score is None:
                print(f"  {str(sym)[:22]:<22}{pct:>7.2f}%{'--':>8}{'excluded':>10}")
            else:
                print(f"  {str(sym)[:22]:<22}{pct:>7.2f}%{float(score):>8.0f}"
                      f"{pct * float(score):>10.1f}")

        cur.execute("""
            SELECT health_score, health_score_100, coverage_pct,
                   scored_holdings, total_holdings, category_rank
            FROM mf_score WHERE scheme_code=%s AND as_of_date=%s AND algo_version=%s
        """, (code, as_of, FUND_ALGO))
        r = cur.fetchone()
    if r:
        print(f"\n  health_score {f(r[0]):.2f}  ({f(r[1]):.1f}/100)")
        print(f"  coverage {f(r[2]):.1f}%  "
              f"({r[3]}/{r[4]} holdings)  rank in category: {r[5]}")
    else:
        print("\n  (not yet scored -- run without --explain first)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE)
    ap.add_argument("--explain")
    ap.add_argument("--macd-only", action="store_true", dest="macd_only",
                    help="MACD-line-direction score over the top 50 pct of "
                         "NAV. Writes fund-v1m on stock-v2m.")
    ap.add_argument("--v1", action="store_true", dest="pregate",
                    help="regenerate the PRE-GATE series: fund-v1 built on "
                         "stock-v2, rescaled on its own -5 floor. For "
                         "comparison only -- the site reads fund-v2.")
    ap.add_argument("--legacy", action="store_true",
                    help="the earlier engine: top-50-pct-of-NAV holdings, raw "
                         "weighted SUM, legacy ADX. Writes algo_version "
                         "fund-v1l on stock-v2l; leaves fund-v1 alone.")
    args = ap.parse_args()

    stock_algo = (MACD_STOCK_ALGO if args.macd_only
                  else LEGACY_STOCK_ALGO if args.legacy
                  else PREGATE_STOCK_ALGO if args.pregate else STOCK_ALGO)
    fund_algo = (MACD_FUND_ALGO if args.macd_only
                 else LEGACY_FUND_ALGO if args.legacy
                 else PREGATE_FUND_ALGO if args.pregate else FUND_ALGO)
    # Both experimental engines cut to the top 50 pct of NAV and report a raw
    # weighted SUM, so they differ from each other only in the stock rule.
    topcut = args.legacy or args.macd_only

    with psycopg.connect(DB) as conn:
        if args.date:
            as_of = date.fromisoformat(args.date)
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(as_of_date) FROM stock_score "
                            "WHERE algo_version=%s", (stock_algo,))
                as_of = cur.fetchone()[0]
            if not as_of:
                print(f"No stock scores for {stock_algo}. Run score_stocks.py "
                      f"{'--legacy ' if args.legacy else ''}first.")
                sys.exit(1)

        if args.explain:
            explain(conn, args.explain, as_of)
            return

        print(f"Scoring funds as at {as_of} ({fund_algo} on {stock_algo})")
        if topcut:
            print(f"{'MACD-only' if args.macd_only else 'Legacy'} engine: top "
                  f"{COVERAGE_TARGET_PCT:.0f}% of NAV by weight, RAW weighted "
                  f"sum (not an average)")
        print(f"Minimum coverage: {args.min_coverage:.0f}%\n")

        with conn.cursor() as cur:
            cur.execute("""INSERT INTO batch_run_log (run_type, business_date, status)
                           VALUES ('SCORE_FUNDS', %s, 'RUNNING') RETURNING run_id""",
                        (as_of,))
            run_id = cur.fetchone()[0]
            conn.commit()

            cur.execute(AGGREGATE_TOP if topcut else AGGREGATE_ALL,
                        {"as_of": as_of, "stock_algo": stock_algo,
                         "target": COVERAGE_TARGET_PCT})
            raw = cur.fetchall()

        print(f"Aggregated {len(raw)} funds with holdings")

        rows, low_coverage, no_scores = [], [], []

        for (code, h_date, total_h, scored_h, total_w, scored_w,
             w_total, w_adx, w_mom, w_trend) in raw:

            total_w = f(total_w) or 0.0
            scored_w = f(scored_w) or 0.0

            if scored_w <= 0 or not w_total:
                no_scores.append(code)
                continue

            coverage = 100.0 * scored_w / total_w if total_w else 0.0
            if coverage < args.min_coverage:
                low_coverage.append((code, coverage))
                continue

            if topcut:
                # RAW WEIGHTED SUM -- no division. This is the 300-700 number
                # from the earlier engine: roughly COVERAGE_TARGET_PCT times the
                # average points per stock. It is NOT on the -5..32 scale and
                # must not be compared against a fund-v1 score.
                health = f(w_total)
                # health_score_100 keeps the old engine's own normalised
                # figure, so the column still means "0-100" in every row.
                health_100 = (to_100_macd if args.macd_only
                              else to_100_legacy)(f(w_total) / scored_w)
            else:
                health = f(w_total) / scored_w
                # --v1 rows are stock-v2 numbers and belong on the -5 floor.
                health_100 = (to_100_pregate if args.pregate else to_100)(health)

            rows.append((
                code, as_of, round(health, 4), health_100,
                round(f(w_total) / scored_w, 4),   # avg_stock_score: always the mean
                h_date, round(coverage, 3),
                scored_h, total_h,
                round(f(w_mom) / scored_w, 4),
                round(f(w_trend) / scored_w, 4),
                round(f(w_adx) / scored_w, 4),
                fund_algo, run_id,
            ))

        if not rows:
            print("\nNo funds scoreable. Check that stock_score and mf_holding "
                  "both have data, and that ISINs match between them.")
            sys.exit(1)

        with conn.cursor() as cur:
            cur.executemany(UPSERT, rows)
            cur.execute(RANK, (as_of, fund_algo, as_of, fund_algo))
            cur.execute("""UPDATE batch_run_log SET status='SUCCESS',
                           finished_at=NOW(), rows_read=%s, rows_written=%s
                           WHERE run_id=%s""", (len(raw), len(rows), run_id))
        conn.commit()

        # ---------------- report ----------------
        print(f"\nScored {len(rows)} funds. run_id = {run_id}")
        if low_coverage:
            print(f"Skipped {len(low_coverage)} below {args.min_coverage:.0f}% coverage "
                  f"(debt/hybrid funds, mostly)")
        if no_scores:
            print(f"Skipped {len(no_scores)} with no scoreable holdings at all")

        with conn.cursor() as cur:
            cur.execute("""
                SELECT ROUND(AVG(health_score),2), MIN(health_score),
                       MAX(health_score), ROUND(AVG(coverage_pct),1)
                FROM mf_score WHERE as_of_date=%s AND algo_version=%s
            """, (as_of, fund_algo))
            avg, lo, hi, cov = cur.fetchone()
            print(f"\nHealth score: avg {avg}, range {lo} to {hi}")
            print(f"Average coverage: {cov}%")

            cur.execute("""
                SELECT c.category AS cat,
                       COUNT(*), ROUND(AVG(s.health_score),2)
                FROM mf_score s JOIN v_scheme_category c USING (scheme_code)
                WHERE s.as_of_date=%s AND s.algo_version=%s
                GROUP BY 1 HAVING COUNT(*) >= 3
                ORDER BY 3 DESC LIMIT 12
            """, (as_of, fund_algo))
            print("\nAverage by category (this is where the small-cap skew shows):")
            for cat, n, a in cur.fetchall():
                print(f"  {cat[:34]:<34} {n:>3} funds   {a:>6}")

            cur.execute("""
                SELECT LEFT(m.scheme_name, 46), ROUND(s.health_score,1),
                       ROUND(s.health_score_100,0), ROUND(s.coverage_pct,0),
                       c.category, COALESCE(s.category_rank::text, '-')
                FROM mf_score s
                JOIN mf_scheme m USING (scheme_code)
                JOIN v_scheme_category c USING (scheme_code)
                WHERE s.as_of_date=%s AND s.algo_version=%s
                ORDER BY s.health_score DESC LIMIT 12
            """, (as_of, fund_algo))
            print("\nTop 12 (fund, score, /100, coverage%, category, rank):")
            for r in cur.fetchall():
                print("  ", r)


if __name__ == "__main__":
    main()
