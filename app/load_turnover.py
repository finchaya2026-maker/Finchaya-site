"""
load_turnover.py -- the AMC's own portfolio turnover figure
----------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/load_turnover.py turnover.csv
    --dry-run      match every row and report, write nothing
    --template     write a blank CSV with the right headers and stop

WHY THIS IS LOADED AND NOT COMPUTED
    Portfolio turnover is a disclosed number. Every AMC publishes it
    monthly in the scheme factsheet, computed to a formula SEBI sets,
    from the actual purchases and sales in the scheme's books.

    We hold a monthly snapshot of holdings, so it is tempting to derive
    it: compare two months, add up what moved, divide. That estimate
    would be wrong in a specific and un-fixable way. It cannot see a
    stock bought and sold between two snapshots -- which is precisely
    the trading a turnover ratio exists to reveal -- and it cannot see
    the derivative and debt legs at all. It would systematically read
    LOW, most on exactly the funds a client should be warned about.

    A number on an advisory page has to survive the client checking it
    against the factsheet. An estimate that reads 40% beside a factsheet
    that says 112% does not cost us an argument about methodology; it
    costs us the client's belief in every other number on the page. So
    the column stays empty until a real figure is loaded, and the page
    says "not published here yet" rather than showing a guess.

WHAT THE CSV NEEDS
    scheme_code,scheme_name,as_of_date,turnover_pct,source

    scheme_code    preferred. Ours, as it appears in v_fund_canonical.
    scheme_name    used only when scheme_code is blank. Matched
                   case-insensitively against the canonical name; a name
                   that matches more than one fund is REFUSED, not
                   guessed at. A wrong fund's turnover is worse than no
                   turnover.
    as_of_date     the month the factsheet is for, any ISO date in it.
                   Stored as the first of that month, because that is
                   what it means -- a factsheet dated 30 June and one
                   dated 1 July for June are the same disclosure.
    turnover_pct   as published. 112.4 means 112.4%, not 1.124.
                   Accepts a trailing % sign and commas.
    source         free text, e.g. "HDFC factsheet Jun 2026". Optional
                   but strongly worth filling: in a year, "where did
                   this come from" is a question somebody will ask.

RE-RUNNING IS SAFE
    The key is (scheme_code, month). Loading the same file twice
    overwrites with identical values. Loading a corrected file
    overwrites with the correction.
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import date

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Is /opt/mfapi/.env present?")

HEADERS = ["scheme_code", "scheme_name", "as_of_date", "turnover_pct", "source"]

DDL = """
CREATE TABLE IF NOT EXISTS fund_turnover (
    scheme_code   text    NOT NULL,
    as_of_date    date    NOT NULL,   -- first of the month it describes
    turnover_pct  numeric NOT NULL,
    source        text,
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code, as_of_date)
);
CREATE INDEX IF NOT EXISTS ix_turnover_latest
    ON fund_turnover (scheme_code, as_of_date DESC);
"""

UPSERT = """
INSERT INTO fund_turnover (scheme_code, as_of_date, turnover_pct, source,
                           loaded_at)
VALUES (%(code)s, %(month)s, %(pct)s, %(src)s, now())
ON CONFLICT (scheme_code, as_of_date) DO UPDATE SET
    turnover_pct = EXCLUDED.turnover_pct,
    source = EXCLUDED.source,
    loaded_at = now()
