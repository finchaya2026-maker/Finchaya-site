"""
add_invest_columns.py -- optional "when and how it went in" per holding.
---------------------------------------------------------------------------
Run once:
    /opt/mfapi/venv/bin/python3 /opt/mfapi/add_invest_columns.py

    --check      report only. Writes nothing. Shows whether the columns
                 exist, whether the site can WRITE them, and how many
                 holdings have been filled in.

    --set-role   act as the role that owns portfolio_holding for this
                 run. Only works where the site's own role is already a
                 member of that one, and then it needs no extra
                 credentials. Run whodb.py first -- it says whether this
                 will work before you try it.

    --admin [USER]
                 connect as the cluster's admin login instead (default
                 'doadmin', which is what DigitalOcean's managed
                 Postgres calls it), do the ALTER as them, and then
                 re-connect as the site's own role to check the result.
                 The password is PROMPTED FOR, never taken from the
                 command line -- so it stays out of the shell history
                 and out of `ps`.

                 This is the answer when whodb.py says the site's role
                 cannot alter the table and cannot become the owner.

IF IT REFUSES WITH "must be owner of table"
    That is not a bug and nothing was written. ALTER TABLE belongs to
    the table's owner, and the role the site connects as is not it.
    Run whodb.py; it prints who owns the table, whether this box can
    become them, and which of the two fixes applies.

WHAT THIS ADDS
    Three nullable columns on portfolio_holding:

      invested_on    date    when the money started going in
      invest_mode    text    'lumpsum' or 'sip'
      invest_amount  numeric the lump sum, or the monthly instalment

    All three optional, all three or none. A holding with them filled in
    gets a real return; a holding without behaves exactly as it does
    today. Nothing that works now stops working.

WHY THREE COLUMNS AND NOT ONE DATE
    A date alone cannot produce a return. The existing `amount` column
    is what the holding is worth TODAY. To work out what it earned you
    need what went IN, and when -- the date supplies "when" and says
    nothing about "what".

    And the mode is not a detail. Take 5,000 a month for three years:
    180,000 paid in, worth 230,000 now. Treated as a lump sum held for
    three years that is 8.5% a year. The true figure is 16.6%, because
    the average rupee was invested for eighteen months, not thirty-six.
    Storing the date and the amount without the mode would let the
    report state the first number, which is not a rounding error -- it
    is half, and it understates the client's own returns.

WHAT invest_amount MEANS IN EACH MODE
    lumpsum   the single amount that went in on invested_on.
    sip       the amount of ONE instalment, paid monthly from
              invested_on until today.

    One column rather than two because no holding is both, and a nullable
    column that is only meaningful in one mode invites being filled in
    for the other. The CHECK constraint below makes the pair inseparable:
    a mode without an amount, or an amount without a mode, is rejected by
    the database rather than by whichever bit of code happens to look at
    it first.

WHAT IT DOES NOT MODEL
    A SIP that was paused, stepped up, or stopped. One-off top-ups on
    top of a SIP. Partial redemptions. Switches between funds.

    Those are real and they are common, and none of them can be captured
    in three fields -- they need the transaction history that a CAS
    provides. What this gives is an honest answer for the ordinary case
    of "I have been putting X a month into this since Y", and the report
    says that is the assumption it made rather than implying more
    precision than the input can carry.
"""

import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

COLUMNS = ("invested_on", "invest_mode", "invest_amount")

DDL = """
ALTER TABLE portfolio_holding
    ADD COLUMN IF NOT EXISTS invested_on   date,
    ADD COLUMN IF NOT EXISTS invest_mode   text,
    ADD COLUMN IF NOT EXISTS invest_amount numeric(18,2);
"""

# Separate, and attempted after the columns are committed: re-running
# the script must not fail because the constraint is already there, and
# a constraint failure must not take the columns down with it.
CONSTRAINT = """
ALTER TABLE portfolio_holding
    ADD CONSTRAINT portfolio_holding_invest_ck CHECK (
        -- Nothing filled in is always fine.
        (invested_on IS NULL AND invest_mode IS NULL AND invest_amount IS NULL)
        OR
        -- Otherwise all three, a mode we understand, and a real amount.
        (invested_on IS NOT NULL
         AND invest_mode IN ('lumpsum', 'sip')
         AND invest_amount IS NOT NULL AND invest_amount > 0)
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
                   WHERE conname = 'portfolio_holding_invest_ck'""")
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
                              count(invested_on) AS filled
                       FROM portfolio_holding""")
        r = cur.fetchone()
        print(f"\n  {r[1]} of {r[0]} holdings have a date on them.")


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
                               WHERE conname = 'portfolio_holding_invest_ck'""")
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
                           WHERE conname = 'portfolio_holding_invest_ck'""")
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
