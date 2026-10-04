"""
build_fund_consistency.py -- how reliably a fund beats its benchmark.
----------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/build_fund_consistency.py
    --dry-run     compute and report, write nothing
    --years 3 5   which window lengths (default both)

WHY ROLLING WINDOWS AND NOT THE 3Y NUMBER WE ALREADY SHOW
    A single three-year return is one draw. It depends entirely on where
    the window happens to start, and a fund that looks excellent measured
    from March 2022 can look ordinary measured from June 2022. Ranking
    funds on one window ranks their start dates as much as their managers.

    Ten years of NAV gives about 85 overlapping three-year windows per
    fund. "Beat its benchmark in 71 of 85" is a statement about the
    manager. "Beat it over the last three years" is a statement about a
    date.

    This is the measure that separates a fund that is reliably a little
    ahead from one carried by a single good run -- and the second is far
    more common, because a fund only reaches a shortlist after a good run.

WHAT IS STORED
    windows_total   how many windows the history allows
    windows_beat    how many the fund won
    worst_excess    the worst it ever did against the index over a full
                    window. The closest thing to a downside measure
                    available without daily data, and the number that says
                    what a bad stretch actually looked like.
    median_excess   the typical margin, which is not the average -- one
                    spectacular window should not carry the figure.

FUNDS WITH TOO LITTLE HISTORY ARE ABSENT, NOT ZERO
    A fund with two years of NAV cannot have a three-year window. It gets
    no row rather than a row of zeroes, so that selection can say "not
    enough history to judge" instead of ranking it last as though it had
    been measured and found wanting.
"""

import os
import sys
from collections import defaultdict
from statistics import median

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")

DRY = "--dry-run" in sys.argv
if "--years" in sys.argv:
    WINDOWS = [int(a) for a in sys.argv[sys.argv.index("--years") + 1:]
               if a.isdigit()] or [3, 5]
else:
    WINDOWS = [3, 5]

# Funds are processed in chunks. Seventeen million NAV rows will not fit in
# memory, and one query per fund would be thousands of round trips.
CHUNK = 200

DDL = """
CREATE TABLE IF NOT EXISTS fund_consistency (
    scheme_code    text NOT NULL,
    window_years   int  NOT NULL,
    windows_total  int  NOT NULL,
    windows_beat   int  NOT NULL,
    beat_pct       numeric NOT NULL,
    median_excess  numeric,
    worst_excess   numeric,
    best_excess    numeric,
    median_cagr    numeric,
    -- Against the median fund in its own category, over the same windows.
    -- Kept beside the benchmark figures rather than instead of them: "did
    -- this beat an index nobody sells" and "did this beat the alternatives
    -- I could actually have chosen" are different questions, and the
    -- second is the one a distributor is answering.
    cat_windows    int,
    cat_beat       int,
    cat_beat_pct   numeric,
    cat_median_excess numeric,
    bench_source   text,
    first_window   date,
    last_window    date,
    built_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code, window_years)
);
CREATE INDEX IF NOT EXISTS ix_consistency_window
    ON fund_consistency (window_years, beat_pct DESC);
"""

# One NAV per month per fund -- the last trading day of each month. Daily
# data would be 250 points a year to answer a question about years, and
# month ends are the convention every factsheet uses anyway.
MONTHLY_NAV = """
SELECT DISTINCT ON (scheme_code, date_trunc('month', nav_date))
       scheme_code, nav_date, nav
FROM mf_nav
WHERE scheme_code = ANY(%(codes)s) AND nav > 0
ORDER BY scheme_code, date_trunc('month', nav_date), nav_date DESC
"""

MONTHLY_INDEX = """
SELECT DISTINCT ON (benchmark_id, date_trunc('month', index_date))
       benchmark_id, index_date, index_value
FROM benchmark_nav
WHERE index_value > 0
ORDER BY benchmark_id, date_trunc('month', index_date), index_date DESC
"""

# The benchmark each fund is measured against.
BENCH_MAP = """
SELECT DISTINCT ON (scheme_code) scheme_code, benchmark_id
FROM mf_benchmark_map
WHERE benchmark_id IS NOT NULL
ORDER BY scheme_code, benchmark_id
"""

# Only 542 of 3,378 schemes carry their own mapping, so a per-scheme lookup
# alone leaves two thirds of the universe unmeasurable -- for reasons that
# have nothing to do with the funds. This is the same fallback api.py
# already uses: the benchmark most of a category's mapped schemes point at,
# applied to every fund in that category.
BENCH_BY_CATEGORY = """
SELECT c.category, m.benchmark_id, COUNT(*) AS n
FROM mf_benchmark_map m
JOIN v_scheme_category c ON c.scheme_code = m.scheme_code
WHERE m.benchmark_id IS NOT NULL
GROUP BY 1, 2
ORDER BY c.category, n DESC
"""

SCHEME_CATEGORY = """
SELECT c.canonical_scheme_code AS scheme_code, vc.category
FROM v_fund_canonical c
JOIN v_scheme_category vc ON vc.scheme_code = c.canonical_scheme_code
WHERE vc.category IS NOT NULL
"""

