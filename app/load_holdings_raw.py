"""
load_holdings_raw.py
--------------------
Runs parse_holdings.py over a folder of AMC portfolio files and writes
everything into mf_holding_raw -- the staging table.

Staging means: store what the file said, argue about it later. No
foreign keys, no scheme_code, no validation. Getting the data safely
out of 155 Excel files and into a table you can query is the whole job.

USAGE
    python load_holdings_raw.py portfolios 2026-07-31

    argument 1 = folder holding the AMC files
    argument 2 = the portfolio date (the "as on" date on the files)

Put ALL your AMC files in that one folder, including files unzipped
from an AMC's zip. Sub-folders are searched too.
"""

import glob
import os
import sys
from datetime import datetime

import psycopg

from parse_holdings import parse_file

# ---------------------------------------------------------------------
#DB = "host=localhost port=5432 dbname=finchaya user=postgres password=Finchaya@2026"
from dotenv import load_dotenv
load_dotenv()
DB = os.getenv("FINCHAYA_DB")

EXTENSIONS = ("*.xlsx", "*.xls", "*.XLSX", "*.XLS")


def collect_files(folder):
    paths = []
    for ext in EXTENSIONS:
        paths += glob.glob(os.path.join(folder, "**", ext), recursive=True)
    # Excel leaves ~$lockfiles behind when a file is open -- skip them
    paths = [p for p in paths if not os.path.basename(p).startswith("~$")]
    return sorted(set(paths))


INSERT = """
INSERT INTO mf_holding_raw
    (source_file, sheet_name, amc_fund_name, as_of_date,
     isin, instrument_name, industry, instrument_type,
     quantity, market_value, pct_of_nav, scale_applied)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source_file, sheet_name, as_of_date, instrument_name)
DO UPDATE SET
    -- amc_fund_name MUST be refreshed here. It is not part of the conflict
    -- key, so without this line a re-load updates every column except the
    -- name, freezing whatever the FIRST load produced. Samco staged as its
    -- sheet codes (SAMSCF, SAMMCAP, SAMTSF) under an earlier parser; the
    -- reload fixed the holdings and left the codes in place, so the promote
    -- matched nothing and reported zero funds with no error anywhere.
    -- Any parser improvement is invisible to already-staged rows without it.
    amc_fund_name   = EXCLUDED.amc_fund_name,
    isin            = EXCLUDED.isin,
    industry        = EXCLUDED.industry,
    instrument_type = EXCLUDED.instrument_type,
    quantity        = EXCLUDED.quantity,
    market_value    = EXCLUDED.market_value,
    pct_of_nav      = EXCLUDED.pct_of_nav,
    scale_applied   = EXCLUDED.scale_applied,
    loaded_at       = NOW()
"""


