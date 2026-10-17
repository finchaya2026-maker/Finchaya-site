"""
create_portfolio_monitor_tables.py -- the tables behind the monthly plan report and the plan alerts.
---------------------------------------------------------------------------------------------------
Run once. Safe to re-run: every statement is IF NOT EXISTS. Needs create_alert_rules.py to have been run first (portfolio_alert).

    cd /opt/mfapi && venv/bin/python3 create_portfolio_monitor_tables.py --admin doadmin
    venv/bin/python3 create_portfolio_monitor_tables.py --check          # read-only: are the tables there, and can the site use them?

The site's own database login cannot create tables ("permission denied for schema public"), so run it with --admin doadmin: it asks for the
admin password (from the database provider's control panel; not shown as you type, not stored) and grants the site's login access to what it made.
This is the same pattern as create_stock_alert_tables.py and create_billing_tables.py.

WHAT EACH TABLE IS FOR
    portfolio_report_pref   one row per portfolio that has changed its monthly-report setting. No row means the default: ON.
                            A report is a service the person set up by locking a plan, and the lock dialog says so.
    portfolio_report_log    one row per portfolio per month: what was sent, when, and a snapshot of the numbers it carried.
                            The snapshot is what next month's report compares against ("moved from the top quarter to the
                            second"), so a report never has to rebuild last month from data that may have been revised.
                            The (portfolio, month) key is also what stops a second email going out in the same month.
    portfolio_alert_event   one row per flagged condition: a fund behind its benchmark three month ends running, a fund in the
                            bottom half of its category three month ends running, a pair of funds overlapping above the line.
                            state = 'open' while the condition holds and 'cleared' once it stops, so the same condition is
                            told once and not every night, and told again if it clears and then returns.
    portfolio_monitor_run   one row per run of the checker, so "has it run, and what did it do" is a query, not a guess.
"""

import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

TABLES = ["portfolio_report_pref", "portfolio_report_log", "portfolio_alert_event", "portfolio_monitor_run"]
SEQUENCES = ["portfolio_alert_event_event_id_seq", "portfolio_monitor_run_run_id_seq"]

DDL = """
CREATE TABLE IF NOT EXISTS portfolio_report_pref (
    portfolio_id  int         PRIMARY KEY REFERENCES portfolio(portfolio_id) ON DELETE CASCADE,
    monthly       boolean     NOT NULL DEFAULT true,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS portfolio_report_log (
    portfolio_id  int         NOT NULL REFERENCES portfolio(portfolio_id) ON DELETE CASCADE,
    report_month  date        NOT NULL,           -- the first day of the month the report is FOR (its data is as at the month end before)
    status        text        NOT NULL,           -- sent | failed | skipped
    detail        text,
    subject       text,
    snapshot      jsonb,
    attempts      int         NOT NULL DEFAULT 0,
    sent_at       timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_id, report_month),
    CONSTRAINT portfolio_report_log_status_ck CHECK (status IN ('sent', 'failed', 'skipped'))
);

CREATE TABLE IF NOT EXISTS portfolio_alert_event (
    event_id        bigserial   PRIMARY KEY,
    portfolio_id    int         NOT NULL REFERENCES portfolio(portfolio_id) ON DELETE CASCADE,
    rule_key        text        NOT NULL,         -- behind_benchmark | rank_slip | overlap_high
    scheme_code     text        NOT NULL,         -- the fund; for a pair, the lower code
    other_code      text        NOT NULL DEFAULT '',   -- the other fund of a pair, '' otherwise
    state           text        NOT NULL DEFAULT 'open',
    first_seen_on   date        NOT NULL,
    last_seen_on    date        NOT NULL,
    cleared_on      date,
    sentence        text        NOT NULL,         -- the plain-English reading, as it was told
    evidence        jsonb,
    email_status    text        NOT NULL DEFAULT 'pending',
    email_attempts  int         NOT NULL DEFAULT 0,
    email_error     text,
    email_sent_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT portfolio_alert_event_state_ck CHECK (state IN ('open', 'cleared')),
    CONSTRAINT portfolio_alert_event_email_ck CHECK (email_status IN ('pending', 'sent', 'failed', 'off'))
);
-- one OPEN event per condition: this is what makes "tell it once" a property of the data, not of the code's good behaviour
CREATE UNIQUE INDEX IF NOT EXISTS portfolio_alert_event_open
    ON portfolio_alert_event (portfolio_id, rule_key, scheme_code, other_code) WHERE state = 'open';
CREATE INDEX IF NOT EXISTS portfolio_alert_event_pending
    ON portfolio_alert_event (portfolio_id) WHERE email_status IN ('pending', 'failed');

CREATE TABLE IF NOT EXISTS portfolio_monitor_run (
    run_id      bigserial   PRIMARY KEY,
    kind        text        NOT NULL,             -- alerts | monthly
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    checked     int         NOT NULL DEFAULT 0,
    flagged     int         NOT NULL DEFAULT 0,
    emailed     int         NOT NULL DEFAULT 0,
    failed      int         NOT NULL DEFAULT 0,
    note        text,
    error       text
);
CREATE INDEX IF NOT EXISTS portfolio_monitor_run_recent ON portfolio_monitor_run (kind, started_at DESC);
"""

