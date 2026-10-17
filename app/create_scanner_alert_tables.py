"""
create_scanner_alert_tables.py -- the tables behind "email me when a new stock appears on this scanner". Run once, by hand.
---------------------------------------------------------------------------
    python create_scanner_alert_tables.py --check            # what is there and what is missing; changes nothing
    python create_scanner_alert_tables.py                    # create them with the site's own login
    python create_scanner_alert_tables.py --admin doadmin    # ... or with the cluster admin login (asks for the password at the prompt; it is never written down)

    scanner_alert      one row per alert: who set it, which scanner, its settings (jsonb, exactly as validated), the sentence the person was shown, how they want to be told
                       (only new stocks, or a daily list), the stocks that were on the scanner at the last look (so "new" means new since then), the email waiting to go
                       (saved before it is sent, so a mail outage loses nothing) and what happened to the last one
    scanner_alert_run  one row each time the checker (scanner_alerts.py) runs: the page reads it to say honestly whether alerts are being checked at all

WHEN THE SITE'S LOGIN CANNOT CREATE TABLES
    The first run says "permission denied for schema public": use --admin. A table made by the admin login is owned by it and the site's login gets nothing by default, so
    this also GRANTS the site's login what it needs (SELECT / INSERT / UPDATE / DELETE, and the id sequences).

THE LINK TO mf_user
    scanner_alert is tied to the account with ON DELETE CASCADE, so deleting an account (which the privacy policy promises on request) deletes its alerts with it.

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

TABLES = ("scanner_alert", "scanner_alert_run")
SEQUENCES = ("scanner_alert_alert_id_seq", "scanner_alert_run_run_id_seq")

DDL = """
CREATE TABLE IF NOT EXISTS scanner_alert (
    alert_id          bigserial    PRIMARY KEY,
    user_id           bigint       NOT NULL,
    screen            text         NOT NULL,            -- the scanner's key, e.g. dow_change
    title             text         NOT NULL,            -- the scanner's name at the time
    settings          jsonb        NOT NULL,            -- the scanner's settings, exactly as validated
    settings_key      text         NOT NULL,            -- screen + settings as sorted text: one alert per person per scanner per set of settings
    sentence          text         NOT NULL,            -- what the person was told it watches for
    mode              text         NOT NULL DEFAULT 'new' CHECK (mode IN ('new', 'daily')),
    active            boolean      NOT NULL DEFAULT true,
    members           jsonb        NOT NULL DEFAULT '[]'::jsonb,   -- ISINs on the scanner at the last look (and those that were there when the alert was made)
    pending           jsonb,                            -- the email waiting to go: what is to be told, saved before it is sent
    pending_attempts  integer      NOT NULL DEFAULT 0,
    emails_sent       integer      NOT NULL DEFAULT 0,
    emailed_count     integer      NOT NULL DEFAULT 0,  -- new stocks told about so far
    last_emailed_at   timestamptz,
    last_digest_date  date,                             -- daily mode: the trading day of the last list sent
    last_checked_at   timestamptz,
    last_stamp        text,                             -- newest trading day + today at the last look: a new day means look again
    last_as_of        text,                             -- the date of the data the scanner read at the last look
    last_error        text,
    created_at        timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (user_id, settings_key)
);
CREATE INDEX IF NOT EXISTS ix_scanner_alert_user ON scanner_alert (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS scanner_alert_run (
    run_id      bigserial    PRIMARY KEY,
    started_at  timestamptz  NOT NULL DEFAULT now(),
    checked     integer      NOT NULL DEFAULT 0,      -- alerts actually looked at
    skipped     integer      NOT NULL DEFAULT 0,      -- alerts left alone because no new prices had arrived
    waiting     integer      NOT NULL DEFAULT 0,      -- alerts whose scanner's data had not caught up with the prices yet
    new_matches integer      NOT NULL DEFAULT 0,
    emailed     integer      NOT NULL DEFAULT 0,
    failed      integer      NOT NULL DEFAULT 0,      -- emails that could not be sent (they are retried)
    error       text
);
"""

FK = """
DO $$
BEGIN
    BEGIN
        ALTER TABLE scanner_alert ADD CONSTRAINT scanner_alert_user_fk
            FOREIGN KEY (user_id) REFERENCES mf_user (user_id) ON DELETE CASCADE;
    EXCEPTION
        WHEN duplicate_object THEN NULL;
        WHEN insufficient_privilege THEN RAISE NOTICE 'no_fk';
        WHEN undefined_table THEN RAISE NOTICE 'no_fk';
    END;
END $$;
"""

GRANTS = """
GRANT SELECT, INSERT, UPDATE, DELETE ON scanner_alert, scanner_alert_run TO "{u}";
GRANT USAGE, SELECT ON SEQUENCE scanner_alert_alert_id_seq, scanner_alert_run_run_id_seq TO "{u}";
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
        print("  %-22s %s" % (t, "present" if there else "MISSING"))
        if not there:
            ok = False
            continue
        cur.execute("SELECT count(*) FROM " + t)
        print("  %-22s %d rows" % ("", cur.fetchone()[0]))
    cur.execute("SELECT to_regclass('scanner_alert') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute("SELECT count(*) FROM pg_constraint WHERE conrelid = 'scanner_alert'::regclass AND contype = 'f'")
        print("  %-22s %s" % ("", "linked to mf_user" if cur.fetchone()[0] else "NOT linked to mf_user (deleting an account will not remove these rows)"))
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
        print("  " + ", ".join(TABLES) + " ready")
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
