"""
grant_premium.py -- who has a paid subscription.
--------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/grant_premium.py --email you@example.com
    /opt/mfapi/venv/bin/python3 /opt/mfapi/grant_premium.py --email x@y.com --days 365
    /opt/mfapi/venv/bin/python3 /opt/mfapi/grant_premium.py --list
    /opt/mfapi/venv/bin/python3 /opt/mfapi/grant_premium.py --revoke x@y.com

WHY THIS HAD TO EXIST BEFORE THE PAYWALL COULD BE TURNED ON
    `is_premium` and `premium_until` were READ in four places and written
    by nothing. Setting MF_PAYWALL_ENABLED=true without this would have
    made every account on the site non-premium at once -- including the
    owner's -- with no way back except hand-written SQL against a live
    database at whatever hour the mistake was noticed.

    mf8 recorded the same trap the other way round: "the paywall cannot
    ship before login works". Login works now. This is the other half.

WHY FROM A TERMINAL AND NOT FROM THE SITE
    Same reason as grant_admin.py: a page that grants privileges is a page
    that can be tricked into granting them. Granting a subscription should
    require access to the server, which is a far smaller set of people
    than "anyone who can sign in".

ON EXPIRY
    --days sets premium_until. Leaving it off grants access with NO expiry,
    which is right for the owner's own account and wrong for a customer.
    has_score_access() treats a NULL premium_until as "no end date", so an
    accidental open-ended grant is silent -- hence --list prints the date
    for every holder, and flags the ones that never end.
"""

import argparse
import os
import sys
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv("/opt/mfapi/.env")
DB = os.getenv("FINCHAYA_DB")

if not DB:
    sys.exit("FINCHAYA_DB is not set. Is /opt/mfapi/.env present?")


def find_user(cur, email):
    cur.execute("SELECT user_id, email, is_premium, premium_until "
                "FROM mf_user WHERE lower(email) = lower(%s)", (email,))
    return cur.fetchone()


def grant(cur, email, days):
    user = find_user(cur, email)
    if not user:
        # Deliberately not created here. An account exists once someone has
        # signed in; inventing a row would make a subscription for an
        # address that may be a typo, and it would never be noticed.
        sys.exit("No account for %s. They must sign in once first, then "
                 "run this again." % email)

    until = (date.today() + timedelta(days=days)) if days else None
    cur.execute("""
        UPDATE mf_user SET is_premium = true, premium_until = %s
        WHERE user_id = %s
    """, (until, user["user_id"]))
    print("Granted: %s" % user["email"])
    print("   until: %s" % (until if until else "no expiry"))
    if not until:
        print("   NOTE: no end date. Use --days for a customer.")


def revoke(cur, email):
    user = find_user(cur, email)
    if not user:
        sys.exit("No account for %s." % email)
    cur.execute("UPDATE mf_user SET is_premium = false, premium_until = NULL "
                "WHERE user_id = %s", (user["user_id"],))
    print("Revoked: %s" % user["email"])


def show(cur):
    # Everyone the gate currently lets through, and everyone it does not
    # but whose row still claims premium -- an expired subscription is not
    # the same state as a revoked one and should not look like it.
    cur.execute("""
        SELECT email, premium_until,
               (premium_until IS NULL OR premium_until >= CURRENT_DATE) AS live
        FROM mf_user WHERE is_premium = true
        ORDER BY premium_until NULLS FIRST, email
    """)
    rows = cur.fetchall()
    if not rows:
        print("Nobody has a subscription.")
        print("With MF_PAYWALL_ENABLED=true that means NOBODY can see "
              "scores, the funds tab, the look-through or rising.")
        return
    print("%-38s %-12s %s" % ("email", "until", "state"))
    for r in rows:
        print("%-38s %-12s %s" % (
            r["email"][:38],
            r["premium_until"] or "no expiry",
            "active" if r["live"] else "EXPIRED"))
    live = sum(1 for r in rows if r["live"])
    print("\n%d active, %d expired" % (live, len(rows) - live))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", help="grant a subscription to this account")
    ap.add_argument("--days", type=int, default=0,
                    help="how long it lasts. Omit for no expiry -- right "
                         "for your own account, wrong for a customer.")
    ap.add_argument("--revoke", help="take the subscription away")
    ap.add_argument("--list", action="store_true", dest="show",
                    help="who has one, and until when")
    args = ap.parse_args()

    if not (args.email or args.revoke or args.show):
        ap.print_help()
        return

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        if args.email:
            grant(cur, args.email, args.days)
        if args.revoke:
            revoke(cur, args.revoke)
        conn.commit()
        if args.show:
            show(cur)

    # Printed after any change, because the failure this file exists to
    # prevent is turning the gate on with nobody behind it.
    if args.email or args.revoke:
        print("\nCheck who has access:  grant_premium.py --list")


main()
