"""
referral_report.py -- who has referred whom.
---------------------------------------------------------------------------
Read-only. Run it on the server to see the referral numbers and to check a
test: did the friend's sign-up get credited to the right person?

    cd /opt/mfapi
    venv/bin/python3 referral_report.py                    # summary + recent
    venv/bin/python3 referral_report.py --email a@x.com    # one member's code
                                                           # and who joined

Like grant_admin.py and grant_premium.py this is a terminal tool, not a page:
it shows email addresses, and a page that lists who invited whom is a page
that has to be locked down. Nothing here can change anything.

THE TABLES DO NOT EXIST UNTIL SOMEONE OPENS "REFER A FRIEND". They are
created by the website on first use, so a report on a site nobody has shared
from yet says so instead of failing.
"""

import argparse
import os
import sys

import psycopg
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv("/opt/mfapi/.env")
load_dotenv(os.path.join(HERE, ".env"))
DB = os.getenv("FINCHAYA_DB")


def tables_exist(cur):
    cur.execute("""SELECT to_regclass('referral_code') IS NOT NULL,
                          to_regclass('referral') IS NOT NULL""")
    return all(cur.fetchone())


def summary(cur, limit=20):
    cur.execute("SELECT count(*) FROM referral_code")
    codes = cur.fetchone()[0]
    cur.execute("SELECT count(*), count(DISTINCT referrer_id) FROM referral")
    joined, referrers = cur.fetchone()
    print("Members with a referral code:      %d" % codes)
    print("Friends credited to a member:      %d" % joined)
    print("Members who brought at least one:  %d" % referrers)

    cur.execute("""
        SELECT u.email, c.code, count(r.referee_id) AS joined
        FROM referral_code c
        JOIN mf_user u ON u.user_id = c.user_id
        LEFT JOIN referral r ON r.referrer_id = c.user_id
        GROUP BY u.email, c.code
        ORDER BY joined DESC, u.email
        LIMIT %s""", (limit,))
    rows = cur.fetchall()
    if rows:
        print("\n%-38s %-10s %s" % ("member", "code", "friends joined"))
        for email, code, n in rows:
            print("%-38s %-10s %d" % (email[:38], code, n))

    cur.execute("""
        SELECT r.created_at::timestamp(0), a.email, b.email
        FROM referral r
        LEFT JOIN mf_user a ON a.user_id = r.referrer_id
        LEFT JOIN mf_user b ON b.user_id = r.referee_id
        ORDER BY r.created_at DESC LIMIT %s""", (limit,))
    rows = cur.fetchall()
    if rows:
        print("\nMost recent referrals:")
        print("%-20s %-34s %s" % ("when", "referred by", "friend who joined"))
        for when, ref, friend in rows:
            print("%-20s %-34s %s" % (when, (ref or "(gone)")[:34],
                                      friend or "(gone)"))
    else:
        print("\nNobody has joined through a link yet.")


def one(cur, email):
    cur.execute("SELECT user_id, email FROM mf_user WHERE lower(email) = lower(%s)",
                (email,))
    u = cur.fetchone()
    if not u:
        print("No account with the email %s." % email)
        return
    uid, em = u
    cur.execute("SELECT code, created_at::timestamp(0) FROM referral_code "
                "WHERE user_id = %s", (uid,))
    row = cur.fetchone()
    if not row:
        print("%s has not opened Refer a friend yet, so has no code." % em)
    else:
        print("%s\n  code: %s   (made %s)" % (em, row[0], row[1]))
    cur.execute("""
        SELECT r.created_at::timestamp(0), b.email
        FROM referral r LEFT JOIN mf_user b ON b.user_id = r.referee_id
        WHERE r.referrer_id = %s ORDER BY r.created_at""", (uid,))
    rows = cur.fetchall()
    print("  friends who joined through them: %d" % len(rows))
    for when, friend in rows:
        print("    %s  %s" % (when, friend or "(gone)"))
    cur.execute("""
        SELECT r.created_at::timestamp(0), a.email, r.code
        FROM referral r LEFT JOIN mf_user a ON a.user_id = r.referrer_id
        WHERE r.referee_id = %s""", (uid,))
    row = cur.fetchone()
    print("  they joined through: %s" % (
        "%s (code %s, %s)" % (row[1] or "(gone)", row[2], row[0])
        if row else "nobody"))


def main():
    ap = argparse.ArgumentParser(description="Who has referred whom.")
    ap.add_argument("--email", help="show one member's code and their friends")
    args = ap.parse_args()
    if not DB:
        sys.exit("FINCHAYA_DB is not set. Is /opt/mfapi/.env present?")

    with psycopg.connect(DB) as conn:
        conn.read_only = True                # this script cannot change anything
        with conn.cursor() as cur:
            if not tables_exist(cur):
                print("No referral activity yet: nobody has opened "
                      "'Refer a friend' since this went live.")
                return
            if args.email:
                one(cur, args.email)
            else:
                summary(cur)


if __name__ == "__main__":
    main()
