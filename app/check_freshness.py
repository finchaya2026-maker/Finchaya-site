"""
check_freshness.py -- the gate. Is the pipeline actually current?
---------------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/check_freshness.py

READ-ONLY. Writes nothing.

Exit code is the number of problems found, so run_step in nightly.sh
counts it as a failed step and the summary line says so.

WHY THIS FILE CHANGED -- THREE SEPARATE FAULTS
    On 12 September 2026 it printed this, and then said everything was
    fine:

        technicals    2026-09-10     2 days old
        stock scores  2026-09-10     2 days old
        fund scores   2026-09-10     2 days old
        returns       2026-09-14    -2 days old
        NAV           2026-09-14    -2 days old
        Freshness OK -- every stage is current and in step.

    1. THE STALE READING WAS NOT A BUG. The technicals limit was 8 days,
       chosen when a human had to log into Kite by hand once a week, and
       the old comment said in as many words: "the day they are
       automated, drop it to 2 or 3". They are automated now -- the
       login runs headless from nightly.sh -- so the limit comes down,
       as instructed.

    2. THE FUTURE DATES WERE A REAL HOLE. Every age test asked only "is
       this too old". With NAV dated two days ahead, (today - nav).days
       is -2, and -2 > 4 is false, so nothing fired. A negative age is
       not freshness; it is a date that cannot exist, and it is worse
       than staleness because everything downstream keys off
       max(nav_date). `returns` was also printed and then never tested
       at all -- the one column carrying the corruption was the one
       column nothing examined.

    3. IT WAS WATCHING TWO RETIRED ALGORITHMS. This is the worst of the
       three. STOCK_ALGO was "stock-v2" and FUND_ALGO was "fund-v1".
       The engine has since moved to stock-v3 and fund-v2 -- api.py
       reads those, and so the site does too. The old labels stopped
       being written on 10 September, so from the 11th onward this
       check was measuring the age of something nobody feeds any more,
       and would have reported a frozen pipeline forever while the real
       one ran perfectly.

       A monitor pointed at a dead table is worse than no monitor: it
       produces alarms nobody can act on, and people stop reading it.
       That is why the drift check below exists -- watching the right
       label is not enough if nothing notices when the label changes
       again. portfolio_api.py and portfolio_lookthrough.py both carry
       comments warning about exactly this; this file did not get the
       message.

WHY BUSINESS DAYS
    A Saturday with no NAV is correct. Counting calendar days means
    every Monday reads as three days of staleness, and a check that
    cries wolf twice a week is one nobody believes on the day it
    matters. Public holidays are not handled -- that needs the exchange
    calendar, and one false alarm a few times a year is a fair price
    for not carrying one.

WHAT THIS STILL CANNOT CATCH
    Wrong numbers that arrive on time. A NAV series that silently
    unsplit itself, a benchmark mapped to the wrong index -- those are
    current, consistent, and completely false. This file only answers
    "is it recent", never "is it right".
"""

import os
import sys
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
DB = os.getenv("FINCHAYA_DB")

# MUST MATCH api.py, which is what the website reads. Overridable by
# environment so the two can be moved together from .env rather than by
# editing two files and remembering both -- which is the mistake that
# left this check watching stock-v2 for two days.
#
# The defaults are the live labels as of 13 September 2026. If you bump
# the engine and forget this line, the drift check below will tell you
# instead of silently reporting a frozen pipeline.
STOCK_ALGO = os.getenv("MF_STOCK_ALGO", "stock-v3")
FUND_ALGO = os.getenv("MF_FUND_ALGO", "fund-v2")

# Business days, not calendar days.
#
# These were 8 (technicals) and 4 (NAV) in calendar days, sized for a
# weekly manual refresh. The stock side now runs nightly from cron, so
# there is no longer any reason to tolerate a week.
#
# WHAT "0" LOOKS LIKE. The check runs at the END of nightly.sh, after
# the fetch, so on a weekday the stock stages should be at today: age 0.
# At a weekend the stock steps are skipped by design and Friday's date
# still reads as age 0, because Saturday and Sunday are not business
# days. So 0 is the normal state every single night, and any age above
# it means a trading day went by without landing.
#
# WHY 1 AND NOT 0. A market holiday looks identical to a failure from
# here -- the fetch runs, Kite returns nothing, and the date does not
# move. Tolerating one absorbs that. Two consecutive missed days is
# beyond what a single holiday explains, and is the shape the September
# 2026 freeze had on its second day.
#
# WHY NAV AND RETURNS GET 2. AMFI publishes on its own schedule and does
# genuinely slip a day; returns are computed from NAV, so they inherit
# that slack. The stock side has no such excuse now that it is automated.
MAX_AGE = {
    "technicals":   1,
    "stock_scores": 1,
    "fund_scores":  1,
    "returns":      2,
    "nav":          2,
}

# How far ahead of today a NAV may legitimately be dated.
#
# Liquid and overnight funds are valued on calendar days and AMFI
# publishes them slightly forward, so "ahead of today" is not by itself
# wrong -- see section 1b below. But accrual explains a few days, not a
# fortnight: anything past this is a feed or parsing fault rather than a
# liquid fund doing its job, and is reported as a problem.
NAV_AHEAD_LIMIT_DAYS = 7

