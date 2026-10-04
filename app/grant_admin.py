"""
grant_admin.py -- who is allowed to edit the allocation rules.
---------------------------------------------------------------
Create the table and make yourself an admin:

    /opt/mfapi/venv/bin/python3 /opt/mfapi/grant_admin.py --email you@example.com

List who has it:

    /opt/mfapi/venv/bin/python3 /opt/mfapi/grant_admin.py --list

Take it away:

    /opt/mfapi/venv/bin/python3 /opt/mfapi/grant_admin.py --revoke someone@example.com

WHY A SEPARATE TABLE AND NOT A FLAG ON mf_user
    Two reasons. Adding a column to mf_user needs ownership of that table,
    and this deployment has already shown that the application's database
    role does not own everything -- the same wall the portfolio foreign key
    hit. And a short list of admins in its own table is easier to audit
    than a boolean spread across every user row: the answer to "who can
    change the rules" is one SELECT with a handful of rows.

WHY THIS IS GRANTED FROM A TERMINAL AND NOT FROM THE SITE
    There is no screen for making someone an admin, deliberately. A page
    that grants privileges is a page that can be tricked into granting
    them. Adding an admin should require access to the server, which is a
    much smaller set of people than "anyone who can sign in".
"""

import os
import sys

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")

DDL = """
CREATE TABLE IF NOT EXISTS app_admin (
    user_id   bigint PRIMARY KEY,
    note      text,
    added_at  timestamptz NOT NULL DEFAULT now()
);

-- NULL owner means the house default that everyone falls back to; a user
-- id would mean that distributor's own override. Nothing reads it yet.
-- The column is added now because altering a table with live rules in it
-- later is a migration, and adding a nullable column today is free.
ALTER TABLE allocation_rule
    ADD COLUMN IF NOT EXISTS owner_user_id bigint;
"""


def find_user(cur, email):
    cur.execute("SELECT user_id, email FROM mf_user WHERE lower(email) = lower(%s)",
                (email,))
    return cur.fetchone()


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()

        if "--list" in args:
            cur.execute("""
                SELECT a.user_id, u.email, a.note, a.added_at
                FROM app_admin a
                LEFT JOIN mf_user u ON u.user_id = a.user_id
                ORDER BY a.added_at
            """)
            rows = cur.fetchall()
            if not rows:
                print("No admins. Nobody can edit the allocation rules.")
            for r in rows:
                print("%-8s %-34s %s" % (r["user_id"], r["email"] or "?",
                                         r["added_at"].date()))
            return

        if "--revoke" in args:
            email = args[args.index("--revoke") + 1]
            u = find_user(cur, email)
            if not u:
                sys.exit("No user with that email.")
            cur.execute("DELETE FROM app_admin WHERE user_id = %s", (u["user_id"],))
            conn.commit()
            print("revoked:", u["email"])
            cur.execute("SELECT COUNT(*) AS n FROM app_admin")
            if cur.fetchone()["n"] == 0:
                # Locking everyone out is recoverable, but only from here,
                # so say it rather than let it be discovered.
                print("WARNING: there are now no admins. Nobody can edit the "
                      "rules until you grant it again from this script.")
            return

        if "--email" in args:
            email = args[args.index("--email") + 1]
            u = find_user(cur, email)
            if not u:
                sys.exit("No user with that email. They have to sign in to "
                         "the site once before they can be made an admin.")
            note = args[args.index("--note") + 1] if "--note" in args else None
            cur.execute("""
                INSERT INTO app_admin (user_id, note) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET note = COALESCE(
                    EXCLUDED.note, app_admin.note)
            """, (u["user_id"], note))
            conn.commit()
            print("admin:", u["email"], "(user_id %s)" % u["user_id"])
            return

        sys.exit(__doc__)


if __name__ == "__main__":
    main()
