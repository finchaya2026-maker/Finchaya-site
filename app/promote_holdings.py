"""
promote_holdings.py
-------------------
Promotes rows from mf_holding_raw (staging) into mf_holding (live),
assigning a scheme_code to each fund along the way.

This is the step that used to be hand-typed SQL. It does two things:

  1. Builds mf_scheme_map rows  -- matches the AMC's fund name against
     mf_scheme to find a scheme_code.
  2. Copies holdings into mf_holding -- but ONLY rows whose ISIN already
     exists in stock_master, because mf_holding has a foreign key that
     rejects anything else (T-bills, government securities, some REITs).

USAGE

    python promote_holdings.py helios 2026-07-31
    python promote_holdings.py --remap          # one-off, see below

    argument 1 = the START of the AMC / fund name, e.g. helios
    argument 2 = the portfolio date, same one you gave load_holdings_raw.py

WHICH SCHEME CODE A FUND GETS
    A fund exists under four scheme codes -- Direct/Regular x Growth/IDCW --
    with identical names. This used to pick the LOWEST code, which is an
    accident of AMFI's numbering: HSBC Midcap got 151033, its Regular IDCW
    plan, for no reason at all.

    That mattered once returns arrived. score_returns.py computes on the
    DIRECT plan, because a category median mixing Direct and Regular
    measures distributor commission rather than the manager. With holdings
    on one code and returns on another, the fund page found no returns.

    Both now read v_fund_canonical: Direct/Growth where it exists, degrading
    through Direct-anything to Regular for the ~72 funds with no direct
    variant. One code identifies a fund everywhere.

--remap
    Repoints EXISTING data written under the old rule. Run it once, then
    score_funds.py and score_returns.py. Safe to re-run: it only touches
    rows that are not already canonical.

Run load_holdings_raw.py FIRST. This script reads what that one staged.

Both scripts read FINCHAYA_DB from .env so they always agree on which
database they are writing to. If .env is missing, this fails loudly
rather than quietly writing to a local database.
"""

import os
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv()
DB = os.getenv("FINCHAYA_DB")

if not DB:
    sys.exit("FINCHAYA_DB is not set. Check that .env exists in this folder.")


# ----------------------------------------------------------------------
# 1. Map fund names to scheme codes.
#
# v_fund_canonical already holds one deliberately chosen code per fund, so
# there is nothing to tie-break here -- the old ORDER BY picked the lowest
# scheme code, which is why holdings and returns ended up on different
# codes for the same fund.
#
# ON CONFLICT DO NOTHING means an existing mapping is left alone. Rows
# written under the old rule are corrected by --remap, not silently here:
# moving a fund's holdings is not something a routine promote should do
# as a side effect.
# ----------------------------------------------------------------------
MAP_SQL = """
INSERT INTO mf_scheme_map (amc_fund_name, scheme_code, match_method,
                           confidence, reviewed)
SELECT DISTINCT ON (r.amc_fund_name)
       r.amc_fund_name, c.canonical_scheme_code, 'EXACT_DG', 1.000, true
FROM mf_holding_raw r
JOIN v_fund_canonical c ON fc_norm(c.scheme_name) = fc_norm(r.amc_fund_name)
WHERE r.amc_fund_name ILIKE %(pattern)s
  AND r.as_of_date = %(as_of)s
ORDER BY r.amc_fund_name, c.canonical_scheme_code
ON CONFLICT (amc_fund_name) DO NOTHING
"""

