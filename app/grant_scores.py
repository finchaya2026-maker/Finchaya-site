"""
grant_scores.py -- who sees scores and "rising" while they are switched off.
------------------------------------------------------------------------------
Scores, ranks and the rising / sideways / falling split are OFF for everyone
(MF_SCORES_ENABLED unset). This list shows them to a CHOSEN FEW anyway -- say
10 of your 100 subscribers -- without turning them on for the rest.

    cd /opt/mfapi

    # See your subscribers and who already has scores (pick from this):
    venv/bin/python3 grant_scores.py --subscribers

    # Give scores to one or more people (comma-separated, or repeat --email):
    venv/bin/python3 grant_scores.py --email a@x.com,b@y.com --note "beta 1"

    # Who has it now:
    venv/bin/python3 grant_scores.py --list

    # Take it away from some people, or from everyone:
    venv/bin/python3 grant_scores.py --revoke a@x.com
    venv/bin/python3 grant_scores.py --revoke-all --yes

NO RESTART NEEDED. The site re-reads this list about every 30 seconds, so a
grant or a revoke is live within the minute. (The API itself must have been
deployed once with the matching code -- after that, only this script.)

WHAT IT DOES, AND DOES NOT, CHANGE
    * It only ADDS people. It never hides scores from anyone the public switch
      (MF_SCORES_ENABLED=true) already shows them to. With that switch on,
      this list has no effect, and the script says so.
    * It is not a subscription. While the paywall is on (MF_PAYWALL_ENABLED),
      a listed person who is not an active subscriber is still shown the
      locked version. The script warns when you grant to such a person.
    * Signed-out visitors never get scores from this list; it needs a login.

WHY A SEPARATE TABLE AND NOT A COLUMN ON mf_user
    Same reasons as grant_admin.py: adding a column to mf_user needs ownership
    of that table, which the application's database role does not have, and a
    short list in its own table is easier to audit than a flag spread across
    every user row. "Who can see scores" is one SELECT. This script creates the
    table itself the first time it runs -- no admin login, no migration.

WHY FROM A TERMINAL AND NOT FROM THE SITE
    Same reason as grant_premium.py: a page that grants access is a page that
    can be tricked into granting it. Showing scores to someone should require
    access to the server.

WHY THIS MATTERS MORE THAN AN ORDINARY FEATURE FLAG
    The scores are withheld pending the SEBI Research Analyst registration.
    Showing them to even a few people is still showing them. That is a
    decision for you, and it is why nobody is on this list until you add them
    here, by name, on purpose.
"""

import argparse
import os
import sys

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv()

DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set. Is the .env file next to this script?")

SCORES_PUBLIC = os.getenv("MF_SCORES_ENABLED", "false").lower() == "true"
PAYWALL_ON = os.getenv("MF_PAYWALL_ENABLED", "false").lower() == "true"

DDL = """
CREATE TABLE IF NOT EXISTS mf_scores_access (
    user_id    bigint PRIMARY KEY,
    note       text,
    granted_at timestamptz NOT NULL DEFAULT now()
);
"""


def split_emails(values):
    out = []
    for v in values or []:
        out.extend(e.strip() for e in v.replace(";", ",").replace("\n", ",")
                   .split(",") if e.strip())
    return out


def find_user(cur, email):
    cur.execute("SELECT user_id, email, is_premium, premium_until "
                "FROM mf_user WHERE lower(email) = lower(%s)", (email,))
    return cur.fetchone()


def is_live_subscriber(u):
    from datetime import date
    return bool(u["is_premium"]) and (u["premium_until"] is None
                                      or u["premium_until"] >= date.today())


def grant(cur, emails, note):
    done = missing = 0
    for email in emails:
        u = find_user(cur, email)
        if not u:
            # Not created here: an account exists once someone has signed in,
            # and inventing one for a typo would never be noticed.
            print("  NOT FOUND  %s  (they must sign in once first)" % email)
            missing += 1
            continue
        cur.execute("""
            INSERT INTO mf_scores_access (user_id, note) VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE
               SET note = COALESCE(EXCLUDED.note, mf_scores_access.note)
        """, (u["user_id"], note))
        done += 1
        print("  granted    %s" % u["email"])
        if PAYWALL_ON and not is_live_subscriber(u):
            print("             NOTE: not an active subscriber, so the paywall "
                  "will still show them the locked version.")
    print("\n%d granted, %d not found." % (done, missing))


