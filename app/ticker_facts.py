"""
ticker_facts.py -- the numbers behind the homepage ticker, computed from the database (read-only).

    python ticker_facts.py

Prints the FACTS lines for index.html and the counts behind each, so every figure on the home page can be traced.
Re-run it after each month's data load (the ticker text says which month it covers) and paste the lines into FACTS.

WHAT IS COUNTED
    For every Direct Growth fund in a category that has a mapped benchmark and a full record, the fund's return over the window
    against its own benchmark's total-return index (fund_capture, built from month-end NAVs). "Trailed" means the fund returned
    less than the index over the whole window.
    The Regular plan of the same fund is measured the same way: the Direct plan's excess return less the NAV gap between the two
    plans over the same dates. Same fund, same portfolio, only the plan's cost differs.
"""
import os
import sys
from datetime import date

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
load_dotenv("/opt/mfapi/.env")
DB = os.environ["FINCHAYA_DB"]
CATS = {"Large Cap Fund": "large-cap", "Mid Cap Fund": "mid-cap", "Small Cap Fund": "small-cap", "Flexi Cap Fund": "flexi-cap"}


def month_back(d, months):
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    # month end of (y, m)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def main(window=5):
    conn = psycopg.connect(DB, row_factory=dict_row)
    conn.read_only = True
    cur = conn.cursor()
    cur.execute("""
        SELECT d.scheme_code dcode, c.category, k.window_years, k.last_month, k.excess_cagr,
               (SELECT r.scheme_code FROM mf_scheme r WHERE r.scheme_name = d.scheme_name AND r.amc_name IS NOT DISTINCT FROM d.amc_name
                   AND r.plan_type = 'REGULAR' AND r.option_type = 'GROWTH' ORDER BY r.scheme_code LIMIT 1) rcode
        FROM mf_scheme d JOIN fund_capture k ON k.scheme_code = d.scheme_code JOIN v_scheme_category c ON c.scheme_code = d.scheme_code
        WHERE d.plan_type = 'DIRECT' AND d.option_type = 'GROWTH' AND k.window_years = %s AND c.category = ANY(%s)""",
                (window, list(CATS)))
    rows = cur.fetchall()
    if not rows:
        sys.exit("no rows")
    last = max(r["last_month"] for r in rows)
    base = month_back(last, window * 12)

    def nav_on(code, d):
        cur.execute("SELECT nav FROM mf_nav WHERE scheme_code = %s AND nav_date <= %s ORDER BY nav_date DESC LIMIT 1", (code, d))
        r = cur.fetchone()
        return float(r["nav"]) if r else None

    out = {}
    for r in rows:
        o = out.setdefault(r["category"], {"n": 0, "dir": 0, "reg_n": 0, "reg": 0})
        o["n"] += 1
        exc = float(r["excess_cagr"])
        if exc < 0:
            o["dir"] += 1
        if r["rcode"]:
            d0, d1, r0, r1 = nav_on(r["dcode"], base), nav_on(r["dcode"], last), nav_on(r["rcode"], base), nav_on(r["rcode"], last)
            if d0 and d1 and r0 and r1:
                gap = ((d1 / d0) ** (1.0 / window) - (r1 / r0) ** (1.0 / window)) * 100
                o["reg_n"] += 1
                if exc - gap < 0:
                    o["reg"] += 1
    print("Window: %d years to %s (base %s)" % (window, last, base))
    for cat, o in out.items():
        print("  %-16s Direct trailed %d of %d (%.0f%%)   Regular trailed %d of %d (%.0f%%)" % (
            cat, o["dir"], o["n"], 100.0 * o["dir"] / o["n"], o["reg"], o["reg_n"], 100.0 * o["reg"] / max(o["reg_n"], 1)))
    print()
    mon = last.strftime("%b %Y")
    for cat in ("Mid Cap Fund", "Large Cap Fund"):
        o = out.get(cat)
        if not o:
            continue
        label = CATS[cat]
        dp, rp = round(100.0 * o["dir"] / o["n"]), round(100.0 * o["reg"] / max(o["reg_n"], 1))
        print('  { n: "%d%%", t: "of %s funds trailed their benchmark over %d years as Regular plans (%d%% as Direct plans), to %s", src: "FinChaya analysis of AMFI NAVs and NSE total-return indices: %d of %d funds Regular, %d of %d Direct" },'
              % (rp, label, window, dp, mon, o["reg"], o["reg_n"], o["dir"], o["n"]))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
