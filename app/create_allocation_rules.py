"""
create_allocation_rules.py -- the rules table, and a starting set of rules.
---------------------------------------------------------------------------
Run once:
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_allocation_rules.py

    --reseed   wipe the seeded rows and write them again. Rows you have
               edited are marked and are NOT touched unless you also pass
               --force, because losing an afternoon of your own judgement
               to a re-run is unacceptable.

WHAT A RULE SAYS
    For a (purpose, horizon, risk) profile: where the money should END UP,
    measured through to the underlying stocks -- not which fund categories
    to buy. A 40% mid cap target can be met by one mid cap fund, or by a
    flexi cap and a small cap fund that happen to hold mid caps between
    them. The rules layer states the destination; fund selection finds
    routes to it.

    That is why buckets are large / mid / small / debt / gold /
    international rather than fund categories. Those six are the
    vocabulary the look-through can actually measure, so a recommendation
    can be checked against what the funds really hold instead of trusted
    because of what they are called.

TARGET vs FLOOR vs CAP
    target_pct  what to aim for. Targets sum to 100.
    min_pct     a floor that must not be broken.
    max_pct     a ceiling.

    "Aim for 40% mid cap" and "never less than 20% in debt" behave
    differently, and a schema with only one number cannot express the
    second.

THE SEEDED NUMBERS ARE A STARTING POINT, NOT ADVICE
    They follow the conventional shape -- more equity with a longer
    horizon, more debt as the date approaches, more small and mid cap with
    higher risk appetite. They are deliberately unremarkable. They are not
    a recommendation and they carry no analysis of any client. Read every
    row, change what you disagree with, and write your own rationale: that
    text is what appears beside the allocation when you explain it to
    someone, and it should be in your words.
"""

import os
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")

RESEED = "--reseed" in sys.argv
FORCE = "--force" in sys.argv

PURPOSES = ["appreciation", "preservation", "income"]
HORIZONS = [("<3", 0, 3), ("3-7", 3, 7), ("7-15", 7, 15), ("15+", 15, 99)]
RISKS = ["conservative", "moderate", "aggressive"]
BUCKETS = ["large", "mid", "small", "debt", "gold", "international"]

DDL = """
CREATE TABLE IF NOT EXISTS allocation_rule (
    rule_id       bigserial PRIMARY KEY,
    purpose       text NOT NULL CHECK (purpose IN
                    ('appreciation','preservation','income')),
    horizon_band  text NOT NULL CHECK (horizon_band IN ('<3','3-7','7-15','15+')),
    risk_band     text NOT NULL CHECK (risk_band IN
                    ('conservative','moderate','aggressive')),
    bucket        text NOT NULL CHECK (bucket IN
                    ('large','mid','small','debt','gold','international')),

    target_pct    numeric NOT NULL CHECK (target_pct BETWEEN 0 AND 100),
    min_pct       numeric CHECK (min_pct  BETWEEN 0 AND 100),
    max_pct       numeric CHECK (max_pct  BETWEEN 0 AND 100),

    rationale     text,
    -- Seeded rows are marked so a re-run can replace them without
    -- touching anything you have written yourself.
    seeded        boolean NOT NULL DEFAULT false,
    valid_from    date NOT NULL DEFAULT CURRENT_DATE,
    updated_at    timestamptz NOT NULL DEFAULT now(),

    UNIQUE (purpose, horizon_band, risk_band, bucket),
    CONSTRAINT sane_band CHECK (min_pct IS NULL OR max_pct IS NULL
                                OR min_pct <= max_pct)
);

CREATE INDEX IF NOT EXISTS ix_alloc_profile
    ON allocation_rule (purpose, horizon_band, risk_band);

-- The goal is where purpose and risk belong, not the client: the same
-- person can be aggressive about a grandchild's education and defensive
-- about their own retirement, and one field per client cannot say both.
CREATE TABLE IF NOT EXISTS portfolio_goal (
    portfolio_id     bigint PRIMARY KEY,
    target_amount    numeric NOT NULL CHECK (target_amount > 0),
    target_date      date    NOT NULL,
    lumpsum_amount   numeric NOT NULL DEFAULT 0 CHECK (lumpsum_amount >= 0),
    sip_amount       numeric NOT NULL DEFAULT 0 CHECK (sip_amount >= 0),
    started_on       date    NOT NULL,
    current_value    numeric CHECK (current_value >= 0),
    valued_on        date,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT value_needs_a_date
        CHECK ((current_value IS NULL) = (valued_on IS NULL))
);

ALTER TABLE portfolio_goal
    ADD COLUMN IF NOT EXISTS purpose   text,
    ADD COLUMN IF NOT EXISTS risk_band text;
"""


