"""
payments.py -- Razorpay Subscriptions, and the one rule that matters.
=====================================================================
Mounted in api.py:

    from payments import router as payments_router
    app.include_router(payments_router)

    pip install razorpay          (2.0.1 at the time of writing)

THE SETTINGS, ALL IN /opt/mfapi/.env, NONE IN THIS FILE:

    RAZORPAY_KEY_ID           public; the browser is given this
    RAZORPAY_KEY_SECRET       server only; never leaves this process
    RAZORPAY_WEBHOOK_SECRET   server only; a DIFFERENT secret from the
                              key secret, set when adding the webhook
    RAZORPAY_PLAN_ID_MF_MONTHLY / _MF_YEARLY          mutual-fund tier
    RAZORPAY_PLAN_ID_STOCKS_MONTHLY / _STOCKS_YEARLY  stocks tier
    RAZORPAY_PLAN_ID_ALL_MONTHLY / _ALL_YEARLY        both
                              plan_xxxxx each, made once in the dashboard
                              (make_razorpay_plans.py prints these lines).
                              The old RAZORPAY_PLAN_ID_YEARLY / _MONTHLY
                              still work and mean the mutual-fund tier.
                              A tier with no id is not offered.

    Needs mf_user.premium_scope and billing_subscription.scope -- run
    create_billing_tables.py once after deploying this version.

With any of them unset the endpoints answer 503 and the paid pages fall
back to the "email us" notice. Half-configured billing refuses to run
rather than half-working.


THE ONE RULE: THE BROWSER NEVER GRANTS ACCESS. THE WEBHOOK DOES.
    Razorpay Checkout calls back into the page with a payment id and a
    signature, and it is tempting to unlock the account right there --
    the person is watching, and it feels instant.

    It is also the single most common way these integrations leak. The
    callback runs on the customer's machine. Anyone can POST to the
    verify endpoint with made-up values; the signature check stops the
    naive version of that, but the deeper problem is that the callback
    says only "Checkout reached its success branch", not "money left a
    bank". A payment can be authorised and then fail capture. A mandate
    can be registered with a five-rupee token charge that is refunded
    immediately -- which is exactly what Razorpay does to validate a card
    or UPI id, and it is not a payment for anything.

    So /verify records what the browser said and returns. Access is
    granted in one place only: the webhook, on subscription.charged,
    which Razorpay's docs define as "sent every time a successful charge
    is made on the subscription".

THE SIGNATURE FORMULA IS REVERSED FOR SUBSCRIPTIONS
    One-time orders:   hmac_sha256(order_id + "|" + payment_id, key_secret)
    Subscriptions:     hmac_sha256(payment_id + "|" + subscription_id, key_secret)

    The operands swap. Razorpay's own Python SDK has both -- this file
    uses verify_subscription_payment_signature and never the other one.
    Getting it backwards fails closed here (nobody gets access), which is
    the right way round for a mistake to fail, but it is worth knowing
    why the generic integration guide's formula does not apply.

    The WEBHOOK signature is a third thing again: hmac_sha256 of the RAW
    request body with the WEBHOOK secret. Re-serialising the parsed JSON
    reorders keys and changes spacing, and the hash stops matching -- so
    the raw bytes are read before anything touches them.

EVERY WEBHOOK ARRIVES AT LEAST TWICE, EVENTUALLY
    Razorpay retries any event that gets a non-2xx or takes longer than
    five seconds, with backoff, for twenty-four hours. Without
    de-duplication, one slow night extends a subscriber's access by an
    extra period each redelivery -- silently, with nothing on screen ever
    looking wrong. Their event id is the primary key of billing_event and
    a repeat insert does nothing. That table is the whole defence.
"""

import hashlib
import hmac
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from psycopg.types.json import Json
from pydantic import BaseModel

from portfolio_api import DB, _q

router = APIRouter(prefix="/api/pay", tags=["payments"])

KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "").strip()
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()

# THREE TIERS x TWO CADENCES = SIX PLANS, each made once in the Razorpay
# dashboard (or with make_razorpay_plans.py) and named in .env as
# RAZORPAY_PLAN_ID_<TIER>_<CADENCE>. A tier is what a subscription UNLOCKS,
# and is stored on the account as mf_user.premium_scope:
#
#     mf      the mutual-fund tools: look-through, fund screener, plan
#             builder, overlap and review              (₹149 / ₹999)
#     stocks  the stock tools: stock screens, quarterly results, stock
#             alerts, the back-tester, which funds own a stock and what
#             changed                                   (₹399 / ₹2,999)
#             (check-your-view is not released yet: it stays admin-only and
#             is not advertised with the plans)
#     all     both                                       (₹599 / ₹3,499)
#
# The old names RAZORPAY_PLAN_ID_YEARLY / _MONTHLY / RAZORPAY_PLAN_ID still
# work and mean the mutual-fund tier, which is what the whole site used to
# be. A tier with no plan id in .env is simply not offered.
SCOPES = ("mf", "stocks", "all")
SCOPE_INFO = {
    "mf": {"title": "Mutual funds",
           "blurb": "Portfolio look-through, fund screener, plan builder, "
                    "overlap and review."},
    "stocks": {"title": "Stocks",
               "blurb": "Stock screens and your own screener builder, "
                        "quarterly results, stock alerts, the back-tester, "
                        "which funds own a stock and what changed."},
    "all": {"title": "Mutual funds + stocks",
            "blurb": "Everything on the site."},
}
CADENCES = ("monthly", "yearly")


