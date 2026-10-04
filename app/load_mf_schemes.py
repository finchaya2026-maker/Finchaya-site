"""
load_mf_schemes.py  (v3 -- uses AMFI's real plan/option columns)
----------------------------------------------------------------
Downloads AMFI's daily NAV file and loads it into TWO tables:
    mf_scheme  -- the fund's identity
    mf_nav     -- one NAV per fund per day

WHY V3: AMFI's file now carries dedicated 'Plan' and 'Option' columns.
Older code had to guess these from the scheme name; that guessing is now
only a FALLBACK for files that lack the columns. Reading a real column
beats inferring from a name every time.

Safe to re-run. It will overwrite the bad rows v1 wrote.
"""

import re
import os
import sys
from datetime import datetime

import psycopg
import requests

# ---------------------------------------------------------------------
# DB = "host=localhost port=5432 dbname=finchaya user=postgres password=..."
from dotenv import load_dotenv
load_dotenv()
DB = os.getenv("FINCHAYA_DB")
AMFI_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
LOCAL_FALLBACK = r"C:\finchaya\NAVAll.txt"


def get_text():
    try:
        print("Downloading from AMFI...")
        r = requests.get(AMFI_URL, timeout=60,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        print("Download OK.")
        return r.text
    except Exception as e:
        print(f"Download failed: {e}")
        try:
            with open(LOCAL_FALLBACK, "r", encoding="utf-8") as f:
                print("Using local fallback file.")
                return f.read()
        except FileNotFoundError:
            print(f"\nFIX: open {AMFI_URL} in Chrome,")
            print(f"     save as {LOCAL_FALLBACK}, run again.")
            sys.exit(1)


# ---------------------------------------------------------------------
# COLUMN DETECTION -- the heart of v2
#
# We look at the header line and work out which position holds what,
# matching on distinctive words rather than on position.
# ---------------------------------------------------------------------
def build_column_map(header_line):
    names = [p.strip().lower() for p in header_line.split(";")]
    mapping = {}

    for i, name in enumerate(names):
        if "scheme code" in name:
            mapping["code"] = i
        elif "scheme name" in name:
            mapping["name"] = i
        elif "isin" in name and ("payout" in name or "growth" in name):
            mapping["isin"] = i
        elif "net asset value" in name or name == "nav":
            mapping["nav"] = i
        elif name == "plan":
            mapping["plan"] = i
        elif name == "option":
            mapping["option"] = i
        elif name == "date" or name.endswith(" date"):
            mapping["date"] = i

    return mapping, names


# ---------------------------------------------------------------------
def normalise_plan(value):
    """Map whatever AMFI writes into our two allowed values."""
    lowered = (value or "").lower()
    if "direct" in lowered:
        return "DIRECT"
    if "regular" in lowered or "existing" in lowered:
        return "REGULAR"
    return None


def normalise_option(value):
    lowered = (value or "").lower()
    # Check IDCW spellings first -- some text contains both words.
    for token in ("idcw", "dividend", "payout", "reinvest"):
        if token in lowered:
            return "IDCW"
    if "growth" in lowered or "cumulative" in lowered:
        return "GROWTH"
    return None


CATEGORY_RE = re.compile(r"^(Open|Close|Interval)\s*Ended\s*Schemes?\s*\((.+)\)\s*$",
                         re.IGNORECASE)


def split_category(inside):
    inside = inside.strip()
    if " - " in inside:
        head, tail = inside.split(" - ", 1)
        return head.strip(), tail.strip()
    return inside, None


def parse_date(value):
    value = (value or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_nav(value):
    value = (value or "").strip()
    if not value or value.upper() in ("N.A.", "NA", "-", "NULL"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------
def parse(text):
    schemes, navs = {}, {}
    current_category = current_sub = current_amc = None
    cols = None

    bad_date_samples = []
    bad_nav_samples = []
    raw_plan_values = set()
    raw_option_values = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if ";" not in line:
            match = CATEGORY_RE.match(line)
            if match:
                current_category, current_sub = split_category(match.group(2))
            else:
                current_amc = line
            continue

        parts = [p.strip() for p in line.split(";")]

        # --- header line: build the map, then move on ---
        if cols is None and not parts[0].isdigit():
            cols, header_names = build_column_map(line)
            print("\nHeader found. Columns in your file:")
            for i, n in enumerate(header_names):
                print(f"   [{i}] {n}")
            print(f"Detected mapping: {cols}\n")

            missing = {"code", "name", "nav", "date"} - set(cols)
            if missing:
                print(f"ERROR: could not locate columns: {missing}")
                print("Paste the header line above to Claude.")
                sys.exit(1)
            continue

        if not parts[0].isdigit() or cols is None:
            continue

        # Guard against short/ragged lines
        if len(parts) <= max(cols.values()):
            continue

        code = parts[cols["code"]]
        name = parts[cols["name"]]
        isin = parts[cols["isin"]] if "isin" in cols else None
        nav = parse_nav(parts[cols["nav"]])
        nav_date = parse_date(parts[cols["date"]])

        # Prefer AMFI's own columns; fall back to reading the name only
        # if this file doesn't have them.
        if "plan" in cols:
            raw_plan = parts[cols["plan"]]
            plan = normalise_plan(raw_plan)
            raw_plan_values.add(raw_plan)
        else:
            plan = normalise_plan(name)

        if "option" in cols:
            raw_option = parts[cols["option"]]
            option = normalise_option(raw_option)
            raw_option_values.add(raw_option)
        else:
            option = normalise_option(name)

        schemes[code] = (
            code,
            isin if isin not in ("", "-", None) else None,
            name,
            current_amc,
            current_category,
            current_sub,
            plan,
            option,
        )

        if nav is None and len(bad_nav_samples) < 3:
            bad_nav_samples.append(parts[cols["nav"]])
        if nav_date is None and len(bad_date_samples) < 3:
            bad_date_samples.append(parts[cols["date"]])

        if nav is not None and nav_date is not None:
            navs[(code, nav_date)] = (code, nav_date, nav)

    print(f"Parsed {len(schemes)} schemes, {len(navs)} NAV rows")
    if raw_plan_values:
        print(f"  raw PLAN values in file:   {sorted(raw_plan_values)}")
    if raw_option_values:
        print(f"  raw OPTION values in file: {sorted(raw_option_values)}")
    if bad_date_samples:
        print(f"  sample unparsed dates: {bad_date_samples}")
    if bad_nav_samples:
        print(f"  sample unparsed navs:  {bad_nav_samples}")
    return list(schemes.values()), list(navs.values())


# ---------------------------------------------------------------------
UPSERT_SCHEME = """
INSERT INTO mf_scheme
    (scheme_code, scheme_isin, scheme_name, amc_name,
     category, sub_category, plan_type, option_type, is_active, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, NOW())
ON CONFLICT (scheme_code) DO UPDATE SET
    scheme_isin  = EXCLUDED.scheme_isin,
    scheme_name  = EXCLUDED.scheme_name,
    amc_name     = EXCLUDED.amc_name,
    category     = EXCLUDED.category,
    sub_category = EXCLUDED.sub_category,
    plan_type    = EXCLUDED.plan_type,
    option_type  = EXCLUDED.option_type,
    is_active    = TRUE,
    updated_at   = NOW()
"""

UPSERT_NAV = """
INSERT INTO mf_nav (scheme_code, nav_date, nav)
VALUES (%s, %s, %s)
ON CONFLICT (scheme_code, nav_date) DO NOTHING
"""


def main():
    schemes, navs = parse(get_text())

    if not schemes:
        print("Nothing parsed -- stopping.")
        sys.exit(1)

    if not navs:
        print("\nWARNING: zero NAV rows parsed. Something is still wrong.")
        print("Stopping before touching the database. Send Claude the")
        print("header listing printed above.")
        sys.exit(1)

    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO batch_run_log (run_type, business_date, status)
                VALUES ('LOAD_MF_NAV', CURRENT_DATE, 'RUNNING')
                RETURNING run_id
            """)
            run_id = cur.fetchone()[0]
            conn.commit()

            try:
                cur.executemany(UPSERT_SCHEME, schemes)
                cur.executemany(UPSERT_NAV, navs)
                cur.execute("""
                    UPDATE batch_run_log
                       SET status='SUCCESS', finished_at=NOW(),
                           rows_read=%s, rows_written=%s
                     WHERE run_id=%s
                """, (len(schemes), len(schemes) + len(navs), run_id))
                conn.commit()
                print(f"Loaded OK. run_id = {run_id}")
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

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM mf_scheme")
            print(f"\nmf_scheme: {cur.fetchone()[0]} rows")
            cur.execute("SELECT COUNT(*) FROM mf_nav")
            print(f"mf_nav:    {cur.fetchone()[0]} rows")

            cur.execute("""
                SELECT COALESCE(plan_type,'(unknown)'),
                       COALESCE(option_type,'(unknown)'), COUNT(*)
                FROM mf_scheme GROUP BY 1,2 ORDER BY 3 DESC
            """)
            print("\nBreakdown by plan and option:")
            for plan, option, count in cur.fetchall():
                print(f"  {plan:<10} {option:<10} {count}")

            cur.execute("""
                SELECT scheme_code, LEFT(scheme_name, 55), nav, nav_date
                FROM mf_scheme s
                JOIN mf_nav n USING (scheme_code)
                WHERE s.category LIKE 'Equity%'
                ORDER BY scheme_code LIMIT 5
            """)
            print("\nSample equity funds with NAV:")
            for row in cur.fetchall():
                print(" ", row)


if __name__ == "__main__":
    main()