def revoke(cur, emails):
    done = 0
    for email in emails:
        u = find_user(cur, email)
        if not u:
            print("  NOT FOUND  %s" % email)
            continue
        cur.execute("DELETE FROM mf_scores_access WHERE user_id = %s",
                    (u["user_id"],))
        if cur.rowcount:
            done += 1
            print("  revoked    %s" % u["email"])
        else:
            print("  not on the list: %s" % u["email"])
    print("\n%d revoked." % done)


def show_list(cur):
    cur.execute("""
        SELECT a.user_id, u.email, u.is_premium, u.premium_until,
               a.note, a.granted_at
        FROM mf_scores_access a
        LEFT JOIN mf_user u ON u.user_id = a.user_id
        ORDER BY a.granted_at, u.email
    """)
    rows = cur.fetchall()
    if not rows:
        print("Nobody is on the list. Scores are shown to nobody.")
    else:
        print("%-36s %-12s %-11s %s" % ("email", "since", "subscriber", "note"))
        for r in rows:
            sub = ("-" if r["email"] is None else
                   "yes" if is_live_subscriber(r) else "no")
            print("%-36s %-12s %-11s %s" % (
                (r["email"] or "(account gone)")[:36],
                r["granted_at"].date(), sub, r["note"] or ""))
        print("\n%d on the list." % len(rows))
    status()


def show_subscribers(cur):
    cur.execute("""
        SELECT u.email, u.premium_until,
               (u.premium_until IS NULL OR u.premium_until >= CURRENT_DATE) AS live,
               (a.user_id IS NOT NULL) AS has_scores
        FROM mf_user u
        LEFT JOIN mf_scores_access a ON a.user_id = u.user_id
        WHERE u.is_premium = true
        ORDER BY has_scores DESC, u.email
    """)
    rows = cur.fetchall()
    if not rows:
        print("Nobody has a subscription.")
        status()
        return
    print("%-36s %-12s %-8s %s" % ("subscriber", "until", "state", "scores"))
    for r in rows:
        print("%-36s %-12s %-8s %s" % (
            r["email"][:36], r["premium_until"] or "no expiry",
            "active" if r["live"] else "EXPIRED",
            "YES" if r["has_scores"] else "-"))
    have = sum(1 for r in rows if r["has_scores"])
    print("\n%d subscribers, %d with scores." % (len(rows), have))
    status()


def status():
    if SCORES_PUBLIC:
        print("\nNOTE: MF_SCORES_ENABLED=true -- scores are already shown to "
              "EVERYONE, so this list changes nothing.")
    else:
        print("\nPublic switch is OFF (MF_SCORES_ENABLED unset): only the "
              "people above see scores.")


def main():
    ap = argparse.ArgumentParser(
        description="Show scores and rising to a chosen few while they are "
                    "switched off for everyone.")
    ap.add_argument("--email", action="append",
                    help="give scores to this account; comma-separated, or "
                         "repeat the flag")
    ap.add_argument("--note", help="a reminder stored with the grant")
    ap.add_argument("--revoke", action="append",
                    help="take scores away from this account")
    ap.add_argument("--revoke-all", action="store_true",
                    help="empty the list (needs --yes)")
    ap.add_argument("--yes", action="store_true",
                    help="confirm --revoke-all")
    ap.add_argument("--list", action="store_true", dest="show_list",
                    help="who has scores now")
    ap.add_argument("--subscribers", action="store_true",
                    help="every subscriber, marking who has scores")
    args = ap.parse_args()

    if not (args.email or args.revoke or args.revoke_all
            or args.show_list or args.subscribers):
        ap.print_help()
        return

    if args.revoke_all and not args.yes:
        sys.exit("--revoke-all removes everyone. Add --yes to confirm.")

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()

        if args.email:
            grant(cur, split_emails(args.email), args.note)
        if args.revoke:
            revoke(cur, split_emails(args.revoke))
        if args.revoke_all:
            cur.execute("DELETE FROM mf_scores_access")
            print("Removed everyone (%d)." % cur.rowcount)
        conn.commit()

        if args.show_list:
            show_list(cur)
        if args.subscribers:
            show_subscribers(cur)

    if args.email or args.revoke or args.revoke_all:
        print("\nLive within about 30 seconds. Check:  grant_scores.py --list")


main()