def _env(*names):
    """First non-empty environment variable among `names`."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


PLAN_IDS = {
    "mf_monthly": _env("RAZORPAY_PLAN_ID_MF_MONTHLY", "RAZORPAY_PLAN_ID_MONTHLY",
                       "RAZORPAY_PLAN_ID"),
    "mf_yearly": _env("RAZORPAY_PLAN_ID_MF_YEARLY", "RAZORPAY_PLAN_ID_YEARLY"),
    "stocks_monthly": _env("RAZORPAY_PLAN_ID_STOCKS_MONTHLY"),
    "stocks_yearly": _env("RAZORPAY_PLAN_ID_STOCKS_YEARLY"),
    "all_monthly": _env("RAZORPAY_PLAN_ID_ALL_MONTHLY"),
    "all_yearly": _env("RAZORPAY_PLAN_ID_ALL_YEARLY"),
}

# What each plan costs, for the page to show. NOT used to charge anybody --
# Razorpay charges the plan's own amount, and if these disagree the plan
# wins. Here so the button can say a price without a round trip. Override
# with RAZORPAY_PRICE_<TIER>_<CADENCE> (the old RAZORPAY_PRICE_MONTHLY /
# _YEARLY still apply to the mutual-fund tier).
DEFAULT_PRICES = {
    "mf_monthly": "₹149 / month", "mf_yearly": "₹999 / year",
    "stocks_monthly": "₹399 / month", "stocks_yearly": "₹2,999 / year",
    "all_monthly": "₹599 / month", "all_yearly": "₹3,499 / year",
}
PRICES = {
    name: (_env("RAZORPAY_PRICE_" + name.upper(),
                *(["RAZORPAY_PRICE_" + name.split("_")[1].upper()]
                  if name.startswith("mf_") else []))
           or DEFAULT_PRICES[name])
    for name in DEFAULT_PRICES
}

# Razorpay require a cycle count rather than "forever" and cap the total
# span at 100 years: 1200 monthly cycles, 99 yearly ones. Using 1200 for
# a yearly plan would be 1200 years and Razorpay would refuse it.
TOTAL_COUNT = {
    "monthly": int(os.environ.get("RAZORPAY_TOTAL_COUNT_MONTHLY", "1200")),
    "yearly": int(os.environ.get("RAZORPAY_TOTAL_COUNT_YEARLY", "99")),
}

# plan name ("mf_yearly") -> (razorpay plan id, billing cycles to ask for)
PLANS = {name: (pid, TOTAL_COUNT[name.split("_")[1]])
         for name, pid in PLAN_IDS.items()}

# What the page sent before tiers existed. Both meant the mutual-fund tier.
LEGACY_PLAN_NAMES = {"monthly": "mf_monthly", "yearly": "mf_yearly"}


def plan_scope(name: str) -> str:
    return name.split("_")[0]


def plan_cadence(name: str) -> str:
    return name.split("_")[1]


def plan_label(name: Optional[str]) -> Optional[str]:
    """'mf_yearly' -> 'Mutual funds, yearly'. None stays None."""
    if not name or name not in PLANS:
        return None
    return "%s, %s" % (SCOPE_INFO[plan_scope(name)]["title"], plan_cadence(name))


def scope_of_plan_id(pid: Optional[str]) -> Optional[str]:
    """Which tier a Razorpay plan id unlocks, by the current .env."""
    for name, (p, _n) in PLANS.items():
        if p and p == pid:
            return plan_scope(name)
    return None


def _rupees(price: str) -> Optional[int]:
    digits = "".join(ch for ch in price.split("/")[0] if ch.isdigit())
    return int(digits) if digits else None


def plan_note(name: str) -> str:
    """The small print under a price: what the yearly plan saves against
    twelve months of the monthly one, worked out from the price strings so
    an override in .env keeps it honest. Falls back to plain words."""
    if plan_cadence(name) == "monthly":
        return "Cancel any time"
    y = _rupees(PRICES[name])
    m = _rupees(PRICES[plan_scope(name) + "_monthly"])
    if y and m and m * 12 > y:
        return ("Best value – about ₹%s a month, saves ₹%s a year"
                % (f"{round(y / 12):,}", f"{m * 12 - y:,}"))
    return "Best value"

# How long after the paid period ends before access stops. Razorpay begin
# a debit about 36 hours ahead of the nominal date and retry failures over
# several days; cutting somebody off at midnight on the renewal date would
# lock out people whose money is already in flight.
GRACE_DAYS = int(os.environ.get("RAZORPAY_GRACE_DAYS", "3"))

CONFIGURED = bool(KEY_ID and KEY_SECRET and WEBHOOK_SECRET
                  and any(pid for pid, _n in PLANS.values()))


def _client():
    """The Razorpay client, or a 503 that says which part is missing.

    Imported here rather than at module scope so the whole site still
    starts when the package is not installed yet -- billing is the newest
    thing here and must not be able to take the fund pages down with it.
    """
    if not CONFIGURED:
        missing = [n for n, v in (
            ("RAZORPAY_KEY_ID", KEY_ID), ("RAZORPAY_KEY_SECRET", KEY_SECRET),
            ("RAZORPAY_PLAN_ID_<TIER>_<CADENCE> (at least one)",
             any(pid for pid, _n in PLANS.values())),
            ("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)) if not v]
        raise HTTPException(503, "Payments are not configured yet (missing %s)."
                            % ", ".join(missing))
    try:
        import razorpay
    except ImportError:
        raise HTTPException(503, "The razorpay package is not installed "
                                 "on the server.")
    return razorpay.Client(auth=(KEY_ID, KEY_SECRET))


def _me(request: Request):
    from api import current_user
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Sign in first.")
    return u


# ---------------------------------------------------------------------
# What the page is allowed to know
# ---------------------------------------------------------------------
@router.get("/config")
def config():
    """Key id and price. Never the secret, and there is no code path here
    that could return it -- it is not in the dict at all."""
    tiers, flat = [], []
    if CONFIGURED:
        for scope in SCOPES:
            plans = []
            # Yearly first: it is the one we want people to pick.
            for cadence in ("yearly", "monthly"):
                name = "%s_%s" % (scope, cadence)
                if not PLANS[name][0]:
                    continue
                p = {"plan": name, "scope": scope, "cadence": cadence,
                     "title": SCOPE_INFO[scope]["title"],
                     "price": PRICES[name], "note": plan_note(name),
                     # yearly is the default within a tier; a tier with
                     # only a monthly plan defaults to that.
                     "default": cadence == "yearly" or not PLANS[scope + "_yearly"][0]}
                plans.append(p)
                flat.append(p)
            if plans:
                tiers.append({"scope": scope, "title": SCOPE_INFO[scope]["title"],
                              "blurb": SCOPE_INFO[scope]["blurb"], "plans": plans})
    return {
        "enabled": CONFIGURED,
        "key_id": KEY_ID if CONFIGURED else None,
        # `tiers` is what the page draws. `plans` is the flat list the older
        # page read (one radio per entry) and still works with it.
        "tiers": tiers,
        "plans": flat,
    }


@router.get("/status")
def status(request: Request):
    """Where this account stands, for the page to draw the right thing."""
    u = _me(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        # A LIVE subscription wins over a newer abandoned one. Every closed
        # checkout window leaves a 'created' row behind, and showing that
        # as "your subscription" would hide the one actually being billed.
        row = _q(cur, """
            SELECT subscription_id, plan_id, scope, status, current_end,
                   cancel_requested
            FROM billing_subscription
            WHERE user_id = %(u)s
            ORDER BY (status IN ('authenticated','active','pending')) DESC,
                     created_at DESC
            LIMIT 1
        """, {"u": u["user_id"]}, one=True)
    by_id = {pid: name for name, (pid, _n) in PLANS.items() if pid}
    premium = bool(u["is_premium"]) and (
        u["premium_until"] is None or u["premium_until"] >= date.today())
    plan_name = by_id.get(row["plan_id"]) if row else None
    return {
        "signed_in": True,
        # is_premium is the answer the rest of the site uses, so it is the
        # answer reported here. A page that read the subscription row
        # instead could disagree with the pages behind the paywall.
        "premium": premium,
        # Which tier the account has. A premium account with no scope on
        # record (granted by hand before tiers existed) has everything.
        "scope": (u.get("premium_scope") or "all") if premium else None,
        "premium_until": (u["premium_until"].isoformat()
                          if u["premium_until"] else None),
        "subscription": ({"id": row["subscription_id"],
                          "plan": plan_name,
                          "plan_label": plan_label(plan_name),
                          "scope": row["scope"] or scope_of_plan_id(row["plan_id"]),
                          "status": row["status"],
                          "cancel_requested": bool(row["cancel_requested"]),
                          "current_end": (row["current_end"].isoformat()
                                          if row["current_end"] else None)}
                         if row else None),
    }


# ---------------------------------------------------------------------
# Starting one
# ---------------------------------------------------------------------
@router.post("/subscription")
def create_subscription(request: Request, plan: str = "all_yearly"):
    """Make a Razorpay subscription for the signed-in account.

    `plan` is a NAME such as "mf_yearly" or "stocks_monthly" (the old
    "yearly" / "monthly" still mean the mutual-fund tier) -- never a
    Razorpay plan id, so the browser cannot ask us to subscribe somebody
    to an arbitrary plan on the account.

    Returns the id for Checkout to open. No access is granted here and
    none is implied: at this point the person has not paid anything and
    may well close the modal.
    """
    u = _me(request)
    client = _client()
    plan = LEGACY_PLAN_NAMES.get(plan, plan)
    if plan not in PLANS or not PLANS[plan][0]:
        raise HTTPException(400, "That plan is not available.")
    plan_id, total_count = PLANS[plan]
    scope = plan_scope(plan)

    # ONE LIVE SUBSCRIPTION PER TIER. Without this a second click (or a
    # direct call) would start a second mandate and bill the person twice.
    # 'created' rows are abandoned checkouts and do not count. One that is
    # already cancelled-at-period-end does not count either, so somebody
    # who changed their mind can subscribe again.
    #
    # A live subscription on a DIFFERENT tier is a plan change, and that is
    # allowed: the new one starts now, and the webhook cancels the old one
    # the moment the new one's first charge lands (see the charged branch).
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        live = _q(cur, """
            SELECT subscription_id, plan_id, scope FROM billing_subscription
            WHERE user_id = %(u)s
              AND status IN ('authenticated','active','pending')
              AND NOT cancel_requested
        """, {"u": u["user_id"]})
    replacing = []
    for r in live:
        r_scope = r["scope"] or scope_of_plan_id(r["plan_id"])
        if r_scope == scope:
            raise HTTPException(409, "You already have an active %s subscription. "
                                     "Manage it from the Subscription button."
                                     % SCOPE_INFO[scope]["title"])
        replacing.append(r["subscription_id"])

    try:
        sub = client.subscription.create({
            "plan_id": plan_id,
            "total_count": total_count,
            "quantity": 1,
            # Razorpay send the mandate and receipt emails. One sender for
            # billing means one place for a customer to look, and we are
            # not the system of record for their money.
            "customer_notify": 1,
            # OUR user id travels with the subscription, so a webhook
            # arriving days later can be matched to an account even if the
            # row below were somehow lost.
            "notes": {"user_id": str(u["user_id"]), "email": u["email"] or ""},
        })
    except HTTPException:
        raise
    except Exception as e:
        # Razorpay's errors carry useful detail and also carry the request
        # id; neither belongs in a browser. Logged upstream, generic here.
        raise HTTPException(502, "Could not start the subscription with the "
                                 "payment provider. Nothing has been charged.")

    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        # The tier is written down with the subscription, so a plan id that
        # is later renamed or retired in .env cannot change what somebody
        # already paid for.
        cur.execute("""
            INSERT INTO billing_subscription
                   (subscription_id, user_id, plan_id, scope, status)
            VALUES (%(s)s, %(u)s, %(p)s, %(sc)s, %(st)s)
            ON CONFLICT (subscription_id) DO UPDATE
               SET status = EXCLUDED.status, updated_at = now()
        """, {"s": sub["id"], "u": u["user_id"], "p": plan_id, "sc": scope,
              "st": sub.get("status") or "created"})
        conn.commit()

    return {"subscription_id": sub["id"], "key_id": KEY_ID,
            "status": sub.get("status"), "plan": plan, "scope": scope,
            # The page waits for THIS subscription to be charged, not for
            # "premium" in general -- somebody changing plan is premium
            # already, and the old test would have reloaded too early.
            "replaces": replacing}


# ---------------------------------------------------------------------
# What the browser reports back -- recorded, never trusted
# ---------------------------------------------------------------------
@router.post("/verify")
async def verify(request: Request):
    """Check Checkout's callback and write down what it said.

    THIS DOES NOT GRANT ACCESS, and the wording it returns is careful not
    to promise any. It exists for two reasons: it tells the page whether
    to say "thank you" or "something went wrong", and a mismatch here is
    worth knowing about because it means somebody is posting invented
    values at the endpoint.
    """
    u = _me(request)
    client = _client()
    # A body that is not JSON, or is JSON but not an object, used to raise out of here as a 500.
    try:
        body = await request.json()
    except Exception:                                            # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "Missing payment details.")

    need = ("razorpay_payment_id", "razorpay_subscription_id",
            "razorpay_signature")
    if not all(isinstance(body.get(k), str) and body.get(k) for k in need):
        raise HTTPException(400, "Missing payment details.")

    # THE SUBSCRIPTION ID COMES FROM OUR DATABASE, NOT FROM THE BROWSER.
    # Verifying the signature against a value the caller supplied would
    # check only that they can hash consistently.
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _q(cur, """
            SELECT subscription_id FROM billing_subscription
            WHERE subscription_id = %(s)s AND user_id = %(u)s
        """, {"s": body["razorpay_subscription_id"], "u": u["user_id"]},
            one=True)
    if not row:
        raise HTTPException(400, "That subscription does not belong to this "
                                 "account.")

    import razorpay
    try:
        client.utility.verify_subscription_payment_signature({
            "razorpay_subscription_id": row["subscription_id"],
            "razorpay_payment_id": body["razorpay_payment_id"],
            "razorpay_signature": body["razorpay_signature"],
        })
    except razorpay.errors.SignatureVerificationError:
        # Caught BY NAME. The SDK raises rather than returning False, so a
        # bare `except Exception: pass` around this call would hand out
        # free subscriptions to anyone who sent nonsense.
        raise HTTPException(400, "That payment could not be verified.")

    return {
        "ok": True,
        # Deliberately not "you are subscribed". The money event arrives
        # by webhook, usually within seconds but not always, and a page
        # that says "active" before it lands teaches people to refresh.
        "message": "Payment received. Your subscription is being confirmed "
                   "by the bank; access switches on as soon as it is.",
    }


# ---------------------------------------------------------------------
# The only place access is granted
# ---------------------------------------------------------------------
GRANT = """
UPDATE mf_user
   SET is_premium = true,
       premium_scope = %(scope)s,
       -- GREATEST, so a webhook arriving out of order never SHORTENS
       -- somebody's access. Razorpay do not promise delivery order, and
       -- an older charged event overtaking a newer one would otherwise
       -- pull the end date backwards.
       premium_until = GREATEST(COALESCE(premium_until, CURRENT_DATE), %(until)s)
 WHERE user_id = %(u)s
