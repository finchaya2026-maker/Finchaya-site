"""
referral_api.py -- a personal referral link for every signed-in member.
------------------------------------------------------------------------
A member gets one code. They share a link that carries it (and a message
written for sharing); a friend who opens the link and signs up for the first
time is recorded against that member. That is all it does.

    GET  /api/referral/me      my code, my link, the message, how many joined
    POST /api/referral/claim   a new member says "this code brought me here"

WHAT IT DELIBERATELY DOES NOT DO
    No rewards, credits or discounts. Nothing here pays anyone, so there is
    nothing to game and no money moving between members. If a reward is added
    later, the sensible trigger is "the friend subscribed", not "the friend
    signed up", and that is a change to this file, not to the table.

    It sends nothing. FinChaya never emails or messages a friend: the member
    does, from their own WhatsApp, phone or mail. It reads no contacts.

THE CODE
    Eight characters from an alphabet with no 0/O or 1/I, so it survives
    being read aloud or typed from a screenshot. Random, not derived from the
    user id or email, so a code reveals nothing about its owner and cannot be
    guessed from another one. Uniqueness is enforced by the table, not by
    hoping: a clash is retried.

WHO COUNTS AS "NEW"
    Only an account created in the last NEW_ACCOUNT_DAYS can be credited. A
    long-standing member opening a friend's link is not a referral, and
    without this a person could claim a code on any account at any time. One
    referrer per person, first one wins, and nobody can refer themselves.

TESTING WITH AN EXISTING ACCOUNT
    MF_REFERRAL_ANY_ACCOUNT=true in .env lifts the "new account" rule so a
    second account that already exists can be used to try the whole flow.
    Leave it unset in normal running.

THE TABLES
    Created on first use by the application's own login (the same way
    grant_scores.py makes its table), so there is no migration to run and no
    admin password. No foreign key to mf_user, on purpose: deleting an
    account must not silently delete the record of who invited whom, and a
    constraint on a table the application does not own is a permission
    problem waiting to happen.

    referral_code(user_id PK, code UNIQUE)   one code per member
    referral(referee_id PK, referrer_id)     who brought whom; PK = one referrer

APP-READY
    Authentication is the same cookie-or-bearer-token the rest of the API
    uses, and the message and link come from here, so a future mobile app
    shares exactly what the website does without carrying its own copy.
"""

import os
import re
import secrets
import threading

import psycopg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from portfolio_api import DB

router = APIRouter(prefix="/api/referral", tags=["referral"])

# 32 characters: A-Z and 2-9 without the look-alikes O, I (and 0, 1).
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{8}$")

NEW_ACCOUNT_DAYS = 3
ANY_ACCOUNT = os.getenv("MF_REFERRAL_ANY_ACCOUNT", "").strip().lower() == "true"

# Optional absolute address for the link when it will be shown somewhere that
# is not this website (a future app). The website builds the link from the
# address it is open on, which is right for production and for local testing.
PUBLIC_URL = os.getenv("MF_PUBLIC_URL", "").strip().rstrip("/")

# Where the link lands. The goal planner, because the message is about
# planning a goal and its first half is free: the friend gets value straight
# away instead of meeting a sign-in wall.
LANDING_PATH = "/plan"

EMAIL_SUBJECT = "A simple way to plan your money goals"

# THE MESSAGE. {link} is replaced with the member's own link.
#
# Written to describe what FinChaya really does, in the order a friend meets
# it: first plan a goal, then look inside a fund before choosing. Care taken:
#   * It says "free" only for what is free -- planning a goal. The look-
#     through, screener and fund-picking tools are part of the subscription,
#     so the message does not promise them.
#   * It does not say "best", "better" or "top" about any fund, and does not
#     promise or imply returns. FinChaya is not registered with SEBI as an
#     investment adviser or research analyst, and a message that tells people
#     which fund is better reads as exactly that advice. The closing line says
#     what the product is.
MESSAGE = (
    "Hi! I've been using FinChaya to plan my money goals and thought you'd "
    "find it useful.\n"
    "\n"
    "Tell it what you're saving for — a house, retirement, your child's "
    "education — and it works out the return your plan actually needs, "
    "so you know what you're really asking of your money.\n"
    "\n"
    "Then, before you choose a mutual fund, you can look inside it: the "
    "companies it really holds, how it has behaved over time, and how it "
    "compares with similar funds.\n"
    "\n"
    "Planning your goal is free: {link}\n"
    "\n"
    "(An educational tool, not investment advice.)"
)


class ClaimBody(BaseModel):
    code: str


# ---------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------
def _conn():
    """One connection per request. A function so a test can substitute its
    own without touching the real tables."""
    return psycopg.connect(DB)


