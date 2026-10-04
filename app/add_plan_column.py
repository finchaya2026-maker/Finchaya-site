"""
add_plan_column.py -- which plan each holding is actually in.
---------------------------------------------------------------------------
Run once:
    /opt/mfapi/venv/bin/python3 /opt/mfapi/add_plan_column.py --admin doadmin

    --check      report only. Writes nothing.
    --set-role   act as the table's owner, where the site's role is
                 already a member of it.
    --admin [USER]
                 connect as the cluster's admin login (default 'doadmin')
                 and do the ALTER as them, then re-check as the site's
                 own role. The password is prompted for, never taken on
                 the command line.

WHAT THIS ADDS
    One nullable column on portfolio_holding:

      plan_type   'REGULAR' or 'DIRECT', or NULL

WHY IT MATTERS MORE THAN IT LOOKS
    Everything this platform computes runs on the DIRECT plan, and that
    is right for scoring: a category median mixing Direct and Regular
    measures distributor commission rather than the manager.

    A distributor's client does not hold Direct. They hold Regular, and
    Regular carries the commission inside the NAV. The gap runs around
    0.5 to 1.2 percentage points a year for equity funds, which is
    roughly 3% less money over five years and 8% over ten. Reporting a
    client's own holding at Direct figures overstates their money, and
    it does so specifically about them -- which is the worst kind of
    error this platform can make.

WHY NULL IS A REAL VALUE AND NOT A GAP
    NULL means "nobody has said", and the reader resolves it by rule:
    Regular in a portfolio that belongs to a client, Direct in one of
    your own. That default is right almost every time and needs no
    typing.

    It is NOT backfilled into the column, deliberately. Writing the
    guess into the data would make it indistinguishable from a person
    having chosen it, and the two should never look alike -- a client
    who bought a fund Direct themselves has to be recordable as an
    exception, and you have to be able to see which holdings were
    assumed rather than stated.
"""

import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

COLUMNS = ("plan_type",)

DDL = """
ALTER TABLE portfolio_holding
    ADD COLUMN IF NOT EXISTS plan_type text;
"""

# Separate, and attempted after the columns are committed: re-running
# the script must not fail because the constraint is already there, and
# a constraint failure must not take the columns down with it.
CONSTRAINT = """
ALTER TABLE portfolio_holding
    ADD CONSTRAINT portfolio_holding_plan_ck CHECK (
        plan_type IS NULL OR plan_type IN ('REGULAR', 'DIRECT')
    )
"""


def _one_line(sql):
    """Collapse SQL to a single line for pasting into psql -c.

    STRIPS `--` COMMENTS FIRST, and that is the whole reason this
    exists rather than " ".join(sql.split()). On one line a `--`
    comments out everything after it, so a statement that is perfectly
    readable in this file turns into a truncated fragment the moment it
    is flattened -- and the command printed for someone to run would
    fail, or worse, half-run.
    """
    body = "\n".join(l.split("--")[0] for l in sql.splitlines())
    return " ".join(body.split())


def site_dsn():
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")
    return dsn


def connect():
    return psycopg.connect(site_dsn())


def admin_dsn(user):
    """The site's own connection string with a different login on it.

    PARSED WITH PSYCOPG'S OWN conninfo_to_dict, not with urlparse.
    A PostgreSQL connection string comes in two shapes -- the URL one,
    postgresql://user:pw@host/db, and the keyword one, `host=... port=...
    dbname=... user=...`. Managed providers hand out the keyword shape,
    and urlparse reads it as a path with no host at all, then cheerfully
    builds a nonsense URL out of the pieces. psycopg reads both, which
    is the entire reason to use its parser rather than write another.

    ASKED FOR, NEVER PASSED IN. The password is read with getpass, so it
    is not echoed to the screen, not left in the shell history, and not
    visible to anyone running `ps`. On a managed database the admin
    password is the keys to everything -- it should not end up in a
    text file on an application server because a migration script found
    that convenient.

    Host, port, database and the SSL settings are reused from
    FINCHAYA_DB, because they are the same cluster and retyping them is
    just another thing to get wrong.
    """
    d = conninfo_to_dict(site_dsn())
    print(f"\nConnecting to {d.get('host', '(unknown host)')} as '{user}'.")
    print("The password is in your database provider's control panel,")
    print("not on this machine. It is not shown as you type it.")
    pw = getpass.getpass(f"{user} password: ")
    if not pw:
        sys.exit("No password given. Nothing was changed.")
    d["user"] = user
    d["password"] = pw
    return make_conninfo(**d)


