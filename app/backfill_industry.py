"""
backfill_industry.py -- fill stock_master.industry from mf_holding_raw.
------------------------------------------------------------------------
v2. v1 took the most common industry across ALL history. That was wrong in
a way worth spelling out, because the same trap waits in any other
"most common value wins" backfill.

    Aditya Birla Real Estate Limited
        Paper, Forest & Jute Products   278 filings
        Realty                          137 filings

Both are correct FOR THEIR OWN DATE. The company was Century Textiles and
was reclassified when it became a real estate business. Counting all
history equally lets the stale label outvote the current one, so a realty
company gets filed under paper and jute -- and every company that
reclassifies fails the same way. Reclassification happens to exactly the
companies a reader is most likely to ask about.

So this version:

  1. USES RECENT FILINGS ONLY (RECENT_MONTHS below). The current label
     wins because the stale one is no longer in the vote at all.

  2. DROPS JUNK. "N.A." is not an industry, and neither is "64.8" --
     bare numbers in that column are a percentage that landed one column
     over in some AMC file layout. They never won a vote, but they should
     not be in the count.

  3. GROUPS PUNCTUATION VARIANTS. "Agricultural, Commercial &
     Construction Vehicles" and "Agricultural, Commercial and Constr" are
     one industry filed three ways. Votes are counted on a normalised key
     and the most common RAW spelling of the winner is stored, so the
     display value stays something an AMC actually wrote.

SAFE TO RE-RUN. Writes only rows whose value would change; --dry-run
shows what would happen and writes nothing.

USAGE
    /opt/mfapi/venv/bin/python3 /opt/mfapi/backfill_industry.py --dry-run
    /opt/mfapi/venv/bin/python3 /opt/mfapi/backfill_industry.py
"""

import os
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")

DRY = "--dry-run" in sys.argv

# Long enough that a company held by few funds still gets votes, short
# enough that a reclassification takes effect within about two disclosure
# cycles rather than being outvoted by years of history.
RECENT_MONTHS = 9

# Normalised vote key: case, "and"/"&" and punctuation folded away.
NORM = (r"regexp_replace(lower(regexp_replace(btrim(industry), "
        r"'\s+and\s+', ' & ', 'gi')), '[^a-z0-9]', '', 'g')")

CANDIDATES = f"""
WITH recent AS (
    SELECT isin, btrim(industry) AS industry
    FROM mf_holding_raw
    WHERE isin IS NOT NULL
      AND industry IS NOT NULL
      AND btrim(industry) <> ''
      -- junk: placeholders, and the stray numeric from a shifted column
      AND upper(btrim(industry)) NOT IN
          ('N.A.', 'NA', 'N/A', '-', 'NIL', 'OTHERS')
      AND btrim(industry) !~ '^[0-9.]+$'
      AND as_of_date >= (SELECT max(as_of_date) FROM mf_holding_raw)
                        - INTERVAL '{RECENT_MONTHS} months'
),
keyed AS (
    SELECT isin, industry, {NORM} AS k FROM recent
),
tally AS (
    SELECT isin, k, count(*) AS votes FROM keyed GROUP BY isin, k
),
winner AS (
    SELECT isin, k,
           row_number() OVER (PARTITION BY isin ORDER BY votes DESC, k) AS rn
    FROM tally
),
spelling AS (
    SELECT k1.isin, k1.industry, count(*) AS n
    FROM keyed k1
    JOIN winner w ON w.isin = k1.isin AND w.k = k1.k AND w.rn = 1
    GROUP BY k1.isin, k1.industry
),
best AS (
    SELECT isin, industry,
           row_number() OVER (PARTITION BY isin ORDER BY n DESC, industry) AS rn
    FROM spelling
)
SELECT isin, industry FROM best WHERE rn = 1
"""

with psycopg.connect(DB) as conn, conn.cursor() as cur:
    cur.execute("SELECT max(as_of_date) FROM mf_holding_raw")
    print("newest disclosure in raw   : %s  (voting window: last %d months)"
          % (cur.fetchone()[0], RECENT_MONTHS))

    cur.execute(f"CREATE TEMP TABLE pick AS {CANDIDATES}")
    cur.execute("SELECT count(*) FROM pick")
    print("ISINs with a classification: %d" % cur.fetchone()[0])

    # What this version would CHANGE against what is stored now -- the v1
    # mistakes, listed rather than silently corrected.
    cur.execute("""
        SELECT m.company_name, m.industry AS was, p.industry AS now
        FROM pick p JOIN stock_master m ON m.isin = p.isin
        WHERE m.industry IS NOT NULL
          AND m.industry IS DISTINCT FROM p.industry
        ORDER BY m.company_name LIMIT 25
    """)
    changes = cur.fetchall()
    if changes:
        print("\nchanging (up to 25 shown):")
        for name, was, now in changes:
            print("  %-38s %s  ->  %s" % ((name or "")[:38], was, now))
    else:
        print("\nnothing already stored would change.")

    if DRY:
        print("\n--dry-run: nothing written.")
        raise SystemExit

    cur.execute("""
        UPDATE stock_master m
           SET industry = p.industry, updated_at = now()
          FROM pick p
         WHERE m.isin = p.isin
           AND m.industry IS DISTINCT FROM p.industry
    """)
    print("\nrows updated : %d" % cur.rowcount)
    conn.commit()

    cur.execute("""
        SELECT count(DISTINCT h.isin),
               count(DISTINCT h.isin) FILTER (WHERE m.industry IS NOT NULL)
        FROM mf_holding h LEFT JOIN stock_master m ON m.isin = h.isin
    """)
    held, classified = cur.fetchone()
    print("held ISINs classified: %d of %d (%.1f%%)"
          % (classified, held, 100.0 * classified / held if held else 0))
