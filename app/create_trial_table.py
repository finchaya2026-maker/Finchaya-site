"""
create_trial_table.py -- the table behind the 7-day free trial (trial.py). Run once, by hand.
---------------------------------------------------------------------------
    python create_trial_table.py --check            # what is there and what is missing; changes nothing
    python create_trial_table.py                    # create it with the site's own login
    python create_trial_table.py --admin doadmin    # ... or with the cluster admin login (asks for the password at the prompt; it is never written down)

    billing_trial   one row per account that has had a free trial: which tier it tried, the first and last day, the two emails (reminder on day 6, farewell on day 8) with when
                    each went and what the mail service said, and whether the person subscribed afterwards. user_id and email_key are both UNIQUE: one trial per account,
                    and one per person (the address with a Gmail address's dots and "+tags" removed).

THE LINK TO mf_user
    Tied to the account with ON DELETE CASCADE, so deleting an account (which the privacy policy promises on request) deletes its trial row with it.

WHEN THE SITE'S LOGIN CANNOT CREATE TABLES
    Use --admin: a table made by the admin login belongs to it, so this also GRANTS the site's login what it needs (SELECT / INSERT / UPDATE / DELETE, and the id sequence).

SAFE TO RE-RUN. Every statement is IF NOT EXISTS (or already-exists tolerant).
"""

import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv("/opt/mfapi/.env")

TABLES = ("billing_trial",)
SEQUENCES = ("billing_trial_trial_id_seq",)

DDL = """
CREATE TABLE IF NOT EXISTS billing_trial (
    trial_id          bigserial    PRIMARY KEY,
    user_id           bigint       NOT NULL UNIQUE,
    email_key         text         NOT NULL UNIQUE,         -- the address with Gmail dots and +tags removed: one trial per person
    scope             text         NOT NULL CHECK (scope IN ('mf', 'stocks', 'all')),
    started_on        date         NOT NULL,                -- day 1
    ends_on           date         NOT NULL,                -- the last day of access (day 7)
    reminder_sent_at  timestamptz,                          -- the "ends tomorrow" email (day 6)
    reminder_note     text,                                 -- what the mail service said (or why it was not sent): a failed one is tried again on the next run
    farewell_sent_at  timestamptz,                          -- the "sorry to let you go" email (day 8)
    farewell_note     text,
    converted_on      date,                                 -- the day we noticed the person had subscribed (so neither email goes)
    expired_on        date,                                 -- the day the job switched the account's plan off
    created_at        timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_billing_trial_ends ON billing_trial (ends_on);
"""

FK = """
DO $$
BEGIN
    BEGIN
        ALTER TABLE billing_trial ADD CONSTRAINT billing_trial_user_fk
            FOREIGN KEY (user_id) REFERENCES mf_user (user_id) ON DELETE CASCADE;
    EXCEPTION
        WHEN duplicate_object THEN NULL;
        WHEN insufficient_privilege THEN RAISE NOTICE 'no_fk';
        WHEN undefined_table THEN RAISE NOTICE 'no_fk';
    END;
END $$;
"""

GRANTS = """
GRANT SELECT, INSERT, UPDATE, DELETE ON billing_trial TO "{u}";
GRANT USAGE, SELECT ON SEQUENCE billing_trial_trial_id_seq TO "{u}";
"""


def site_dsn():
    dsn = os.environ.get("FINCHAYA_DB") or os.environ.get("DATABASE_URL") or os.environ.get("MF_DSN")
    if not dsn:
        sys.exit("No FINCHAYA_DB in the environment or .env.")
    return dsn


def admin_dsn(user):
    d = conninfo_to_dict(site_dsn())
    print("\nConnecting to %s as '%s'." % (d.get("host", "(unknown host)"), user))
    print("The password is in your database provider's control panel, not on this machine. It is not shown as you type.")
    pw = getpass.getpass("%s password: " % user)
    if not pw:
        sys.exit("No password given. Nothing was changed.")
    d["user"], d["password"] = user, pw
    return make_conninfo(**d)


def app_user():
    return conninfo_to_dict(site_dsn()).get("user")


def report(cur):
    cur.execute("SELECT current_database(), current_user")
    db, who = cur.fetchone()
    print("\nconnected to '%s' as '%s'\n" % (db, who))
    ok = True
    for t in TABLES:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (t,))
        there = cur.fetchone()[0]
        print("  %-18s %s" % (t, "present" if there else "MISSING"))
        if not there:
            ok = False
            continue
        cur.execute("SELECT count(*) FROM " + t)
        print("  %-18s %d rows" % ("", cur.fetchone()[0]))
        cur.execute("SELECT count(*) FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'f'", (t,))
        print("  %-18s %s" % ("", "linked to mf_user" if cur.fetchone()[0] else "NOT linked to mf_user (deleting an account will not remove these rows)"))
    me = app_user()
    if me and ok:
        for t in TABLES:
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT'), has_table_privilege(%s, %s, 'INSERT'), has_table_privilege(%s, %s, 'UPDATE'), has_table_privilege(%s, %s, 'DELETE')",
                        (me, t) * 4)
            good = all(cur.fetchone())
            print("  %s usable by %s: %s" % (t, me, "yes" if good else "NO -- run with --admin"))
            ok = ok and good
        for seq in SEQUENCES:
            cur.execute("SELECT has_sequence_privilege(%s, %s, 'USAGE')", (me, seq))
            good = cur.fetchone()[0]
            print("  sequence %s usable by %s: %s" % (seq, me, "yes" if good else "NO -- run with --admin"))
            ok = ok and good
    return ok


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
            admin = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else "doadmin"
    dsn = admin_dsn(admin) if admin else site_dsn()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user")
        me = cur.fetchone()[0]
        print("\nrunning as '%s'" % me)
        cur.execute(DDL)
        print("  billing_trial ready")
        cur.execute(FK)
        site = app_user()
        if site and me != site:
            cur.execute(GRANTS.format(u=site))
            print("  granted table and sequence access to %s" % site)
        conn.commit()
    print("\n--- verifying as the site's own login ---")
    with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
        ok = report(cur)
    print("\nDone." if ok else "\nSomething above says NO or MISSING. Re-run with --admin doadmin.")
    print()


if __name__ == "__main__":
    main()
