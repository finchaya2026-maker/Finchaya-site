"""
create_fund_allocation.py -- allocation rules written in FUND CATEGORIES.
---------------------------------------------------------------------------
Run once:
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_fund_allocation.py

    --reseed   wipe the seeded rows and write them again. Rows you have
               edited are marked and are NOT touched unless you also pass
               --force. Losing an afternoon of your own judgement to a
               re-run is unacceptable.
    --check    report only. Writes nothing. Use it to see how many real
               funds each category matches.

WHY A SECOND SET OF RULES RATHER THAN CHANGING THE FIRST
    allocation_rule says where money should END UP, measured through to
    the underlying stocks: large / mid / small / debt / gold /
    international. That vocabulary exists because the look-through can
    MEASURE it -- a recommendation can be checked against what the funds
    really hold rather than trusted because of what they are called.

    But nobody can buy "40% mid cap". They buy a fund. So this table says
    what to BUY, in the categories a distributor actually selects from,
    and it leads: the recommendation is built from these rows.

    The exposure rules stay, demoted to information. When the funds
    chosen here happen to land on the exposure targets, that is worth
    seeing. When they do not, that is worth seeing too -- two flexi caps
    can quietly hold the same mid caps, and the fund category alone
    cannot show it. Neither layer is a verdict on the other.

WHY THE VOCABULARY IS A TABLE AND NOT A CHECK CONSTRAINT
    allocation_rule hard-codes its six buckets into the schema, so adding
    one means a migration against a live table. Here the allowed
    categories are rows. Adding "Gold" is an INSERT.

WHY EACH CATEGORY CARRIES MATCH STRINGS
    v_scheme_category is not clean. It mixes SEBI's 2018 categories with
    the AMFI scheme-type labels that predate them -- 'Income' (4,550
    schemes), 'Growth' (245), 'Debt Funds', and doubles like 'Gilt' vs
    'Gilt Fund' and 'Money Market' vs 'Money Market Fund'. A rule written
    against those strings selects junk.

    So a category here names the exact strings it accepts. Anything not
    listed is not selectable, which is the point: the vocabulary is
    curated, not inherited.

GOLD IS THE AWKWARD ONE
    There is no gold category. Gold funds sit inside 'ETFs' and
    'FoF Domestic' and can only be found by scheme NAME. Hence
    match_name_like. It is the only category that needs it, and it is
    why the column exists rather than a cleaner design.

THE SEEDED NUMBERS ARE A STARTING POINT, NOT ADVICE
    They follow the conventional shape: more equity with a longer
    horizon and a higher risk appetite, more mid and small cap the same
    way, and a short horizon forced back to large cap whatever the
    appetite -- because what hurts a three-year goal is not low returns,
    it is a drawdown in year three with no time to recover.

    They are deliberately unremarkable, they carry no analysis of any
    client, and the 'income' profiles in particular deserve your own
    attention rather than mine. Read every row and write your own
    rationale: that text is what appears beside the allocation when you
    explain it to someone, and it should be in your words.
"""

import os
import sys

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Is /opt/mfapi/.env present?")

RESEED = "--reseed" in sys.argv
FORCE = "--force" in sys.argv
CHECK_ONLY = "--check" in sys.argv

PURPOSES = ["appreciation", "preservation", "income"]
HORIZONS = ["<3", "3-7", "7-15", "15+"]
RISKS = ["conservative", "moderate", "aggressive"]


