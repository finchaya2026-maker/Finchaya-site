"""
build_fund_profile.py -- what each fund actually holds.
--------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/build_fund_profile.py
    --dry-run

WHY THIS EXISTS
    Selection matches a set of funds to a TARGET EXPOSURE measured through
    to the shares. That is impossible without knowing what each fund's
    exposure is, and a fund's name is not a measurement. This computes it:
    for every fund we hold a portfolio for, the share of its equity in
    large, mid and small caps, how concentrated it is, and how much of the
    fund is in listed shares at all.

MANDATE DRIFT, AND WHY IT REPLACES AUM
    A small cap fund that grows to tens of thousands of crores cannot trade
    small caps nimbly, so it drifts into larger companies and quietly stops
    being what it is sold as. The usual proxy for this is AUM. We do not
    need a proxy: with AMFI's classification on every holding, the drift
    itself is measurable.

    SEBI's category rules give the line -- at least 80% large cap for a
    Large Cap Fund, at least 65% for Mid Cap and Small Cap funds. A fund
    below its own floor is not necessarily bad, but it is not the thing its
    name says, and a portfolio built on the name would be built on a
    mistake.

    The floor is checked against the fund's LISTED EQUITY, not the fund,
    because SEBI's test is of equity allocation and a fund holding cash
    should not fail for holding cash.

WHAT IS NOT HERE
    Nothing is scored or ranked. These are measurements. Ranking depends on
    what a goal needs, and belongs in selection where the target is known.
"""

import os
import sys
from collections import defaultdict

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")
DRY = "--dry-run" in sys.argv
CHUNK = 300

# SEBI's minimum for a fund to carry the name, as a share of equity.
MANDATE_FLOOR = {
    "Large Cap Fund": ("large", 80),
    "Mid Cap Fund": ("mid", 65),
    "Small Cap Fund": ("small", 65),
}

DDL = """
CREATE TABLE IF NOT EXISTS fund_profile (
    scheme_code     text PRIMARY KEY,
    as_of_date      date NOT NULL,
    holding_count   int  NOT NULL,
    equity_pct      numeric NOT NULL,   -- of the FUND
    large_pct       numeric NOT NULL,   -- of its equity
    mid_pct         numeric NOT NULL,
    small_pct       numeric NOT NULL,
    unclassified_pct numeric NOT NULL,
    international_pct numeric NOT NULL DEFAULT 0,
    top10_pct       numeric,            -- of its equity
    mandate_bucket  text,               -- what the name promises
    mandate_floor   numeric,
    mandate_actual  numeric,
    drifted         boolean NOT NULL DEFAULT false,
    built_at        timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE fund_profile
    ADD COLUMN IF NOT EXISTS international_pct numeric NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS top10_pct        numeric,
    ADD COLUMN IF NOT EXISTS mandate_bucket   text,
    ADD COLUMN IF NOT EXISTS mandate_floor    numeric,
    ADD COLUMN IF NOT EXISTS mandate_actual   numeric,
    ADD COLUMN IF NOT EXISTS drifted          boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS ix_profile_drift ON fund_profile (drifted);
"""

FUNDS = """
SELECT c.canonical_scheme_code AS scheme_code, vc.category
FROM v_fund_canonical c
LEFT JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
WHERE EXISTS (SELECT 1 FROM mf_holding h
               WHERE h.scheme_code = c.canonical_scheme_code)
"""

# Latest disclosed portfolio per fund, with each holding's cap class taken
# from the AMFI period nearest that disclosure -- so a portfolio filed in
# March is read against March's classification, not today's.
HOLDINGS = """
WITH latest AS (
    SELECT scheme_code, MAX(as_of_date) AS d
    FROM mf_holding WHERE scheme_code = ANY(%(codes)s)
    GROUP BY scheme_code
)
SELECT h.scheme_code, h.as_of_date, h.pct_of_nav,
       -- An ISIN that does not begin with INE is not an Indian listed
       -- security. Those holdings were falling through as unclassified,
       -- which understated a fund's real equity and left the international
       -- bucket permanently unfillable -- Parag Parikh's overseas book is
       -- the obvious case, and it is a quarter of the fund.
       CASE WHEN h.isin !~ '^INE' THEN 'International'
            ELSE COALESCE(cc.cap_class, 'Unclassified') END AS cap_class
FROM mf_holding h
JOIN latest l ON l.scheme_code = h.scheme_code AND l.d = h.as_of_date
LEFT JOIN LATERAL (
    SELECT c.cap_class FROM stock_cap_class c
    WHERE c.isin = h.isin
    ORDER BY (c.as_of_period <= h.as_of_date) DESC,
             abs(c.as_of_period - h.as_of_date)
    LIMIT 1
) cc ON true
WHERE h.isin IS NOT NULL AND h.pct_of_nav IS NOT NULL
"""


