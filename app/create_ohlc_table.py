"""
create_ohlc_table.py -- the daily candle store that makes incremental
fetching possible.
---------------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_ohlc_table.py --check
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_ohlc_table.py --admin doadmin

WHAT THIS IS FOR
    fetch_technicals.py downloads about 2,965 daily candles per stock,
    computes three indicator rows from them, and throws the candles
    away. Tomorrow it downloads all 2,965 again for the sake of one new
    day. That is the 64-minute run.

    Keeping the candles turns the nightly job into "fetch the days I do
    not already have", which is one day per stock. It also means a new
    indicator can be computed over eleven years of history without
    re-downloading anything, and a night that fails is recoverable
    instead of simply lost.

WHY THIS SCRIPT EXISTS WHEN ipo_breakout_schema.sql ALREADY DEFINES IT
    It defines the same table for the IPO screener, and if that file was
    ever run this script will find the table already present and change
    nothing. But the schema file has to be applied by hand with psql,
    and the IPO screener may never have been set up on this server. This
    checks rather than assumes, and it also does the part the .sql file
    does not: granting the application role access to a table created by
    the admin login.

WHY A SEPARATE TABLE AND NOT COLUMNS ON stock_technical
    stock_technical holds DERIVED indicators, one bar per stock per
    timeframe per run. Raw candles are a different shape with different
    history needs -- every trading day since listing, and volume, which
    has no meaning on a weekly or monthly indicator row. Keeping them
    apart means a bad candle backfill cannot corrupt fund scores.

WHO CAN RUN IT
    The application login (mfapp) usually cannot CREATE TABLE. --check
    tells you what is there and what is missing without changing
    anything. --admin connects as the cluster's admin login instead and
    asks for that password at the prompt, so it is never written down.

SAFE TO RE-RUN. Every statement is IF NOT EXISTS.
"""

import getpass
import os
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

TABLE = "stock_ohlc_daily"

# NUMERIC(14,4) rather than float: a price is money, and binary floating
# point cannot hold 1234.5675 exactly. The indicator maths converts to
# float anyway, but what is STORED should be what the exchange said.
CREATE = """
CREATE TABLE IF NOT EXISTS stock_ohlc_daily (
    isin        VARCHAR(12) NOT NULL,
    as_of_date  DATE        NOT NULL,
    open        NUMERIC(14, 4),
    high        NUMERIC(14, 4),
    low         NUMERIC(14, 4),
    close       NUMERIC(14, 4),
    volume      BIGINT,
    source      VARCHAR(20) DEFAULT 'KITE',
    created_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (isin, as_of_date)
)
"""

# The nightly job's first question for every stock is "what is the
# newest date I hold for you". That is a backwards scan of one stock's
# rows, and this index answers it from its first entry.
INDEX = """
CREATE INDEX IF NOT EXISTS ix_ohlc_isin_date_desc
    ON stock_ohlc_daily (isin, as_of_date DESC)
"""

# A table created by the admin login is owned by it, and the application
# role gets nothing by default. Without this the nightly job would fail
# with "permission denied for table stock_ohlc_daily" -- at 11:30pm,
# unattended, which is the worst possible time to discover a grant.
GRANT = "GRANT SELECT, INSERT, UPDATE ON stock_ohlc_daily TO %s"


def site_dsn():
    dsn = (os.environ.get("FINCHAYA_DB")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("MF_DSN"))
    if not dsn:
        sys.exit("No FINCHAYA_DB in /opt/mfapi/.env.")
    return dsn


def admin_dsn(user):
    """The site's connection string with a different login on it.

    Parsed with psycopg's own conninfo_to_dict, not urlparse. A
    PostgreSQL connection string comes in two shapes -- the URL one and
    the `host=... port=...` keyword one -- and managed providers hand
    out the keyword shape, which urlparse reads as a path with no host.

    The password is read with getpass: not echoed, not in shell history,
    not visible to anyone running `ps`.
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
    """Which role the website connects as -- the one that needs the grant."""
    d = conninfo_to_dict(site_dsn())
    return d.get("user")


def report(cur):
    cur.execute("SELECT current_database(), current_user")
    db, who = cur.fetchone()
    print(f"\nconnected to '{db}' as '{who}'")

    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (TABLE,))
    exists = cur.fetchone()[0]
    print(f"\n  {TABLE:<22}{'present' if exists else 'MISSING'}")

    if not exists:
        print("\n  Nothing is storing daily candles yet, so every nightly")
        print("  run re-downloads eleven years of history per stock.")
        return False

    cur.execute(f"""
        SELECT count(*), count(DISTINCT isin),
               min(as_of_date), max(as_of_date)
        FROM {TABLE}
    """)
    n, stocks, lo, hi = cur.fetchone()
    print(f"  {'rows':<22}{n:,}")
    print(f"  {'stocks':<22}{stocks:,}")
    print(f"  {'earliest candle':<22}{lo}")
    print(f"  {'latest candle':<22}{hi}")

    # A table you cannot write to is the same as no table, and the
    # difference only shows up at 11:30pm. Ask Postgres directly.
    me = app_user()
    if me:
        cur.execute("""
            SELECT has_table_privilege(%s, %s, 'SELECT'),
                   has_table_privilege(%s, %s, 'INSERT'),
                   has_table_privilege(%s, %s, 'UPDATE')
        """, (me, TABLE, me, TABLE, me, TABLE))
        sel, ins, upd = cur.fetchone()
        ok = sel and ins and upd
        print(f"\n  readable and writable by {me}: "
              f"{'yes' if ok else 'NO -- run with --admin'}")
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

    dsn = admin_dsn(admin) if admin else site_dsn()

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user")
        print(f"\nrunning as '{cur.fetchone()[0]}'")

        cur.execute(CREATE)
        print(f"  {TABLE} ready")
        cur.execute(INDEX)
        print("  index ready")

        # Grant to the application role -- but only when running as
        # somebody else. Granting yourself what you already own is noise.
        me_app = app_user()
        if me_app:
            cur.execute("SELECT current_user")
            if cur.fetchone()[0] != me_app:
                cur.execute(GRANT % f'"{me_app}"')
                print(f"  granted SELECT/INSERT/UPDATE to {me_app}")
        conn.commit()

    # VERIFY AS THE APPLICATION, not as the admin who just did the work.
    # An admin can always read its own table; that proves nothing about
    # whether the nightly job can.
    print("\n--- verifying as the application login ---")
    with psycopg.connect(site_dsn()) as conn, conn.cursor() as cur:
        ok = report(cur)

    if ok:
        print("\nDone. fetch_technicals.py can now store candles.\n")
    else:
        print("\nThe table exists but the application cannot write to it.")
        print("Re-run with --admin doadmin to apply the grant.\n")


if __name__ == "__main__":
    main()
