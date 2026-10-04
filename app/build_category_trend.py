"""
build_category_trend.py -- the average rising/sideways/falling split, by category.
---------------------------------------------------------------------------------
Gives a fund's split something to be compared against. "Rising 23" means
little on its own; "Rising 23, and the average small cap fund is 31" means
the fund is behind its peers on the same reading.

WHY THIS IS A BUILT TABLE AND NOT A QUERY
    Doing it live would mean exploding every fund in a category on every
    page load -- hundreds of funds, each with hundreds of holdings, for one
    number on a page. Built once after the nightly score run, it is a
    single row lookup.

WHY IT IMPORTS FROM portfolio_api INSTEAD OF REIMPLEMENTING IN SQL
    The classification rules are Python, not SQL: _trend_summary applies
    "sideways wins over the supertrend direction", and _is_sideways reads
    the Bollinger bands. Writing an equivalent in SQL would create a second
    copy of those rules, and the first time a threshold is tuned the
    category average would quietly disagree with the fund pages it is
    printed beside. So this calls the same functions the API calls.

EQUAL WEIGHT PER FUND, NOT PER RUPEE
    Each fund contributes one observation regardless of its AUM. The
    question being answered is "how does this FUND compare with its peers",
    so a large fund should not drag the peer average toward itself.

WHAT THE AVERAGE COVERS -- AND THE HONEST LABEL FOR IT
    Only funds whose portfolios we hold. That is not every fund in the
    category, so the page must say "the N funds we hold data for", never
    "the category". fund_count is stored precisely so the page can say it.

IT ALSO KEEPS THE PER-FUND HISTORY (mf_fund_trend)
    This job already computes every fund's own rising split -- it has to,
    in order to average them -- and until now it threw them away and kept
    only the category mean.

    Nothing else in the product records what a fund's rising score WAS.
    The figure on every page is computed live from today's stock scores,
    so "rising has fallen for three months" was not a hard question, it
    was an unanswerable one: there was no record of the previous three
    months to compare against.

    Keeping the per-fund rows costs one extra INSERT on numbers already in
    memory, and it is what makes any alert about a fund's trend possible.
    History only accrues forward -- the first run starts the record, and
    an alert asking for three months of it cannot fire until three months
    have been kept. That is worth starting early rather than well.

USAGE
    /opt/mfapi/venv/bin/python3 /opt/mfapi/build_category_trend.py
    --dry-run    compute and print, write nothing
    --min-funds  categories with fewer than this are skipped (default 5)
"""

import os
import sys
import time
from collections import defaultdict

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from dotenv import load_dotenv

sys.path.insert(0, "/opt/mfapi")
from portfolio_api import TREND_BATCH, _q, _trend_summary   # noqa: E402

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")

DRY = "--dry-run" in sys.argv
MIN_FUNDS = 5
if "--min-funds" in sys.argv:
    MIN_FUNDS = int(sys.argv[sys.argv.index("--min-funds") + 1])

# Below this much of a category's NAV carrying a reading, the average is
# not a fact about prices -- it is a fact about our coverage, and saying
# it in the vocabulary of rising and falling would misreport it.
MIN_READABLE = 40.0

# Funds are sent to TREND_BATCH in chunks. The batch query dedupes ISINs
# across the funds in one call, so bigger chunks are cheaper per fund --
# but one enormous call would hold a long transaction and lose all
# progress on any error.
CHUNK = 40

# One row per fund per score date. The category table above is an
# average OF this; keeping both means the fund's own history and its
# peers' can be read on the same date without recomputing either.
#
# readable_pct is stored rather than inferred: a fund holding bonds or
# other funds' units carries almost no stock readings, and its 0.0 is a
# fact about our coverage, not about the market. An alert that fired on
# that would be reporting our own blind spot as a fall in the fund.
FUND_DDL = """
CREATE TABLE IF NOT EXISTS mf_fund_trend (
    scheme_code   text NOT NULL,
    as_of_date    date NOT NULL,       -- the score date the split was read at
    category      text,
    up_pct        numeric NOT NULL,
    sideways_pct  numeric NOT NULL,
    down_pct      numeric NOT NULL,
    unscored_pct  numeric NOT NULL,
    readable_pct  numeric NOT NULL,    -- up + sideways + down
    built_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code, as_of_date)
);
-- The alert questions are all "this fund, over the last N dates", so the
-- index that matters is newest-first within a fund.
CREATE INDEX IF NOT EXISTS mf_fund_trend_code_date
    ON mf_fund_trend (scheme_code, as_of_date DESC);
"""