def main():
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()

        cur.execute(FUNDS)
        funds = {str(r["scheme_code"]): r["category"] for r in cur.fetchall()}
        codes = list(funds)
        print("funds with holdings:", len(codes))

        rows_out, drifted, no_caps = [], 0, 0

        for i in range(0, len(codes), CHUNK):
            chunk = codes[i:i + CHUNK]
            cur.execute(HOLDINGS, {"codes": chunk})
            held = defaultdict(list)
            for r in cur.fetchall():
                held[str(r["scheme_code"])].append(r)

            for code in chunk:
                rows = held.get(code)
                if not rows:
                    continue
                equity = sum(float(r["pct_of_nav"]) for r in rows)
                if equity <= 0:
                    continue

                by_cap = defaultdict(float)
                for r in rows:
                    by_cap[r["cap_class"]] += float(r["pct_of_nav"])

                pct = {k: round(100.0 * v / equity, 1) for k, v in by_cap.items()}
                if pct.get("Unclassified", 0) > 60:
                    # Mostly unclassifiable holdings -- a debt fund, a fund
                    # of funds, or an international fund. Recording a cap mix
                    # for it would be recording noise.
                    no_caps += 1

                weights = sorted((float(r["pct_of_nav"]) for r in rows),
                                 reverse=True)
                top10 = round(100.0 * sum(weights[:10]) / equity, 1)

                cat = funds.get(code)
                bucket, floor = MANDATE_FLOOR.get(cat, (None, None))
                # SEBI's floor is a share of the fund's INDIAN equity, so an
                # overseas holding should neither help nor count against it.
                indian = equity - by_cap.get("International", 0.0)
                actual = (round(100.0 * by_cap.get(bucket.capitalize(), 0.0)
                                / indian, 1) if bucket and indian > 0 else None)
                # Only a fund whose name makes a promise can break one.
                is_drift = bool(bucket and actual is not None and actual < floor)
                if is_drift:
                    drifted += 1

                rows_out.append({
                    "code": code, "as_of": rows[0]["as_of_date"],
                    "n": len(rows), "equity": round(equity, 1),
                    "large": pct.get("Large", 0.0), "mid": pct.get("Mid", 0.0),
                    "small": pct.get("Small", 0.0),
                    "unc": pct.get("Unclassified", 0.0),
                    "intl": pct.get("International", 0.0),
                    "top10": top10, "bucket": bucket, "floor": floor,
                    "actual": actual, "drifted": is_drift,
                })
            print("  %d/%d" % (min(i + CHUNK, len(codes)), len(codes)), end="\r")
        print(" " * 30, end="\r")

        print("profiles computed:", len(rows_out))
        print("mostly unclassifiable (debt / FoF):", no_caps)
        print("funds holding overseas equity:",
              sum(1 for r in rows_out if r["intl"] > 0.5))
        print("funds below their own mandate floor:", drifted)

        if DRY:
            print("\n--dry-run: nothing written.")
        else:
            for r in rows_out:
                cur.execute("""
                    INSERT INTO fund_profile
                      (scheme_code, as_of_date, holding_count, equity_pct,
                       large_pct, mid_pct, small_pct, unclassified_pct,
                       international_pct, top10_pct, mandate_bucket,
                       mandate_floor, mandate_actual, drifted, built_at)
                    VALUES (%(code)s, %(as_of)s, %(n)s, %(equity)s, %(large)s,
                            %(mid)s, %(small)s, %(unc)s, %(intl)s, %(top10)s,
                            %(bucket)s, %(floor)s, %(actual)s, %(drifted)s,
                            now())
                    ON CONFLICT (scheme_code) DO UPDATE SET
                        as_of_date = EXCLUDED.as_of_date,
                        holding_count = EXCLUDED.holding_count,
                        equity_pct = EXCLUDED.equity_pct,
                        large_pct = EXCLUDED.large_pct,
                        mid_pct = EXCLUDED.mid_pct,
                        small_pct = EXCLUDED.small_pct,
                        unclassified_pct = EXCLUDED.unclassified_pct,
                        international_pct = EXCLUDED.international_pct,
                        top10_pct = EXCLUDED.top10_pct,
                        mandate_bucket = EXCLUDED.mandate_bucket,
                        mandate_floor = EXCLUDED.mandate_floor,
                        mandate_actual = EXCLUDED.mandate_actual,
                        drifted = EXCLUDED.drifted,
                        built_at = now()
                """, r)
            conn.commit()
            print("written:", len(rows_out))

        # The drifted list is the point of the exercise: these are funds
        # whose name no longer describes them.
        if not DRY:
            cur.execute("""
                SELECT s.scheme_name, p.mandate_bucket, p.mandate_floor,
                       p.mandate_actual, p.large_pct, p.mid_pct, p.small_pct
                FROM fund_profile p
                JOIN mf_scheme s ON s.scheme_code = p.scheme_code
                WHERE p.drifted
                ORDER BY (p.mandate_floor - p.mandate_actual) DESC LIMIT 12
            """)
            print("\nfurthest from their own mandate:")
            for r in cur.fetchall():
                print("  %-40s %s floor %d, holds %.0f  (L%.0f/M%.0f/S%.0f)"
                      % (r["scheme_name"][:40], r["mandate_bucket"],
                         r["mandate_floor"], r["mandate_actual"],
                         r["large_pct"], r["mid_pct"], r["small_pct"]))

            cur.execute("""
                SELECT s.scheme_name, p.top10_pct, p.holding_count
                FROM fund_profile p
                JOIN mf_scheme s ON s.scheme_code = p.scheme_code
                WHERE p.equity_pct > 60 AND p.holding_count >= 15
                ORDER BY p.top10_pct DESC LIMIT 8
            """)
            print("\nmost concentrated (top ten as a share of equity):")
            for r in cur.fetchall():
                print("  %-46s %.0f%% in ten of %d"
                      % (r["scheme_name"][:46], r["top10_pct"],
                         r["holding_count"]))


if __name__ == "__main__":
    main()