# =====================================================================
# THE VOCABULARY
#
# code, label, asset_class, [accepted v_scheme_category strings], name_like
#
# EDIT THIS LIST. Adding a category is a row here plus a re-run; it needs
# no schema change. The fund counts printed at the end tell you at once
# whether a category you added actually matches anything.
# =====================================================================
CATEGORIES = [
    # --- equity ------------------------------------------------------
    ("large_cap",      "Large Cap Fund",        "equity",
     ["Large Cap Fund"], None),
    ("large_mid_cap",  "Large & Mid Cap Fund",  "equity",
     ["Large & Mid Cap Fund"], None),
    ("mid_cap",        "Mid Cap Fund",          "equity",
     ["Mid Cap Fund"], None),
    ("small_cap",      "Small Cap Fund",        "equity",
     ["Small Cap Fund"], None),
    ("flexi_cap",      "Flexi Cap Fund",        "equity",
     ["Flexi Cap Fund"], None),
    ("multi_cap",      "Multi Cap Fund",        "equity",
     ["Multi Cap Fund"], None),
    ("focused",        "Focused Fund",          "equity",
     ["Focused Fund"], None),
    ("value_contra",   "Value / Contra Fund",   "equity",
     ["Value Fund", "Contra Fund"], None),
    ("elss",           "ELSS (tax saving)",     "equity",
     ["ELSS"], None),
    ("sectoral",       "Sectoral / Thematic",   "equity",
     ["Sectoral/Thematic"], None),
    ("index",          "Index Fund / ETF",      "equity",
     ["Index Funds", "ETFs"], None),

    # Momentum is NOT a SEBI category. These funds track Nifty 200
    # Momentum 30 and similar, and sit inside Index Funds or Thematic.
    # Name matching is the only way to find them, and it is imperfect --
    # check what it returns before writing a rule against it.
    ("momentum",       "Momentum Fund",         "equity",
     ["Index Funds", "ETFs", "Sectoral/Thematic"], "%momentum%"),

    # --- hybrid ------------------------------------------------------
    ("aggressive_hybrid",   "Aggressive Hybrid Fund",  "hybrid",
     ["Aggressive Hybrid Fund"], None),
    ("balanced_advantage",  "Balanced Advantage",      "hybrid",
     ["Dynamic Asset Allocation or Balanced Advantage"], None),
    ("multi_asset",         "Multi Asset Allocation",  "hybrid",
     ["Multi Asset Allocation"], None),
    ("equity_savings",      "Equity Savings",          "hybrid",
     ["Equity Savings"], None),
    ("conservative_hybrid", "Conservative Hybrid Fund", "hybrid",
     ["Conservative Hybrid Fund"], None),

    # --- debt --------------------------------------------------------
    # The doubles here are deliberate: 'Ultra Short Duration Fund',
    # 'Ultra Short Term Fund' and 'Ultra Short to Short Term Fund' are
    # the same thing under three labels from different vintages.
    ("liquid",         "Liquid Fund",           "debt",
     ["Liquid Fund"], None),
    ("overnight",      "Overnight Fund",        "debt",
     ["Overnight Fund"], None),
    ("ultra_short",    "Ultra Short Duration",  "debt",
     ["Ultra Short Duration Fund", "Ultra Short Term Fund",
      "Ultra Short to Short Term Fund"], None),
    ("low_duration",   "Low Duration Fund",     "debt",
     ["Low Duration Fund"], None),
    ("money_market",   "Money Market Fund",     "debt",
     ["Money Market Fund", "Money Market"], None),
    ("short_duration", "Short Duration Fund",   "debt",
     ["Short Duration Fund", "Short Term Fund"], None),
    ("corporate_bond", "Corporate Bond Fund",   "debt",
     ["Corporate Bond Fund"], None),
    ("banking_psu",    "Banking & PSU Debt",    "debt",
     ["Banking and PSU Debt Fund"], None),
    ("gilt",           "Gilt Fund",             "debt",
     ["Gilt Fund", "Gilt"], None),
    ("dynamic_bond",   "Dynamic Bond Fund",     "debt",
     ["Dynamic Term Fund"], None),
    ("credit_risk",    "Credit Risk Fund",      "debt",
     ["Credit Risk Fund"], None),
    ("arbitrage",      "Arbitrage Fund",        "debt",
     ["Arbitrage Fund"], None),

    # --- other -------------------------------------------------------
    ("international",  "International Fund",    "international",
     ["FoF Overseas", "ETFs investing overseas"], None),
    ("gold",           "Gold Fund / ETF",       "gold",
     ["ETFs", "FoF Domestic"], "%gold%"),
]


DDL = """
CREATE TABLE IF NOT EXISTS allocation_category (
    code          text PRIMARY KEY,
    label         text NOT NULL,
    asset_class   text NOT NULL CHECK (asset_class IN
                    ('equity','hybrid','debt','gold','international')),
    -- The exact v_scheme_category strings this category accepts.
    match_categories text[] NOT NULL,
    -- Only for categories a category label cannot express. Gold, today.
    match_name_like  text,
    sort_order    int  NOT NULL DEFAULT 100,
    active        boolean NOT NULL DEFAULT true,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS allocation_fund_rule (
    rule_id       bigserial PRIMARY KEY,
    purpose       text NOT NULL CHECK (purpose IN
                    ('appreciation','preservation','income')),
    horizon_band  text NOT NULL CHECK (horizon_band IN ('<3','3-7','7-15','15+')),
    risk_band     text NOT NULL CHECK (risk_band IN
                    ('conservative','moderate','aggressive')),
    -- No CHECK list here on purpose. The vocabulary lives in
    -- allocation_category, so adding a category is an INSERT, not a
    -- migration against a live table.
    category_code text NOT NULL REFERENCES allocation_category(code),

    target_pct    numeric NOT NULL CHECK (target_pct BETWEEN 0 AND 100),
    min_pct       numeric CHECK (min_pct BETWEEN 0 AND 100),
    max_pct       numeric CHECK (max_pct BETWEEN 0 AND 100),

    rationale     text,
    seeded        boolean NOT NULL DEFAULT false,
    valid_from    date NOT NULL DEFAULT CURRENT_DATE,
    updated_at    timestamptz NOT NULL DEFAULT now(),

    UNIQUE (purpose, horizon_band, risk_band, category_code),
    CONSTRAINT sane_band CHECK (min_pct IS NULL OR max_pct IS NULL
                                OR min_pct <= max_pct)
);

CREATE INDEX IF NOT EXISTS ix_fund_rule_profile
    ON allocation_fund_rule (purpose, horizon_band, risk_band);
"""


