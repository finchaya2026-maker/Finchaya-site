"""
add_company_profile_columns.py -- one-time migration.
------------------------------------------------------------------------
    python add_company_profile_columns.py --check
    python add_company_profile_columns.py --admin doadmin

WHO CAN RUN THIS
    stock_master already exists and is owned by the admin login, same as
    every other pre-existing table on this server -- the app role
    (mfapp) can read and write its rows but cannot ALTER it. --check
    reports whether the columns are already there and writable; --admin
    connects as the cluster's admin login instead (password typed at a
    prompt, never stored) to actually add them. No GRANT step is needed
    afterward: the app role already has row-level access to every other
    column on this table, and a new column inherits the same table-level
    privilege.

WHY THESE LAND ON stock_master, NOT A NEW TABLE
    Each is a single current value per stock -- one description, one
    sector, one P/E -- the same shape industry and sector already are.
    A second table would mean a second join everywhere the profile is
    read, for no benefit: nothing here has a history worth keeping
    separately, unlike stock_score or stock_ohlc_daily, which store a
    row per day on purpose.

WHY pe_ratio/pb_ratio/eps/roe_pct ARE HERE ALREADY
    They ride along for free on the same BSE call fetched for sector
    classification (see backfill_company_profile.py) -- there was no
    separate cost to capturing them. They are NOT surfaced on the stock
    page yet; that is a deliberate, separate decision still open. Storing
    them now just means real numbers are already backfilling by the time
    that decision gets made, instead of starting from zero then.

SAFE TO RE-RUN. Every column uses ADD COLUMN IF NOT EXISTS.
"""

import getpass
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv
import os

load_dotenv("/opt/mfapi/.env")
load_dotenv()

TABLE = "stock_master"

COLUMNS = [
    ("description", "text"),
    ("description_source", "text"),
    ("description_updated_at", "timestamptz"),
    ("pe_ratio", "numeric"),
    ("pb_ratio", "numeric"),
    ("eps", "numeric"),
    ("roe_pct", "numeric"),
    ("fundamentals_updated_at", "timestamptz"),
    ("bse_scrip_code", "text"),
]

ALTER = "ALTER TABLE stock_master " + ", ".join(
    f"ADD COLUMN IF NOT EXISTS {name} {typ}" for name, typ in COLUMNS)


def site_dsn():
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in .env.")
    return dsn


def admin_dsn(user):
    d = conninfo_to_dict(site_dsn())
    print(f"\nConnecting to {d.get('host', '(unknown host)')} as '{user}'.")
    print("The password is in your database provider's control panel, "
          "not on this machine. It is not shown as you type it.")
    pw = getpass.getpass(f"{user} password: ")
    if not pw:
        sys.exit("No password given. Nothing was changed.")
    d["user"] = user
    d["password"] = pw
    return make_conninfo(**d)


def app_user():
    return conninfo_to_dict(site_dsn()).get("user")


def report(cur):
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (TABLE,))
    have = {r[0] for r in cur.fetchall()}
    missing = [name for name, _ in COLUMNS if name not in have]
    print(f"\n{len(COLUMNS) - len(missing)} of {len(COLUMNS)} profile "
          f"columns already present on {TABLE}.")
    if missing:
        print("missing: " + ", ".join(missing))
    me = app_user()
    if me:
        cur.execute("SELECT has_table_privilege(%s, %s, 'UPDATE')", (me, TABLE))
        writable = cur.fetchone()[0]
        print(f"{me} can write to {TABLE}: {'yes' if writable else 'NO'}")
    return not missing


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--check" in args:
        with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
            report(cur)
        print("\nRead-only check. Nothing was changed.\n")
        raise SystemExit

    admin = None
    for i, a in enumerate(args):
        if a == "--admin":
            admin = (args[i + 1] if i + 1 < len(args)
                      and not args[i + 1].startswith("-") else "doadmin")
    if not admin:
        sys.exit("Run with --check first, or --admin <role> to apply the "
                  "migration (e.g. --admin doadmin).")

    with psycopg.connect(admin_dsn(admin)) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user")
        print(f"\nrunning as '{cur.fetchone()[0]}'")
        cur.execute(ALTER)
        conn.commit()
        report(cur)
        print("\nDone.")