# stage key -> (table, date column, what feeds it, algo label or None)
SOURCE = {
    "technicals":   ("stock_technical", "as_of_date", "fetch_technicals.py", None),
    "stock_scores": ("stock_score",     "as_of_date", "score_stocks.py",     STOCK_ALGO),
    "fund_scores":  ("mf_score",        "as_of_date", "score_funds.py",      FUND_ALGO),
    "returns":      ("mf_returns",      "as_of_date", "score_returns.py",    None),
    "nav":          ("mf_nav",          "nav_date",   "load_mf_schemes.py",  None),
}

LABEL = {
    "technicals":   "technicals",
    "stock_scores": "stock scores",
    "fund_scores":  "fund scores",
    "returns":      "returns",
    "nav":          "NAV",
}

ORDER = ["technicals", "stock_scores", "fund_scores", "returns", "nav"]


def business_days_between(a, b):
    """Business days from a to b, weekends excluded. Negative when b is
    before a -- the sign is the point, not an accident."""
    if a is None or b is None:
        return None
    if a == b:
        return 0
    lo, hi = (a, b) if b > a else (b, a)
    n, d = 0, lo
    while d < hi:
        d += timedelta(days=1)
        if d.isoweekday() <= 5:
            n += 1
    return n if b > a else -n


def describe(age):
    if age < 0:
        n = -age
        return f"{n} business day{'s' if n != 1 else ''} AHEAD"
    if age == 0:
        return "current"
    return f"{age} business day{'s' if age != 1 else ''} old"


