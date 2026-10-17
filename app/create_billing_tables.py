"""
create_billing_tables.py -- the tables payments.py writes to.
---------------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_billing_tables.py --check
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_billing_tables.py --admin doadmin

SAFE TO RE-RUN. Every statement is IF NOT EXISTS.

billing_subscription   one row per Razorpay subscription we started
billing_event          one row per webhook delivery; event_id is the PRIMARY
                       KEY, and that is the whole defence against a retried
                       webhook extending somebody's access twice
billing_invoice        one per captured payment (tax invoice or receipt)
billing_customer       the customer's own billing name / GSTIN / address

No foreign key to mf_user, on purpose: deleting an account must not silently
delete the record of what that person was charged.
"""

import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

TABLES = ["billing_subscription", "billing_event", "billing_invoice", "billing_customer"]

CREATE = [
    """
    CREATE TABLE IF NOT EXISTS billing_subscription (
        subscription_id text        PRIMARY KEY,
        user_id         bigint      NOT NULL,
        plan_id         text        NOT NULL,
        status          text        NOT NULL DEFAULT 'created',
        current_end     date,
        last_payment_id text,
        created_at      timestamptz NOT NULL DEFAULT now(),
        updated_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS billing_event (
        event_id        text        PRIMARY KEY,
        event           text        NOT NULL,
        subscription_id text,
        payment_id      text,
        payload         jsonb       NOT NULL,
        received_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    """CREATE INDEX IF NOT EXISTS ix_billing_sub_user
         ON billing_subscription (user_id, created_at DESC)""",
    # Added after first deploy. True once the customer has asked to cancel;
    # access still runs to the end of the paid period.
    """ALTER TABLE billing_subscription
         ADD COLUMN IF NOT EXISTS cancel_requested boolean NOT NULL DEFAULT false""",
    # Tiers (Oct 2026). `scope` on a subscription is the tier it was bought
    # for ('mf', 'stocks', 'all'), written when it is started so a plan id
    # renamed in .env later cannot change what somebody paid for. The same
    # word on the account is what the gates read; NULL there means "all",
    # so every account granted before tiers existed keeps everything.
    """ALTER TABLE billing_subscription
         ADD COLUMN IF NOT EXISTS scope text""",
    """ALTER TABLE mf_user
         ADD COLUMN IF NOT EXISTS premium_scope text""",
    # Invoices (Oct 2026). One per captured payment; payment_id UNIQUE is
    # what stops a replayed webhook issuing a second invoice. fy + seq is
    # the gap-free run per financial year that the number is built from.
    """
    CREATE TABLE IF NOT EXISTS billing_invoice (
        invoice_no      text        PRIMARY KEY,
        fy              text        NOT NULL,
        seq             integer     NOT NULL,
        user_id         bigint      NOT NULL,
        subscription_id text,
        payment_id      text        NOT NULL UNIQUE,
        event_id        text,
        issued_on       date        NOT NULL,
        tax_invoice     boolean     NOT NULL DEFAULT false,
        plan_name       text,
        plan_label      text,
        scope           text,
        period_start    date,
        period_end      date,
        currency        text        NOT NULL DEFAULT 'INR',
        gross_paise     bigint      NOT NULL,
        taxable_paise   bigint      NOT NULL,
        cgst_paise      bigint      NOT NULL DEFAULT 0,
        sgst_paise      bigint      NOT NULL DEFAULT 0,
        igst_paise      bigint      NOT NULL DEFAULT 0,
        gst_rate        numeric(5,2) NOT NULL DEFAULT 0,
        place_of_supply text,
        method          text,
        supplier        jsonb       NOT NULL,
        customer        jsonb       NOT NULL,
        created_at      timestamptz NOT NULL DEFAULT now(),
        UNIQUE (fy, seq)
    )
    """,
    """CREATE INDEX IF NOT EXISTS ix_billing_invoice_user
         ON billing_invoice (user_id, issued_on DESC)""",
    # What the customer wants on the invoice: a business name, GSTIN,
    # address and state. Filled in from "My subscription"; copied onto
    # each invoice at issue time, never applied to old ones.
    """
    CREATE TABLE IF NOT EXISTS billing_customer (
        user_id     bigint      PRIMARY KEY,
        legal_name  text,
        gstin       text,
        address     text,
        state_code  text,
        updated_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
]

# payments.py updates subscriptions but only ever inserts events.
GRANTS = [
    "GRANT SELECT, INSERT, UPDATE ON billing_subscription TO %s",
    "GRANT SELECT, INSERT ON billing_event TO %s",
    # Invoices are never updated or deleted by the app: a wrong one is
    # answered with a credit note by hand, not an edit.
    "GRANT SELECT, INSERT ON billing_invoice TO %s",
    "GRANT SELECT, INSERT, UPDATE ON billing_customer TO %s",
]


def site_dsn():
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")
    return dsn


def admin_dsn(user):
    d = conninfo_to_dict(site_dsn())
    print(f"\nConnecting to {d.get('host', '(unknown host)')} as '{user}'.")
    print("The password is in your database provider's control panel.")
    print("It is not shown as you type it.")
    pw = getpass.getpass(f"{user} password: ")
    if not pw:
        sys.exit("No password given. Nothing was changed.")
    d["user"] = user
    d["password"] = pw
    return make_conninfo(**d)


def app_user():
    return conninfo_to_dict(site_dsn()).get("user")


def report(cur):
    cur.execute("SELECT current_database(), current_user")
    db, me = cur.fetchone()
    print(f"\nconnected to '{db}' as '{me}'\n")
    ok = True
    for t in TABLES:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (t,))
        if not cur.fetchone()[0]:
            print(f"  {t:<24}MISSING")
            ok = False
            continue
        cur.execute(f"SELECT count(*) FROM {t}")
        n = cur.fetchone()[0]
        cur.execute("SELECT has_table_privilege(%s,%s,'SELECT'),"
                    "has_table_privilege(%s,%s,'INSERT')", (me, t, me, t))
        sel, ins = cur.fetchone()
        print(f"  {t:<24}present, {n:,} rows, "
              f"read/write by {me}: {'yes' if sel and ins else 'NO'}")
        ok = ok and sel and ins
    for t, c in (("billing_subscription", "scope"), ("mf_user", "premium_scope")):
        cur.execute("""SELECT 1 FROM information_schema.columns
                       WHERE table_name = %s AND column_name = %s""", (t, c))
        have = cur.fetchone() is not None
        print(f"  {t + '.' + c:<36}{'present' if have else 'MISSING (tiers need it)'}")
        ok = ok and have
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
            admin = (args[i + 1] if i + 1 < len(args)
                     and not args[i + 1].startswith("-") else "doadmin")

    with psycopg.connect(admin_dsn(admin) if admin else site_dsn()) as conn, \
            conn.cursor() as cur:
        cur.execute("SELECT current_user")
        who = cur.fetchone()[0]
        print(f"\nrunning as '{who}'")
        for ddl in CREATE:
            cur.execute(ddl)
        print("  tables and index ready")

        me_app = app_user()
        if me_app and who != me_app:
            for g in GRANTS:
                cur.execute(g % f'"{me_app}"')
            print(f"  granted access to {me_app}")
        conn.commit()

    # Verify as the APPLICATION login, not the admin who did the work.
    print("\n--- verifying as the application login ---")
    with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
        ok = report(cur)
    print("\nDone.\n" if ok else
          "\nSomething is missing or the application cannot write to it.\n")


if __name__ == "__main__":
    main()
