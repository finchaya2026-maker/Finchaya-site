"""
add_ema_columns.py -- seven EMA columns on stock_technical.
------------------------------------------------------------
Run once. Safe to re-run: every column is ADD COLUMN IF NOT EXISTS, and
existing rows are left exactly as they are.

    /opt/mfapi/venv/bin/python3 /opt/mfapi/add_ema_columns.py
    --admin doadmin     connect as the owner to apply grants

WHAT IT ADDS
    ema_5, ema_10, ema_20, ema_26, ema_50, ema_100, ema_200

    numeric, nullable. NULL is a real answer here, not a gap to be filled
    later: a 200-period EMA on a stock with sixty bars of history does not
    exist, and the honest storage for "does not exist" is NULL.

WHY COLUMNS RATHER THAN A LONG (isin, date, indicator, value) TABLE
    He has said he wants to add more and more indicators, and that
    normally argues for a long table -- new indicators then need no
    migration at all.

    Columns still win here, for now, because of who reads them. The
    consumer is score_stocks.py, which wants ONE ROW per stock per day
    with every reading on it, and a long table would make that a pivot on
    every run. stock_technical is already 1.6M rows; a long table with
    twenty indicators would be 30M and every read would need reassembling.

    The point at which that flips is roughly thirty columns. Past that,
    the migrations get tedious and the row gets wide enough that Postgres
    starts storing it out of line anyway. Worth revisiting then, and worth
    NOT pre-building now.

AFTER THIS
    backfill_ema.py fills the history. The nightly run fills new rows by
    itself once fetch_technicals.py is deployed.
"""

import argparse
import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

PERIODS = (5, 10, 20, 26, 50, 100, 200)
TABLE = "stock_technical"


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
    is in keyword form, and urlparse on it silently produces nonsense.

    The password is read with getpass -- not echoed, not in shell history,
    not visible to anyone running `ps`. The same way create_ohlc_table.py
    does it, because there is no reason for two scripts on one machine to
    ask for the same password in two different ways.

    The first version of this dropped the password and hoped .pgpass would
    supply it. psycopg does not prompt the way psql does, so that would
    have failed with "no password supplied" the moment anybody reached
    for --admin -- a second wall immediately behind the first.
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

    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND column_name LIKE 'ema\\_%%'
        ORDER BY column_name
    """, (TABLE,))
    have = [r[0] for r in cur.fetchall()]
    want = ["ema_%d" % p for p in PERIODS]
    missing = [c for c in want if c not in have]
    print("  present : %s" % (", ".join(have) if have else "none"))
    if missing:
        print("  missing : %s" % ", ".join(missing))
    return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin", help="role that owns the table (e.g. doadmin)")
    args = ap.parse_args()

    dsn = admin_dsn(args.admin) if args.admin else site_dsn()

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + TABLE,))
        if not cur.fetchone()[0]:
            sys.exit("%s does not exist. Nothing to add columns to." % TABLE)

        report(cur)
        print()

        # ALTER TABLE needs OWNERSHIP, which a GRANT does not confer --
        # so the site login can write every row of this table and still
        # not be allowed to add a column to it. That is the correct
        # separation, and it means this script has to be told who the
        # owner is. Caught and explained rather than left as a traceback.
        try:
            for p in PERIODS:
                cur.execute(
                    "ALTER TABLE %s ADD COLUMN IF NOT EXISTS ema_%d numeric"
                    % (TABLE, p))
                print("  ema_%d ready" % p)
            conn.commit()
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            cur.execute("SELECT current_user")
            who = cur.fetchone()[0]
            cur.execute("SELECT tableowner FROM pg_tables WHERE tablename = %s",
                        (TABLE,))
            owner = (cur.fetchone() or ["the owner"])[0]
            print()
            print("'%s' can write to %s but does not own it, and only the"
                  % (who, TABLE))
            print("owner may add a column. The owner is '%s'." % owner)
            print()
            print("Re-run as the owner:")
            print("  /opt/mfapi/venv/bin/python3 /opt/mfapi/add_ema_columns.py"
                  " --admin %s" % owner)
            print()
            print("It will ask for that password at the prompt. Nothing has")
            print("been changed.")
            sys.exit(1)

        # Only when running as somebody else -- granting yourself what you
        # already own is noise, and the common case here is that the site
        # role owns the table anyway.
        app = conninfo_to_dict(site_dsn()).get("user")
        cur.execute("SELECT current_user")
        if app and cur.fetchone()[0] != app:
            cur.execute('GRANT SELECT, INSERT, UPDATE ON %s TO "%s"'
                        % (TABLE, app))
            conn.commit()
            print("\n  granted SELECT/INSERT/UPDATE on %s to %s" % (TABLE, app))

    # VERIFY AS THE APPLICATION, not as the admin who just did the work.
    # An admin can always see its own columns; that proves nothing about
    # whether the nightly job can write them.
    print("\n--- verifying as the application login ---")
    with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
        missing = report(cur)

    if missing:
        print("\nThe columns are not visible to the application.")
        print("Re-run with --admin doadmin to apply the grant.")
        sys.exit(1)

    print("\nDone. Every row's EMAs are NULL until backfill_ema.py runs;")
    print("new rows get them from the next fetch_technicals.py run.")


if __name__ == "__main__":
    main()