"""

# A plan CHANGE: the old subscription is being cancelled, so its end date
# must not carry over -- a yearly mutual-fund plan swapped for a monthly
# "both" plan would otherwise unlock everything until the old year ran out.
GRANT_SWITCH = """
UPDATE mf_user
   SET is_premium = true,
       premium_scope = %(scope)s,
       premium_until = %(until)s
 WHERE user_id = %(u)s
"""

REVOKE = """
UPDATE mf_user SET is_premium = false WHERE user_id = %(u)s
"""


@router.post("/webhook")
async def webhook(request: Request):
    """Razorpay's own account of what happened. The authoritative path.

    Returns 200 for anything it has understood, INCLUDING events it does
    not act on -- a non-2xx makes Razorpay redeliver for twenty-four
    hours and then disable the webhook entirely, so refusing to
    acknowledge an event we simply do not care about would eventually
    switch off the ones we do.
    """
    if not CONFIGURED:
        raise HTTPException(503, "Payments are not configured.")

    # RAW BYTES, BEFORE ANYTHING PARSES THEM. request.json() would work
    # and then re-serialising for the hash would reorder keys and change
    # spacing, and the signature would never match again.
    raw = await request.body()
    sent = request.headers.get("x-razorpay-signature", "")
    expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sent):
        # compare_digest, not ==, so the failure takes the same time
        # whatever the first wrong byte is.
        raise HTTPException(400, "Bad signature.")

    import json
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:                                           # a valid signature on something that is not JSON
        raise HTTPException(400, "Bad payload.")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Bad payload.")
    # The signature proves it came from Razorpay, not that every field has the shape we expect: a field of the wrong type used to
    # raise out of here as a 500, and a non-2xx makes Razorpay redeliver for a day and then disable the webhook.
    def _obj(x):
        return x if isinstance(x, dict) else {}

    event = payload.get("event")
    event = event if isinstance(event, str) else ""
    # Razorpay's id for this delivery. Ours would defeat the purpose.
    event_id = request.headers.get("x-razorpay-event-id") or ""
    ent = _obj(payload.get("payload"))
    sub = _obj(_obj(ent.get("subscription")).get("entity"))
    pay = _obj(_obj(ent.get("payment")).get("entity"))
    sub_id = sub.get("id") if isinstance(sub.get("id"), str) else None
    receipt = None          # (to, name, invoice row, next charge) once charged

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        if event_id:
            cur.execute("""
                INSERT INTO billing_event
                       (event_id, event, subscription_id, payment_id, payload)
                VALUES (%(e)s, %(ev)s, %(s)s, %(p)s, %(pl)s)
                ON CONFLICT (event_id) DO NOTHING
            """, {"e": event_id, "ev": event, "s": sub_id,
                  "p": pay.get("id"), "pl": Json(payload)})
            if cur.rowcount == 0:
                # Seen before. Acknowledge and do nothing -- this is the
                # line that stops a retried charge extending access twice.
                conn.commit()
                return {"ok": True, "duplicate": True}

        if not sub_id:
            conn.commit()
            return {"ok": True, "ignored": event}

        row = _q(cur, "SELECT user_id, plan_id, scope FROM billing_subscription "
                      "WHERE subscription_id = %(s)s", {"s": sub_id}, one=True)
        # A subscription we have no row for: fall back to the user id we
        # put in notes when we created it.
        user_id = (row or {}).get("user_id")
        if not user_id:
            try:
                user_id = int(_obj(sub.get("notes")).get("user_id") or 0) or None
            except (TypeError, ValueError):
                user_id = None

        cur.execute("""
            UPDATE billing_subscription
               SET status = %(st)s, last_payment_id = COALESCE(%(p)s, last_payment_id),
                   updated_at = now()
             WHERE subscription_id = %(s)s
        """, {"st": sub.get("status") or event.split(".")[-1],
              "p": pay.get("id"), "s": sub_id})

        # -------- the money event ------------------------------------
        if event == "subscription.charged" and user_id:
            # current_end is the end of the period just paid for. Razorpay
            # send it as a unix timestamp; a missing one falls back to a
            # month, which is wrong for a yearly plan but errs towards
            # LESS access rather than more, and the next charge corrects it.
            until = _end_date(sub.get("current_end"))
            cur.execute("""
                UPDATE billing_subscription SET current_end = %(d)s
                 WHERE subscription_id = %(s)s
            """, {"d": until, "s": sub_id})

            # The tier this subscription unlocks: what we wrote down when
            # we started it, else the plan id Razorpay report, else the
            # plan id we stored. An unknown plan errs towards the SMALLER
            # tier -- under-serving somebody is a complaint, over-serving
            # them is a giveaway nobody notices.
            scope = ((row or {}).get("scope")
                     or scope_of_plan_id(sub.get("plan_id"))
                     or scope_of_plan_id((row or {}).get("plan_id"))
                     or "mf")

            # A plan change: every OTHER live subscription on the account
            # is cancelled now that the new one has been paid for. Done
            # here, on the charge, and not when the new one was created,
            # so an abandoned checkout can never switch off a plan that
            # is still being paid for.
            others = _q(cur, """
                SELECT subscription_id, status FROM billing_subscription
                WHERE user_id = %(u)s
                  AND subscription_id <> %(s)s
                  AND status IN ('authenticated','active','pending','halted')
                  AND NOT cancel_requested
            """, {"u": user_id, "s": sub_id})
            for o in others:
                res = _cancel_now(o["subscription_id"])
                cur.execute("""
                    UPDATE billing_subscription
                       SET cancel_requested = true,
                           status = CASE WHEN %(res)s = 'ok' THEN 'cancelled'
                                         WHEN %(res)s IS NULL THEN status
                                         ELSE %(res)s END,
                           updated_at = now()
                     WHERE subscription_id = %(o)s
                """, {"o": o["subscription_id"], "res": res})

            cur.execute(GRANT_SWITCH if others else GRANT,
                        {"until": until + timedelta(days=GRACE_DAYS),
                         "u": user_id, "scope": scope})

            # -------- the invoice, in the same transaction -----------
            # Under a savepoint: an invoice problem (table missing, odd
            # payload) must never take the access grant down with it.
            # The grant is what the customer paid for; the invoice can be
            # issued again by hand.
            cur.execute("SAVEPOINT invoice")
            try:
                import billing_invoices as bi
                u = _q(cur, "SELECT user_id, email, full_name FROM mf_user "
                            "WHERE user_id = %(u)s", {"u": user_id}, one=True)
                pid = sub.get("plan_id") or (row or {}).get("plan_id")
                plan_name = next((n for n, (p, _c) in PLANS.items()
                                  if p and p == pid), None)
                inv, created = bi.create_invoice(
                    cur, user=u or {"user_id": user_id}, subscription_id=sub_id,
                    payment=pay, subscription=sub, plan_name=plan_name,
                    plan_label=plan_label(plan_name) or SCOPE_INFO[scope]["title"],
                    scope=scope, event_id=event_id)
                cur.execute("RELEASE SAVEPOINT invoice")
                # Email once per payment: a charge Razorpay re-send under
                # a new delivery id reuses the invoice and sends nothing.
                if inv and created and u and u.get("email"):
                    nxt = (_end_date(sub.get("charge_at"))
                           if sub.get("charge_at") else None)
                    receipt = (u["email"], u.get("full_name") or "", dict(inv), nxt)
            except Exception as ex:                              # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT invoice")
                print("mfapi: invoice for %s NOT issued: %r" % (pay.get("id"), ex),
                      flush=True)

        # -------- the end of one --------------------------------------
        elif event in ("subscription.cancelled", "subscription.completed",
                       "subscription.expired") and user_id:
            # Access is NOT torn down here. They paid for a period and the
            # period runs out on its own -- premium_until already says
            # when. Revoking on cancellation would take away time somebody
            # has paid for, which is the worst thing billing code can do.
            pass

        # -------- retries exhausted -----------------------------------
        elif event == "subscription.halted" and user_id:
            # halted means Razorpay have given up retrying. Even here we
            # leave premium_until alone: it lapses by itself on the date
            # already paid up to. Nothing is taken away early.
            pass

        # subscription.pending is deliberately not handled at all: a
        # charge is being retried and the person has done nothing wrong.

        conn.commit()

    # After the commit, never before: an email about an invoice that was
    # then rolled back would be worse than no email. Sent from a thread so
    # Razorpay get their 200 inside five seconds whatever the mail server
    # is doing; failures are logged, not raised.
    if receipt:
        try:
            import billing_invoices as bi
            to, name, inv, nxt = receipt
            bi.send_in_background(bi.receipt_email(to, name, inv, nxt))
        except Exception as ex:                                  # noqa: BLE001
            print("mfapi: receipt email not built: %r" % ex, flush=True)

    return {"ok": True, "event": event}


# ---------------------------------------------------------------------
# Invoices and the customer's billing details
# ---------------------------------------------------------------------
class BillingDetails(BaseModel):
    legal_name: str = ""
    gstin: str = ""
    address: str = ""
    state_code: str = ""


def _is_admin(cur, user_id) -> bool:
    try:
        return _q(cur, "SELECT 1 AS x FROM app_admin WHERE user_id = %(u)s",
                  {"u": user_id}, one=True) is not None
    except Exception:                                            # noqa: BLE001
        return False


@router.get("/billing-details")
def billing_details(request: Request):
    """What will be printed on the customer's next invoice, plus the list
    of states for the form."""
    import billing_invoices as bi
    u = _me(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        row = _q(cur, "SELECT legal_name, gstin, address, state_code, updated_at "
                      "FROM billing_customer WHERE user_id = %(u)s",
                 {"u": u["user_id"]}, one=True)
    return {
        "details": {
            "legal_name": (row or {}).get("legal_name") or "",
            "gstin": (row or {}).get("gstin") or "",
            "address": (row or {}).get("address") or "",
            "state_code": (row or {}).get("state_code") or "",
        },
        "states": [{"code": c, "name": n} for c, n in sorted(bi.STATES.items(), key=lambda x: x[1])],
        # Whether FinChaya issues tax invoices at all (registered) or
        # receipts. The form says which, so nobody expects a GST break-up
        # that cannot legally be given.
        "tax_invoices": bool(bi.GSTIN),
    }


@router.post("/billing-details")
def save_billing_details(request: Request, body: BillingDetails):
    """Applies to invoices issued from now on. Earlier invoices are left
    as they were -- an invoice records what was known on the day."""
    import billing_invoices as bi
    u = _me(request)
    name = body.legal_name.strip()[:160]
    gstin = body.gstin.strip().upper()
    address = " ".join(body.address.split())[:400]
    state = body.state_code.strip()
    bad = bi.validate_gstin(gstin)
    if bad:
        raise HTTPException(400, bad)
    if state and state not in bi.STATES:
        raise HTTPException(400, "Pick a state from the list.")
    if gstin and not state:
        state = gstin[:2]                         # the GSTIN says where
    if gstin and state != gstin[:2]:
        raise HTTPException(400, "The GSTIN starts with %s (%s) but the state "
                                 "chosen is %s." % (gstin[:2], bi.STATES.get(gstin[:2], "?"),
                                                    bi.STATES.get(state, state)))
    if gstin and not name:
        raise HTTPException(400, "Give the business name the GSTIN is registered in.")
    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO billing_customer (user_id, legal_name, gstin, address, state_code)
            VALUES (%(u)s, %(n)s, %(g)s, %(a)s, %(s)s)
            ON CONFLICT (user_id) DO UPDATE
               SET legal_name = EXCLUDED.legal_name, gstin = EXCLUDED.gstin,
                   address = EXCLUDED.address, state_code = EXCLUDED.state_code,
                   updated_at = now()
        """, {"u": u["user_id"], "n": name, "g": gstin, "a": address, "s": state})
        conn.commit()
    return {"ok": True, "message": "Saved. These details go on every invoice from "
                                   "the next payment onward."}


