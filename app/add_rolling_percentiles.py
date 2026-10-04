"""
add_rolling_percentiles.py -- give mf_rolling a shape, not just ends
----------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/add_rolling_percentiles.py
    --admin doadmin        when the site login does not own the table

Adds four nullable columns to mf_rolling and the same four to
mf_rolling_category. Nothing is backfilled and no existing value is
touched: every row keeps the average, worst, best and hurdle figures it
already had, with NULL in the new columns until score_rolling.py is
re-run.

WHY FOUR NUMBERS AND NOT THE WINDOWS THEMSELVES
    The page wants to draw the distribution, and right now it cannot,
    because what is stored is the two ends and the middle. Worst -6%,
    average 22%, best 61% is consistent with a fund that lands near 22%
    almost every time and one that swings wildly between -6 and 61 and
    averages out. Those are different funds and the current table cannot
    tell them apart.

    The honest fix would be to keep every window. A ten-year history
    gives about 85 three-year windows per fund; across the universe that
    is millions of rows to store and hundreds of numbers to ship to a
    browser to draw a shape that four numbers already describe.

        p10, p25, p75, p90

    With the worst and best already there, that is a box plot: where the
    middle half landed, where nine in ten landed, and how far the tails
    reach beyond them. It answers "what usually happened", which is the
    question a client is really asking when they ask what a fund returns.

WHY NOT A HISTOGRAM COLUMN
    A jsonb of bucket counts would draw a prettier curve. It would also
    need a bucket width chosen once and applied to every fund -- a width
    that suits a liquid fund's 6-to-8 spread makes a small-cap's -20-to-
    70 spread three bars, and a width that suits the small cap makes the
    liquid fund one. Percentiles are scale-free and need no such choice.

ON THE CATEGORY TABLE
    The same four columns go on mf_rolling_category, and they are the
    MEDIAN FUND'S percentile, not the percentile of all funds pooled
    together. "The middle fund's bad quarter" is a fund that exists;
    the pooled tenth percentile is an artefact of how many funds the
    category happens to contain. score_rolling.py's rollup already takes
    a median of every column for exactly this reason.
"""

import argparse
import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

# Both tables get the same four, so one list drives both.
COLUMNS = ["p10_cagr", "p25_cagr", "p75_cagr", "p90_cagr"]
TABLES = ["mf_rolling", "mf_rolling_category"]


def site_dsn():
    d = (os.environ.get("FINCHAYA_DB")
         or os.environ.get("DATABASE_URL")
         or os.environ.get("MF_DSN"))
    if not d:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")
    return d


def admin_dsn(user):
    """The same server and database, as a different role.

    conninfo_to_dict/make_conninfo rather than string surgery: FINCHAYA_DB
    is in keyword form and urlparse on it silently produces nonsense.
    """
    d = conninfo_to_dict(site_dsn())
    print("\nConnecting to %s as '%s'." % (d.get("host", "(unknown host)"), user))
    print("The password is in your database provider's control panel,")
    print("not on this machine. It is not shown as you type it.")
    pw = getpass.getpass("%s password: " % user)
    if not pw:
        sys.exit("No password given. Nothing was changed.")
    d["user"] = user
    d["password"] = pw
    return make_conninfo(**d)


def report(cur):
    cur.execute("SELECT current_database(), current_user")
    db, who = cur.fetchone()
    print("connected to '%s' as '%s'" % (db, who))
    missing = []
    for t in TABLES:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s AND column_name = ANY(%s)
            ORDER BY column_name
        """, (t, COLUMNS))
        have = [r[0] for r in cur.fetchall()]
        gone = [c for c in COLUMNS if c not in have]
        print("  %-22s present %d of %d%s"
              % (t, len(have), len(COLUMNS),
                 "" if not gone else "   missing: " + ", ".join(gone)))
        missing += [(t, c) for c in gone]
    return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin", help="role that owns the tables (e.g. doadmin)")
    args = ap.parse_args()

    dsn = admin_dsn(args.admin) if args.admin else site_dsn()

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for t in TABLES:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + t,))
            if not cur.fetchone()[0]:
                sys.exit("%s does not exist. Run score_rolling.py once first "
                         "-- it creates both tables." % t)

        report(cur)
        print()

        # ALTER TABLE needs OWNERSHIP, which a GRANT does not confer -- so
        # the site login can write every row of these tables and still not
        # be allowed to add a column. Caught and explained rather than
        # left as a traceback.
        try:
            for t in TABLES:
                for c in COLUMNS:
                    cur.execute("ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s numeric"
                                % (t, c))
                print("  %s ready" % t)
            conn.commit()
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            cur.execute("SELECT current_user")
            who = cur.fetchone()[0]
            cur.execute("SELECT tableowner FROM pg_tables WHERE tablename = %s",
                        ("mf_rolling",))
            owner = (cur.fetchone() or ["the owner"])[0]
            print()
            print("'%s' can write to these tables but does not own them, and"
                  % who)
            print("only the owner may add a column. The owner is '%s'." % owner)
            print()
            print("Re-run as the owner:")
            print("  /opt/mfapi/venv/bin/python3 "
                  "/opt/mfapi/add_rolling_percentiles.py --admin %s" % owner)
            print()
            print("It will ask for that password at the prompt. Nothing has")
            print("been changed.")
            sys.exit(1)

        # Only when running as somebody else -- granting yourself what you
        # already own is noise. Every table is granted explicitly because
        # there is no ALTER DEFAULT PRIVILEGES on this database.
        app = conninfo_to_dict(site_dsn()).get("user")
        cur.execute("SELECT current_user")
        if app and cur.fetchone()[0] != app:
            for t in TABLES:
                cur.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON %s TO "%s"'
                            % (t, app))
            conn.commit()
            print("\n  granted SELECT/INSERT/UPDATE/DELETE on %s to %s"
                  % (", ".join(TABLES), app))

    # VERIFY AS THE APPLICATION, not as the admin who just did the work.
    # An admin can always see its own columns; that proves nothing about
    # whether the site can read them.
    print("\n--- verifying as the application login ---")
    with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
        missing = report(cur)
        cur.execute("""
            SELECT count(*) AS rows, count(p25_cagr) AS filled
            FROM mf_rolling
        """)
        total, filled = cur.fetchone()
        print("  rolling rows on file : %d, of which %d carry percentiles"
              % (total, filled))

    if missing:
        sys.exit("\nStill missing: %s"
                 % ", ".join("%s.%s" % m for m in missing))

    print("""
Columns are in place and empty, which is correct -- nothing has been
computed yet. Fill them with:

    /opt/mfapi/venv/bin/python3 /opt/mfapi/score_rolling.py

That re-reads every NAV and rewrites every row, so it takes as long as it
normally does. Until it finishes, the fund page draws the range it always
could and leaves the middle of the distribution out, rather than drawing
a shape from numbers it does not have.
""")


main()