_DDL = (
    """CREATE TABLE IF NOT EXISTS referral_code (
           user_id    bigint PRIMARY KEY,
           code       text   NOT NULL UNIQUE,
           created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS referral (
           referee_id  bigint PRIMARY KEY,
           referrer_id bigint NOT NULL,
           code        text   NOT NULL,
           created_at  timestamptz NOT NULL DEFAULT now())""",
    """CREATE INDEX IF NOT EXISTS ix_referral_referrer
           ON referral (referrer_id)""",
)

_ready = False
_ready_lock = threading.Lock()


def ensure_tables(conn):
    """Create the two tables if they are not there. Cheap, idempotent, and
    done once per process; a failure is reported as "not available yet"
    rather than a stack trace, because the usual cause is a permission."""
    global _ready
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        try:
            with conn.cursor() as cur:
                for stmt in _DDL:
                    cur.execute(stmt)
            conn.commit()
        except psycopg.Error as e:
            conn.rollback()
            print("mfapi: referral tables could not be created: %s" % e,
                  flush=True)
            raise HTTPException(503, "Referrals are not available yet.")
        _ready = True


def _me(request: Request):
    from api import current_user
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Sign in first.")
    return u


# ---------------------------------------------------------------------
# THE RULES (pure on a connection, so they can be tested without a server)
# ---------------------------------------------------------------------
def new_code():
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def normalise(code):
    """Upper-case and trim; None if it cannot be one of ours."""
    code = (code or "").strip().upper()
    return code if CODE_RE.match(code) else None


def get_or_create_code(conn, user_id):
    """The member's code, made on first request. Safe to call twice at once:
    the insert says DO NOTHING on either unique rule, and a loser simply
    reads the winner's row."""
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM referral_code WHERE user_id = %s",
                    (user_id,))
        row = cur.fetchone()
        if row:
            return row[0]
        for _ in range(12):
            cur.execute("""INSERT INTO referral_code (user_id, code)
                           VALUES (%s, %s) ON CONFLICT DO NOTHING
                           RETURNING code""", (user_id, new_code()))
            row = cur.fetchone()
            if row:
                return row[0]
            # Either we collided on the code (try another) or another
            # request made this member's row first (use it).
            cur.execute("SELECT code FROM referral_code WHERE user_id = %s",
                        (user_id,))
            row = cur.fetchone()
            if row:
                return row[0]
    raise RuntimeError("could not find an unused referral code")


def count_referred(conn, user_id):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM referral WHERE referrer_id = %s",
                    (user_id,))
        return cur.fetchone()[0]


def claim(conn, user_id, raw_code):
    """Credit `user_id` to the owner of `raw_code`, if the rules allow.

    Returns (status, reason): ("joined", "") or ("ignored", why). Never
    raises for a rule -- a code that does not count is an answer, not an
    error, and the page needs to be told so it can stop carrying the code."""
    code = normalise(raw_code)
    if not code:
        return "ignored", "invalid_code"

    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM referral_code WHERE code = %s",
                    (code,))
        owner = cur.fetchone()
        if not owner:
            return "ignored", "unknown_code"
        referrer = owner[0]
        if referrer == user_id:
            return "ignored", "own_code"

        cur.execute("SELECT 1 FROM referral WHERE referee_id = %s",
                    (user_id,))
        if cur.fetchone():
            return "ignored", "already_referred"

        if not ANY_ACCOUNT:
            cur.execute("""SELECT created_at >= now() - make_interval(days => %s)
                           FROM mf_user WHERE user_id = %s""",
                        (NEW_ACCOUNT_DAYS, user_id))
            row = cur.fetchone()
            if not row or not row[0]:
                return "ignored", "not_a_new_account"

        cur.execute("""INSERT INTO referral (referee_id, referrer_id, code)
                       VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                       RETURNING referee_id""", (user_id, referrer, code))
        if not cur.fetchone():
            return "ignored", "already_referred"   # lost a race to ourselves
    return "joined", ""


# ---------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------
@router.get("/me")
def my_referral(request: Request):
    """The signed-in member's code, link path, the message to share, and how
    many people have joined through them."""
    user = _me(request)
    with _conn() as conn:
        ensure_tables(conn)
        code = get_or_create_code(conn, user["user_id"])
        joined = count_referred(conn, user["user_id"])
        conn.commit()
    path = "%s?ref=%s" % (LANDING_PATH, code)
    return {
        "code": code,
        "path": path,
        # Absolute only when MF_PUBLIC_URL is set; the website otherwise
        # builds it from the address it is open on.
        "url": (PUBLIC_URL + path) if PUBLIC_URL else None,
        "message": MESSAGE,
        "email_subject": EMAIL_SUBJECT,
        "referred": joined,
    }


@router.post("/claim")
def claim_referral(body: ClaimBody, request: Request):
    """Called by the page right after a sign-in, with the code the visitor
    arrived with. Always 200 for a well-formed request: whether the code
    counted is in the body."""
    user = _me(request)
    with _conn() as conn:
        ensure_tables(conn)
        status, reason = claim(conn, user["user_id"], body.code)
        conn.commit()
    return {"status": status, "reason": reason}
