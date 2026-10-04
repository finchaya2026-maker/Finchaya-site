"""
detect_nav_splits.py -- find and correct unit splits in NAV history.
---------------------------------------------------------------------
AMFI publishes the post-split NAV without restating what came before, so
a 1:10 split leaves a series that drops by 90% overnight. Any return
measured across that date reads as a catastrophic loss. Eighteen ETFs in
this database are affected.

USAGE
    python detect_nav_splits.py                 report only, changes nothing
    python detect_nav_splits.py --apply         record and correct
    python detect_nav_splits.py --list          show what has been corrected

WHAT COUNTS AS A SPLIT
    A day-over-day ratio close to a round fraction -- 1/2, 1/5, 1/10,
    1/100, or their inverses for a reverse split. A real fund does not
    move 90% in a day, and one that moves 47% is a market event, not a
    split, so anything that is not close to a round ratio is reported
    and left alone for a human to look at.

WHAT IT DOES
    Records the split in mf_nav_split, then multiplies every NAV BEFORE
    that date by the factor, making the series continuous. The raw
    figures are recoverable: divide back by the factors on record.

    Idempotent. A split already in mf_nav_split is never applied twice,
    so this is safe to run nightly.
"""

import os
import sys
import argparse

import psycopg
from dotenv import load_dotenv

load_dotenv()
DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Check that .env exists in this folder.")

# Ratios we will act on, and how close the observed value must be.
# 8% tolerance comfortably covers a split day that also had a market
# move, while staying far from any ratio a market alone could produce.
KNOWN_RATIOS = [0.01, 0.02, 0.10, 0.20, 0.25, 0.50, 2.0, 4.0, 5.0, 10.0, 50.0, 100.0]
TOLERANCE = 0.08

# Only look at jumps beyond this. Nothing a market does in one day comes
# close, so everything caught here is a corporate action or bad data.
JUMP_LOW, JUMP_HIGH = 0.6, 1.8


DDL = """
CREATE TABLE IF NOT EXISTS mf_nav_split (
    scheme_code varchar(20) NOT NULL REFERENCES mf_scheme(scheme_code),
    -- first date at the NEW scale
    split_date  date        NOT NULL,
    observed    numeric(14,6) NOT NULL,   -- the raw nav/prev_nav we saw
    factor      numeric(14,6) NOT NULL,   -- snapped; multiply PRE-split navs by this
    applied     boolean NOT NULL DEFAULT FALSE,
    rows_fixed  integer,
    detected_at timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scheme_code, split_date)
);
"""

# Scoped to the funds we actually score. mf_nav holds 17m rows across
# 14,000 schemes; running a window function over all of them takes many
# minutes and tells us nothing about funds that never reach a page.
# --all lifts the restriction.
FIND = """
WITH wanted AS (
    SELECT DISTINCT scheme_code FROM mf_score
    WHERE as_of_date = (SELECT MAX(as_of_date) FROM mf_score)
    UNION
    SELECT scheme_code FROM mf_scheme WHERE %(all)s
),
r AS (
    SELECT n.scheme_code, n.nav_date, n.nav,
           LAG(n.nav) OVER (PARTITION BY n.scheme_code ORDER BY n.nav_date) AS prev_nav
    FROM mf_nav n
    JOIN wanted w USING (scheme_code)
)
SELECT r.scheme_code, m.scheme_name, r.nav_date, r.prev_nav, r.nav,
       r.nav / r.prev_nav AS ratio
FROM r
JOIN mf_scheme m USING (scheme_code)
WHERE r.prev_nav > 0
  AND (r.nav / r.prev_nav < %(low)s OR r.nav / r.prev_nav > %(high)s)
  AND NOT EXISTS (SELECT 1 FROM mf_nav_split s
                  WHERE s.scheme_code = r.scheme_code
                    AND s.split_date = r.nav_date)
ORDER BY m.scheme_name, r.nav_date
"""


def snap(ratio):
    """Return the round ratio this is close to, or None."""
    for known in KNOWN_RATIOS:
        if abs(ratio - known) / known <= TOLERANCE:
            return known
    return None