def report(cur):
    cur.execute("SELECT current_database(), current_user")
    db, who = cur.fetchone()
    print(f"\nconnected to '{db}' as '{who}'\n")

    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name = 'portfolio_holding'
                     AND column_name = ANY(%s)""", (list(COLUMNS),))
    have = {r[0] for r in cur.fetchall()}
    for c in COLUMNS:
        print(f"  {c:<16} {'yes' if c in have else 'NO'}")

    cur.execute("""SELECT 1 FROM pg_constraint
                   WHERE conname = 'portfolio_holding_plan_ck'""")
    print(f"  {'check constraint':<16} "
          f"{'yes' if cur.fetchone() else 'NO'}")

    # Existing AND writable by whoever is connected. A column added by
    # the table's owner is covered by a table-level GRANT but NOT by a
    # column-level one -- so "the column is there" and "the API can
    # write to it" are two different questions, and only the second one
    # decides whether the feature works.
    if len(have) == len(COLUMNS):
        writable = all(
            cur.execute("SELECT has_column_privilege('portfolio_holding',"
                        " %s, 'UPDATE')", (c,)).fetchone()[0]
            for c in COLUMNS)
        print(f"  {'writable by ' + who:<16} {'yes' if writable else 'NO'}")
        if not writable:
            print(f"\n  '{who}' cannot write these columns. As the owner:")
            print(f"    GRANT UPDATE ON portfolio_holding TO {who};")

        cur.execute("""SELECT count(*) AS total,
                              count(plan_type) AS stated,
                              count(*) FILTER (WHERE plan_type = 'REGULAR')
                                AS regular
                       FROM portfolio_holding""")
        r = cur.fetchone()
        print(f"\n  {r[1]} of {r[0]} holdings state a plan "
              f"({r[2]} of them REGULAR).")
        print("  The rest resolve by rule: REGULAR for a client's "
              "portfolio,\n  DIRECT for your own.")


def main():
    args = sys.argv[1:]
    if "--check" in args:
        with connect() as conn, conn.cursor() as cur:
            report(cur)
        return

    # --admin: do the ALTER as the cluster's admin login, then VERIFY as
    # the site's own role.
    #
    # The verification is the point. A migration that runs as a
    # superuser and reports success proves the superuser can do it,
    # which was never in doubt. What matters is whether the role the
    # website connects as can now read and write the new columns, and
    # that question can only be answered on a second connection.
    admin = None
    for i, a in enumerate(args):
        if a == "--admin":
            admin = args[i + 1] if i + 1 < len(args) \
                and not args[i + 1].startswith("-") else "doadmin"

    if admin:
        with psycopg.connect(admin_dsn(admin)) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user")
            db, who = cur.fetchone()
            print(f"\nconnected to '{db}' as '{who}'")
            cur.execute(DDL)
            conn.commit()
            print("columns added (or already present).")
            try:
                cur.execute("""SELECT 1 FROM pg_constraint
                               WHERE conname = 'portfolio_holding_plan_ck'""")
                if cur.fetchone():
                    print("check constraint already in place.")
                else:
                    cur.execute(CONSTRAINT)
                    conn.commit()
                    print("check constraint added.")
            except Exception as exc:                   # noqa: BLE001
                conn.rollback()
                print("check constraint NOT added: %s"
                      % str(exc).strip().splitlines()[0])

        print("\n--- now checking as the role the SITE uses ---")
        with connect() as conn, conn.cursor() as cur:
            report(cur)
        print("\nIf that says writable: yes, restart the API and you are done:")
        print("  systemctl restart mfapi")
        return

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('portfolio_holding') IS NOT NULL")
        if not cur.fetchone()[0]:
            cur.execute("SELECT current_database()")
            sys.exit(f"No portfolio_holding table in '{cur.fetchone()[0]}'. "
                     "Check FINCHAYA_DB points at the database the site uses.")

        # --set-role: BECOME THE OWNER, when we are already allowed to.
        #
        # Postgres lets a role act as any role it is a member of, and a
        # site role is often a member of the role that owns its tables.
        # Where that holds, this needs no extra credentials at all --
        # which matters here, because the database is not necessarily on
        # this machine and there may be no superuser password to hand.
        #
        # Not the default, and deliberately so: silently acquiring more
        # privilege than the caller asked for is a bad habit for a
        # script that runs against a live database. whodb.py says
        # whether it will work before you try it.
        if "--set-role" in sys.argv[1:]:
            cur.execute("""SELECT tableowner FROM pg_tables
                           WHERE tablename = 'portfolio_holding'""")
            owner = (cur.fetchone() or [None])[0]
            cur.execute("SELECT current_user")
            me = cur.fetchone()[0]
            if not owner:
                sys.exit("No portfolio_holding table here.")
            cur.execute("SELECT pg_has_role(%s, %s, 'MEMBER')", (me, owner))
            if not cur.fetchone()[0]:
                sys.exit(f"'{me}' is not a member of '{owner}', so --set-role "
                         f"cannot help.\nRun whodb.py to see the options.")
            cur.execute(f'SET ROLE "{owner}"')
            print(f"acting as '{owner}' for this run.")

        # ALTER TABLE NEEDS OWNERSHIP OF THE TABLE, and the role the
        # site connects as does not have it -- portfolio_holding is
        # owned by someone else. create_distributor_tables.py met the
        # same wall on the portfolio table and solved it by printing
        # the statement for a human to run as the owner. Same here:
        # this script cannot grant itself the right, and pretending the
        # run succeeded would leave a feature half-installed.
        try:
            cur.execute(DDL)
            conn.commit()
            print("columns added (or already present).")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            cur.execute("SELECT current_database(), current_user")
            db, who = cur.fetchone()
            cur.execute("""SELECT tableowner FROM pg_tables
                           WHERE tablename = 'portfolio_holding'""")
            owner = (cur.fetchone() or ["?"])[0]
            one_line = _one_line(DDL)
            print(f"""