# ----------------------------------------------------------------------
# 2. Copy holdings across.
#
# JOIN stock_master (not LEFT JOIN) is deliberate: it drops any holding
# whose ISIN is not a known stock. Without it the whole INSERT fails on
# the mf_holding_isin_fkey constraint -- one bad row rejects all of them.
# ----------------------------------------------------------------------
COPY_SQL = """
INSERT INTO mf_holding (scheme_code, as_of_date, isin, instrument_name,
                        instrument_type, quantity, market_value, pct_of_nav)
SELECT DISTINCT ON (sm.scheme_code, r.as_of_date, r.instrument_name)
       sm.scheme_code, r.as_of_date, COALESCE(a.new_isin, r.isin),
       r.instrument_name,
       r.instrument_type, r.quantity, r.market_value, r.pct_of_nav
FROM mf_holding_raw r
JOIN mf_scheme_map sm ON sm.amc_fund_name = r.amc_fund_name
-- A SPLIT CHANGES THE ISIN. Persistent Systems is INE262H01013 in a 2023
-- portfolio and INE262H01021 today; same company, and the join below found
-- neither, so the holding was dropped with no error while coverage_pct
-- still reported full coverage -- that column measures matched-among-loaded
-- rather than share of NAV. MO Midcap lost as much as a sixth of its NAV in
-- a single month this way, and the bias has a direction: only stocks that
-- performed well enough to split are affected, so the holdings being
-- silently deleted were disproportionately the winners.
-- NOTE: no per-cent signs in this comment. psycopg scans the whole query
-- string for placeholders, so a literal one would have to be doubled, and
-- a stray one aborts the statement before it reaches the database.
LEFT JOIN isin_alias a ON a.old_isin = r.isin
JOIN stock_master s   ON s.isin = COALESCE(a.new_isin, r.isin)
WHERE r.amc_fund_name ILIKE %(pattern)s
  AND r.as_of_date = %(as_of)s
-- ONE ROW PER HOLDING, NEWEST FILE WINS.
-- mf_holding_raw de-duplicates on (source_file, sheet_name, as_of_date,
-- instrument_name), so the SAME portfolio staged from two differently
-- named files -- Motilal.xlsx and MO_2025-07.xlsx, say -- is two raw
-- rows, not one. Both then map to the same (scheme_code, as_of_date,
-- instrument_name), and Postgres refuses to let ON CONFLICT DO UPDATE
-- touch a row twice in one statement: CardinalityViolation, nothing
-- written, and an error naming a constraint rather than the real cause,
-- which is a filename. DISTINCT ON collapses them; loaded_at DESC keeps
-- the most recently staged version, which is the one parsed by the
-- current parser.
-- The ORDER BY must lead with the DISTINCT ON columns in the same order.
ORDER BY sm.scheme_code, r.as_of_date, r.instrument_name, r.loaded_at DESC
-- Re-running a promote used to abort on the first row that already
-- existed, and because it is one statement, NOTHING was written -- so a
-- retry after a partial month left the month empty rather than complete.
-- Updating on conflict makes a repeat harmless, which matters when
-- loading a year of files one month at a time.
ON CONFLICT (scheme_code, as_of_date, instrument_name) DO UPDATE SET
    isin            = EXCLUDED.isin,
    instrument_type = EXCLUDED.instrument_type,
    quantity        = EXCLUDED.quantity,
    market_value    = EXCLUDED.market_value,
    pct_of_nav      = EXCLUDED.pct_of_nav
"""

# Rows that could NOT be promoted, so you can see what was left behind.
SKIPPED_SQL = """
SELECT DISTINCT r.isin, r.instrument_name, r.instrument_type
FROM mf_holding_raw r
LEFT JOIN isin_alias a   ON a.old_isin = r.isin
LEFT JOIN stock_master s ON s.isin = COALESCE(a.new_isin, r.isin)
WHERE r.amc_fund_name ILIKE %(pattern)s
  AND r.as_of_date = %(as_of)s
  AND s.isin IS NULL
ORDER BY 3, 2
"""

# What ended up in mf_holding, per fund.
RESULT_SQL = """
SELECT sm.amc_fund_name, sm.scheme_code, count(h.holding_id) AS rows
FROM mf_scheme_map sm
LEFT JOIN mf_holding h ON h.scheme_code = sm.scheme_code
                      AND h.as_of_date = %(as_of)s
WHERE sm.amc_fund_name ILIKE %(pattern)s
GROUP BY 1, 2
ORDER BY 1
"""


# ======================================================================
# --remap : move data written under the old lowest-scheme-code rule.
#
# Three statements, one transaction. If any fails, nothing moves.
# ======================================================================

# The fund each non-canonical code belongs to, and where it should point.
REMAP_TARGETS = """
SELECT sm.amc_fund_name, sm.scheme_code AS old_code,
       c.canonical_scheme_code AS new_code, old.scheme_name
FROM mf_scheme_map sm
JOIN mf_scheme old ON old.scheme_code = sm.scheme_code
JOIN v_fund_canonical c
       ON c.scheme_name = old.scheme_name
      AND c.amc_name IS NOT DISTINCT FROM old.amc_name
WHERE sm.scheme_code <> c.canonical_scheme_code
ORDER BY 1
"""

REMAP_MAP = """
UPDATE mf_scheme_map sm
   SET scheme_code = c.canonical_scheme_code
FROM mf_scheme old, v_fund_canonical c
WHERE old.scheme_code = sm.scheme_code
  AND c.scheme_name = old.scheme_name
  AND c.amc_name IS NOT DISTINCT FROM old.amc_name
  AND sm.scheme_code <> c.canonical_scheme_code
"""