DDL = """
CREATE TABLE IF NOT EXISTS mf_category_trend (
    category      text NOT NULL,
    as_of_date    date NOT NULL,       -- the score date the split was read at
    fund_count    int  NOT NULL,
    up_pct        numeric NOT NULL,
    sideways_pct  numeric NOT NULL,
    down_pct      numeric NOT NULL,
    unscored_pct  numeric NOT NULL,
    built_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (category, as_of_date)
);
"""

# Every fund we can actually explode, with its category. A fund with no
# holdings loaded is not "a fund with nothing rising" -- it is a fund we
# cannot read, and including it as zeroes would drag every average down.
FUNDS = """
SELECT DISTINCT h.scheme_code, vc.category
FROM mf_holding h
JOIN v_scheme_category vc ON vc.scheme_code = h.scheme_code
WHERE vc.category IS NOT NULL
"""


def main():
    started = time.time()
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(FUND_DDL)

        # The website reads mf_fund_trend (the alerts screen asks how much
        # history exists) but this job may be running as a different role
        # from the one the site connects as. Grants here are explicit per
        # table in this codebase -- there are no default privileges -- so a
        # table created by the admin role is invisible to the app until
        # somebody says otherwise.
        #
        # Attempted, not assumed: granting yourself what you already own is
        # a no-op, and a database without that role is not a failure of
        # this job. Either way the table is what matters.
        try:
            app = conninfo_to_dict(DB).get("user")
            cur.execute("SELECT current_user")
            if app and cur.fetchone()[0] != app:
                cur.execute('GRANT SELECT ON mf_fund_trend TO "%s"' % app)
                cur.execute('GRANT SELECT ON mf_category_trend TO "%s"' % app)
                print("granted SELECT on the trend tables to %s" % app)
        except Exception as e:
            conn.rollback()
            cur.execute(DDL)
            cur.execute(FUND_DDL)
            print("note: could not grant on the trend tables (%s)"
                  % str(e).strip().splitlines()[0][:80])

        funds = _q(cur, FUNDS)
        by_cat = defaultdict(list)
        for f in funds:
            by_cat[f["category"]].append(str(f["scheme_code"]))
        print("funds with holdings : %d across %d categories"
              % (len(funds), len(by_cat)))

        skipped = {c: len(v) for c, v in by_cat.items() if len(v) < MIN_FUNDS}
        if skipped:
            # Named, not silently dropped. A category missing from the
            # output should be explainable without reading this file.
            print("skipped (under %d funds): %s" % (MIN_FUNDS, ", ".join(
                "%s [%d]" % (c, n) for c, n in sorted(skipped.items()))))

        rows_out, unreadable, score_date = [], [], None
        fund_rows = []          # (code, category, split) for mf_fund_trend

        # EVERY category is now read, not only the publishable ones.
        #
        # The category average still needs MIN_FUNDS to be worth printing,
        # but a fund's OWN history does not stop being worth keeping just
        # because it has few peers -- and a fund in a four-fund category
        # whose rising score is sliding is exactly as alertable as one in a
        # forty-fund category. Skipping those categories outright meant
        # those funds were never even queried.
        for category, codes in sorted(by_cat.items()):
            splits = []
            for i in range(0, len(codes), CHUNK):
                chunk = codes[i:i + CHUNK]
                grouped = defaultdict(list)
                for r in _q(cur, TREND_BATCH, {"codes": chunk}):
                    grouped[str(r["scheme_code"])].append(r)
                for code in chunk:
                    rows = grouped.get(code)
                    if not rows:
                        continue          # no readings: not a zero, absent
                    s = _trend_summary(rows)
                    score_date = score_date or s.get("score_date")
                    splits.append(s)
                    fund_rows.append((code, category, s))

            if len(codes) < MIN_FUNDS or len(splits) < MIN_FUNDS:
                continue

            n = len(splits)
            avg = {k: round(sum(s[k] for s in splits) / n, 1)
                   for k in ("up_pct", "sideways_pct", "down_pct", "unscored_pct")}

            # A category whose holdings carry almost no readings is not a
            # category where nothing is rising -- it is one we cannot read.
            # FoF Domestic holds other funds' units and Income holds bonds;
            # neither has a stock score, so both averaged to 0.0 / 0.0 / 0.0
            # and would have printed beside a fund as though the market had
            # stopped moving. Published zeroes are worse than no row.
            readable = avg["up_pct"] + avg["sideways_pct"] + avg["down_pct"]
            if readable < MIN_READABLE:
                unreadable.append((category, n, readable))
                continue

            rows_out.append((category, n, avg))
            print("  %-34s n=%-4d rising %5.1f  sideways %5.1f  falling %5.1f"
                  % (category[:34], n, avg["up_pct"],
                     avg["sideways_pct"], avg["down_pct"]))

        if unreadable:
            print("\nnot published (under %.0f%% of NAV carries a reading -- "
                  "these hold fund units or bonds, not scored stocks):"
                  % MIN_READABLE)
            for c, n, r in unreadable:
                print("  %-34s n=%-4d only %.1f%% readable" % (c[:34], n, r))

        if not rows_out:
            sys.exit("Nothing to write -- no category met the minimum.")
        if not score_date:
            sys.exit("No score date came back; is stock_score populated?")

        print("\nscore date          : %s" % score_date)
        print("categories computed : %d" % len(rows_out))
        print("fund rows to keep   : %d" % len(fund_rows))
        print("elapsed             : %.1fs" % (time.time() - started))

        if DRY:
            print("\n--dry-run: nothing written.")
            return

        for category, n, avg in rows_out:
            cur.execute("""
                INSERT INTO mf_category_trend
                    (category, as_of_date, fund_count,
                     up_pct, sideways_pct, down_pct, unscored_pct)
                VALUES (%(c)s, %(d)s, %(n)s, %(u)s, %(s)s, %(dn)s, %(x)s)
                ON CONFLICT (category, as_of_date) DO UPDATE
                   SET fund_count   = EXCLUDED.fund_count,
                       up_pct       = EXCLUDED.up_pct,
                       sideways_pct = EXCLUDED.sideways_pct,
                       down_pct     = EXCLUDED.down_pct,
                       unscored_pct = EXCLUDED.unscored_pct,
                       built_at     = now()
            """, {"c": category, "d": score_date, "n": n,
                  "u": avg["up_pct"], "s": avg["sideways_pct"],
                  "dn": avg["down_pct"], "x": avg["unscored_pct"]})
        # The per-fund history. Written after the category rows and in the
        # same transaction, so the two can never disagree about a date.
        for code, category, sp in fund_rows:
            readable = round(sp["up_pct"] + sp["sideways_pct"]
                             + sp["down_pct"], 1)
            cur.execute("""
                INSERT INTO mf_fund_trend
                    (scheme_code, as_of_date, category,
                     up_pct, sideways_pct, down_pct, unscored_pct,
                     readable_pct)
                VALUES (%(c)s, %(d)s, %(cat)s, %(u)s, %(s)s, %(dn)s,
                        %(x)s, %(r)s)
                ON CONFLICT (scheme_code, as_of_date) DO UPDATE
                   SET category     = EXCLUDED.category,
                       up_pct       = EXCLUDED.up_pct,
                       sideways_pct = EXCLUDED.sideways_pct,
                       down_pct     = EXCLUDED.down_pct,
                       unscored_pct = EXCLUDED.unscored_pct,
                       readable_pct = EXCLUDED.readable_pct,
                       built_at     = now()
            """, {"c": code, "d": score_date, "cat": category,
                  "u": sp["up_pct"], "s": sp["sideways_pct"],
                  "dn": sp["down_pct"], "x": sp["unscored_pct"],
                  "r": readable})

        conn.commit()
        print("\nwritten for %s" % score_date)
        print("  %d category rows, %d fund rows" % (len(rows_out), len(fund_rows)))

        # How much history exists yet, because an alert asking for three
        # months cannot fire until three months have been kept, and that is
        # worth seeing rather than discovering later.
        cur.execute("""
            SELECT count(DISTINCT as_of_date) AS dates,
                   MIN(as_of_date) AS first_date
            FROM mf_fund_trend
        """)
        h = cur.fetchone()
        print("  fund trend history: %d date(s), starting %s"
              % (h["dates"], h["first_date"]))


if __name__ == "__main__":
    main()