def main():
    if len(sys.argv) < 3:
        print("USAGE: python load_holdings_raw.py <folder> <YYYY-MM-DD>")
        sys.exit(1)

    folder = sys.argv[1]
    as_of = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()

    files = collect_files(folder)
    if not files:
        print(f"No Excel files found under {folder}")
        sys.exit(1)

    print(f"Found {len(files)} files. Parsing...")

    batch = []
    funds = 0
    failed_files = []
    # The parser has always returned a `problems` list per fund and this
    # loader has always thrown it away. A fund whose percentages summed to
    # nine million was reported as parsed, and the first thing to object
    # was a column width three thousand rows into the insert. Warnings are
    # worth nothing unless somebody reads them.
    flagged = []

    for i, path in enumerate(files, 1):
        try:
            for result in parse_file(path):
                funds += 1
                if result.get("problems"):
                    flagged.append((os.path.basename(path), result["sheet"],
                                    result["fund_name"], result["problems"]))
                for h in result["holdings"]:
                    batch.append((
                        result["source_file"],
                        result["sheet"],
                        result["fund_name"],
                        as_of,
                        h["isin"],
                        (h["name"] or h["isin"])[:300],
                        h["industry"],
                        h["type"],
                        h["quantity"],
                        h["value"],
                        h["pct"],
                        result["scale_applied"],
                    ))
        except Exception as e:
            failed_files.append((os.path.basename(path), str(e)[:120]))

        if i % 25 == 0:
            print(f"  {i}/{len(files)} files, {funds} funds, {len(batch):,} rows")

    print(f"\nParsed {funds} funds, {len(batch):,} holding rows "
          f"from {len(files) - len(failed_files)} files")

    if failed_files:
        print(f"\n{len(failed_files)} file(s) could not be parsed:")
        for name, err in failed_files[:10]:
            print(f"  {name}: {err}")

    if flagged:
        print(f"\n{len(flagged)} fund(s) parsed WITH WARNINGS "
              f"(loaded anyway -- read these):")
        for name, sheet, fund, probs in flagged[:20]:
            print(f"  {name} [{sheet}] {(fund or '')[:40]}")
            for pr in probs:
                print(f"      {pr}")
        if len(flagged) > 20:
            print(f"  ... and {len(flagged) - 20} more")

    if not batch:
        print("Nothing to load. Stopping.")
        sys.exit(1)

    # -----------------------------------------------------------------
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO batch_run_log (run_type, business_date, status)
                VALUES ('LOAD_HOLDINGS_RAW', %s, 'RUNNING')
                RETURNING run_id
            """, (as_of,))
            run_id = cur.fetchone()[0]
            conn.commit()

            try:
                # Chunked so a 27k-row insert doesn't build one huge statement
                CHUNK = 2000
                for start in range(0, len(batch), CHUNK):
                    cur.executemany(INSERT, batch[start:start + CHUNK])
                    conn.commit()
                    print(f"  written {min(start + CHUNK, len(batch)):,}/{len(batch):,}")

                cur.execute("""
                    UPDATE batch_run_log
                       SET status = %s, finished_at = NOW(),
                           rows_read = %s, rows_written = %s,
                           error_message = %s
                     WHERE run_id = %s
                """, ('PARTIAL' if failed_files else 'SUCCESS',
                      len(batch), len(batch),
                      "; ".join(filter(None, [
                          f"{len(failed_files)} files failed" if failed_files else None,
                          f"{len(flagged)} funds flagged" if flagged else None,
                      ])) or None,
                      run_id))
                conn.commit()
                print(f"\nLoaded OK. run_id = {run_id}")

            except Exception as e:
                conn.rollback()
                cur.execute("""
                    UPDATE batch_run_log
                       SET status='FAILED', finished_at=NOW(), error_message=%s
                     WHERE run_id=%s
                """, (str(e)[:2000], run_id))
                conn.commit()
                print(f"FAILED: {e}")
                sys.exit(1)

        # ---------------- report ----------------
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(DISTINCT amc_fund_name),
                       COUNT(DISTINCT isin) FILTER (WHERE instrument_type='EQUITY')
                FROM mf_holding_raw WHERE as_of_date = %s
            """, (as_of,))
            rows, distinct_funds, distinct_isins = cur.fetchone()
            print(f"\nmf_holding_raw for {as_of}:")
            print(f"  {rows:,} rows | {distinct_funds} funds | {distinct_isins:,} equity ISINs")

            # THE KEY NUMBER: how many holdings link to a stock you know?
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE s.isin IS NOT NULL),
                       COUNT(*)
                FROM mf_holding_raw r
                LEFT JOIN stock_master s ON s.isin = r.isin
                WHERE r.as_of_date = %s AND r.instrument_type = 'EQUITY'
            """, (as_of,))
            matched, total_eq = cur.fetchone()
            pct = 100.0 * matched / total_eq if total_eq else 0
            print(f"  equity holdings matching stock_master: "
                  f"{matched:,}/{total_eq:,} ({pct:.1f}%)")

            cur.execute("""
                SELECT instrument_type, COUNT(*)
                FROM mf_holding_raw WHERE as_of_date = %s
                GROUP BY 1 ORDER BY 2 DESC
            """, (as_of,))
            print("\nBy instrument type:")
            for kind, count in cur.fetchall():
                print(f"  {kind:<10} {count:>7,}")


if __name__ == "__main__":
    main()
