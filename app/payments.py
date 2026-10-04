"""
payments.py -- Razorpay Subscriptions, and the one rule that matters.
=====================================================================
Mounted in api.py:

    from payments import router as payments_router
    app.include_router(payments_router)

    pip install razorpay          (2.0.1 at the time of writing)

FOUR SETTINGS, ALL IN /opt/mfapi/.env, NONE IN THIS FILE:

    RAZORPAY_KEY_ID           public; the browser is given this
    RAZORPAY_KEY_SECRET       server only; never leaves this process
    RAZORPAY_PLAN_ID_YEARLY   plan_xxxxx, made once in the dashboard
    RAZORPAY_PLAN_ID_MONTHLY  plan_xxxxx (either plan alone also works)
    RAZORPAY_WEBHOOK_SECRET   server only; a DIFFERENT secret from the
                              key secret, set when adding the webhook

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
from psycopg.types.json import Json

from portfolio_api import DB, _q

router = APIRouter(prefix="/api/pay", tags=["payments"])

KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "").strip()
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()

# Two plans, made once in the dashboard. RAZORPAY_PLAN_ID is the old
# single-plan name and still works as the monthly plan.
PLAN_MONTHLY = (os.environ.get("RAZORPAY_PLAN_ID_MONTHLY")
                or os.environ.get("RAZORPAY_PLAN_ID") or "").strip()
PLAN_YEARLY = os.environ.get("RAZORPAY_PLAN_ID_YEARLY", "").strip()

# What each plan costs, for the page to show. NOT used to charge anybody --
# Razorpay charges the plan's own amount, and if these disagree the plan
# wins. Here so the button can say a price without a round trip.
PRICE_MONTHLY = os.environ.get("RAZORPAY_PRICE_MONTHLY", "").strip() or "₹149 / month"
PRICE_YEARLY = os.environ.get("RAZORPAY_PRICE_YEARLY", "").strip() or "₹999 / year"

# plan name -> (razorpay plan id, billing cycles to ask for).
# Razorpay require a cycle count rather than "forever" and cap the total
# span at 100 years: 1200 monthly cycles, 99 yearly ones. Using 1200 for
# the yearly plan would be 1200 years and Razorpay would refuse it.
PLANS = {
    "monthly": (PLAN_MONTHLY, int(os.environ.get("RAZORPAY_TOTAL_COUNT_MONTHLY", "1200"))),
    "yearly": (PLAN_YEARLY, int(os.environ.get("RAZORPAY_TOTAL_COUNT_YEARLY", "99"))),
}

# How long after the paid period ends before access stops. Razorpay begin
# a debit about 36 hours ahead of the nominal date and retry failures over
# several days; cutting somebody off at midnight on the renewal date would
# lock out people whose money is already in flight.
GRACE_DAYS = int(os.environ.get("RAZORPAY_GRACE_DAYS", "3"))

CONFIGURED = bool(KEY_ID and KEY_SECRET and WEBHOOK_SECRET
                  and (PLAN_MONTHLY or PLAN_YEARLY))


def _client():
    """The Razorpay client, or a 503 that says which part is missing.

    Imported here rather than at module scope so the whole site still
    starts when the package is not installed yet -- billing is the newest
    thing here and must not be able to take the fund pages down with it.
    """
    if not CONFIGURED:
        missing = [n for n, v in (
            ("RAZORPAY_KEY_ID", KEY_ID), ("RAZORPAY_KEY_SECRET", KEY_SECRET),
            ("RAZORPAY_PLAN_ID_YEARLY or _MONTHLY",
             PLAN_MONTHLY or PLAN_YEARLY),
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
    plans = []
    if CONFIGURED:
        # Yearly first: it is the one we want people to pick.
        if PLAN_YEARLY:
            plans.append({"plan": "yearly", "price": PRICE_YEARLY,
                          "note": "Best value – about ₹83 a month, "
                                  "saves ₹789 a year",
                          "default": True})
        if PLAN_MONTHLY:
            plans.append({"plan": "monthly", "price": PRICE_MONTHLY,
                          "note": "Cancel any time",
                          "default": not PLAN_YEARLY})
    return {
        "enabled": CONFIGURED,
        "key_id": KEY_ID if CONFIGURED else None,
        "plans": plans,
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
            SELECT subscription_id, plan_id, status, current_end,
                   cancel_requested
            FROM billing_subscription
            WHERE user_id = %(u)s
            ORDER BY (status IN ('authenticated','active','pending')) DESC,
                     created_at DESC
            LIMIT 1
        """, {"u": u["user_id"]}, one=True)
    by_id = {pid: name for name, (pid, _n) in PLANS.items() if pid}
    return {
        "signed_in": True,
        # is_premium is the answer the rest of the site uses, so it is the
        # answer reported here. A page that read the subscription row
        # instead could disagree with the pages behind the paywall.
        "premium": bool(u["is_premium"]) and (
            u["premium_until"] is None or u["premium_until"] >= date.today()),
        "premium_until": (u["premium_until"].isoformat()
                          if u["premium_until"] else None),
        "subscription": ({"id": row["subscription_id"],
                          "plan": by_id.get(row["plan_id"]),
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
def create_subscription(request: Request, plan: str = "yearly"):
    """Make a Razorpay subscription for the signed-in account.

    `plan` is "yearly" or "monthly" -- a NAME, never a Razorpay plan id,
    so the browser cannot ask us to subscribe somebody to an arbitrary
    plan on the account.

    Returns the id for Checkout to open. No access is granted here and
    none is implied: at this point the person has not paid anything and
    may well close the modal.
    """
    u = _me(request)
    client = _client()
    if plan not in PLANS or not PLANS[plan][0]:
        raise HTTPException(400, "That plan is not available.")
    plan_id, total_count = PLANS[plan]

    # ONE LIVE SUBSCRIPTION PER ACCOUNT. Without this a second click (or a
    # direct call) would start a second mandate and bill the person twice.
    # 'created' rows are abandoned checkouts and do not count. One that is
    # already cancelled-at-period-end does not count either, so somebody
    # who changed their mind can subscribe again.
    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        live = _q(cur, """
            SELECT 1 AS x FROM billing_subscription
            WHERE user_id = %(u)s
              AND status IN ('authenticated','active','pending')
              AND NOT cancel_requested
            LIMIT 1
        """, {"u": u["user_id"]}, one=True)
    if live:
        raise HTTPException(409, "You already have an active subscription. "
                                 "Manage it from the Subscription button.")

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
        cur.execute("""
            INSERT INTO billing_subscription
                   (subscription_id, user_id, plan_id, status)
            VALUES (%(s)s, %(u)s, %(p)s, %(st)s)
            ON CONFLICT (subscription_id) DO UPDATE
               SET status = EXCLUDED.status, updated_at = now()
        """, {"s": sub["id"], "u": u["user_id"], "p": plan_id,
              "st": sub.get("status") or "created"})
        conn.commit()

    return {"subscription_id": sub["id"], "key_id": KEY_ID,
            "status": sub.get("status")}


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
    body = await request.json()

    need = ("razorpay_payment_id", "razorpay_subscription_id",
            "razorpay_signature")
    if not all(body.get(k) for k in need):
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
       -- GREATEST, so a webhook arriving out of order never SHORTENS
       -- somebody's access. Razorpay do not promise delivery order, and
       -- an older charged event overtaking a newer one would otherwise
       -- pull the end date backwards.
       premium_until = GREATEST(COALESCE(premium_until, CURRENT_DATE), %(until)s)
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
    payload = json.loads(raw.decode("utf-8"))
    event = payload.get("event") or ""
    # Razorpay's id for this delivery. Ours would defeat the purpose.
    event_id = request.headers.get("x-razorpay-event-id") or ""
    ent = (payload.get("payload") or {})
    sub = ((ent.get("subscription") or {}).get("entity")) or {}
    pay = ((ent.get("payment") or {}).get("entity")) or {}
    sub_id = sub.get("id")

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

        row = _q(cur, "SELECT user_id FROM billing_subscription "
                      "WHERE subscription_id = %(s)s", {"s": sub_id}, one=True)
        # A subscription we have no row for: fall back to the user id we
        # put in notes when we created it.
        user_id = (row or {}).get("user_id")
        if not user_id:
            try:
                user_id = int((sub.get("notes") or {}).get("user_id") or 0) or None
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
            cur.execute(GRANT, {"until": until + timedelta(days=GRACE_DAYS),
                                "u": user_id})

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

    return {"ok": True, "event": event}


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
