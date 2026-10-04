"""
build_active_share.py -- how much of a fund is NOT its index
----------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/build_active_share.py
    --dry-run       compute and report, write nothing
    --explain NAME  one fund, printed in full, nothing written
    --weights-only  rebuild the index weights and stop

WHAT THIS ANSWERS THAT A RETURN CANNOT
    A fund holding HDFC Bank at 8% when its index holds it at 8% has
    made no decision about HDFC Bank. Do that across the whole
    portfolio and you have an index fund charging an active fee, which
    no return, ranking or rating on a factsheet will tell you.

        overlap        of every 100 rupees, how many are invested
                       exactly as the index is
        active share   the other side of it -- how much of the
                       portfolio is a decision rather than a default

    And then the part that actually names names:

        biggest bets   per stock, the fund's weight less the index's.
                       A manager who has avoided the index's weakest
                       constituents shows up here as an underweight in
                       those names, and one who is quietly hugging the
                       index shows up with nothing much either way.

WHERE THE INDEX WEIGHTS COME FROM, AND WHY THEY ARE A PROXY
    NSE does not publish full constituent weights. Its public
    factsheets give the top ten of a 150- or 500-stock index. Full
    weights are a paid feed.

    But an index fund IS the index. A passive fund tracking the Nifty
    Midcap 150 discloses its complete portfolio to AMFI every month,
    with weights, and we already ingest exactly that. So the tracker's
    holdings stand in for the index's constituents.

    THIS IS A PROXY AND IS LABELLED ONE EVERYWHERE IT APPEARS. A
    tracker holds a little cash, rebalances a day or two after the
    index does, and can still hold a name the index dropped last week.
    These figures are right to a fraction of a percent, not exact, and
    the tracker used is stored on every row so no number has a mystery
    behind it.

    index_match.py decides which tracker stands in for which index, on
    provider, market slice, index size and actual holding count. It
    carries its own self-test over the pairings that went wrong the
    first time -- a NASDAQ 100 ETF standing in for the Nifty 100, the
    US S&P 500 for the Nifty 500.

BOTH SIDES ARE NORMALISED TO 100, AND THAT IS A CHOICE
    A fund holding 4% cash and a tracker holding 0.1% would otherwise
    show 4% of "active share" that is not a stock decision at all.
    Scaling both to 100 makes this a measure of STOCK SELECTION.

    The cost is that a fund sitting on 30% cash looks like any other,
    so equity_pct is stored beside it and the page shows it. Hiding a
    cash call inside a selection figure would be the wrong kind of
    tidy.
"""

import argparse
import os
import sys
from collections import defaultdict

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_match import why_not                              # noqa: E402

DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Is /opt/mfapi/.env present?")

# A tracker below this is not tracking a broad index whatever it says.
MIN_TRACKER_NAMES = 20

# How many over- and underweights to keep per fund. Ten each is what a
# reader will actually look at; storing all 500 differences would be a
# table nobody queries.
TOP_BETS = 10

DDL = """
CREATE TABLE IF NOT EXISTS index_weight (
    benchmark_id  int  NOT NULL,
    isin          text NOT NULL,
    pct           numeric NOT NULL,     -- normalised to sum 100
    as_of_date    date NOT NULL,        -- the tracker's portfolio date
    source_scheme text NOT NULL,        -- which tracker this came from
    source_name   text NOT NULL,
    built_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (benchmark_id, isin)
);

CREATE TABLE IF NOT EXISTS fund_active_share (
    scheme_code   text NOT NULL,
    benchmark_id  int  NOT NULL,
    as_of_date    date NOT NULL,        -- the FUND's portfolio date
    index_as_of   date NOT NULL,
    overlap_pct   numeric NOT NULL,
    active_share  numeric NOT NULL,
    fund_names    int  NOT NULL,        -- equity holdings in the fund
    index_names   int  NOT NULL,
    shared_names  int  NOT NULL,
    -- Held by the fund and not in the index at all. The purest form of
    -- "this manager went looking somewhere the index does not".
    off_index_pct numeric,
    off_index_names int,
    equity_pct    numeric,              -- so a cash call is not hidden
    source_scheme text NOT NULL,
    built_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code)
);
CREATE INDEX IF NOT EXISTS ix_active_share
    ON fund_active_share (benchmark_id, active_share DESC);

CREATE TABLE IF NOT EXISTS fund_index_bet (
    scheme_code   text NOT NULL,
    isin          text NOT NULL,
    fund_pct      numeric NOT NULL,
    index_pct     numeric NOT NULL,
    diff          numeric NOT NULL,     -- fund less index
    built_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code, isin)
);
"""

