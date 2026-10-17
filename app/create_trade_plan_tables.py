"""
create_trade_plan_tables.py -- the tables behind "My trade plan" and its alerts. Run once, by hand.
---------------------------------------------------------------------------
    python create_trade_plan_tables.py --check            # what is there and what is missing; changes nothing
    python create_trade_plan_tables.py                    # create them with the site's own login
    python create_trade_plan_tables.py --admin doadmin    # ... or with the cluster admin login (asks for the
                                                          #     password at the prompt; it is never written down)

    trade_plan         one row per saved plan: who owns it, the instrument, the direction it is written for,
                       the time frame traded on, and the conditions (jsonb: a list, each with its own time
                       frame, indicator, test, numbers and AND / OR)
    indicator_request  what people have asked us to add: an indicator ("Ichimoku Cloud") or data ("hourly
                       candles"). One row per person per thing; the same request spelled differently is
                       one name_key. An administrator reads it counted by how many people asked.
    trade_alert        "email me when this condition is met": who asked, which stock, the condition exactly as it is
                       evaluated (already turned round for a bearish view), its status (active / triggered / paused)
                       and what happened to the email (pending / sent / failed, with the error)
    trade_alert_run    one row each time the checker (tradeplan_alerts.py) runs: when, how many it looked at, how many
                       fired and were emailed. The page reads it to say honestly whether alerts are being checked at all.

WHEN THE SITE'S LOGIN CANNOT CREATE TABLES
    The first run says "permission denied for schema public" -- use --admin. A table made by the admin login is
    owned by it and the site's login gets nothing by default, so this also GRANTS the site's login what it needs:
    SELECT / INSERT / UPDATE / DELETE on both tables AND usage of their id sequences (without the sequences
    every save fails with "permission denied for sequence", which only shows up when somebody first presses Save).

THE LINK TO mf_user
    Both tables are tied to the account with ON DELETE CASCADE, so deleting an account (which the privacy policy
    promises on request) deletes its plans and requests with it. If the login used here is not allowed to make
    that link, the tables are still created, a warning says so, and deleting an account would then need its
    plans removed by hand (DELETE FROM trade_plan WHERE user_id = ...).

ADDING THE ALERT TABLES LATER: just run it again. The two original tables are left alone (IF NOT EXISTS) and the two new
ones are created and granted.

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

TABLES = ("trade_plan", "indicator_request", "trade_alert", "trade_alert_run")
USER_TABLES = ("trade_plan", "indicator_request", "trade_alert")            # tied to mf_user (trade_alert_run has no user)
SEQUENCES = ("trade_plan_plan_id_seq", "indicator_request_request_id_seq", "trade_alert_alert_id_seq",
             "trade_alert_run_run_id_seq")

DDL = """
CREATE TABLE IF NOT EXISTS trade_plan (
    plan_id          bigserial    PRIMARY KEY,
    user_id          bigint       NOT NULL,
    name             text         NOT NULL,
    instrument       text         NOT NULL,          -- nifty50 | banknifty | sensex | stock | natgas | gold | silver | crude | other
    instrument_label text,                           -- what the person called it, for 'other'
    direction        text         NOT NULL CHECK (direction IN ('bullish', 'bearish')),
    trade_tf         text         NOT NULL,          -- the time frame they trade on
    closed_only      boolean      NOT NULL DEFAULT false,
    notes            text,
    conditions       jsonb        NOT NULL,          -- [{tf, indicator, params, test, inputs, join}, ...]
    created_at       timestamptz  NOT NULL DEFAULT now(),
    updated_at       timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_trade_plan_user ON trade_plan (user_id, updated_at DESC);

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

CREATE TABLE IF NOT EXISTS trade_alert (
    alert_id        bigserial    PRIMARY KEY,
    user_id         bigint       NOT NULL,
    plan_id         bigint,                           -- the plan it was set from (for the person's reference; the plan may be deleted later)
    plan_name       text         NOT NULL,
    scope           text         NOT NULL CHECK (scope IN ('condition', 'set', 'plan')),
    instrument      text         NOT NULL,
    isin            text,                             -- the stock; NULL for an instrument that is not a stock
    symbol          text,
    company_name    text,
    for_view        text         NOT NULL CHECK (for_view IN ('bullish', 'bearish')),
    spec            jsonb        NOT NULL,            -- {"conditions": [...], "closed_only": bool}, AS EVALUATED (already mirrored)
    sentence        text         NOT NULL,            -- what the person is told it is watching for
    status          text         NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'triggered', 'paused')),
    note            text,                             -- why it was paused, if it was
    last_checked_at timestamptz,
    last_stamp      text,                             -- newest candle date + today at the last look: a new day or new candle means look again
    last_state      text,                             -- met | not_met | no_data at the last look
    triggered_at    timestamptz,
    trigger_reading jsonb,                            -- what each condition read when it became met
    email_status    text         NOT NULL DEFAULT 'pending' CHECK (email_status IN ('pending', 'sent', 'failed')),
    email_attempts  integer      NOT NULL DEFAULT 0,
    email_error     text,
    email_sent_at   timestamptz,
    created_at      timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_trade_alert_user ON trade_alert (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_trade_alert_status ON trade_alert (status);

CREATE TABLE IF NOT EXISTS trade_alert_run (
    run_id      bigserial    PRIMARY KEY,
    started_at  timestamptz  NOT NULL DEFAULT now(),
    checked     integer      NOT NULL DEFAULT 0,      -- alerts actually evaluated
    skipped     integer      NOT NULL DEFAULT 0,      -- alerts left alone because nothing had changed since the last look
    fired       integer      NOT NULL DEFAULT 0,      -- alerts whose condition became met this run
    emailed     integer      NOT NULL DEFAULT 0,
    failed      integer      NOT NULL DEFAULT 0,      -- emails that could not be sent (they are retried)
    error       text
);
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
GRANT SELECT, INSERT, UPDATE, DELETE ON trade_plan, indicator_request, trade_alert, trade_alert_run TO "{u}";
GRANT USAGE, SELECT ON SEQUENCE trade_plan_plan_id_seq, indicator_request_request_id_seq, trade_alert_alert_id_seq,
                                 trade_alert_run_run_id_seq TO "{u}";
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
        print("  %-20s %s" % (t, "present" if there else "MISSING"))
        if not there:
            ok = False
            continue
        cur.execute("SELECT count(*) FROM " + t)
        print("  %-20s %d rows" % ("", cur.fetchone()[0]))
        if t in USER_TABLES:
            cur.execute("""SELECT count(*) FROM pg_constraint
                           WHERE conrelid = %s::regclass AND contype = 'f'""", (t,))
            print("  %-20s %s" % ("", "linked to mf_user" if cur.fetchone()[0] else
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