@router.get("/invoices")
def invoices(request: Request):
    """The signed-in account's invoices, newest first."""
    import billing_invoices as bi
    u = _me(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = _q(cur, """
            SELECT invoice_no, issued_on, tax_invoice, plan_label, plan_name,
                   gross_paise, period_start, period_end, payment_id
            FROM billing_invoice WHERE user_id = %(u)s
            ORDER BY issued_on DESC, seq DESC
        """, {"u": u["user_id"]})
    return {"invoices": [{
        "invoice_no": r["invoice_no"], "date": r["issued_on"].isoformat(),
        "kind": "Tax invoice" if r["tax_invoice"] else "Receipt",
        "plan": r["plan_label"] or r["plan_name"], "amount": bi.rupees(r["gross_paise"]),
        "period_start": r["period_start"].isoformat() if r["period_start"] else None,
        "period_end": r["period_end"].isoformat() if r["period_end"] else None,
        "url": "/api/pay/invoice/" + r["invoice_no"],
    } for r in rows]}


@router.get("/invoice/{invoice_no:path}", response_class=HTMLResponse)
def invoice_page(invoice_no: str, request: Request):
    """One invoice as a printable page. Its owner, or an admin."""
    import billing_invoices as bi
    u = _me(request)
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        inv = _q(cur, "SELECT * FROM billing_invoice WHERE invoice_no = %(n)s",
                 {"n": invoice_no.strip()}, one=True)
        if not inv:
            raise HTTPException(404, "No such invoice.")
        if inv["user_id"] != u["user_id"] and not _is_admin(cur, u["user_id"]):
            raise HTTPException(404, "No such invoice.")
    return HTMLResponse(bi.invoice_html(inv))


@router.get("/invoices.csv", response_class=PlainTextResponse)
def invoices_csv(request: Request, start: Optional[str] = None, end: Optional[str] = None):
    """Every invoice in a date range, for GST filing. Admins only."""
    import billing_invoices as bi
    u = _me(request)
    try:
        d0 = date.fromisoformat(start) if start else date(2000, 1, 1)
        d1 = date.fromisoformat(end) if end else date(2100, 1, 1)
    except ValueError:
        raise HTTPException(400, "Dates are YYYY-MM-DD.")
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        if not _is_admin(cur, u["user_id"]):
            raise HTTPException(403, "Admins only.")
        rows = _q(cur, """
            SELECT * FROM billing_invoice
            WHERE issued_on BETWEEN %(a)s AND %(b)s
            ORDER BY fy, seq
        """, {"a": d0, "b": d1})
    return PlainTextResponse(bi.invoice_csv_rows(rows), media_type="text/csv",
                             headers={"Content-Disposition":
                                      'attachment; filename="finchaya-invoices.csv"'})


def _end_date(ts) -> date:
    """Razorpay's current_end (unix seconds) as a date, with a floor.

    A missing or unparseable value must not produce today's date, which
    would grant nothing and look like a bug on the customer's side. A
    month is the smallest sensible period Razorpay offers.
    """
    try:
        if ts:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return date.today() + timedelta(days=30)


# ---------------------------------------------------------------------
# Stopping one
# ---------------------------------------------------------------------
def _cancel_now(sub_id):
    """Cancel a subscription IMMEDIATELY on Razorpay, for a plan change.

    Not at the end of the cycle: the person has just paid for the plan
    that replaces this one, and leaving the old mandate running would bill
    them for both. Returns "ok", the final status Razorpay already had it
    in, or None if nothing could be done (logged; the new plan's access is
    granted regardless, and the old one can be cancelled by hand).
    """
    try:
        client = _client()
    except HTTPException as e:
        print("mfapi: cannot cancel %s on plan change: %s" % (sub_id, e.detail),
              flush=True)
        return None
    try:
        client.subscription.cancel(sub_id, {"cancel_at_cycle_end": 0})
        return "ok"
    except Exception as e:
        print("mfapi: razorpay cancel-now failed for %s: %r" % (sub_id, e),
              flush=True)
    try:
        st = client.subscription.fetch(sub_id).get("status")
    except Exception as e:
        print("mfapi: razorpay fetch failed for %s: %r" % (sub_id, e), flush=True)
        return None
    return st if st in ("cancelled", "completed", "expired") else None


def _cancel_one(client, sub_id, status):
    """Ask Razorpay to cancel one subscription. Returns "ok" if it accepted,
    the final status if the subscription was already over on Razorpay's side,
    or None if it could not be cancelled.

    EVERY FAILURE IS LOGGED, with Razorpay's own message, to stdout (which
    systemd keeps: `journalctl -u mfapi`). The browser still gets only a
    generic sentence -- Razorpay's errors can carry request ids and account
    detail -- but "the provider did not accept it" with nothing in any log
    is a bug nobody can diagnose.

    The order is: cancel at the end of the paid period; if that is refused,
    cancel outright; if that is refused too, ask Razorpay what state it is
    in. Cancelling outright is safe because access here is decided by
    premium_until, which the charge already set -- not by Razorpay's status.
    A customer keeps what they paid for either way.
    """
    # cancel_at_cycle_end only means something once a charge has happened
    # ('active'). Anything earlier has nothing paid for to protect.
    for flag in ([1, 0] if status == "active" else [0]):
        try:
            client.subscription.cancel(sub_id, {"cancel_at_cycle_end": flag})
            return "ok"
        except Exception as e:
            print("mfapi: razorpay cancel failed for %s (cancel_at_cycle_end=%s): %r"
                  % (sub_id, flag, e), flush=True)
    try:
        s = client.subscription.fetch(sub_id)
    except Exception as e:
        print("mfapi: razorpay fetch failed for %s: %r" % (sub_id, e), flush=True)
        return None
    st = s.get("status")
    print("mfapi: razorpay says %s is %s (scheduled changes: %s)"
          % (sub_id, st, s.get("has_scheduled_changes")), flush=True)
    if st in ("cancelled", "completed", "expired"):
        return st                                   # already over there
    if st == "active" and s.get("has_scheduled_changes"):
        return "ok"                                 # cancel already scheduled
    return None


@router.post("/cancel")
def cancel(request: Request):
    """Cancel at the end of the paid period, not immediately.

    cancel_at_cycle_end=1 on purpose. Somebody cancelling on day two of a
    month they have paid for should keep the other twenty-eight days; an
    immediate cancellation would be us keeping the money and withdrawing
    the service.
    """
    u = _me(request)
    client = _client()
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        # EVERY live subscription, not just the newest: an account that
        # ended up with two (before the guard above existed) must stop
        # being billed on both, or "cancel" would quietly leave one running.
        rows = _q(cur, """
            SELECT subscription_id, status FROM billing_subscription
            WHERE user_id = %(u)s
              AND status IN ('authenticated','active','pending','halted')
              AND NOT cancel_requested
        """, {"u": u["user_id"]})
    if not rows:
        raise HTTPException(404, "No live subscription on this account.")

    failed = 0
    for r in rows:
        result = _cancel_one(client, r["subscription_id"], r["status"])
        if result is None:
            failed += 1
            continue
        with psycopg.connect(DB) as conn, conn.cursor() as cur:
            # `result` is "ok" when Razorpay accepted the cancellation, or
            # the final status it was already in ("cancelled", ...). In the
            # second case our own row was simply behind, so it is caught up.
            cur.execute("""
                UPDATE billing_subscription
                   SET cancel_requested = true,
                       status = CASE WHEN %(res)s = 'ok' THEN status ELSE %(res)s END,
                       updated_at = now()
                 WHERE subscription_id = %(s)s
            """, {"s": r["subscription_id"], "res": result})
            conn.commit()

    if failed == len(rows):
        raise HTTPException(502, "The payment provider did not accept the "
                                 "cancellation. Nothing has changed.")
    return {"ok": True,
            "message": "Cancelled. Your access continues until the end of "
                       "the period you have already paid for."
                       + (" One part could not be cancelled - please try "
                          "again." if failed else "")}


# ---------------------------------------------------------------------
# The free trial (trial.py) is served under /api/pay/trial. Mounted last and on its own: a copy of this file next to no trial.py keeps every other billing endpoint.
# ---------------------------------------------------------------------
try:
    import trial as _trial
    if _trial.router is not None:
        router.include_router(_trial.router)
except ImportError:
    pass