Cannot add the columns: '{who}' does not own portfolio_holding.
That table belongs to '{owner}', and ALTER TABLE needs its owner.

NOTHING WAS CHANGED. Run these two as the owner instead -- on this
box that usually means going in through the postgres superuser:

  sudo -u postgres psql -d {db} -c "{one_line}"

  sudo -u postgres psql -d {db} -c "{_one_line(CONSTRAINT)}"

Then check it worked, as the site's own role:

  /opt/mfapi/venv/bin/python3 /opt/mfapi/add_invest_columns.py --check

The last line of that must say writable by {who}: yes. If it says NO,
the columns exist but the site cannot write them, and the owner needs
one more statement:

  sudo -u postgres psql -d {db} -c "GRANT UPDATE ON portfolio_holding TO {who};"
""")
            sys.exit(1)

        # ALTER TABLE needs ownership. If the app role does not have it
        # the columns above may still have succeeded -- say which part
        # failed rather than reporting the whole run as broken.
        try:
            cur.execute("""SELECT 1 FROM pg_constraint
                           WHERE conname = 'portfolio_holding_plan_ck'""")
            if cur.fetchone():
                print("check constraint already in place.")
            else:
                cur.execute(CONSTRAINT)
                conn.commit()
                print("check constraint added.")
        except Exception as exc:                       # noqa: BLE001
            conn.rollback()
            print("\ncheck constraint NOT added: %s"
                  % str(exc).strip().splitlines()[0])
            print("The columns are there and the feature works without it.")
            print("The constraint only stops a half-filled row being")
            print("stored; the API checks the same thing before writing.")

        # Back to the role the SITE connects as before reporting. The
        # last line of that report answers "can the site write these",
        # and answering it as the owner we just borrowed would be the
        # wrong question with a reassuring answer.
        cur.execute("RESET ROLE")
        report(cur)
        print("\nNothing existing changed. Every new column is NULL, and a")
        print("holding with NULLs behaves exactly as it did before.")


if __name__ == "__main__":
    main()
