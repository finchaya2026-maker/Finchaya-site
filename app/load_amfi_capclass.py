"""
load_amfi_capclass.py -- load AMFI's stock categorisation into Postgres.
------------------------------------------------------------------------
AMFI publishes "Average Market Capitalization of listed companies" twice a
year. It carries the SEBI categorisation: the top 100 companies by six-month
average market cap are Large Cap, the next 150 are Mid Cap, the rest Small
Cap. It is the same list the fund houses classify against, so numbers
derived from it reconcile with what an AMC publishes.

WHY A PERIOD-STAMPED TABLE AND NOT A COLUMN
    AMFI reclassifies every six months. A single column on stock_master
    would be overwritten each time, and a report on March holdings would
    silently be recomputed with September's classification -- a fund that
    genuinely held mid caps in March would be redescribed as holding large
    caps, with nothing on the page admitting the change. Keeping the period
    means a report can ask for the classification that was current when the
    holdings were disclosed.

    This is the same failure as the industry backfill's stale-label
    problem, caught before it was written this time rather than after.

WHY ISIN AND NOT NAME
    The file carries an ISIN column, and mf_holding joins stock_master on
    ISIN at 100%. Matching on company name -- "Prestige Estates Projects
    Limited" against "Prestige Estates Projects Ltd." -- would have needed
    a fuzzy match and a manual exception list. It does not.

SAFE TO RE-RUN. Upserts on (isin, as_of_period), so re-running the same
file changes nothing and loading a new file adds a period beside the old
one rather than replacing it.

USAGE
    /opt/mfapi/venv/bin/python3 /opt/mfapi/load_amfi_capclass.py \\
        /opt/mfapi/data/AverageMarketCapitalization30Jun2026.xlsx --dry-run

    Then the same without --dry-run.
    --period YYYY-MM overrides the period parsed from the filename.
"""

import os
import re
import sys

import psycopg
from dotenv import load_dotenv

try:
    import openpyxl
except ImportError:
    sys.exit("openpyxl is not installed in this environment. Run:\n"
             "  /opt/mfapi/venv/bin/pip install openpyxl")

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

VALID = {"Large Cap": "Large", "Mid Cap": "Mid", "Small Cap": "Small"}

DDL = """
CREATE TABLE IF NOT EXISTS stock_cap_class (
    isin         text NOT NULL,
    cap_class    text NOT NULL CHECK (cap_class IN ('Large','Mid','Small')),
    as_of_period date NOT NULL,          -- first of the month the average ends
    avg_mcap_cr  numeric,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (isin, as_of_period)
);
CREATE INDEX IF NOT EXISTS ix_cap_class_period
    ON stock_cap_class (as_of_period DESC, isin);
"""


def period_from_name(path):
    """'AverageMarketCapitalization30Jun2026.xlsx' -> date(2026, 6, 1)."""
    m = re.search(r"(\d{1,2})([A-Za-z]{3})(\d{4})", os.path.basename(path))
    if not m:
        return None
    mon = MONTHS.get(m.group(2).lower())
    return "%s-%02d-01" % (m.group(3), mon) if mon else None