def main():
    if not DB:
        print("FINCHAYA_DB is not set")
        return 1

    with psycopg.connect(DB, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT MAX(as_of_date) FROM stock_technical)        AS technicals,
                  (SELECT MAX(as_of_date) FROM stock_score
                    WHERE algo_version = %(sa)s)                       AS stock_scores,
                  (SELECT MAX(as_of_date) FROM mf_score
                    WHERE algo_version = %(fa)s)                       AS fund_scores,
                  (SELECT MAX(as_of_date) FROM mf_returns)             AS returns,
                  (SELECT MAX(nav_date) FROM mf_nav
                    WHERE nav_date <= CURRENT_DATE)                    AS nav,
                  (SELECT MAX(nav_date) FROM mf_nav)                   AS nav_any,
                  (SELECT count(*) FROM mf_nav
                    WHERE nav_date > CURRENT_DATE)                     AS nav_ahead
            """, {"sa": STOCK_ALGO, "fa": FUND_ALGO})
            r = cur.fetchone()

        today = date.today()
        problems = []

        print(f"\n  watching  stock_score = '{STOCK_ALGO}'   "
              f"mf_score = '{FUND_ALGO}'   (must match api.py)\n")

        print(f"  {'stage':<14}{'newest':<13}{'age':<26}{'limit':<7}")
        for key in ORDER:
            d = r[key]
            limit = MAX_AGE[key]
            if d is None:
                print(f"  {LABEL[key]:<14}{'EMPTY':<13}{'':<26}{limit:<7}")
                continue
            age = business_days_between(d, today)
            flag = ("  <-- IMPOSSIBLE" if age < 0 else
                    "  <-- STALE" if age > limit else "")
            print(f"  {LABEL[key]:<14}{str(d):<13}{describe(age):<26}"
                  f"{limit:<7}{flag}")
        print("")

        # ---- 1. EMPTY ------------------------------------------------
        for key in ORDER:
            if r[key] is None:
                table, _, _, algo = SOURCE[key]
                problems.append(
                    f"{table} has no rows" + (f" for algo_version '{algo}'"
                                              if algo else " at all"))

        # ---- 1b. NAV AHEAD OF TODAY IS NORMAL. SAY SO, DO NOT ALARM. --
        #
        # Liquid and overnight funds are valued on CALENDAR days, not
        # business days, because they accrue interest every day
        # including weekends. AMFI's feed therefore carries NAVs dated
        # past the last trading day for exactly those schemes, and on
        # 13 September 2026 that was 662 rows across twenty-plus AMCs --
        # every one a liquid or overnight fund, with the tiny
        # day-on-day accrual you would expect (452.9246 -> 453.1299,
        # about 0.045%, which is roughly 7% a year).
        #
        # So mf_nav's freshness is measured from the newest NON-future
        # NAV -- see the query above -- and the forward rows are
        # reported as a note.
        #
        # An earlier draft of this file treated them as corruption. That
        # would have printed IMPOSSIBLE every night for something
        # correct, and a check that alarms on normal behaviour is one
        # people stop reading -- which is how it comes to be silent on
        # the night that matters. Getting this wrong would have been the
        # same class of mistake as the 8-day limit it replaced.
        #
        # The escalation below is the honest bound: accrual explains a
        # few days ahead, nothing explains a fortnight.
        if r["nav_ahead"]:
            print(f"  note: {r['nav_ahead']:,} NAV row(s) dated ahead of today, "
                  f"newest {r['nav_any']} -- normal for liquid and overnight\n"
                  f"        funds, which are valued on calendar days.\n")
            if r["nav_any"] and (r["nav_any"] - today).days > NAV_AHEAD_LIMIT_DAYS:
                problems.append(
                    "mf_nav has rows dated %s, %d days ahead of today (limit %d) "
                    "-- daily accrual does not explain that far forward; "
                    "run nav_future.py"
                    % (r["nav_any"], (r["nav_any"] - today).days,
                       NAV_AHEAD_LIMIT_DAYS))

        # ---- 2. DATED IN THE FUTURE ----------------------------------
        #
        # Checked BEFORE staleness, and never skipped, because this is
        # the failure the old version could not see. A stage ahead of
        # today is not early -- something wrote a date that does not
        # exist, and every max(date) downstream is now anchored to it.
        #
        # mf_nav is excluded here and handled above: its forward rows
        # are real. mf_returns is NOT excluded and never will be -- it
        # is derived, score_returns.py is clamped to today, and any
        # future date in it is a bug by definition.
        for key in ORDER:
            if key == "nav":
                continue        # handled above; r["nav"] is clamped anyway
            d = r[key]
            if d is None or d <= today:
                continue
            table, col, _, _ = SOURCE[key]
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT count(*) AS n FROM {table} WHERE {col} > CURRENT_DATE")
                n = cur.fetchone()["n"]
            problems.append(
                f"{table} has {n:,} row(s) dated {d}, which is in the FUTURE "
                f"-- anything keyed to max({col}) is now keyed to it")

        # ---- 3. HAS THE ENGINE MOVED WITHOUT US? ---------------------
        #
        # The fault that made this rewrite necessary. Watching the right
        # label today is worth nothing if nothing notices when the label
        # changes again -- and it has changed once already, fund-v1 to
        # fund-v2, which is why portfolio_api.py refuses to pin one.
        #
        # This check cannot pin dynamically: api.py has a LAB VIEW on an
        # experimental algo_version, and a monitor that always followed
        # the newest label would chase a lab run nobody ships. So it
        # watches what the SITE watches, and reports when some other
        # label in the same table is fresher than the one it was told
        # about. That is a configuration error, not a data error, and it
        # is reported as loudly as one.
        for key, table in (("stock_scores", "stock_score"),
                           ("fund_scores", "mf_score")):
            watched = SOURCE[key][3]
            mine = r[key]
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT algo_version, MAX(as_of_date) AS d
                    FROM {table} GROUP BY 1
                    ORDER BY 2 DESC NULLS LAST LIMIT 1
                """)
                top = cur.fetchone()
            if not top or top["algo_version"] == watched:
                continue
            if mine is None or (top["d"] and top["d"] > mine):
                problems.append(
                    "%s: this check watches '%s' (%s) but '%s' is fresher (%s) "
                    "-- the engine moved and the monitor did not. Update "
                    "STOCK_ALGO/FUND_ALGO here to match api.py"
                    % (table, watched, mine or "no rows",
                       top["algo_version"], top["d"]))

        # ---- 4. THE CHAIN --------------------------------------------
        #
        # technicals -> stock scores -> fund scores. A stage behind the
        # one feeding it means that stage stopped running, or ran and
        # found nothing new to do.
        if r["technicals"] and r["stock_scores"] and \
                r["stock_scores"] < r["technicals"]:
            problems.append(
                "stock scores (%s) are BEHIND technicals (%s) -- score_stocks.py "
                "has not been run since the last fetch"
                % (r["stock_scores"], r["technicals"]))

        if r["stock_scores"] and r["fund_scores"] and \
                r["fund_scores"] < r["stock_scores"]:
            problems.append(
                "fund scores (%s) are BEHIND stock scores (%s) -- score_funds.py "
                "is scoring against older inputs than it could"
                % (r["fund_scores"], r["stock_scores"]))

        # ---- 5. ABSOLUTE STALENESS -----------------------------------
        #
        # The chain can be perfectly consistent and still be a month
        # old -- every stage agreeing on the wrong date. That is the
        # case the chain tests above are structurally blind to, so each
        # stage is also measured against today on its own.
        #
        # Every stage, including returns. Leaving returns out of this
        # loop is how 12,415 wrong rows sat unexamined in a table the
        # report reads on every page load.
        for key in ORDER:
            d = r[key]
            if d is None or d > today:
                continue            # empty and future are already reported
            age = business_days_between(d, today)
            if age > MAX_AGE[key]:
                table, _, fed_by, _ = SOURCE[key]
                problems.append(
                    "%s is %d business day%s old (limit %d) -- %s is not landing"
                    % (table, age, "s" if age != 1 else "",
                       MAX_AGE[key], fed_by))

        if not problems:
            print("Freshness OK -- every stage is current and in step.")
            return 0

        print("FRESHNESS PROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  !! %s" % p)
        print("")
        print("The site is showing these numbers to paying users. A stage that")
        print("reports success while its inputs are frozen -- or while its")
        print("dates are impossible -- is the failure mode this check exists")
        print("to catch.")
        return len(problems)


if __name__ == "__main__":
    sys.exit(main())
