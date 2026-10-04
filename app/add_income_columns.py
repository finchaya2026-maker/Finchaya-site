"""
add_income_columns.py -- let portfolio_goal hold a regular-income plan
----------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/add_income_columns.py
    --admin doadmin        when the site login does not own the table

Adds three nullable columns to portfolio_goal and nothing else. Existing
rows are untouched and stay exactly as they were: NULL in all three means
"a saving plan", which is what every goal saved so far is.

WHY THE COLUMNS ARE NEEDED AND A DERIVED TARGET IS NOT ENOUGH
    A regular-income plan on the planner reduces to one number -- the
    corpus the income needs on the day it starts -- and that number fits
    target_amount perfectly. So it would be easy to stop there.

    It would also be wrong, and wrong in the quiet way. Reopening that
    goal would show "you need 1.4 crore by March 2031" with no hint that
    the figure came from wanting 50,000 a month for twenty-five years,
    and the planner would repaint it as a SAVING plan with the income
    boxes empty. The distributor would then either retype the income from
    memory or, far more likely, accept the corpus as though it had been
    the client's own answer. A derived number that has lost the question
    it answers is worse than no number, because it still looks like data.

    So the three inputs are stored beside the output:

        income_amount   rupees a month the client wants
        income_mode     'keep'  -- income from growth, capital untouched
                        'spend' -- capital drawn down to nothing
        draw_years      how long it is drawn down over ('spend' only)

WHAT IT DOES NOT DO
    It does not backfill anything, because there is nothing to backfill:
    no income plan has ever been saved. It does not touch target_amount,
    which keeps meaning "the sum needed on the target date" for both
    kinds of plan -- that is what makes the progress tracker, the
    valuation and the alerts carry on working with no change at all.
"""

import argparse
import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

TABLE = "portfolio_goal"

# name, type, and the constraint that keeps a typo out of income_mode.
COLUMNS = [
    ("income_amount", "numeric"),
    ("income_mode", "text"),
    ("draw_years", "integer"),
]

# Written as a named constraint so re-running finds it and skips it. An
# unnamed CHECK would be added again on every run.
MODE_CHECK = """
ALTER TABLE portfolio_goal
  ADD CONSTRAINT portfolio_goal_income_mode_ck
  CHECK (income_mode IS NULL OR income_mode IN ('keep', 'spend'))
"""


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

    The password is read with getpass -- not echoed, not in shell history,
    not visible to anyone running `ps` -- the same way create_ohlc_table.py
    and add_ema_columns.py do it.
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
        WHERE table_name = %s
          AND column_name IN ('income_amount', 'income_mode', 'draw_years')
        ORDER BY column_name
    """, (TABLE,))
    have = [r[0] for r in cur.fetchall()]
    missing = [c for c, _ in COLUMNS if c not in have]
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

        # ALTER TABLE needs OWNERSHIP, which a GRANT does not confer -- so
        # the site login can write every row of this table and still not be
        # allowed to add a column to it. That is the correct separation,
        # and it means this script has to be told who the owner is. Caught
        # and explained rather than left as a traceback.
        try:
            for name, kind in COLUMNS:
                cur.execute("ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s"
                            % (TABLE, name, kind))
                print("  %s ready" % name)
            # Separate savepoint: the columns are the point, and a
            # constraint that already exists must not undo them.
            cur.execute("SAVEPOINT ck")
            try:
                cur.execute(MODE_CHECK)
                print("  income_mode limited to 'keep' or 'spend'")
            except psycopg.errors.DuplicateObject:
                cur.execute("ROLLBACK TO SAVEPOINT ck")
                print("  income_mode check already in place")
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
            print("  /opt/mfapi/venv/bin/python3 /opt/mfapi/add_income_columns.py"
                  " --admin %s" % owner)
            print()
            print("It will ask for that password at the prompt. Nothing has")
            print("been changed.")
            sys.exit(1)

        # Only when running as somebody else -- granting yourself what you
        # already own is noise.
        app = conninfo_to_dict(site_dsn()).get("user")
        cur.execute("SELECT current_user")
        if app and cur.fetchone()[0] != app:
            cur.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON %s TO "%s"'
                        % (TABLE, app))
            conn.commit()
            print("\n  granted SELECT/INSERT/UPDATE/DELETE on %s to %s"
                  % (TABLE, app))

    # VERIFY AS THE APPLICATION, not as the admin who just did the work.
    # An admin can always see its own columns; that proves nothing about
    # whether the site can read them.
    print("\n--- verifying as the application login ---")
    with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
        missing = report(cur)
        cur.execute("""
            SELECT count(*) AS total,
                   count(income_amount) AS income_plans
            FROM portfolio_goal
        """)
        total, inc = cur.fetchone()
        print("  goals on file : %d, of which %d are income plans"
              % (total, inc))

    if missing:
        print("\nThe columns are not visible to the application.")
        print("Re-run with --admin doadmin to apply the grant.")
        sys.exit(1)

    print("\nDone. Every existing goal keeps NULL in all three, which reads")
    print("as a saving plan -- which is what they all are. Restart the API")
    print("so saved_portfolio_api.py picks up the new fields:")
    print("  systemctl restart mfapi")


if __name__ == "__main__":
    main()