def read_rows(path):
    """Yield (isin, cap_class, avg_mcap) from the FINAL sheet.

    Columns are read BY HEADER NAME, not by position. AMFI has changed
    column order between releases before; a positional read would then load
    silently wrong data rather than failing.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)

    header, idx = None, {}
    for row in rows:
        cells = [(str(c).strip().lower() if c is not None else "") for c in row]
        if any(c == "isin" for c in cells):
            header = cells
            break
    if not header:
        sys.exit("No header row containing 'ISIN' found. "
                 "Check this is the AMFI categorisation file.")

    def find(*needles):
        for i, c in enumerate(header):
            if all(n in c for n in needles):
                return i
        return None

    idx["isin"] = find("isin")
    idx["cat"] = find("categorization") or find("categorisation")
    idx["avg"] = find("average of all exchanges")
    idx["name"] = find("company name")

    missing = [k for k in ("isin", "cat") if idx[k] is None]
    if missing:
        sys.exit("Could not find column(s) %s. Header seen: %r" % (missing, header))

    for row in rows:
        isin = (str(row[idx["isin"]]).strip() if row[idx["isin"]] else "")
        cat = (str(row[idx["cat"]]).strip() if row[idx["cat"]] else "")
        if not isin or cat not in VALID:
            continue
        avg = None
        if idx["avg"] is not None and row[idx["avg"]] not in (None, "", "-"):
            try:
                avg = float(row[idx["avg"]])
            except (TypeError, ValueError):
                avg = None
        yield isin, VALID[cat], avg


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__.strip().split("USAGE")[-1])
    path = args[0]
    dry = "--dry-run" in sys.argv

    period = None
    if "--period" in sys.argv:
        period = sys.argv[sys.argv.index("--period") + 1] + "-01"
    period = period or period_from_name(path)
    if not period:
        sys.exit("Could not work out the period from the filename. "
                 "Pass --period YYYY-MM.")

    rows = list(read_rows(path))
    if not rows:
        sys.exit("No usable rows found in that file.")

    counts = {}
    for _, cls, _ in rows:
        counts[cls] = counts.get(cls, 0) + 1
    print("file    : %s" % os.path.basename(path))
    print("period  : %s" % period)
    print("rows    : %d  (%s)" % (len(rows), ", ".join(
        "%s %d" % (k, counts[k]) for k in ("Large", "Mid", "Small") if k in counts)))

    # The SEBI rule is fixed at 100 and 150. A different count means the
    # file is not what we think it is, so say so rather than load it quietly.
    if counts.get("Large") != 100 or counts.get("Mid") != 150:
        print("  WARNING: expected 100 Large and 150 Mid per the SEBI rule.")

    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        cur.execute(DDL)

        cur.execute("CREATE TEMP TABLE incoming ("
                    "isin text, cap_class text, avg_mcap_cr numeric)")
        with cur.copy("COPY incoming (isin, cap_class, avg_mcap_cr) "
                      "FROM STDIN") as cp:
            for isin, cls, avg in rows:
                cp.write_row((isin, cls, avg))

        # Coverage against what people actually hold -- the only number
        # that decides whether the report panel is worth showing.
        cur.execute("""
            SELECT count(DISTINCT h.isin),
                   count(DISTINCT h.isin) FILTER (WHERE i.isin IS NOT NULL)
            FROM mf_holding h
            LEFT JOIN incoming i ON i.isin = h.isin
        """)
        held, covered = cur.fetchone()
        print("held ISINs covered by this file: %d of %d (%.1f%%)"
              % (covered, held, 100.0 * covered / held if held else 0))

        cur.execute("""
            SELECT COALESCE(m.company_name, h.isin)
            FROM (SELECT DISTINCT isin FROM mf_holding WHERE isin IS NOT NULL) h
            LEFT JOIN incoming i ON i.isin = h.isin
            LEFT JOIN stock_master m ON m.isin = h.isin
            WHERE i.isin IS NULL LIMIT 10
        """)
        misses = [r[0] for r in cur.fetchall()]
        if misses:
            print("not in the AMFI list (sample): %s" % ", ".join(
                str(x)[:34] for x in misses))

        if dry:
            print("\n--dry-run: nothing written.")
            return

        cur.execute("""
            INSERT INTO stock_cap_class (isin, cap_class, as_of_period, avg_mcap_cr)
            SELECT isin, cap_class, %(p)s::date, avg_mcap_cr FROM incoming
            ON CONFLICT (isin, as_of_period) DO UPDATE
               SET cap_class   = EXCLUDED.cap_class,
                   avg_mcap_cr = EXCLUDED.avg_mcap_cr,
                   loaded_at   = now()
        """, {"p": period})
        print("\nrows written : %d" % cur.rowcount)
        conn.commit()

        cur.execute("""SELECT as_of_period, count(*) FROM stock_cap_class
                       GROUP BY 1 ORDER BY 1 DESC""")
        print("periods now in stock_cap_class:")
        for p, n in cur.fetchall():
            print("   %s  %d rows" % (p, n))


if __name__ == "__main__":
    main()
