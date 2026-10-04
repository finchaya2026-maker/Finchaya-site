"""
create_admin_log.py -- the record of who looked at whose portfolio.
---------------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_admin_log.py --check
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_admin_log.py --admin doadmin

SAFE TO RE-RUN. Every statement is IF NOT EXISTS.

WHY THIS EXISTS BEFORE THE PAGE DOES
    The admin view can open any user's portfolio and read their holdings.
    That is a useful thing to be able to do -- support, spotting a broken
    import, understanding why a number looks wrong -- and it is also
    reading somebody's finances without them present.

    Two things make that acceptable rather than creepy. One is a line in
    the privacy policy saying staff may access portfolio data for support
    and quality purposes, which is writing, not code. The other is a
    record of every time it happens, which is this table.

    It is built now, with the feature, rather than later. An access log
    added afterwards is worth much less: it can only tell you about the
    period since somebody thought to add it, and the questions that make
    you want one are always about the period before.

    For a platform heading towards SEBI registration this is also the
    cheap version of a control you will be asked about eventually.

WHAT IS NOT HERE
    No retention policy, no automatic pruning. Rows are small and rare,
    and a log that quietly deletes its own history is not much of a log.
    Revisit when it gets large, which will take a very long time.
"""

import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

TABLE = "admin_access_log"

CREATE = """
CREATE TABLE IF NOT EXISTS admin_access_log (
    log_id        bigserial PRIMARY KEY,
    admin_user_id bigint      NOT NULL,
    action        text        NOT NULL,
    portfolio_id  bigint,
    owner_user_id bigint,
    detail        text,
    at            timestamptz NOT NULL DEFAULT now()
)
"""

# The question this table gets asked is almost always "who looked at
# THIS", or "what did THAT admin look at". Both are answered from an
# index; neither should scan the table.
INDEXES = [
    """CREATE INDEX IF NOT EXISTS ix_admin_log_portfolio
         ON admin_access_log (portfolio_id, at DESC)""",
    """CREATE INDEX IF NOT EXISTS ix_admin_log_admin
         ON admin_access_log (admin_user_id, at DESC)""",
]

# INSERT and SELECT only. No UPDATE, no DELETE -- deliberately. An audit
# record the audited party can edit is not an audit record, and the
# application role is the one the admin page runs as.
GRANT = "GRANT SELECT, INSERT ON admin_access_log TO %s"
GRANT_SEQ = "GRANT USAGE, SELECT ON SEQUENCE admin_access_log_log_id_seq TO %s"


def site_dsn():
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")
    return dsn


def admin_dsn(user):
    """The site's connection string with a different login on it.

    Parsed with psycopg's own conninfo_to_dict rather than urlparse: a
    managed provider hands out the `host=... port=...` keyword form, and
    urlparse reads that as a path with no host at all.

    The password is read with getpass, so it is not echoed, not left in
    shell history, and not visible to anyone running `ps`.
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


def app_user():
    return conninfo_to_dict(site_dsn()).get("user")


def report(cur):
    cur.execute("SELECT current_database() AS db, current_user AS me")
    r = cur.fetchone()
    print(f"\nconnected to '{r[0]}' as '{r[1]}'")

    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (TABLE,))
    if not cur.fetchone()[0]:
        print(f"\n  {TABLE:<22}MISSING")
        print("\n  Nothing records who opened whose portfolio. The admin")
        print("  page should not go live without it.")
        return False

    cur.execute(f"SELECT count(*), max(at) FROM {TABLE}")
    n, last = cur.fetchone()
    print(f"\n  {TABLE:<22}present")
    print(f"  {'rows':<22}{n:,}")
    print(f"  {'last entry':<22}{last or '(none yet)'}")

    me = app_user()
    if me:
        cur.execute("""SELECT has_table_privilege(%s,%s,'SELECT'),
                              has_table_privilege(%s,%s,'INSERT')""",
                    (me, TABLE, me, TABLE))
        sel, ins = cur.fetchone()
        ok = sel and ins
        print(f"\n  readable and writable by {me}: "
              f"{'yes' if ok else 'NO -- re-run with --admin'}")
        return ok
    return True


def main():
    args = sys.argv[1:]

    if "--check" in args:
        with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
            report(cur)
        print("\nRead-only check. Nothing was changed.\n")
        return

    admin = None
    for i, a in enumerate(args):
        if a == "--admin":
            admin = (args[i + 1] if i + 1 < len(args)
                     and not args[i + 1].startswith("-") else "doadmin")

    with psycopg.connect(admin_dsn(admin) if admin else site_dsn()) as conn, \
            conn.cursor() as cur:
        cur.execute("SELECT current_user")
        who = cur.fetchone()[0]
        print(f"\nrunning as '{who}'")

        cur.execute(CREATE)
        print(f"  {TABLE} ready")
        for ddl in INDEXES:
            cur.execute(ddl)
        print("  indexes ready")

        me_app = app_user()
        if me_app and who != me_app:
            cur.execute(GRANT % f'"{me_app}"')
            cur.execute(GRANT_SEQ % f'"{me_app}"')
            print(f"  granted SELECT/INSERT to {me_app}")
        conn.commit()

    # Verify as the APPLICATION, not as the admin who just did the work.
    # An admin can always read its own table; that proves nothing about
    # whether the page can write to it.
    print("\n--- verifying as the application login ---")
    with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
        ok = report(cur)
    print("\nDone.\n" if ok else
          "\nThe table exists but the application cannot write to it.\n")


if __name__ == "__main__":
    main()