GRANTS = """
GRANT SELECT, INSERT, UPDATE, DELETE ON portfolio_report_pref, portfolio_report_log, portfolio_alert_event, portfolio_monitor_run TO "{u}";
GRANT USAGE, SELECT ON SEQUENCE portfolio_alert_event_event_id_seq, portfolio_monitor_run_run_id_seq TO "{u}";
"""


def site_dsn():
    dsn = os.environ.get("FINCHAYA_DB") or os.environ.get("DATABASE_URL") or os.environ.get("MF_DSN")
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")
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
        print("  %-24s %s" % (t, "present" if there else "MISSING"))
        if not there:
            ok = False
            continue
        cur.execute("""SELECT has_table_privilege(current_user, %s, 'SELECT'), has_table_privilege(current_user, %s, 'INSERT'),
                              has_table_privilege(current_user, %s, 'UPDATE'), has_table_privilege(current_user, %s, 'DELETE')""", (t,) * 4)
        good = all(cur.fetchone())
        print("  %-24s usable by the site's login: %s" % ("", "yes" if good else "NO -- run with --admin doadmin"))
        ok = ok and good
    for seq in SEQUENCES:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (seq,))
        if not cur.fetchone()[0]:
            ok = False
            print("  sequence %-34s MISSING" % seq)
            continue
        cur.execute("SELECT has_sequence_privilege(current_user, %s, 'USAGE')", (seq,))
        good = cur.fetchone()[0]
        print("  sequence %-34s usable: %s" % (seq, "yes" if good else "NO -- run with --admin doadmin"))
        ok = ok and good
    cur.execute("SELECT to_regclass('portfolio_alert') IS NOT NULL")
    if not cur.fetchone()[0]:
        print("\n  NOTE: portfolio_alert is missing. Run create_alert_rules.py first.")
        ok = False
    return ok


def main():
    args = sys.argv[1:]
    if "--check" in args:
        with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
            ok = report(cur)
        print("\nRead-only check. Nothing was changed.\n")
        sys.exit(0 if ok else 1)

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
        site = app_user()
        if site and me != site:
            cur.execute(GRANTS.format(u=site))
            print("  granted table and sequence access to %s" % site)
        conn.commit()

    print("\n--- verifying as the site's own login ---")
    with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
        ok = report(cur)
    print("\nDone." if ok else "\nSomething above says NO or MISSING. Re-run with --admin doadmin.")
    print("\nNothing is sent by creating these. The checker (portfolio_monitor.py) does that, on the schedule in DEPLOY_ALERTS.md.\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