"""

CANONICAL = """
SELECT canonical_scheme_code AS code, scheme_name FROM v_fund_canonical
"""


def norm(s):
    """Fold a fund name to something two spellings of it agree on.

    Factsheets write "HDFC Mid-Cap Opportunities Fund - Direct Plan -
    Growth" and our canonical name may carry different punctuation or
    plan wording. Case, punctuation and runs of spaces are dropped; the
    words are not, so two genuinely different funds still differ.
    """
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def parse_pct(raw):
    v = (raw or "").strip().replace("%", "").replace(",", "")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_month(raw):
    """Any ISO-ish date, snapped to the first of its month."""
    v = (raw or "").strip()
    m = re.match(r"^(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?$", v)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", v)
    if m:                                   # dd/mm/yyyy, the Indian order
        return date(int(m.group(3)), int(m.group(2)), 1)
    return None


def template(path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADERS)
        w.writerow(["", "HDFC Mid-Cap Opportunities Fund", "2026-06-30",
                    "31.5", "HDFC factsheet Jun 2026"])
    print("Wrote %s with the headers and one example row." % path)
    print("Delete the example row before loading it.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="?", help="the file to load")
    ap.add_argument("--dry-run", action="store_true",
                    help="match every row and report, write nothing")
    ap.add_argument("--template", metavar="PATH",
                    help="write a blank CSV with the right headers and stop")
    args = ap.parse_args()

    if args.template:
        template(args.template)
        return
    if not args.csv:
        sys.exit("Give me a CSV, or --template PATH to get a blank one.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        if not args.dry_run:
            cur.execute(DDL)
            conn.commit()

        cur.execute(CANONICAL)
        by_code, by_name = set(), defaultdict(list)
        for r in cur.fetchall():
            by_code.add(str(r["code"]))
            by_name[norm(r["scheme_name"])].append(str(r["code"]))

        rows, problems = [], []
        with open(args.csv, newline="") as fh:
            for i, rec in enumerate(csv.DictReader(fh), 2):
                rec = {(k or "").strip().lower(): (v or "").strip()
                       for k, v in rec.items()}
                code = rec.get("scheme_code") or ""
                name = rec.get("scheme_name") or ""
                month = parse_month(rec.get("as_of_date"))
                pct = parse_pct(rec.get("turnover_pct"))

                if code and code not in by_code:
                    problems.append((i, "scheme_code %r is not one of ours"
                                     % code))
                    continue
                if not code:
                    hits = by_name.get(norm(name)) or []
                    if not hits:
                        problems.append((i, "no fund named %r" % name))
                        continue
                    # REFUSED, NOT GUESSED. Two funds sharing a folded
                    # name means the name is not enough to identify one,
                    # and attaching a turnover to the wrong fund is a
                    # wrong number nobody would ever catch.
                    if len(hits) > 1:
                        problems.append(
                            (i, "%r matches %d funds -- put the scheme_code in"
                             % (name, len(hits))))
                        continue
                    code = hits[0]
                if month is None:
                    problems.append((i, "cannot read the date %r"
                                     % rec.get("as_of_date")))
                    continue
                if pct is None:
                    problems.append((i, "cannot read the turnover %r"
                                     % rec.get("turnover_pct")))
                    continue
                # A published turnover of 4000% is a decimal that slipped.
                # Flagged, not silently loaded.
                if pct < 0 or pct > 1000:
                    problems.append((i, "turnover %.1f%% is outside anything "
                                     "a factsheet prints" % pct))
                    continue
                rows.append({"code": code, "month": month, "pct": pct,
                             "src": rec.get("source") or None})

        print("%d row(s) read, %d matched, %d rejected."
              % (len(rows) + len(problems), len(rows), len(problems)))
        for line, why in problems[:25]:
            print("  line %d: %s" % (line, why))
        if len(problems) > 25:
            print("  ... and %d more" % (len(problems) - 25))

        if args.dry_run:
            print("\n--dry-run: nothing was written.")
            return
        if not rows:
            sys.exit("\nNothing to load.")

        for r in rows:
            cur.execute(UPSERT, r)
        conn.commit()

        cur.execute("""
            SELECT count(*) AS n, count(DISTINCT scheme_code) AS funds,
                   MAX(as_of_date) AS latest
            FROM fund_turnover
        """)
        s = cur.fetchone()
        print("\nfund_turnover now holds %d figure(s) for %d fund(s), "
              "latest month %s." % (s["n"], s["funds"], s["latest"]))

        cur.execute("SELECT count(*) AS n FROM v_fund_canonical")
        total = cur.fetchone()["n"]
        print("That is %d of %d funds. The rest show nothing on the page "
              "rather than a guess." % (s["funds"], total))


main()
