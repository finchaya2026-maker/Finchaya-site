"""
load_index_constituents.py -- load NSE index member lists into Postgres.
---------------------------------------------------------------------------
Runs on your laptop (it reads files you downloaded) against the same database
the site uses, like load_benchmarks.py:

    python load_index_constituents.py ind_nifty50list.csv --dry-run
    python load_index_constituents.py ind_nifty50list.csv ind_nifty100list.csv ^
        ind_niftymidcap150list.csv ind_niftysmallcap250list.csv --admin doadmin

WHAT IT IS FOR
    The back-test page offers ready-made groups of stocks. The cap segments come
    from the AMFI table already in the database. The NSE indices (Nifty 50,
    Nifty 100, Midcap 150, Smallcap 250 ...) need their member lists, and nothing
    in the database held them: NSE's factsheets publish only the top ten.
    NSE does publish every index's full list as a small CSV on niftyindices.com
    (open the index's page and use its "Index Constituents" CSV link). The
    columns are Company Name, Industry, Symbol, Series, ISIN Code.

    The page offers an index only once its list is loaded here, and the lists
    change a few times a year, so load a fresh file after each reconstitution
    (March and September). Loading an index REPLACES its old list.

TODAY'S MEMBERS, NOT HISTORY
    This is who is in the index now. The back-test applies it to the whole ten
    years and says so on the page: it leaves out whoever was in the index earlier
    and has since dropped out, which flatters the result.

THE TABLE
    index_constituent (index_name, isin, symbol, company_name, industry,
                       as_of_date, loaded_at), primary key (index_name, isin).
    Created if it is missing. The website's login can usually only READ and not
    create tables, so the first run needs --admin (it asks for the admin password
    at the prompt; it is never written down) and grants the site's login access.

SAFE TO RE-RUN. Each index is replaced inside one transaction, so a failure
leaves the old list in place. --dry-run reads and checks the files and touches
no database at all.

NAMES. The index name is taken from the file name (ind_nifty50list.csv ->
"Nifty 50"). For any other file name pass --index "Name" with a single file.
"""

import argparse
import csv
import getpass
import os
import re
import sys
from datetime import date

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")

# NSE's file names, as they appear on niftyindices.com.
KNOWN = {
    "ind_nifty50list": "Nifty 50",
    "ind_niftynext50list": "Nifty Next 50",
    "ind_nifty100list": "Nifty 100",
    "ind_nifty200list": "Nifty 200",
    "ind_nifty500list": "Nifty 500",
    "ind_niftymidcap100list": "Nifty Midcap 100",
    "ind_niftymidcap150list": "Nifty Midcap 150",
    "ind_niftysmallcap100list": "Nifty Smallcap 100",
    "ind_niftysmallcap250list": "Nifty Smallcap 250",
    "ind_niftymidsmallcap400list": "Nifty MidSmallcap 400",
}

DDL = """
CREATE TABLE IF NOT EXISTS index_constituent (
    index_name   text NOT NULL,
    isin         text NOT NULL,
    symbol       text,
    company_name text,
    industry     text,
    as_of_date   date NOT NULL,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (index_name, isin)
);
CREATE INDEX IF NOT EXISTS ix_index_constituent_isin ON index_constituent (isin);
"""


def index_name_for(path, override=None):
    """'.../ind_nifty50list.csv' -> 'Nifty 50'. None when the file name is not one NSE uses."""
    if override:
        return override.strip()
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    stem = re.sub(r"\s*\(\d+\)$", "", stem)            # a browser's "ind_nifty50list (1).csv"
    return KNOWN.get(stem)


def read_members(path):
    """[(isin, symbol, company_name, industry)] from one NSE constituent CSV.

    Columns are found BY HEADER NAME, not position: NSE has reordered them before,
    and a positional read would then load the wrong column without complaint.
    Returns (members, problems).
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f)
        header = None
        for row in rd:                                   # tolerate blank lines above the header
            if any(c.strip() for c in row):
                header = [c.strip().lower() for c in row]
                break
        if header is None:
            raise SystemExit("%s is empty." % path)
        need = {"isin": ("isin code", "isin"), "symbol": ("symbol",),
                "name": ("company name", "company"), "industry": ("industry",)}
        col = {}
        for key, names in need.items():
            for n in names:
                if n in header:
                    col[key] = header.index(n)
                    break
        if "isin" not in col:
            raise SystemExit("%s has no ISIN column (looked for 'ISIN Code'). Its columns are: %s"
                             % (path, ", ".join(header)))
        members, problems, seen = [], [], set()
        for row in rd:
            if not any(c.strip() for c in row):
                continue
            get = lambda k: row[col[k]].strip() if k in col and col[k] < len(row) else ""
            isin = get("isin").upper()
            if not ISIN_RE.match(isin):
                problems.append("row %r has no usable ISIN (%r)" % (get("symbol") or row[:2], isin))
                continue
            if isin in seen:
                problems.append("%s appears twice; kept once" % isin)
                continue
            seen.add(isin)
            members.append((isin, get("symbol"), get("name"), get("industry")))
    return members, problems


# ----------------------------------------------------------------- database
def site_dsn():
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("/opt/mfapi/.env")
    dsn = os.environ.get("FINCHAYA_DB") or os.environ.get("DATABASE_URL") or os.environ.get("MF_DSN")
    if not dsn:
        sys.exit("No FINCHAYA_DB found. Run this from C:\\finchaya, where .env is.")
    return dsn


def admin_dsn(user):
    """The site's connection string with a different login on it; the password is asked for
    at the prompt, so it is never in a file, in shell history or on the command line."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    d = conninfo_to_dict(site_dsn())
    print("\nConnecting to %s as '%s'." % (d.get("host", "(unknown host)"), user))
    print("The password is in your database provider's control panel. It is not shown as you type it.")
    pw = getpass.getpass("%s password: " % user)
    if not pw:
        sys.exit("No password given. Nothing was changed.")
    d["user"], d["password"] = user, pw
    return make_conninfo(**d)


