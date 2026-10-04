"""
create_distributor_tables.py -- the distributor tier's storage.
----------------------------------------------------------------
Run once:
    /opt/mfapi/venv/bin/python3 /opt/mfapi/create_distributor_tables.py

TWO TABLES

distributor   one row per user who acts as a distributor. Holds the ARN and
              the name and contact that appear on reports. Today those three
              strings live in each browser's localStorage, which means they
              are retyped on a new machine and lost when a cache is cleared.

client        the people a distributor advises. Owned by a user, and
              portfolio.client_id -- already in the schema, unused until now
              -- points here, so a client's portfolios are the ordinary
              portfolio rows with an owner beside them.

WHY YEAR OF BIRTH AND NOT AGE
    An age is wrong within a year of being typed and goes on being wrong
    silently. A birth year is right forever, computes the age whenever it is
    needed, and is less identifying than a full date of birth -- which this
    platform has no use for, since it does no KYC.

ON HOLDING OTHER PEOPLE'S DATA
    These rows are personal data about individuals who are not users of this
    site, which is a different obligation from holding a user's own account.
    Two things are built in rather than added later:

      consent_on   when the client agreed to their details being held here.
                   Nullable, because it is the distributor's judgement
                   whether to record it -- but a column that exists can be
                   filled in, and one that does not requires a migration
                   across a live client book.

      ON DELETE CASCADE on the portfolio link, so deleting a client really
      removes them rather than leaving orphaned rows that still name them.
"""

import os
import psycopg
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")

DDL = """
CREATE TABLE IF NOT EXISTS distributor (
    user_id       bigint PRIMARY KEY,
    display_name  text,
    arn           text,
    contact       text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS client (
    client_id     bigserial PRIMARY KEY,
    owner_user_id bigint NOT NULL,
    name          text   NOT NULL,
    birth_year    int    CHECK (birth_year BETWEEN 1900 AND 2100),
    purpose       text,
    goal_note     text,
    consent_on    date,
    archived      boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_client_owner
    ON client (owner_user_id, archived, name);

"""

# Kept separate and attempted AFTER the tables are committed.
#
# ALTER TABLE requires ownership of portfolio, which the application's
# database role does not have. Run inside the main block it took the two
# new tables down with it -- one transaction, one failure, nothing created.
#
# The constraint is a nicety, not a requirement: delete_client() in
# distributor_api.py removes a client's portfolios explicitly, so deletion
# is complete with or without it.
# BOTH of these touch the portfolio table, and BOTH need ownership of it --
# CREATE INDEX no less than ALTER TABLE. Leaving the index in the main DDL
# reproduced the identical failure with a different statement, which is why
# the first fix did not work.
OPTIONAL = [
    ("foreign key", """
        ALTER TABLE portfolio
            ADD CONSTRAINT portfolio_client_fk
            FOREIGN KEY (client_id) REFERENCES client(client_id)
            ON DELETE CASCADE
     """),
    ("index", """
        CREATE INDEX IF NOT EXISTS ix_portfolio_client
            ON portfolio (client_id) WHERE client_id IS NOT NULL
     """),
]

with psycopg.connect(os.getenv("FINCHAYA_DB")) as conn, conn.cursor() as cur:
    cur.execute(DDL)
    conn.commit()
    print("tables created.")

    # Each in its OWN transaction, so one permission error cannot undo the
    # tables above or stop the other from being attempted.
    cur.execute("""SELECT 1 FROM pg_constraint
                   WHERE conname = 'portfolio_client_fk'""")
    have_fk = bool(cur.fetchone())

    skipped = []
    for label, sql in OPTIONAL:
        if label == "foreign key" and have_fk:
            print("foreign key already in place.")
            continue
        try:
            cur.execute(sql)
            conn.commit()
            print("%s added." % label)
        except Exception as exc:
            conn.rollback()
            skipped.append((label, sql, str(exc).strip().splitlines()[0]))

    if skipped:
        cur.execute("""SELECT tableowner FROM pg_tables
                       WHERE tablename = 'portfolio'""")
        owner = (cur.fetchone() or ["unknown"])[0]
        print("\n--- skipped, and that is fine ---")
        for label, sql, why in skipped:
            print("  %-12s %s" % (label, why))
        print("\nBoth need ownership of the portfolio table, which is owned by")
        print("'%s' while the app connects as a different role." % owner)
        print("Nothing depends on them: deleting a client removes their")
        print("portfolios in application code, and the index only affects speed")
        print("at a scale you are nowhere near.")
        print("\nTo add them anyway, run these as %s:" % owner)
        for _, sql, _ in skipped:
            print("   %s;" % " ".join(sql.split()))
    for table in ("distributor", "client"):
        cur.execute("""SELECT column_name, data_type
                       FROM information_schema.columns
                       WHERE table_name = %s ORDER BY ordinal_position""",
                    (table,))
        print("\n%s:" % table)
        for name, kind in cur.fetchall():
            print("   %-14s %s" % (name, kind))
    cur.execute("""SELECT conname FROM pg_constraint
                   WHERE conname = 'portfolio_client_fk'""")
    print("\nportfolio.client_id -> client:",
          "linked" if cur.fetchone() else "NOT LINKED")