# =====================================================================
# THE STARTING RULES
# =====================================================================
# How much of the money is in equity at all, before the risk shift.
EQUITY_BY_PURPOSE = {
    "appreciation": {"<3": 30, "3-7": 60, "7-15": 80, "15+": 90},
    "preservation": {"<3": 10, "3-7": 25, "7-15": 35, "15+": 40},
    "income":       {"<3": 15, "3-7": 30, "7-15": 40, "15+": 45},
}
RISK_SHIFT = {"conservative": -15, "moderate": 0, "aggressive": 10}

# How the equity share is split across categories. Shares of the EQUITY
# part, not of the whole portfolio. At most three, so no profile ever
# prescribes more than four funds in total.
EQUITY_MIX = {
    "conservative": [("large_cap", 65), ("flexi_cap", 35)],
    "moderate":     [("flexi_cap", 45), ("large_mid_cap", 35), ("mid_cap", 20)],
    "aggressive":   [("flexi_cap", 30), ("mid_cap", 40), ("small_cap", 30)],
}

# Under three years the appetite does not get a vote. A drawdown in year
# three has no time to recover, so the equity that remains is large cap.
SHORT_EQUITY = [("large_cap", 100)]

# The debt side, by horizon. One category except at the short end, where
# the equity side is already down to one.
DEBT_MIX = {
    "<3":   [("liquid", 60), ("ultra_short", 40)],
    "3-7":  [("short_duration", 100)],
    "7-15": [("corporate_bond", 100)],
    "15+":  [("corporate_bond", 100)],
}

RATIONALE = ("Seeded starting point, not advice. Conventional shape: equity "
             "rises with horizon and appetite, and a short horizon forces the "
             "equity back to large cap whatever the appetite. Edit this text.")

# A slice thinner than this is not worth a fund.
#
# The first version of this file split every equity allocation three ways
# regardless of its size, which gave preservation/3-7/moderate a 5% mid
# cap slice -- on a 10 lakh portfolio, Rs 50,000 in a fourth fund, adding
# paperwork and no diversification. When there is little equity it should
# go into FEWER categories, not the same three sliced thinner.
MIN_SLICE = 10


def _split(total, mix):
    """Divide `total` across `mix`, dropping categories too small to matter.

    Trims the narrowest category and re-weights until every surviving
    slice clears MIN_SLICE, or one category is left holding all of it."""
    if total <= 0:
        return []
    parts = list(mix)
    while True:
        weight = sum(share for _, share in parts)
        out, got = [], 0
        for i, (code, share) in enumerate(parts):
            pct = (total - got) if i == len(parts) - 1 \
                else round(total * share / weight)
            got += pct
            out.append((code, pct))
        if len(parts) == 1 or all(p >= MIN_SLICE for _, p in out):
            return [(c, p) for c, p in out if p > 0]
        # The LAST entry goes first, so write each mix above with the
        # category you would give up first at the end of the list.
        parts.pop()


def seed_rows():
    """3-4 categories for each of the 36 profiles, summing to 100."""
    rows = []
    for purpose in PURPOSES:
        for band in HORIZONS:
            for risk in RISKS:
                eq = EQUITY_BY_PURPOSE[purpose][band] + RISK_SHIFT[risk]
                eq = max(0, min(95, eq))

                # _split can thin a mix down to one category, but it
                # cannot delete the last one -- so a 95/5 equity/debt
                # profile still ended up prescribing a 5% debt fund.
                # A whole side too small to be worth a fund is dropped,
                # and the other side takes it.
                if 100 - eq < MIN_SLICE:
                    eq = 100
                elif eq < MIN_SLICE:
                    eq = 0
                debt = 100 - eq

                mix = SHORT_EQUITY if band == "<3" else EQUITY_MIX[risk]

                parts = _split(eq, mix) + _split(debt, DEBT_MIX[band])

                total = sum(p for _, p in parts)
                if total != 100:
                    raise AssertionError(
                        "%s/%s/%s sums to %d, not 100: %s"
                        % (purpose, band, risk, total, parts))
                if len(parts) > 4:
                    raise AssertionError(
                        "%s/%s/%s prescribes %d categories, more than 4"
                        % (purpose, band, risk, len(parts)))

                for code, pct in parts:
                    rows.append((purpose, band, risk, code, pct, RATIONALE))
    return rows


