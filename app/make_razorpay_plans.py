"""
make_razorpay_plans.py -- create the subscription plans in Razorpay and
print the .env lines payments.py reads.
---------------------------------------------------------------------------
    /opt/mfapi/venv/bin/python3 /opt/mfapi/make_razorpay_plans.py --dry-run
    /opt/mfapi/venv/bin/python3 /opt/mfapi/make_razorpay_plans.py
    /opt/mfapi/venv/bin/python3 /opt/mfapi/make_razorpay_plans.py --only stocks,all

Uses RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET from /opt/mfapi/.env (or the
.env next to this file), so it creates plans in whichever MODE those keys
belong to -- Live keys make Live plans, Test keys make Test plans. Plan
ids differ between the two modes; run it once per mode you use.

SAFE TO RE-RUN. Before creating a plan it lists the plans already on the
account and reuses one with the same name, period and amount. Nothing is
charged, nothing is deleted. The mutual-fund plans already exist on the
account (made by hand on 3 Oct 2026), which is why the default --only is
"stocks,all": pass --only mf,stocks,all to check or create every tier.

What it prints is meant to be pasted into /opt/mfapi/.env, after which
`systemctl restart mfapi` makes the new tiers appear on the site.
"""

import argparse
import os
import sys

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv("/opt/mfapi/.env")
load_dotenv(os.path.join(HERE, ".env"))

# amount is in PAISE. period/interval are Razorpay's words for "every month"
# and "every year".
SPECS = {
    "mf_monthly":     ("FinChaya Mutual funds - monthly",         14900,  "monthly", "RAZORPAY_PLAN_ID_MF_MONTHLY"),
    "mf_yearly":      ("FinChaya Mutual funds - yearly",          99900,  "yearly",  "RAZORPAY_PLAN_ID_MF_YEARLY"),
    "stocks_monthly": ("FinChaya Stocks - monthly",               39900,  "monthly", "RAZORPAY_PLAN_ID_STOCKS_MONTHLY"),
    "stocks_yearly":  ("FinChaya Stocks - yearly",                299900, "yearly",  "RAZORPAY_PLAN_ID_STOCKS_YEARLY"),
    "all_monthly":    ("FinChaya Mutual funds + stocks - monthly", 59900, "monthly", "RAZORPAY_PLAN_ID_ALL_MONTHLY"),
    "all_yearly":     ("FinChaya Mutual funds + stocks - yearly",  349900, "yearly", "RAZORPAY_PLAN_ID_ALL_YEARLY"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="stocks,all",
                    help="comma-separated tiers to handle: mf, stocks, all "
                         "(default: stocks,all -- the mf plans already exist)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be created; create nothing")
    args = ap.parse_args()
    tiers = {t.strip() for t in args.only.split(",") if t.strip()}
    bad = tiers - {"mf", "stocks", "all"}
    if bad:
        sys.exit("Unknown tier(s): %s" % ", ".join(sorted(bad)))

    key, secret = os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key or not secret:
        sys.exit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set.")
    mode = "LIVE" if key.startswith("rzp_live_") else "TEST" if key.startswith("rzp_test_") else "unknown"
    print("Key %s... (%s mode). Plans made now belong to that mode." % (key[:12], mode))

    try:
        import razorpay
    except ImportError:
        sys.exit("pip install razorpay (in the venv) first.")
    client = razorpay.Client(auth=(key, secret))

    # Everything already on the account, so a re-run reuses instead of duplicating.
    existing = []
    skip = 0
    while True:
        page = client.plan.all({"count": 100, "skip": skip})
        items = page.get("items") or []
        existing.extend(items)
        if len(items) < 100:
            break
        skip += 100

    def find(name, amount, period):
        for p in existing:
            it = p.get("item") or {}
            if (it.get("name") == name and int(it.get("amount") or 0) == amount
                    and p.get("period") == period and int(p.get("interval") or 1) == 1):
                return p
        return None

    lines = []
    for plan_name, (name, amount, period, env) in SPECS.items():
        if plan_name.split("_")[0] not in tiers:
            continue
        have = find(name, amount, period)
        if have:
            print("  %-16s exists  %s  (%s, ₹%s)" % (plan_name, have["id"], period, amount // 100))
            lines.append("%s=%s" % (env, have["id"]))
            continue
        if args.dry_run:
            print("  %-16s WOULD CREATE  %s, ₹%s %s" % (plan_name, name, amount // 100, period))
            lines.append("%s=<made on the real run>" % env)
            continue
        made = client.plan.create({
            "period": period, "interval": 1,
            "item": {"name": name, "amount": amount, "currency": "INR",
                     "description": "FinChaya subscription"},
            "notes": {"tier": plan_name.split("_")[0], "cadence": period},
        })
        print("  %-16s created %s  (%s, ₹%s)" % (plan_name, made["id"], period, amount // 100))
        lines.append("%s=%s" % (env, made["id"]))

    print("\nPaste into /opt/mfapi/.env, then: systemctl restart mfapi\n")
    print("\n".join(lines))
    print()


if __name__ == "__main__":
    main()