FUNDS = """
SELECT c.canonical_scheme_code AS scheme_code
FROM v_fund_canonical c
WHERE EXISTS (SELECT 1 FROM mf_nav n
               WHERE n.scheme_code = c.canonical_scheme_code)
"""


def cagr(start, end, years):
    if start <= 0 or end <= 0 or years <= 0:
        return None
    return (end / start) ** (1.0 / years) - 1.0


def windows_for(series, index_series, years):
    """Every window of `years` that the history allows.

    Both sides are stepped by MONTH INDEX, not by looking up dates
    separately -- so the fund and the index are always measured over
    exactly the same period. Comparing a fund's Jan-to-Jan against an
    index's Feb-to-Feb would produce excess returns that are mostly
    calendar noise.
    """
    step = years * 12
    out = []
    for i in range(len(series) - step):
        d0, n0 = series[i]
        d1, n1 = series[i + step]
        f = cagr(n0, n1, years)
        if f is None:
            continue
        b = None
        if index_series:
            i0 = index_series.get(d0.replace(day=1))
            i1 = index_series.get(d1.replace(day=1))
            if i0 and i1:
                b = cagr(i0, i1, years)
        out.append((d0, d1, f, b))
    return out


def main():
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()

        cur.execute(BENCH_MAP)
        own_bench = {str(r["scheme_code"]): r["benchmark_id"]
                     for r in cur.fetchall()}

        cur.execute(BENCH_BY_CATEGORY)
        cat_bench = {}
        for r in cur.fetchall():
            cat_bench.setdefault(r["category"], r["benchmark_id"])

        cur.execute(SCHEME_CATEGORY)
        category = {str(r["scheme_code"]): r["category"] for r in cur.fetchall()}

        print("schemes with their own benchmark:", len(own_bench))
        print("categories with a modal benchmark:", len(cat_bench))

        cur.execute(MONTHLY_INDEX)
        idx = defaultdict(dict)
        for r in cur.fetchall():
            idx[r["benchmark_id"]][r["index_date"].replace(day=1)] = \
                float(r["index_value"])
        print("benchmark series:", len(idx))

        cur.execute(FUNDS)
        codes = [str(r["scheme_code"]) for r in cur.fetchall()]
        print("funds with NAV:", len(codes))

        # PASS ONE: every fund's windows, held in memory.
        #
        # The category median cannot be computed fund by fund -- it needs
        # every fund's return over the SAME window before any of them can be
        # compared to it. About half a million small tuples, which is a few
        # tens of megabytes and far cheaper than a second trip through
        # seventeen million NAV rows.
        per_fund = {}
        skipped = 0
        for start_i in range(0, len(codes), CHUNK):
            chunk = codes[start_i:start_i + CHUNK]
            cur.execute(MONTHLY_NAV, {"codes": chunk})
            series = defaultdict(list)
            for r in cur.fetchall():
                series[str(r["scheme_code"])].append(
                    (r["nav_date"], float(r["nav"])))

            for code in chunk:
                s = series.get(code)
                if not s or len(s) < 13:
                    skipped += 1
                    continue
                bid = own_bench.get(code) or cat_bench.get(category.get(code))
                src_label = ("scheme" if own_bench.get(code)
                             else "category" if bid else None)
                iseries = idx.get(bid) if bid else None
                per_fund[code] = {"bid": bid, "src": src_label, "w": {}}
                for years in WINDOWS:
                    per_fund[code]["w"][years] = windows_for(s, iseries, years)
            print("  reading %d/%d" % (min(start_i + CHUNK, len(codes)),
                                       len(codes)), end="\r")
        print(" " * 44, end="\r")

        # The median fund in each category, for each window start.
        pool = defaultdict(list)
        for code, d in per_fund.items():
            cat = category.get(code)
            if not cat:
                continue
            for years, ws in d["w"].items():
                for d0, _d1, f, _b in ws:
                    pool[(cat, years, d0)].append(f)
        cat_median = {k: median(v) for k, v in pool.items() if len(v) >= 5}
        print("category-window medians:", len(cat_median))

        # PASS TWO: score each fund against both yardsticks.
        rows_out = []
        for code, d in per_fund.items():
            cat = category.get(code)
            for years, ws in d["w"].items():
                paired = [(d0, d1, f, b) for d0, d1, f, b in ws if b is not None]
                cat_pairs = [(d0, f, cat_median[(cat, years, d0)])
                             for d0, _d1, f, _b in ws
                             if (cat, years, d0) in cat_median]

                if len(paired) < 12 and len(cat_pairs) < 12:
                    continue

                row = {"code": code, "years": years,
                       "total": 0, "beat": 0, "beat_pct": 0,
                       "median_excess": None, "worst_excess": None,
                       "best_excess": None, "median_cagr": None,
                       "cat_windows": None, "cat_beat": None,
                       "cat_beat_pct": None, "cat_median_excess": None,
                       "bench_source": d["src"],
                       "first": None, "last": None}

                if len(paired) >= 12:
                    ex = [f - b for _, _, f, b in paired]
                    row.update(
                        total=len(paired),
                        beat=sum(1 for e in ex if e > 0),
                        beat_pct=round(100.0 * sum(1 for e in ex if e > 0)
                                       / len(paired), 1),
                        median_excess=round(median(ex) * 100, 2),
                        worst_excess=round(min(ex) * 100, 2),
                        best_excess=round(max(ex) * 100, 2),
                        median_cagr=round(
                            median([f for _, _, f, _ in paired]) * 100, 2),
                        first=paired[0][0], last=paired[-1][1])

                if len(cat_pairs) >= 12:
                    cex = [f - m for _, f, m in cat_pairs]
                    row.update(
                        cat_windows=len(cat_pairs),
                        cat_beat=sum(1 for e in cex if e > 0),
                        cat_beat_pct=round(100.0 * sum(1 for e in cex if e > 0)
                                           / len(cex), 1),
                        cat_median_excess=round(median(cex) * 100, 2))
                    if row["median_cagr"] is None:
                        row["median_cagr"] = round(
                            median([f for _, f, _ in cat_pairs]) * 100, 2)
                        row["first"] = cat_pairs[0][0]
                        row["last"] = ws[-1][1]

                rows_out.append(row)

        print("rows %s: %d" % ("that would be written" if DRY else "to write",
                               len(rows_out)))
        print("funds with too little NAV:", skipped)
        print("funds with no benchmark at all:",
              sum(1 for d in per_fund.values() if not d["bid"]))

        if DRY:
            with_b = sum(1 for r in rows_out if r["total"])
            with_c = sum(1 for r in rows_out if r["cat_windows"])
            print("rows scored against a benchmark:", with_b)
            print("rows scored against the category median:", with_c)
            print("\n--dry-run: nothing written.")
            return

        for r in rows_out:
            cur.execute("""
                INSERT INTO fund_consistency
                  (scheme_code, window_years, windows_total, windows_beat,
                   beat_pct, median_excess, worst_excess, best_excess,
                   median_cagr, cat_windows, cat_beat, cat_beat_pct,
                   cat_median_excess, bench_source, first_window, last_window,
                   built_at)
                VALUES (%(code)s, %(years)s, %(total)s, %(beat)s, %(beat_pct)s,
                        %(median_excess)s, %(worst_excess)s, %(best_excess)s,
                        %(median_cagr)s, %(cat_windows)s, %(cat_beat)s,
                        %(cat_beat_pct)s, %(cat_median_excess)s,
                        %(bench_source)s, %(first)s, %(last)s, now())
                ON CONFLICT (scheme_code, window_years) DO UPDATE SET
                   windows_total = EXCLUDED.windows_total,
                   windows_beat  = EXCLUDED.windows_beat,
                   beat_pct      = EXCLUDED.beat_pct,
                   median_excess = EXCLUDED.median_excess,
                   worst_excess  = EXCLUDED.worst_excess,
                   best_excess   = EXCLUDED.best_excess,
                   median_cagr   = EXCLUDED.median_cagr,
                   cat_windows   = EXCLUDED.cat_windows,
                   cat_beat      = EXCLUDED.cat_beat,
                   cat_beat_pct  = EXCLUDED.cat_beat_pct,
                   cat_median_excess = EXCLUDED.cat_median_excess,
                   bench_source  = EXCLUDED.bench_source,
                   first_window  = EXCLUDED.first_window,
                   last_window   = EXCLUDED.last_window,
                   built_at      = now()
            """, r)
        conn.commit()
        print("written:", len(rows_out))

        cur.execute("""
            SELECT window_years, COUNT(*) AS funds,
                   COUNT(beat_pct) AS with_bench,
                   COUNT(cat_beat_pct) AS with_cat,
                   ROUND(AVG(cat_beat_pct), 1) AS avg_cat_beat
            FROM fund_consistency GROUP BY 1 ORDER BY 1
        """)
        print("\nby window:")
        for r in cur.fetchall():
            print("  %dy  %4d funds | %4d vs benchmark | %4d vs category "
                  "(avg beat %.1f%%)"
                  % (r["window_years"], r["funds"], r["with_bench"],
                     r["with_cat"], r["avg_cat_beat"] or 0))

        cur.execute("""
            SELECT s.scheme_name, f.cat_beat_pct, f.cat_windows,
                   f.cat_median_excess, f.worst_excess, f.bench_source
            FROM fund_consistency f
            JOIN mf_scheme s ON s.scheme_code = f.scheme_code
            WHERE f.window_years = 3 AND f.cat_windows >= 50
            ORDER BY f.cat_beat_pct DESC LIMIT 10
        """)
        print("\nmost consistent vs their category, 3y windows:")
        for r in cur.fetchall():
            print("  %-42s %5.1f%% of %3d  median %+.1f  worst vs bench %s"
                  % (r["scheme_name"][:42], r["cat_beat_pct"], r["cat_windows"],
                     r["cat_median_excess"],
                     ("%+.1f" % r["worst_excess"]) if r["worst_excess"] is not None
                     else "n/a"))


if __name__ == "__main__":
    main()