# Holdings move rather than being re-promoted: mf_holding_raw only holds
# the current month, so re-promoting would lose earlier disclosure dates.
# The NOT EXISTS guard avoids a unique-key collision if a fund somehow has
# rows under both codes for the same date and ISIN.
REMAP_HOLDINGS = """
UPDATE mf_holding h
   SET scheme_code = c.canonical_scheme_code
FROM mf_scheme old, v_fund_canonical c
WHERE old.scheme_code = h.scheme_code
  AND c.scheme_name = old.scheme_name
  AND c.amc_name IS NOT DISTINCT FROM old.amc_name
  AND h.scheme_code <> c.canonical_scheme_code
  AND NOT EXISTS (
      SELECT 1 FROM mf_holding h2
      WHERE h2.scheme_code = c.canonical_scheme_code
        AND h2.as_of_date  = h.as_of_date
        AND h2.isin        = h.isin
  )
"""

# Scores are DERIVED, so they are deleted rather than moved -- score_funds
# regenerates them against the new codes. Leaving them would show the same
# fund twice in every listing and every category rank.
REMAP_DROP_SCORES = """
DELETE FROM mf_score s
WHERE NOT EXISTS (
    SELECT 1 FROM v_fund_canonical c
    WHERE c.canonical_scheme_code = s.scheme_code
)
"""


def remap():
    """Repoint everything written under the old rule. Idempotent."""
    print("Remapping to canonical (Direct) scheme codes")
    print(f"Target: {DB.split('dbname=')[1].split()[0]}\n")

    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(REMAP_TARGETS)
            targets = cur.fetchall()
            if not targets:
                print("Nothing to do -- every mapping is already canonical.")
                return

            print(f"{len(targets)} fund(s) to move:\n")
            for fund, old_code, new_code, sname in targets[:25]:
                print(f"   {sname[:42]:<42} {old_code:<8} -> {new_code}")
            if len(targets) > 25:
                print(f"   ... and {len(targets) - 25} more")

            cur.execute(REMAP_MAP)
            print(f"\nmf_scheme_map:  {cur.rowcount} row(s) repointed")

            cur.execute(REMAP_HOLDINGS)
            print(f"mf_holding:     {cur.rowcount} row(s) moved")

            cur.execute(REMAP_DROP_SCORES)
            print(f"mf_score:       {cur.rowcount} stale row(s) deleted")

        conn.commit()

    print("\nNow run, in this order:")
    print("   python score_funds.py")
    print("   python score_returns.py")


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--remap":
        remap()
        return

    if len(sys.argv) != 3:
        sys.exit("Usage: python promote_holdings.py <amc-name> <YYYY-MM-DD>\n"
                 "   eg: python promote_holdings.py helios 2026-07-31\n"
                 "       python promote_holdings.py --remap")

    name, as_of = sys.argv[1], sys.argv[2]

    # ANCHORED at the start of the fund name -- no leading %.
    #
    # With a leading % this was a substring match, so 'ITI' also matched
    # ICICI Prudential Energy OpportunITIes Fund and SBI Arbitrage
    # OpportunITIes Fund, and quietly promoted another AMC's holdings.
    # Fund names always begin with the AMC, so a prefix match is both
    # sufficient and safe.
    args = {"pattern": f"{name}%", "as_of": as_of}

    print(f"Promoting '{name}' holdings as at {as_of}")
    print(f"Target: {DB.split('dbname=')[1].split()[0]}\n")

    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(MAP_SQL, args)
            print(f"Scheme map: {cur.rowcount} new fund(s) mapped")

            cur.execute(COPY_SQL, args)
            print(f"Holdings:   {cur.rowcount} rows promoted\n")

            cur.execute(SKIPPED_SQL, args)
            skipped = cur.fetchall()
            if skipped:
                print(f"Skipped {len(skipped)} ISIN(s) not in stock_master "
                      f"(usually debt/govt securities -- normally fine):")
                for isin, iname, itype in skipped:
                    label = (iname or "")[:40]
                    print(f"   {isin}  {label:<40} {itype}")
                print()

            cur.execute(RESULT_SQL, args)
            print("Now in mf_holding:")
            for fund, code, rows in cur.fetchall():
                flag = "  <-- NO ROWS" if rows == 0 else ""
                print(f"   {fund[:38]:<38} {code:<8} {rows:>5}{flag}")

        conn.commit()

    print("\nDone. Next: python score_funds.py")


if __name__ == "__main__":
    main()