BENCHMARKS = """
SELECT b.benchmark_id, b.display_name,
       (SELECT count(*) FROM mf_benchmark_map m
         WHERE m.benchmark_id = b.benchmark_id) AS mapped
FROM benchmark_master b ORDER BY b.benchmark_id
"""

# Passive funds with a portfolio, and how many EQUITY names are in it.
# The join to stock_master is the house way of dropping cash, TREPS,
# debt and unmatched ISINs -- the same filter the top-holdings list
# uses, so "150 names" means the same thing in both places.
TRACKERS = """
WITH pass AS (
    SELECT c.canonical_scheme_code AS code, c.scheme_name
    FROM v_fund_canonical c
    LEFT JOIN v_scheme_category vc
           ON vc.scheme_code = c.canonical_scheme_code
    WHERE (vc.category ILIKE '%%index%%'
        OR vc.category ILIKE '%%etf%%'
        OR c.scheme_name ILIKE '%%index fund%%'
        OR c.scheme_name ILIKE '%% etf%%'
        OR c.scheme_name ILIKE '%%exchange traded%%')
),
latest AS (
    SELECT p.code, p.scheme_name, MAX(h.as_of_date) AS d
    FROM pass p JOIN mf_holding h ON h.scheme_code = p.code
    GROUP BY 1, 2
)
SELECT l.code, l.scheme_name, l.d AS as_of,
       count(*) AS names, SUM(h.pct_of_nav) AS total_pct
FROM latest l
JOIN mf_holding h ON h.scheme_code = l.code AND h.as_of_date = l.d
JOIN stock_master s ON s.isin = h.isin
WHERE h.pct_of_nav IS NOT NULL
GROUP BY 1, 2, 3
HAVING count(*) >= %(minn)s
ORDER BY count(*) DESC
"""

TRACKER_HOLDINGS = """
SELECT h.isin, SUM(h.pct_of_nav) AS pct
FROM mf_holding h
JOIN stock_master s ON s.isin = h.isin
WHERE h.scheme_code = %(code)s AND h.as_of_date = %(d)s
  AND h.pct_of_nav IS NOT NULL
GROUP BY h.isin
"""

# The actively managed funds to measure, with the index each is
# benchmarked against and its latest equity portfolio.
ACTIVE = """
WITH act AS (
    SELECT DISTINCT ON (c.canonical_scheme_code)
           c.canonical_scheme_code AS code, c.scheme_name, m.benchmark_id
    FROM v_fund_canonical c
    JOIN mf_benchmark_map m ON m.scheme_code = c.canonical_scheme_code
    LEFT JOIN v_scheme_category vc
           ON vc.scheme_code = c.canonical_scheme_code
    WHERE m.benchmark_id IS NOT NULL
      AND NOT (vc.category ILIKE '%%index%%'
            OR vc.category ILIKE '%%etf%%'
            OR c.scheme_name ILIKE '%%index fund%%'
            OR c.scheme_name ILIKE '%% etf%%'
            OR c.scheme_name ILIKE '%%exchange traded%%')
    ORDER BY c.canonical_scheme_code, m.benchmark_id
)
SELECT a.code, a.scheme_name, a.benchmark_id,
       MAX(h.as_of_date) AS as_of
FROM act a JOIN mf_holding h ON h.scheme_code = a.code
GROUP BY 1, 2, 3
"""

FUND_HOLDINGS = """
SELECT h.scheme_code, h.isin, SUM(h.pct_of_nav) AS pct
FROM mf_holding h
JOIN stock_master s ON s.isin = h.isin
WHERE h.scheme_code = ANY(%(codes)s) AND h.as_of_date = %(d)s
  AND h.pct_of_nav IS NOT NULL
GROUP BY 1, 2
"""

# Everything in the portfolio, equity or not, so the cash call can be
# reported rather than silently normalised away.
FUND_EQUITY_PCT = """
SELECT h.scheme_code,
       SUM(h.pct_of_nav) FILTER (WHERE s.isin IS NOT NULL) AS equity,
       SUM(h.pct_of_nav) AS total
FROM mf_holding h
LEFT JOIN stock_master s ON s.isin = h.isin
WHERE h.scheme_code = ANY(%(codes)s) AND h.as_of_date = %(d)s
GROUP BY 1
"""