def seed_rows():
    """A conventional starting allocation for each of the 36 profiles.

    Equity share rises with horizon and with risk appetite; within equity,
    the tilt toward mid and small caps rises the same way, and a short
    horizon forces it back toward large caps regardless of appetite --
    because the thing that hurts a three-year goal is not low returns, it
    is a drawdown arriving in year three with no time to recover.
    """
    equity_by_purpose = {
        "appreciation":  {"<3": 30, "3-7": 60, "7-15": 80, "15+": 90},
        "preservation":  {"<3": 10, "3-7": 25, "7-15": 35, "15+": 40},
        "income":        {"<3": 15, "3-7": 30, "7-15": 40, "15+": 45},
    }
    risk_shift = {"conservative": -15, "moderate": 0, "aggressive": 10}
    # large / mid / small, as a share of the equity part.
    cap_mix = {
        "conservative": (70, 20, 10),
        "moderate":     (55, 28, 17),
        "aggressive":   (40, 33, 27),
    }
    short_mix = (85, 10, 5)      # under three years, whatever the appetite

    rows = []
    for purpose in PURPOSES:
        for band, _lo, _hi in HORIZONS:
            for risk in RISKS:
                eq = equity_by_purpose[purpose][band] + risk_shift[risk]
                eq = max(0, min(95, eq))
                mix = short_mix if band == "<3" else cap_mix[risk]

                large = round(eq * mix[0] / 100)
                mid = round(eq * mix[1] / 100)
                small = eq - large - mid          # absorbs the rounding
                debt = 100 - eq

                why = {
                    "large": f"{band} years to run and a {risk} appetite: large "
                             "caps carry the equity that has to be there without "
                             "the drawdowns that hurt most near the date.",
                    "mid": "Mid caps are where most of the extra return over an "
                           "index has historically come from, and most of the "
                           "extra volatility with it.",
                    "small": "Small caps only earn their place with time to "
                             "recover from a bad run; the share falls as the "
                             "date approaches.",
                    "debt": "Debt is what makes the goal survive a bad equity "
                            "year close to the date. It is not the part that "
                            "grows.",
                    "gold": "Left at zero. Add a share here if you want it as "
                            "a diversifier.",
                    "international": "Left at zero. Add a share here if you "
                                     "want exposure outside India.",
                }

                for bucket, target in (("large", large), ("mid", mid),
                                       ("small", small), ("debt", debt),
                                       ("gold", 0), ("international", 0)):
                    rows.append({
                        "purpose": purpose, "horizon_band": band,
                        "risk_band": risk, "bucket": bucket,
                        "target_pct": max(0, target),
                        "min_pct": max(0, target - 10) if target else 0,
                        "max_pct": min(100, target + 10) if target else 10,
                        "rationale": why[bucket],
                    })
    return rows


with psycopg.connect(DB) as conn, conn.cursor() as cur:
    cur.execute(DDL)
    conn.commit()
    print("allocation_rule ready; portfolio_goal has purpose and risk_band.")

    cur.execute("SELECT COUNT(*) FROM allocation_rule")
    existing = cur.fetchone()[0]

    if existing and not RESEED:
        print("%d rules already present. Nothing seeded." % existing)
        print("Pass --reseed to rewrite the seeded rows (yours are kept).")
    else:
        if RESEED:
            if FORCE:
                cur.execute("DELETE FROM allocation_rule")
                print("--force: every rule deleted, including your own edits.")
            else:
                cur.execute("DELETE FROM allocation_rule WHERE seeded")
                print("seeded rows cleared; edited rows kept.")

        rows = seed_rows()
        for r in rows:
            cur.execute("""
                INSERT INTO allocation_rule
                    (purpose, horizon_band, risk_band, bucket,
                     target_pct, min_pct, max_pct, rationale, seeded)
                VALUES (%(purpose)s, %(horizon_band)s, %(risk_band)s, %(bucket)s,
                        %(target_pct)s, %(min_pct)s, %(max_pct)s,
                        %(rationale)s, true)
                ON CONFLICT (purpose, horizon_band, risk_band, bucket)
                DO NOTHING
            """, r)
        conn.commit()
        print("seeded %d rows across %d profiles."
              % (len(rows), len(PURPOSES) * len(HORIZONS) * len(RISKS)))

    # Every profile's targets must sum to 100. A table that quietly sums to
    # 85 produces allocations that look right and are not.
    cur.execute("""
        SELECT purpose, horizon_band, risk_band, SUM(target_pct) AS total
        FROM allocation_rule
        GROUP BY purpose, horizon_band, risk_band
        HAVING SUM(target_pct) <> 100
        ORDER BY 1, 2, 3
    """)
    bad = cur.fetchall()
    if bad:
        print("\nPROFILES NOT SUMMING TO 100 -- fix before using:")
        for p, h, r, total in bad:
            print("   %-14s %-5s %-13s = %s" % (p, h, r, total))
    else:
        print("all profiles sum to 100.")

    cur.execute("""SELECT purpose, horizon_band, risk_band,
                          string_agg(bucket || ' ' || target_pct, ', '
                                     ORDER BY bucket)
                   FROM allocation_rule
                   WHERE purpose = 'appreciation' AND risk_band = 'aggressive'
                   GROUP BY 1,2,3 ORDER BY 2""")
    print("\nexample -- appreciation / aggressive:")
    for p, h, r, mix in cur.fetchall():
        print("   %-5s %s" % (h, mix))