def show_list(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT m.scheme_name, s.scheme_code, s.split_date,
                   s.observed, s.factor, s.applied, s.rows_fixed
            FROM mf_nav_split s JOIN mf_scheme m USING (scheme_code)
            ORDER BY s.split_date, m.scheme_name
        """)
        rows = cur.fetchall()
    if not rows:
        print("No splits recorded yet.")
        return
    print("%-46s %-8s %-12s %9s %9s %8s %7s"
          % ("FUND", "CODE", "DATE", "OBSERVED", "FACTOR", "APPLIED", "ROWS"))
    for name, code, d, obs, fac, applied, n in rows:
        print("%-46s %-8s %-12s %9.4f %9.4f %8s %7s"
              % (name[:46], code, d, obs, fac, applied, n if n is not None else "-"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="record the splits and correct the NAV history")
    ap.add_argument("--list", action="store_true",
                    help="show splits already recorded")
    ap.add_argument("--all", action="store_true",
                    help="scan every scheme, not just scored funds (slow)")
    args = ap.parse_args()

    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()

        if args.list:
            show_list(conn)
            return

        with conn.cursor() as cur:
            print("Scanning NAV history for abrupt jumps "
                  "(%s)..." % ("all schemes" if args.all else "scored funds only"))
            cur.execute("SET LOCAL statement_timeout = '600s'")
            cur.execute(FIND, {"low": JUMP_LOW, "high": JUMP_HIGH,
                               "all": args.all})
            found = cur.fetchall()

        if not found:
            print("No new jumps found. NAV history looks continuous.")
            return

        splits, unclear = [], []
        for code, name, d, prev, nav, ratio in found:
            factor = snap(float(ratio))
            (splits if factor else unclear).append(
                (code, name, d, float(prev), float(nav), float(ratio), factor))

        print("%d jump(s) found: %d look like splits, %d unclear\n"
              % (len(found), len(splits), len(unclear)))

        if splits:
            print("SPLITS")
            print("%-44s %-12s %11s %11s %8s %7s"
                  % ("FUND", "DATE", "PREV", "NAV", "RATIO", "AS"))
            for code, name, d, prev, nav, ratio, factor in splits:
                label = ("1:%g" % (1 / factor)) if factor < 1 else ("%g:1" % factor)
                print("%-44s %-12s %11.4f %11.4f %8.4f %7s"
                      % (name[:44], d, prev, nav, ratio, label))

        if unclear:
            print("\nUNCLEAR -- not close to a round ratio, left alone:")
            for code, name, d, prev, nav, ratio, _ in unclear:
                print("  %-44s %-12s %11.4f -> %11.4f  ratio %.4f"
                      % (name[:44], d, prev, nav, ratio))
            print("  These need a human. A merger, a bad NAV row, or a "
                  "genuine event -- not something to guess at.")

        if not args.apply:
            print("\nReport only. Re-run with --apply to record and correct.")
            return

        if not splits:
            print("\nNothing to apply.")
            return

        total = 0
        with conn.cursor() as cur:
            for code, name, d, prev, nav, ratio, factor in splits:
                cur.execute("""
                    INSERT INTO mf_nav_split
                        (scheme_code, split_date, observed, factor)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (scheme_code, split_date) DO NOTHING
                """, (code, d, ratio, factor))
                if cur.rowcount == 0:
                    continue          # already recorded by a parallel run

                # Restate everything before the split onto the new scale.
                cur.execute("""
                    UPDATE mf_nav SET nav = nav * %s
                    WHERE scheme_code = %s AND nav_date < %s
                """, (factor, code, d))
                fixed = cur.rowcount

                cur.execute("""
                    UPDATE mf_nav_split SET applied = TRUE, rows_fixed = %s
                    WHERE scheme_code = %s AND split_date = %s
                """, (fixed, code, d))

                total += fixed
                print("  %-44s %s  %d rows restated" % (name[:44], d, fixed))
            conn.commit()

        print("\n%d NAV rows corrected across %d fund(s)." % (total, len(splits)))
        print("Re-run score_returns.py to recompute on the corrected history.")


if __name__ == "__main__":
    main()