def clean(rows, key="isin", val="pct"):
    """{isin: pct} from query rows, dropping what cannot be weighed.

    Some AMC filings disclose a holding without its percentage of NAV.
    SUM() over a group of those returns NULL, not 0, and the NULL
    reached float() and stopped the whole run. Filtered in SQL too;
    this is the second line of defence, because the next query written
    against this table will forget.
    """
    out = {}
    for r in rows:
        v = r[val]
        if v is None:
            continue
        v = float(v)
        if v > 0:
            out[r[key]] = out.get(r[key], 0.0) + v
    return out


def normalise(weights):
    """Scale a {isin: pct} to sum exactly 100.

    Both sides of the comparison get this. A tracker holding 99.7% in
    stocks and a fund holding 94% would otherwise differ by 5.7 points
    that are cash, not a stock decision.
    """
    total = sum(weights.values())
    if total <= 0:
        return {}
    k = 100.0 / total
    return {i: p * k for i, p in weights.items()}


def compare(fund, index):
    """Overlap, active share and every per-stock difference.

    ACTIVE SHARE IS DEFINED AS HALF THE SUM OF ABSOLUTE DIFFERENCES.
    With both sides summing to 100 that is identical to 100 minus the
    sum of minimums, and this computes it BOTH ways and asserts they
    agree -- because they only agree when the normalisation actually
    worked, which makes it a free check on the step most likely to be
    silently wrong.
    """
    keys = set(fund) | set(index)
    overlap = sum(min(fund.get(k, 0.0), index.get(k, 0.0)) for k in keys)
    half_abs = 0.5 * sum(abs(fund.get(k, 0.0) - index.get(k, 0.0)) for k in keys)
    active = 100.0 - overlap
    if abs(active - half_abs) > 0.05:
        raise AssertionError(
            "active share disagrees with itself: %.3f vs %.3f -- the two "
            "sides are not both normalised to 100" % (active, half_abs))

    off = {k: v for k, v in fund.items() if k not in index}
    bets = sorted(((k, fund.get(k, 0.0), index.get(k, 0.0),
                    fund.get(k, 0.0) - index.get(k, 0.0)) for k in keys),
                  key=lambda r: r[3])
    return {
        "overlap": round(overlap, 2),
        "active": round(active, 2),
        "shared": sum(1 for k in keys if k in fund and k in index),
        "off_pct": round(sum(off.values()), 2),
        "off_names": len(off),
        "bets": bets,
    }


def pick_trackers(cur, verbose=True):
    """One tracker per index, chosen and explained."""
    cur.execute(BENCHMARKS)
    indices = cur.fetchall()
    cur.execute(TRACKERS, {"minn": MIN_TRACKER_NAMES})
    trackers = cur.fetchall()

    chosen = {}
    for b in indices:
        name = b["display_name"] or ""
        ok = [t for t in trackers
              if why_not(name, t["scheme_name"], t["names"]) is None]
        if not ok:
            if verbose:
                print("   %-34s NO TRACKER -- skipped" % name[:34])
            continue
        # Fullest portfolio first, then the MOST RECENT, then the
        # closest to 100% invested: the cleanest stand-in for an index.
        #
        # The date sorts DESCENDING. Ascending picked the oldest
        # portfolio on file -- a July index measured against an August
        # fund -- and the only symptom was a tracker name nobody would
        # question. Negated ordinal rather than reverse=True, because
        # the other two keys sort the other way.
        ok.sort(key=lambda t: (-t["names"], -t["as_of"].toordinal(),
                               abs(100.0 - float(t["total_pct"] or 0))))
        chosen[b["benchmark_id"]] = ok[0]
        if verbose:
            print("   %-34s <- %-40s %3d names %5.1f%% %s"
                  % (name[:34], ok[0]["scheme_name"][:40], ok[0]["names"],
                     float(ok[0]["total_pct"] or 0), ok[0]["as_of"]))

    # THE INDEX AND THE FUND MUST DESCRIBE THE SAME MONTH.
    #
    # Trackers do not all file on the same date, so one index can be a
    # month behind the rest. Comparing a fund's August portfolio with a
    # July index charges the manager for a rebalance that had not
    # happened yet -- a small error, invisible, and pointing the same
    # way every time. Said out loud rather than silently absorbed.
    if chosen and verbose:
        newest = max(t["as_of"] for t in chosen.values())
        stale = [(b, t) for b, t in chosen.items()
                 if (newest - t["as_of"]).days > 40]
        for b, t in stale:
            print("   NOTE: index %s is %d days behind the newest portfolio "
                  "on file." % (b, (newest - t["as_of"]).days))
        if stale:
            print("   Funds measured against it are compared with a slightly")
            print("   older index. The date is stored on every row.")
    return indices, chosen