# =====================================================================
def report_coverage(cur):
    """How many real funds each category matches.

    Printed every run, because a category that matches nothing is
    invisible otherwise -- the rule saves fine, the page shows a target,
    and the recommendation silently returns no funds."""
    print("\n%-20s %-26s %8s" % ("code", "label", "funds"))
    print("-" * 58)
    empty = []
    for code, label, _ac, cats, like in CATEGORIES:
        if like:
            cur.execute("""
                SELECT count(*) AS n FROM v_scheme_category c
                JOIN mf_scheme m USING (scheme_code)
                WHERE c.category = ANY(%s) AND m.scheme_name ILIKE %s
            """, (cats, like))
        else:
            cur.execute("""
                SELECT count(*) AS n FROM v_scheme_category c
                WHERE c.category = ANY(%s)
            """, (cats,))
        n = cur.fetchone()["n"]
        print("%-20s %-26s %8d" % (code, label[:26], n))
        if n == 0:
            empty.append(code)

    if empty:
        print("\nWARNING -- these match NO funds: %s" % ", ".join(empty))
        print("A rule using them would show a target and recommend nothing.")
    return empty


def main():
    rows = seed_rows()          # raises before touching the DB if unbalanced
    print("Seed built: %d rows across %d profiles."
          % (len(rows), len(PURPOSES) * len(HORIZONS) * len(RISKS)))

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        if CHECK_ONLY:
            report_coverage(cur)
            print("\n--check: nothing was written.")
            return

        cur.execute(DDL)
        conn.commit()
        print("Tables ready.")

        for i, (code, label, ac, cats, like) in enumerate(CATEGORIES):
            cur.execute("""
                INSERT INTO allocation_category
                    (code, label, asset_class, match_categories,
                     match_name_like, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET
                    label = EXCLUDED.label,
                    asset_class = EXCLUDED.asset_class,
                    match_categories = EXCLUDED.match_categories,
                    match_name_like = EXCLUDED.match_name_like,
                    sort_order = EXCLUDED.sort_order,
                    updated_at = now()
            """, (code, label, ac, cats, like, i * 10))
        conn.commit()
        print("%d categories in the vocabulary." % len(CATEGORIES))

        cur.execute("SELECT count(*) AS n FROM allocation_fund_rule")
        existing = cur.fetchone()["n"]

        if existing and not RESEED:
            print("\n%d rules already exist. Left alone." % existing)
            print("Use --reseed to rewrite the seeded ones "
                  "(--reseed --force to rewrite yours too).")
            report_coverage(cur)
            return

        if RESEED:
            if FORCE:
                cur.execute("DELETE FROM allocation_fund_rule")
                print("--force: every rule deleted, including yours.")
            else:
                cur.execute("DELETE FROM allocation_fund_rule WHERE seeded")
                print("Seeded rules deleted. Edited rules kept.")
            conn.commit()

        written = 0
        for purpose, band, risk, code, pct, why in rows:
            # DO NOTHING, not DO UPDATE: a row you have edited is not
            # seeded any more and must survive a re-seed untouched.
            cur.execute("""
                INSERT INTO allocation_fund_rule
                    (purpose, horizon_band, risk_band, category_code,
                     target_pct, rationale, seeded)
                VALUES (%s, %s, %s, %s, %s, %s, true)
                ON CONFLICT (purpose, horizon_band, risk_band, category_code)
                DO NOTHING
            """, (purpose, band, risk, code, pct, why))
            written += cur.rowcount
        conn.commit()
        print("%d rules written, %d already present and left alone."
              % (written, len(rows) - written))

        report_coverage(cur)

        # Prove the invariant against the DATABASE, not the Python.
        cur.execute("""
            SELECT purpose, horizon_band, risk_band, sum(target_pct) AS total
            FROM allocation_fund_rule
            GROUP BY 1,2,3 HAVING sum(target_pct) <> 100
        """)
        bad = cur.fetchall()
        if bad:
            print("\nWARNING -- these profiles do not sum to 100:")
            for b in bad:
                print("   %s / %s / %s = %s" % (b["purpose"], b["horizon_band"],
                                                b["risk_band"], b["total"]))
        else:
            print("\nAll 36 profiles sum to 100.")


main()