def app_user():
    from psycopg.conninfo import conninfo_to_dict
    return conninfo_to_dict(site_dsn()).get("user")


def load(dsn, jobs, as_of, make_table):
    """jobs: [(index_name, members)]. Each index is replaced in its own transaction."""
    import psycopg
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            if make_table:
                cur.execute(DDL)
                me = app_user()
                cur.execute("SELECT current_user")
                if me and cur.fetchone()[0] != me:
                    cur.execute('GRANT SELECT, INSERT, DELETE ON index_constituent TO "%s"' % me.replace('"', ""))
                    print("  table ready; access granted to %s" % me)
                conn.commit()
        for name, members in jobs:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM index_constituent WHERE index_name = %s", (name,))
                cur.executemany(
                    "INSERT INTO index_constituent (index_name, isin, symbol, company_name, industry, as_of_date) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [(name, i, s, c, ind, as_of) for (i, s, c, ind) in members])
            conn.commit()
            print("  %-24s %4d members loaded" % (name, len(members)))
        # What the back-test will actually be able to use: members we hold prices for.
        with conn.cursor() as cur:
            for name, members in jobs:
                cur.execute("""
                    SELECT count(*) FILTER (WHERE m.isin IS NOT NULL),
                           count(*) FILTER (WHERE EXISTS (SELECT 1 FROM stock_ohlc_daily o WHERE o.isin = i.isin))
                    FROM index_constituent i LEFT JOIN stock_master m ON m.isin = i.isin
                    WHERE i.index_name = %s""", (name,))
                known, priced = cur.fetchone()
                print("  %-24s %4d are in stock_master, %d have price history" % (name, known, priced))
                if priced < len(members):
                    cur.execute("""
                        SELECT i.symbol FROM index_constituent i
                        WHERE i.index_name = %s
                          AND NOT EXISTS (SELECT 1 FROM stock_ohlc_daily o WHERE o.isin = i.isin)
                        ORDER BY i.symbol LIMIT 12""", (name,))
                    print("     without prices (left out of the test): " + ", ".join(r[0] or "?" for r in cur.fetchall()))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Load NSE index constituent CSVs for the back-test's stock groups.")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--index", help='index name, when the file name is not one of NSE\'s (single file only)')
    ap.add_argument("--as-of", help="date the list was published (YYYY-MM-DD); default today")
    ap.add_argument("--dry-run", action="store_true", help="read and check the files, touch no database")
    ap.add_argument("--admin", nargs="?", const="doadmin", metavar="LOGIN",
                    help="connect as this admin login (asks for the password) and create the table if missing")
    a = ap.parse_args(argv)
    if a.index and len(a.files) != 1:
        sys.exit("--index names one index, so give it exactly one file.")
    as_of = a.as_of or date.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", as_of):
        sys.exit("--as-of must look like 2026-10-05.")

    jobs = []
    for path in a.files:
        name = index_name_for(path, a.index)
        if not name:
            sys.exit("I do not recognise the file name %r. Pass --index \"Nifty 50\" (or whichever index it is) "
                     "with that one file." % os.path.basename(path))
        members, problems = read_members(path)
        print("%-24s %4d members read from %s" % (name, len(members), os.path.basename(path)))
        for p in problems[:5]:
            print("     note: " + p)
        if not members:
            sys.exit("%s gave no usable members; nothing loaded." % path)
        jobs.append((name, members))

    if a.dry_run:
        print("\nDry run: nothing was written.")
        return jobs
    dsn = admin_dsn(a.admin) if a.admin else site_dsn()
    print("\nLoading as of %s:" % as_of)
    load(dsn, jobs, as_of, make_table=bool(a.admin))
    print("\nDone. The back-test page offers these groups within ten minutes (it remembers the list that long).")
    return jobs


if __name__ == "__main__":
    main()
