"""
create_stock_alert_tables.py -- the tables behind stock alerts ("email me when Infosys closes above 1,500"). Run once, by hand.
---------------------------------------------------------------------------
    python create_stock_alert_tables.py --check            # what is there and what is missing; changes nothing
    python create_stock_alert_tables.py                    # create them with the site's own login
    python create_stock_alert_tables.py --admin doadmin    # ... or with the cluster admin login (asks for the
                                                           #     password at the prompt; it is never written down)

    stock_alert      one row per alert: who set it, which stock, what it watches (spec, jsonb, exactly as validated), the
                     sentence the person was shown, its status (active / triggered / paused), when it was last looked at,
                     what it read when it fired, and what happened to the email (pending / sent / failed, with the error)
    stock_alert_run  one row each time the checker (stockalerts_checker.py) runs: when, how many it looked at, how many
                     fired and were emailed. The page reads it to say honestly whether alerts are being checked at all.
    indicator_request  "Ask us to add an indicator" (back-test page; the trade plan uses it too): one row per person per thing.
                     Made here as well so one command sets up everything the Stocks tools need; harmless if it exists already.

WHEN THE SITE'S LOGIN CANNOT CREATE TABLES
    The first run says "permission denied for schema public" -- use --admin. A table made by the admin login is owned by it
    and the site's login gets nothing by default, so this also GRANTS the site's login what it needs: SELECT / INSERT / UPDATE /
    DELETE on both tables AND usage of their id sequences (without the sequences every save fails with "permission denied
    for sequence", which only shows up when somebody first sets an alert).

THE LINK TO mf_user
    stock_alert is tied to the account with ON DELETE CASCADE, so deleting an account (which the privacy policy promises on
    request) deletes its alerts with it. If the login used here may not make that link, the tables are still created and a
    warning says so.

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

TABLES = ("stock_alert", "stock_alert_run", "indicator_request")
USER_TABLES = ("stock_alert", "indicator_request")
SEQUENCES = ("stock_alert_alert_id_seq", "stock_alert_run_run_id_seq", "indicator_request_request_id_seq")

DDL = """
CREATE TABLE IF NOT EXISTS stock_alert (
    alert_id        bigserial    PRIMARY KEY,
    user_id         bigint       NOT NULL,
    isin            text         NOT NULL,
    symbol          text,
    company_name    text,
    kind            text         NOT NULL CHECK (kind IN ('price', 'ema_support', 'trend_change', 'indicator', 'results')),
    spec            jsonb        NOT NULL,            -- what it watches, as validated; includes the baseline (last candle that existed)
    sentence        text         NOT NULL,            -- what the person was told it is watching for
    status          text         NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'triggered', 'paused')),
    note            text,                             -- why it was paused, if it was
    last_checked_at timestamptz,
    last_stamp      text,                             -- newest candle date + today at the last look: a new day or candle means look again
    last_state      text,                             -- met | not_met | no_data at the last look
    triggered_at    timestamptz,
    trigger_reading text,                             -- what it read when it fired (the figures, in words)
    trigger_as_of   date,                             -- the candle / filing / meeting date it fired on
    email_status    text         NOT NULL DEFAULT 'pending' CHECK (email_status IN ('pending', 'sent', 'failed')),
    email_attempts  integer      NOT NULL DEFAULT 0,
    email_error     text,
    email_sent_at   timestamptz,
    created_at      timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_stock_alert_user ON stock_alert (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_stock_alert_status ON stock_alert (status, isin);

CREATE TABLE IF NOT EXISTS stock_alert_run (
    run_id      bigserial    PRIMARY KEY,
    started_at  timestamptz  NOT NULL DEFAULT now(),
    checked     integer      NOT NULL DEFAULT 0,      -- alerts actually evaluated
    skipped     integer      NOT NULL DEFAULT 0,      -- alerts left alone because nothing had changed since the last look
    fired       integer      NOT NULL DEFAULT 0,      -- alerts whose condition became met this run
    emailed     integer      NOT NULL DEFAULT 0,
    failed      integer      NOT NULL DEFAULT 0,      -- emails that could not be sent (they are retried)
    error       text
);

-- "Ask us to add an indicator" (back-test page; the trade plan writes here too). The same table, in the same shape, as
-- create_trade_plan_tables.py makes -- IF NOT EXISTS, so running either script first is fine.
CREATE TABLE IF NOT EXISTS indicator_request (
    request_id   bigserial    PRIMARY KEY,
    user_id      bigint       NOT NULL,
    kind         text         NOT NULL DEFAULT 'indicator' CHECK (kind IN ('indicator', 'data')),
    name         text         NOT NULL,              -- as the person typed it
    name_key     text         NOT NULL,              -- lower-case letters and digits only: groups spellings
    details      text,
    timeframe    text,
    instrument   text,
    status       text         NOT NULL DEFAULT 'new',   -- new | planned | done | declined (set by hand)
    created_at   timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (user_id, kind, name_key)
);
CREATE INDEX IF NOT EXISTS ix_indicator_request_key ON indicator_request (kind, name_key);
"""

# Tolerant of "already there" and of a login that may not link to mf_user.
FK = """
DO $$
BEGIN
    BEGIN
        ALTER TABLE {t} ADD CONSTRAINT {t}_user_fk
            FOREIGN KEY (user_id) REFERENCES mf_user (user_id) ON DELETE CASCADE;
    EXCEPTION
        WHEN duplicate_object THEN NULL;
        WHEN insufficient_privilege THEN RAISE NOTICE 'no_fk';
        WHEN undefined_table THEN RAISE NOTICE 'no_fk';
    END;
END $$;
"""

GRANTS = """
GRANT SELECT, INSERT, UPDATE, DELETE ON stock_alert, stock_alert_run, indicator_request TO "{u}";
GRANT USAGE, SELECT ON SEQUENCE stock_alert_alert_id_seq, stock_alert_run_run_id_seq, indicator_request_request_id_seq TO "{u}";
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
        if t in USER_TABLES:
            cur.execute("""SELECT count(*) FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'f'""", (t,))
            print("  %-18s %s" % ("", "linked to mf_user" if cur.fetchone()[0] else
                                  "NOT linked to mf_user (deleting an account will not remove these rows)"))
    me = app_user()
    if me and ok:
        for t in TABLES:
            cur.execute("""SELECT has_table_privilege(%s, %s, 'SELECT'), has_table_privilege(%s, %s, 'INSERT'),
                                  has_table_privilege(%s, %s, 'UPDATE'), has_table_privilege(%s, %s, 'DELETE')""",
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
        for t in USER_TABLES:
            cur.execute(FK.format(t=t))
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