def build_weights(cur, chosen, dry):
    """The index side, one row per constituent."""
    rows = 0
    for bid, t in chosen.items():
        cur.execute(TRACKER_HOLDINGS, {"code": t["code"], "d": t["as_of"]})
        w = normalise(clean(cur.fetchall()))
        if not w:
            continue
        if not dry:
            # Replaced wholesale: a constituent dropped from the index
            # must disappear, not linger with a stale weight.
            cur.execute("DELETE FROM index_weight WHERE benchmark_id = %s",
                        (bid,))
            for isin, pct in w.items():
                cur.execute("""
                    INSERT INTO index_weight (benchmark_id, isin, pct,
                        as_of_date, source_scheme, source_name, built_at)
                    VALUES (%s, %s, %s, %s, %s, %s, now())
                """, (bid, isin, round(pct, 4), t["as_of"], t["code"],
                      t["scheme_name"]))
        rows += len(w)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--explain", help="one fund, printed in full")
    ap.add_argument("--weights-only", action="store_true")
    args = ap.parse_args()
    dry = args.dry_run or bool(args.explain)

    print("Active share from index-fund holdings.")
    print("The index side is a PROXY: a passive tracker's portfolio "
          "standing in for\nthe index's constituents. Right to a fraction "
          "of a percent, not exact.\n")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        if not dry:
            cur.execute(DDL)
            conn.commit()

        print("1. WHICH TRACKER STANDS IN FOR WHICH INDEX")
        _indices, chosen = pick_trackers(cur)
        if not chosen:
            sys.exit("\nNo index has a usable tracker. Nothing to build.")

        print("\n2. THE INDEX SIDE")
        n = build_weights(cur, chosen, dry)
        if not dry:
            conn.commit()
        print("   %d constituent weights across %d indices."
              % (n, len(chosen)))

        # Read them back the way the comparison will use them.
        index_w = defaultdict(dict)
        if dry:
            for bid, t in chosen.items():
                cur.execute(TRACKER_HOLDINGS,
                            {"code": t["code"], "d": t["as_of"]})
                index_w[bid] = normalise(clean(cur.fetchall()))
        else:
            cur.execute("SELECT benchmark_id, isin, pct FROM index_weight")
            for r in cur.fetchall():
                index_w[r["benchmark_id"]][r["isin"]] = float(r["pct"])

        if args.weights_only:
            print("\n--weights-only: stopping here.")
            return

        print("\n3. THE FUNDS")
        cur.execute(ACTIVE)
        funds = [f for f in cur.fetchall() if f["benchmark_id"] in index_w]
        if args.explain:
            funds = [f for f in funds
                     if args.explain.lower() in f["scheme_name"].lower()]
            if not funds:
                sys.exit("No actively managed fund whose name contains %r "
                         "has a benchmark we can measure." % args.explain)
            funds = funds[:1]
        print("   %d actively managed funds to measure." % len(funds))

        # Grouped by portfolio date so the holdings come back in a few
        # queries rather than one per fund.
        by_date = defaultdict(list)
        for f in funds:
            by_date[f["as_of"]].append(f)

        written = skipped = 0
        for d, group in by_date.items():
            codes = [g["code"] for g in group]
            cur.execute(FUND_HOLDINGS, {"codes": codes, "d": d})
            held = defaultdict(dict)
            for r in cur.fetchall():
                if r["pct"] is None:
                    continue
                held[str(r["scheme_code"])][r["isin"]] = float(r["pct"])
            cur.execute(FUND_EQUITY_PCT, {"codes": codes, "d": d})
            eq = {str(r["scheme_code"]):
                  (float(r["equity"] or 0), float(r["total"] or 0))
                  for r in cur.fetchall()}

            for g in group:
                raw = held.get(g["code"])
                if not raw or len(raw) < 5:
                    skipped += 1
                    continue
                idx = index_w[g["benchmark_id"]]
                res = compare(normalise(raw), idx)
                equity, total = eq.get(g["code"], (0.0, 0.0))
                equity_pct = round(100.0 * equity / total, 1) if total else None
                t = chosen[g["benchmark_id"]]

                if args.explain:
                    print("\n%s" % g["scheme_name"])
                    print("  measured against %s (via %s, %s)"
                          % (g["benchmark_id"], t["scheme_name"], t["as_of"]))
                    print("  portfolio %s, %d equity names, %s%% of NAV in shares"
                          % (d, len(raw), equity_pct))
                    print("  overlap with the index %.1f%%   "
                          "active share %.1f%%" % (res["overlap"], res["active"]))
                    print("  %d names shared, %d held that the index does not "
                          "(%.1f%% of the fund)"
                          % (res["shared"], res["off_names"], res["off_pct"]))
                    print("\n  BIGGEST UNDERWEIGHTS (index names it avoided)")
                    for isin, fp, ip, diff in res["bets"][:8]:
                        print("    %-14s fund %5.2f%%  index %5.2f%%  %+6.2f"
                              % (isin, fp, ip, diff))
                    print("\n  BIGGEST OVERWEIGHTS (its own bets)")
                    for isin, fp, ip, diff in list(reversed(res["bets"]))[:8]:
                        print("    %-14s fund %5.2f%%  index %5.2f%%  %+6.2f"
                              % (isin, fp, ip, diff))
                elif not dry:
                    cur.execute("""
                        INSERT INTO fund_active_share (scheme_code,
                            benchmark_id, as_of_date, index_as_of,
                            overlap_pct, active_share, fund_names,
                            index_names, shared_names, off_index_pct,
                            off_index_names, equity_pct, source_scheme,
                            built_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                        ON CONFLICT (scheme_code) DO UPDATE SET
                            benchmark_id = EXCLUDED.benchmark_id,
                            as_of_date = EXCLUDED.as_of_date,
                            index_as_of = EXCLUDED.index_as_of,
                            overlap_pct = EXCLUDED.overlap_pct,
                            active_share = EXCLUDED.active_share,
                            fund_names = EXCLUDED.fund_names,
                            index_names = EXCLUDED.index_names,
                            shared_names = EXCLUDED.shared_names,
                            off_index_pct = EXCLUDED.off_index_pct,
                            off_index_names = EXCLUDED.off_index_names,
                            equity_pct = EXCLUDED.equity_pct,
                            source_scheme = EXCLUDED.source_scheme,
                            built_at = now()
                    """, (g["code"], g["benchmark_id"], d, t["as_of"],
                          res["overlap"], res["active"], len(raw), len(idx),
                          res["shared"], res["off_pct"], res["off_names"],
                          equity_pct, t["code"]))
                    cur.execute("DELETE FROM fund_index_bet WHERE scheme_code=%s",
                                (g["code"],))
                    keep = res["bets"][:TOP_BETS] + res["bets"][-TOP_BETS:]
                    for isin, fp, ip, diff in keep:
                        if abs(diff) < 0.01:
                            continue
                        cur.execute("""
                            INSERT INTO fund_index_bet (scheme_code, isin,
                                fund_pct, index_pct, diff, built_at)
                            VALUES (%s,%s,%s,%s,%s,now())
                            ON CONFLICT (scheme_code, isin) DO UPDATE SET
                                fund_pct = EXCLUDED.fund_pct,
                                index_pct = EXCLUDED.index_pct,
                                diff = EXCLUDED.diff, built_at = now()
                        """, (g["code"], isin, round(fp, 4), round(ip, 4),
                              round(diff, 4)))
                written += 1
            if not dry:
                conn.commit()

        print("\n%d fund(s) %s, %d skipped for too few equity holdings."
              % (written, "computed" if dry else "written", skipped))
        if not dry:
            cur.execute("""
                SELECT ROUND(AVG(active_share), 1) AS avg,
                       ROUND(MIN(active_share), 1) AS min,
                       ROUND(MAX(active_share), 1) AS max
                FROM fund_active_share
            """)
            s = cur.fetchone()
            print("Active share across them: lowest %s%%, average %s%%, "
                  "highest %s%%." % (s["min"], s["avg"], s["max"]))
            print("A fund near the bottom of that range is one to look at.")
        if dry:
            print("Nothing was written.")


if __name__ == "__main__":
    main()
