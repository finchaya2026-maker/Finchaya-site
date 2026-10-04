"""
api.py
------
A small read-only API over the score tables.

WHAT THIS IS
    The bridge between your database and a web page. Browsers cannot talk
    to PostgreSQL, so something has to sit in between, read the tables,
    and hand back plain JSON. That is all this file does.

WHAT THIS IS NOT
    It never computes a score and never writes to the database. The batch
    jobs do that once a day. This only reads what they left behind -- which
    is the whole point of the design you described at the start: expensive
    work once, cheap reads many times.

RUN IT
    python -m uvicorn api:app --reload

Then open http://127.0.0.1:8000/docs in a browser. FastAPI generates an
interactive page listing every endpoint, where you can click "Try it out"
and see real data -- no extra work needed.

CATEGORIES
    Every endpoint reads v_scheme_category rather than mf_scheme directly.
    AMFI ships several spellings of the same category at once, which split
    one peer group across buckets and made category_rank wrong. The view
    merges them and carries rank_meaningful, which is false for groups
    (ETFs, Index Funds, Sectoral/Thematic) where a rank would be a number
    without a meaning.

ENDPOINTS
    GET /api/health              is the service alive, is the data fresh
    GET /api/funds               list + search + sort + page
    GET /api/funds/{code}        one fund, its holdings, and its returns
                                 (returns carry both the benchmark and the
                                  category median for the same window)
    GET /api/categories          category list, for a dropdown filter
    GET /api/stocks              stock scores, searchable
    GET /api/backtest/snapshot   score at a past date vs the return since
    GET /api/backtest/series     each fund's score at every scored date
    GET /api/backtest/categories which categories have data for each section
"""

import hashlib
import json
import os
import re
import secrets
import smtplib
import urllib.parse
import urllib.request
from datetime import date, timedelta
from email.message import EmailMessage
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from fastapi import (FastAPI, HTTPException, Query, Request, Response,
                     Depends)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

load_dotenv()

DB = os.getenv("FINCHAYA_DB")
if not DB:
    raise RuntimeError("FINCHAYA_DB is not set. Check that .env exists "
                       "alongside api.py.")

STOCK_ALGO = "stock-v3"
FUND_ALGO = "fund-v2"

# The experimental engine (top 50 pct of NAV, legacy ADX, raw weighted sum).
# NOT a product feature: it is unvalidated, and on the one fund tested so far
# it predicted nothing. It is returned only to the addresses in LAB_EMAILS so
# it can be eyeballed on real fund pages without any paying user seeing a
# second score next to the real one and reading it as a second opinion.
LEGACY_FUND_ALGO = "fund-v1l"
LAB_EMAILS = {e.strip().lower()
              for e in os.getenv("LAB_EMAILS", "").split(",") if e.strip()}

# ---------------------------------------------------------------------
# AUTH AND PAYWALL CONFIG
#
# MF_PAYWALL_ENABLED defaults to FALSE. Deploying this file changes
# nothing for anyone until you set it to true. That matters because the
# paywall cannot ship before login works -- turn on gating with no way
# to sign in and the scores disappear for everybody, you included.
# ---------------------------------------------------------------------
PAYWALL_ENABLED = os.getenv("MF_PAYWALL_ENABLED", "false").lower() == "true"

# SCORES SWITCH. FinChaya's scores, ranks, contributions, score history and
# the "rising / sideways / falling" split built from them stay OFF until the
# SEBI Research Analyst registration is in place. Unlike the paywall this
# defaults to FALSE and is a hard off: it applies to everybody, subscribers
# included, and every route that would return a score-type field checks it.
# Set MF_SCORES_ENABLED=true in /opt/mfapi/.env to bring them all back.
SCORES_ENABLED = os.getenv("MF_SCORES_ENABLED", "false").lower() == "true"

SESSION_COOKIE = "fc_session"
SESSION_DAYS   = int(os.getenv("MF_SESSION_DAYS", "90"))
OTP_TTL_MIN    = int(os.getenv("MF_OTP_TTL_MIN", "10"))
OTP_MAX_ATTEMPTS = 5
OTP_MAX_PER_HOUR = 5

IS_PRODUCTION = os.getenv("MF_ENV", "dev").lower() == "production"

# Development convenience: a FIXED OTP for listed addresses only. Same
# code path, same tables, real bugs found early -- only the accepted
# code differs. NEVER set these on the droplet.
DEV_OTP    = os.getenv("MF_AUTH_DEV_OTP")
DEV_EMAILS = {e.strip().lower()
              for e in os.getenv("MF_AUTH_DEV_EMAILS", "").split(",")
              if e.strip()}

if IS_PRODUCTION and DEV_OTP:
    raise RuntimeError(
        "MF_AUTH_DEV_OTP is set while MF_ENV=production. A development "
        "bypass reaching production is how user bases get lost. Remove it "
        "from .env on this machine.")

SMTP_HOST = os.getenv("MF_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("MF_SMTP_PORT", "587"))
SMTP_USER = os.getenv("MF_SMTP_USER")
SMTP_PASS = os.getenv("MF_SMTP_PASS")
MAIL_FROM = os.getenv("MF_MAIL_FROM", SMTP_USER or "no-reply@finchaya.com")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Google Sign-In. The client ID is public -- it is embedded in the page
# and visible to anyone. It is an identifier, not a secret. The client
# SECRET is not used by this flow and must not be put here.
GOOGLE_CLIENT_ID = os.getenv("MF_GOOGLE_CLIENT_ID", "")
GOOGLE_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"

app = FastAPI(
    title="FinChaya Fund Health API",
    description="Read-only access to daily fund and stock health scores.",
    version="1.0.0",
)

# CORS lets a web page served from one address call an API on another.
# Without it the browser silently blocks the request -- a confusing first
# bug, because the API log shows success while the page shows nothing.
# Tighten this to your real domain before going public.
# Cookies are only sent cross-origin when allow_credentials is on AND
# the origin is named explicitly -- browsers reject credentials with a
# "*" wildcard. index.html is served by this same app, so the normal
# case is same-origin and CORS does not apply at all. Set
# MF_CORS_ORIGINS if you ever serve the page from elsewhere.
_origins = [o.strip() for o in os.getenv("MF_CORS_ORIGINS", "").split(",")
            if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=bool(_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------
# Portfolio look-through, kept in its own module.
#
# It opens its own connection and imports nothing from here, so it cannot
# break an existing endpoint, and deleting these two lines plus the
# /portfolio route below removes the feature completely.
# ---------------------------------------------------------------------
from portfolio_api import router as portfolio_router      # noqa: E402

app.include_router(portfolio_router)

# Saved portfolios: the same look-through, kept against a user so it
# survives the tab closing. Imports its shared pieces from portfolio_api
# rather than from here, so this file gains no new coupling and the two
# lines below remove the feature completely.
from saved_portfolio_api import router as saved_portfolio_router  # noqa: E402

app.include_router(saved_portfolio_router)


# ---------------------------------------------------------------------
# The page itself.
#
# index.html sits next to this file and is read from disk per request,
# so a deploy of the page needs no restart. Registered LAST in the file
# but matched by path, so it never shadows /api/*.
#
# Resolved relative to this file rather than the working directory:
# systemd starts the service from /, so a bare "index.html" would not
# be found.
# ---------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE_DIR, "index.html")


@app.get("/", include_in_schema=False)
def home():
    if not os.path.exists(INDEX_HTML):
        raise HTTPException(404, "index.html is missing from %s" % BASE_DIR)
    # no-store: the scores change daily and a cached page would show
    # yesterday's numbers with today's date on them.
    return FileResponse(INDEX_HTML, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/index.html", include_in_schema=False)
def home_alias():
    return home()


# ---------------------------------------------------------------------
# Static legal pages.
#
# These are REAL URLs, not hash routes -- Google fetches them when
# publishing the OAuth app and a 404 fails the check. They also have to
# be reachable without signing in, for obvious reasons.
# ---------------------------------------------------------------------
def _static_page(filename: str):
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "%s is missing from %s" % (filename, BASE_DIR))
    return FileResponse(path, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/privacy", include_in_schema=False)
def privacy_page():
    return _static_page("privacy.html")


@app.get("/terms", include_in_schema=False)
def terms_page():
    return _static_page("terms.html")


# Cancellation and refund policy. A payment processor expects this at its
# own address rather than as a paragraph inside the terms.
@app.get("/refund", include_in_schema=False)
def refund_page():
    return _static_page("refund.html")


# The look-through page. Same treatment as the pages above: read from disk
# per request, so publishing a new portfolio.html needs no restart.
@app.get("/portfolio", include_in_schema=False)
def portfolio_page():
    return _static_page("portfolio.html")


# The saved portfolios page. Note the S: /portfolio is the one-shot
# look-through and is unchanged, /portfolios is the list you keep.
@app.get("/portfolios", include_in_schema=False)
def portfolios_page():
    return _static_page("portfolios.html")


# The overlap check, on its own. The planner answers the same question on
# its funds tab, but only for somebody who has already set a goal and
# worked through two tabs to reach it. "Do these two funds hold the same
# thing" is a question people ARRIVE with -- an existing investor with
# four funds and a suspicion, not somebody starting a plan -- so it gets
# its own URL and no prerequisites.
@app.get("/overlap", include_in_schema=False)
def overlap_page():
    return _static_page("overlap.html")


# Pick a rule -- beat the benchmark, sit in the category's top quartile,
# clear a Sharpe/Sortino floor, clear a rolling-return hurdle most of the
# time -- and see which funds meet it. Reads /api/portfolio/screen-universe,
# the same bulk payload plan.html's fund finder already uses, so a click
# here costs no extra round trip.
@app.get("/screener", include_in_schema=False)
def screener_page():
    return _static_page("screener.html")




from distributor_api import router as distributor_router   # noqa: E402

app.include_router(distributor_router)


@app.get("/clients", include_in_schema=False)
def clients_page():
    return _static_page("clients.html")


@app.get("/allocation", include_in_schema=False)
def allocation_page():
    return _static_page("allocation.html")


from selector import router as selector_router                # noqa: E402

app.include_router(selector_router)


@app.get("/suggest", include_in_schema=False)
def suggest_page():
    return _static_page("suggest.html")


# ---------------------------------------------------------------------
# ADMIN: the only surface that looks across users.
#
# Every other portfolio endpoint is scoped to the caller. This one is
# not, which is exactly why it checks app_admin on the server for every
# call rather than relying on the page being hard to find -- a URL is
# not a permission, and the page below is only HTML.
#
# The drill-down writes to admin_access_log before returning anyone's
# holdings, and refuses to serve them at all if that table is missing.
# Run create_admin_log.py --admin doadmin once before using it.
#
# These three lines plus admin_api.py and admin.html are the whole
# feature; deleting them removes it completely.
# ---------------------------------------------------------------------
from admin_api import router as admin_router                  # noqa: E402

app.include_router(admin_router)

# OPTIONAL MODULES. Neither of these is needed to show a fund.
#
# They were imported bare, and the comment beside them claimed that a
# billing problem could not stop the site serving fund data. That was
# half true: it covered a missing API key, and it did not cover a
# missing FILE. If payments.py is not on the server -- because it was
# written in one sitting and deployed in another, which is the normal
# way this gets done -- then `from payments import router` raises
# ModuleNotFoundError while the app is still being built, uvicorn never
# finishes starting, and nginx has nothing to talk to. The visitor gets
# a bare 502 on every page of the site, including all the pages that
# have nothing whatever to do with billing.
#
# A 502 also says nothing about the cause. The traceback is in the
# service log, and the log is the last place anybody looks when a site
# that worked ten minutes ago has stopped.
#
# So each optional router is mounted on its own, and a failure costs
# exactly its own endpoints. The message goes to stdout, which systemd
# captures, so `journalctl -u mfapi` says in one line which feature is
# missing and why -- instead of a stack trace and a dead site.
def _mount_optional(module, what):
    try:
        mod = __import__(module)
        app.include_router(getattr(mod, "router"))
        return True
    except ImportError as e:
        # ONLY the import. A bug inside a module that does exist still
        # raises and still stops the boot, because that is a mistake
        # somebody needs to see rather than route around.
        print("mfapi: %s is not available (%s: %s). Its endpoints will "
              "404; the rest of the site is unaffected."
              % (what, module, e), flush=True)
        return False


# Razorpay subscriptions. The endpoints answer 503 when the keys are
# unset, and 404 when the module itself was never deployed.
_mount_optional("payments", "subscription billing")

# Handing a finished plan over to wherever the person already invests.
# Outbound links only -- no order placement, no credentials, and no
# referral codes. See the module docstring for why both of those are
# deliberate rather than unfinished.
_mount_optional("brokers", "broker hand-off")

# Stock screeners. Four of the seven read the fund holdings rather than the
# chart, which is the half of this database nobody else has; the other
# three are indicator crossings, on a timeframe the reader chooses.
from screener_api import router as screener_router      # noqa: E402
app.include_router(screener_router)


@app.get("/admin", include_in_schema=False)
def admin_page():
    # Served to anyone who asks -- it is a static file and pretending
    # otherwise would be security theatre. It renders nothing without the
    # API behind it, and the API refuses non-admins.
    return _static_page("admin.html")


# The merged flow: set a goal, see what it needs, choose funds, check
# them, lock it. "Review" only described the middle of that.
@app.get("/plan", include_in_schema=False)
def plan_page():
    return _static_page("plan.html")


# The two pages this replaced. Kept as redirects rather than removed:
# links and bookmarks outlive the pages they point at, and a 404 tells a
# visitor the product is broken rather than that it moved.
@app.get("/review", include_in_schema=False)
def review_redirect():
    return RedirectResponse("/plan", status_code=308)


@app.get("/goals", include_in_schema=False)
def goals_redirect():
    return RedirectResponse("/plan", status_code=308)


@app.get("/finchaya.js", include_in_schema=False)
def sitescript():
    path = os.path.join(BASE_DIR, "finchaya.js")
    if not os.path.exists(path):
        raise HTTPException(404, "finchaya.js is missing from %s" % BASE_DIR)
    return FileResponse(path, media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=3600"})


# The shared stylesheet. Cached for an hour rather than the no-store the
# HTML pages use: it changes far less often than they do, and every page
# on the site fetches it.
@app.get("/finchaya.css", include_in_schema=False)
def stylesheet():
    path = os.path.join(BASE_DIR, "finchaya.css")
    if not os.path.exists(path):
        raise HTTPException(404, "finchaya.css is missing from %s" % BASE_DIR)
    return FileResponse(path, media_type="text/css",
                        headers={"Cache-Control": "public, max-age=3600"})


def query(sql, params=None, one=False):
    """Every endpoint goes through here. Opens a connection, runs one
    statement, returns dictionaries rather than tuples so FastAPI can
    turn them straight into JSON."""
    with psycopg.connect(DB, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return cur.fetchone() if one else cur.fetchall()


def query_optional(sql, params=None):
    """A query whose TABLE may not exist yet, and that is not an error.

    api.py is deployed by scp and the builders are run afterwards, by
    hand, sometimes days later. Between those two moments a plain
    query() against fund_capture or fund_turnover raises
    UndefinedTable, FastAPI turns it into a 500, and the entire fund
    page goes blank -- not the new panel, the whole page, including the
    returns and holdings that were working perfectly a minute earlier.

    An absent table means the figures have not been built yet, which is
    a thing the page already knows how to say. So it is caught here and
    returned as no rows.

    IT ALSO CATCHES SCHEMA DRIFT, and that is a considered trade.

    The first version swallowed only UndefinedTable, on the reasoning
    that a missing column or a bad join is a bug and ought to be loud.
    Then a join written to put a benchmark's NAME on the page hit a
    type mismatch -- fund_capture.benchmark_id is text,
    benchmark_master.benchmark_id is an integer -- and Postgres refused
    it. Not a missing table, so it raised; FastAPI turned it into a
    500; and every fund page on the site showed "No score for this
    fund". A decorative panel took down the returns, the holdings and
    the score with it.

    That is the wrong failure. These queries feed panels that the page
    already knows how to render without. So a query that cannot run
    costs its own panel and nothing else, and the reason goes to
    stdout where systemd keeps it -- loud in the log, invisible to the
    visitor, which is the right way round.

    A connection or permission failure still raises: those are not one
    panel's problem.
    """
    try:
        return query(sql, params)
    except (psycopg.errors.UndefinedTable,
            psycopg.errors.UndefinedColumn,
            psycopg.errors.UndefinedFunction,
            psycopg.errors.DatatypeMismatch,
            psycopg.errors.SyntaxError) as e:
        print("mfapi: an optional query failed and its panel will be "
              "omitted. Fix this -- it is a bug, not a missing feature.\n"
              "       %s: %s\n       %s"
              % (type(e).__name__, str(e).split("\n")[0],
                 " ".join(sql.split())[:200]), flush=True)
        return []


def execute(sql, params=None, returning=False):
    """Writes. query() calls fetchall(), which raises on a statement
    that returns no rows -- so INSERT/UPDATE/DELETE come through here."""
    with psycopg.connect(DB, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return cur.fetchone() if returning else None


# =====================================================================
# AUTH
#
# Nothing here stores a secret in the clear. The OTP and the session
# token are both held as SHA-256 hashes; a copy of these tables yields
# no working code and no live session.
#
# SHA-256 is the RIGHT choice for these two and the WRONG choice for a
# PIN. A session token is 32 random bytes -- unguessable regardless of
# how fast the hash is. A PIN is six digits, so speed is the attack;
# that one needs bcrypt. See ACCOUNTS_AND_AUTH.md.
# =====================================================================
def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class OtpRequest(BaseModel):
    email: str


class OtpVerify(BaseModel):
    email: str
    code: str


def current_user(request: Request) -> Optional[dict]:
    """The signed-in user, or None. Cookie first, then a bearer header
    so the API stays usable from a script."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        return None

    return query("""
        SELECT u.user_id, u.email, u.account_type,
               u.is_premium, u.premium_until, u.full_name
        FROM mf_session s
        JOIN mf_user u USING (user_id)
        WHERE s.token_hash = %(h)s
          AND s.revoked_at IS NULL
          AND s.expires_at > now()
          AND u.is_active
    """, {"h": sha256_hex(token)}, one=True)


def has_subscription(user: Optional[dict]) -> bool:
    """Premium gate. With the paywall off, everyone passes -- which is
    what makes this file safe to deploy before login exists.

    This is the answer for the PAID PRODUCTS (look-through, screener, plan).
    Whether SCORES are shown is a separate question: has_score_access()."""
    if not PAYWALL_ENABLED:
        return True
    if not user or not user["is_premium"]:
        return False
    until = user["premium_until"]
    return until is None or until >= date.today()


def has_score_access(user: Optional[dict]) -> bool:
    """May this caller see a score-type field? Only if scores are switched
    on at all AND the caller is subscribed. Every place that strips score
    fields already calls this, so switching scores off strips them for
    everyone, subscribers included, without touching those call sites."""
    return SCORES_ENABLED and has_subscription(user)


def require_scores_on():
    """For routes that are score-derived through and through and have no
    gate of their own (the 'does the score work' evidence endpoints). With
    scores off there is nothing honest to return, so they 404."""
    if not SCORES_ENABLED:
        raise HTTPException(404, "Not available yet.")


def send_otp_email(to_addr: str, code: str) -> None:
    if not (SMTP_USER and SMTP_PASS):
        raise RuntimeError("SMTP is not configured (MF_SMTP_USER / MF_SMTP_PASS)")
    msg = EmailMessage()
    msg["Subject"] = "Your FinChaya sign-in code"
    msg["From"] = MAIL_FROM
    msg["To"] = to_addr
    msg.set_content(
        "Your FinChaya sign-in code is %s.\n\n"
        "It expires in %d minutes. If you did not ask for it, ignore "
        "this email -- nobody can sign in without it.\n" % (code, OTP_TTL_MIN))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


@app.post("/api/auth/request-otp", tags=["auth"])
def request_otp(body: OtpRequest):
    """Issue a code. Deliberately says the same thing whether or not the
    address has an account -- otherwise this endpoint becomes a way to
    ask which of a list of emails are customers."""
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "that does not look like an email address")

    recent = query("""
        SELECT COUNT(*) AS n FROM mf_otp
        WHERE lower(email) = %(e)s AND created_at > now() - interval '1 hour'
    """, {"e": email}, one=True)["n"]
    if recent >= OTP_MAX_PER_HOUR:
        raise HTTPException(429, "too many codes requested. Try again in an hour.")

    dev = DEV_OTP and email in DEV_EMAILS
    code = DEV_OTP if dev else "%06d" % secrets.randbelow(1_000_000)

    execute("""
        INSERT INTO mf_otp (email, code_hash, expires_at)
        VALUES (%(e)s, %(h)s, now() + make_interval(mins => %(ttl)s))
    """, {"e": email, "h": sha256_hex(code), "ttl": OTP_TTL_MIN})

    if dev:
        print("[DEV OTP] fixed code accepted for %s -- MF_AUTH_DEV_OTP is "
              "set. This must never appear in production logs." % email)
    else:
        try:
            send_otp_email(email, code)
        except Exception as exc:                      # noqa: BLE001
            print("OTP send failed for %s: %s" % (email, exc))
            raise HTTPException(502, "could not send the code. Try again.")

    return {"sent": True, "expires_in_minutes": OTP_TTL_MIN}


@app.post("/api/auth/verify-otp", tags=["auth"])
def verify_otp(body: OtpVerify, response: Response):
    """Check the code, create the account if this is a first sign-in,
    and open a session."""
    email = body.email.strip().lower()
    code = body.code.strip()

    row = query("""
        SELECT otp_id, code_hash, attempts
        FROM mf_otp
        WHERE lower(email) = %(e)s
          AND consumed_at IS NULL
          AND expires_at > now()
        ORDER BY created_at DESC LIMIT 1
    """, {"e": email}, one=True)

    if not row:
        raise HTTPException(400, "that code has expired. Request a new one.")
    if row["attempts"] >= OTP_MAX_ATTEMPTS:
        raise HTTPException(429, "too many attempts. Request a new code.")

    if row["code_hash"] != sha256_hex(code):
        execute("UPDATE mf_otp SET attempts = attempts + 1 WHERE otp_id = %(i)s",
                {"i": row["otp_id"]})
        raise HTTPException(400, "that code is not right.")

    execute("UPDATE mf_otp SET consumed_at = now() WHERE otp_id = %(i)s",
            {"i": row["otp_id"]})
    # Any other live code for this address dies with it -- a resent code
    # should not leave the earlier one usable.
    execute("""
        UPDATE mf_otp SET consumed_at = now()
        WHERE lower(email) = %(e)s AND consumed_at IS NULL
    """, {"e": email})

    user = query("SELECT user_id FROM mf_user WHERE lower(email) = %(e)s",
                 {"e": email}, one=True)
    if user:
        execute("UPDATE mf_user SET last_login_at = now() WHERE user_id = %(u)s",
                {"u": user["user_id"]})
        user_id = user["user_id"]
    else:
        user_id = execute("""
            INSERT INTO mf_user (email, last_login_at)
            VALUES (%(e)s, now()) RETURNING user_id
        """, {"e": email}, returning=True)["user_id"]

    token = secrets.token_urlsafe(32)
    execute("""
        INSERT INTO mf_session (token_hash, user_id, expires_at)
        VALUES (%(h)s, %(u)s, now() + make_interval(days => %(d)s))
    """, {"h": sha256_hex(token), "u": user_id, "d": SESSION_DAYS})

    # httponly: JavaScript cannot read it, so an XSS bug cannot steal
    # the session. samesite=lax keeps it off cross-site requests.
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=IS_PRODUCTION, path="/")

    me = query("""
        SELECT user_id, email, account_type, is_premium, premium_until, full_name
        FROM mf_user WHERE user_id = %(u)s
    """, {"u": user_id}, one=True)
    return {"user": me, "token": token,
            "score_access": has_score_access(me),
            "subscribed": has_subscription(me),
            "scores_enabled": SCORES_ENABLED}


class GoogleToken(BaseModel):
    credential: str


def verify_google_token(credential: str) -> dict:
    """Check a Google ID token and return its claims.

    Verification happens at Google's tokeninfo endpoint rather than by
    checking the signature locally. Local checks are faster but need a
    JWT library and Google's rotating public keys; at this volume the
    round trip costs nothing and there is no dependency to break on the
    droplet. It runs over HTTPS on 443 -- the port that works here.

    NEVER trust the token without this call. Anyone can post a
    hand-written JSON blob to this endpoint; the signature is the only
    thing that makes it evidence of anything."""
    url = GOOGLE_TOKENINFO + "?" + urllib.parse.urlencode({"id_token": credential})
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            claims = json.loads(resp.read().decode())
    except Exception as exc:                              # noqa: BLE001
        print("Google token verification failed: %s" % exc)
        raise HTTPException(401, "could not verify that Google sign-in")

    # aud is the check that matters most. A token issued for SOMEONE
    # ELSE'S app is perfectly valid and correctly signed -- it just is
    # not for us. Skipping this lets any Google app's token in.
    if claims.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(401, "that sign-in was issued for a different app")

    if claims.get("iss") not in ("accounts.google.com",
                                 "https://accounts.google.com"):
        raise HTTPException(401, "unexpected token issuer")

    # Google returns this as the string "true", not a boolean.
    if str(claims.get("email_verified", "")).lower() != "true":
        raise HTTPException(401, "that Google account has no verified email")

    email = (claims.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(401, "no usable email on that Google account")

    return claims


@app.post("/api/auth/google", tags=["auth"])
def auth_google(body: GoogleToken, response: Response):
    """Sign in with Google. Creates the account on first use.

    Downstream of this nothing is different: the same mf_user row, the
    same mf_session row, the same cookie. Only the way identity is
    proved has changed."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in is not configured on this server")

    claims = verify_google_token(body.credential)
    email = claims["email"].strip().lower()
    name = (claims.get("name") or "").strip() or None

    user = query("SELECT user_id, full_name FROM mf_user WHERE lower(email) = %(e)s",
                 {"e": email}, one=True)
    if user:
        user_id = user["user_id"]
        execute("""UPDATE mf_user
                      SET last_login_at = now(),
                          full_name = COALESCE(full_name, %(n)s)
                    WHERE user_id = %(u)s""",
                {"u": user_id, "n": name})
    else:
        user_id = execute("""
            INSERT INTO mf_user (email, full_name, last_login_at)
            VALUES (%(e)s, %(n)s, now()) RETURNING user_id
        """, {"e": email, "n": name}, returning=True)["user_id"]

    token = secrets.token_urlsafe(32)
    execute("""
        INSERT INTO mf_session (token_hash, user_id, expires_at)
        VALUES (%(h)s, %(u)s, now() + make_interval(days => %(d)s))
    """, {"h": sha256_hex(token), "u": user_id, "d": SESSION_DAYS})

    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=IS_PRODUCTION, path="/")

    me = query("""
        SELECT user_id, email, account_type, is_premium, premium_until, full_name
        FROM mf_user WHERE user_id = %(u)s
    """, {"u": user_id}, one=True)
    return {"user": me, "token": token,
            "score_access": has_score_access(me),
            "subscribed": has_subscription(me),
            "scores_enabled": SCORES_ENABLED}


@app.get("/api/auth/me", tags=["auth"])
def auth_me(user: Optional[dict] = Depends(current_user)):
    """Who am I, and can I see scores. The page calls this on load."""
    return {"signed_in": user is not None,
            "user": user,
            "paywall_enabled": PAYWALL_ENABLED,
            "score_access": has_score_access(user),
            # subscribed = may use the paid products (look-through,
            # screener, plan). scores_enabled = scores exist at all right
            # now. The page needs both: "locked" and "coming soon" are
            # different messages.
            "subscribed": has_subscription(user),
            "scores_enabled": SCORES_ENABLED,
            # The page needs this to render the Google button. Public by
            # design -- it identifies the app, it does not authorise it.
            "google_client_id": GOOGLE_CLIENT_ID}


@app.post("/api/auth/logout", tags=["auth"])
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        # Revoked, not deleted: the record that a session existed and
        # when it ended is worth keeping.
        execute("""UPDATE mf_session SET revoked_at = now()
                   WHERE token_hash = %(h)s AND revoked_at IS NULL""",
                {"h": sha256_hex(token)})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"signed_out": True}


# =====================================================================
# THE PAYWALL
#
# Hiding the number in index.html is NOT gating it. /api/funds returns
# health_score_100 in its JSON and SORTS BY IT -- so even with the
# figure hidden, sort=score&order=desc hands over the whole ranking to
# anyone who opens the Network tab. Ordering leaks the field as surely
# as printing it.
#
# Hence two rules, both server-side:
#   1. strip the score fields from the payload
#   2. refuse to sort by score or rank
# =====================================================================
FUND_SCORE_FIELDS = ("health_score", "score_100", "category_rank",
                     "momentum", "trend", "strength")

HOLDING_SCORE_FIELDS = ("total_score", "contribution", "adx_score",
                        "macd_score", "rsi_score", "bb_score",
                        "supertrend_score")

# Stock scores are the ingredients of a fund score. Left open, they can
# be recombined with the freely available holding weights to rebuild the
# number the paywall is meant to protect.
STOCK_SCORE_FIELDS = ("total_score", "adx_score", "macd_score",
                      "rsi_score", "bb_score", "supertrend_score")


def strip_fields(rows, fields):
    if isinstance(rows, dict):
        return {k: v for k, v in rows.items() if k not in fields}
    return [{k: v for k, v in r.items() if k not in fields} for r in rows]


# =====================================================================
@app.get("/api/health", tags=["system"])
def health():
    """Is the service up, and how stale is the data?

    Worth having from day one: when the page looks wrong, the first
    question is always 'did the batch job run?' -- and this answers it
    without opening pgAdmin."""
    try:
        row = query("""
            SELECT
              (SELECT MAX(as_of_date) FROM mf_score
                WHERE algo_version = %(fund)s)          AS fund_scores_as_of,
              (SELECT COUNT(*) FROM mf_score
                WHERE algo_version = %(fund)s)          AS fund_score_count,
              (SELECT MAX(as_of_date) FROM stock_score
                WHERE algo_version = %(stock)s)         AS stock_scores_as_of,
              (SELECT COUNT(*) FROM stock_score
                WHERE algo_version = %(stock)s)         AS stock_score_count,
              (SELECT MAX(as_of_date) FROM mf_holding)  AS holdings_as_of
        """, {"fund": FUND_ALGO, "stock": STOCK_ALGO}, one=True)
        row["status"] = "ok"
        row["database"] = "connected"
        return row
    except Exception as e:
        raise HTTPException(503, f"database unavailable: {e}")


# =====================================================================
@app.get("/api/funds", tags=["funds"])
def list_funds(
    search: Optional[str] = Query(None, description="match fund or AMC name"),
    category: Optional[str] = Query(None, description="exact sub-category"),
    min_coverage: float = Query(0, ge=0, le=100),
    sort: str = Query("score", pattern="^(score|name|rank|coverage)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: Optional[dict] = Depends(current_user),
):
    """The main list endpoint. Search, filter, sort and page.

    Paging matters even at 400 funds: the browser has to render every row
    it receives, and 'just send everything' stops being fine the moment
    you load all 14,285 schemes."""

    unlocked = has_score_access(user)

    # sort defaults to "score", so rejecting it outright would 403 the
    # ordinary anonymous request that loads the page. Fall back to name
    # instead and SAY SO in the response, so the UI can show the lock
    # rather than silently reordering behind the user's back.
    if not unlocked and (sort in ("score", "rank")
                         or (sort == "coverage" and not SCORES_ENABLED)):
        sort, order = "name", "asc"

    sort_column = {
        "score": "s.health_score",
        "name": "m.scheme_name",
        "rank": "s.category_rank",
        "coverage": "s.coverage_pct",
    }[sort]
    direction = "DESC" if order == "desc" else "ASC"

    where = ["s.algo_version = %(algo)s",
             "s.as_of_date = (SELECT MAX(as_of_date) FROM mf_score "
             "                 WHERE algo_version = %(algo)s)",
             "s.coverage_pct >= %(min_cov)s"]
    params = {"algo": FUND_ALGO, "min_cov": min_coverage,
              "limit": limit, "offset": offset}

    if search:
        # WORD-BY-WORD, PUNCTUATION-BLIND.
        #
        # A plain ILIKE '%hsbc midcap%' fails against "HSBC Mid Cap Fund"
        # because of one space -- the same AMFI/CRISIL disagreement that
        # norm() handles in map_benchmarks.py. So both sides are stripped
        # of everything that is not a letter or digit before comparing.
        #
        # The query is then split into words and EVERY word must appear.
        # That buys order-independence for free: "midcap hsbc" finds the
        # same fund as "hsbc midcap", which matters because people type
        # the distinctive word first.
        NORM = ("regexp_replace(lower(%s), '[^a-z0-9]', '', 'g')")
        name_n = NORM % "m.scheme_name"
        amc_n = NORM % "m.amc_name"

        words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in search.split()]
        words = [w for w in words if w][:6]      # cap it; 6 is generous

        for i, w in enumerate(words):
            key = "s%d" % i
            where.append("(%s LIKE %%(%s)s OR %s LIKE %%(%s)s)"
                         % (name_n, key, amc_n, key))
            params[key] = "%" + w + "%"
    if category:
        where.append("c.category = %(category)s")
        params["category"] = category

    clause = " AND ".join(where)

    total = query(f"""
        SELECT COUNT(*) AS n
        FROM mf_score s
        JOIN mf_scheme m USING (scheme_code)
        JOIN v_scheme_category c USING (scheme_code)
        WHERE {clause}
    """, params, one=True)["n"]

    rows = query(f"""
        SELECT s.scheme_code,
               m.scheme_name,
               m.amc_name,
               c.category,
               c.rank_meaningful,
               ROUND(s.health_score, 2)      AS health_score,
               ROUND(s.health_score_100, 1)  AS score_100,
               s.category_rank,
               ROUND(s.coverage_pct, 1)      AS coverage_pct,
               s.scored_holdings,
               s.total_holdings,
               ROUND(s.momentum_score, 2)    AS momentum,
               ROUND(s.trend_score, 2)       AS trend,
               ROUND(s.strength_score, 2)    AS strength,
               s.as_of_date,
               s.holdings_as_of_date,
               (SELECT ROUND(nav, 4) FROM mf_nav n
                 WHERE n.scheme_code = s.scheme_code
                 ORDER BY nav_date DESC LIMIT 1) AS nav,
               -- The NAV's own date. Without it the page presents a
               -- three-day-old figure as though it were today's.
               (SELECT nav_date FROM mf_nav n
                 WHERE n.scheme_code = s.scheme_code
                 ORDER BY nav_date DESC LIMIT 1) AS nav_date
        FROM mf_score s
        JOIN mf_scheme m USING (scheme_code)
        JOIN v_scheme_category c USING (scheme_code)
        WHERE {clause}
        ORDER BY {sort_column} {direction} NULLS LAST, m.scheme_name
        LIMIT %(limit)s OFFSET %(offset)s
    """, params)

    if not unlocked:
        rows = strip_fields(rows, FUND_SCORE_FIELDS)
        if not SCORES_ENABLED:
            rows = strip_fields(rows, ("coverage_pct", "scored_holdings"))

    return {"total": total, "limit": limit, "offset": offset,
            "funds": rows,
            "score_locked": not unlocked,
            "scores_enabled": SCORES_ENABLED,
            "sort_applied": sort}


# =====================================================================
@app.get("/api/funds/{scheme_code}", tags=["funds"])
def fund_detail(scheme_code: str,
                holdings_limit: int = Query(30, ge=1, le=200),
                user: Optional[dict] = Depends(current_user)):
    """One fund, plus the holdings that produced its score.

    This is the endpoint that makes the score defensible rather than
    mysterious -- a user can see which positions helped and which hurt."""

    fund = query("""
        SELECT s.scheme_code, m.scheme_name, m.amc_name,
               c.category, c.rank_meaningful,
               m.plan_type, m.option_type,
               ROUND(s.health_score, 2)     AS health_score,
               ROUND(s.health_score_100, 1) AS score_100,
               s.category_rank,
               ROUND(s.coverage_pct, 1)     AS coverage_pct,
               s.scored_holdings, s.total_holdings,
               ROUND(s.momentum_score, 2)   AS momentum,
               ROUND(s.trend_score, 2)      AS trend,
               ROUND(s.strength_score, 2)   AS strength,
               s.as_of_date, s.holdings_as_of_date, s.algo_version,
               (SELECT ROUND(nav, 4) FROM mf_nav n
                 WHERE n.scheme_code = s.scheme_code
                 ORDER BY nav_date DESC LIMIT 1) AS nav,
               (SELECT nav_date FROM mf_nav n
                 WHERE n.scheme_code = s.scheme_code
                 ORDER BY nav_date DESC LIMIT 1) AS nav_date
        FROM mf_score s
        JOIN mf_scheme m USING (scheme_code)
        JOIN v_scheme_category c USING (scheme_code)
        WHERE s.scheme_code = %(code)s AND s.algo_version = %(algo)s
        ORDER BY s.as_of_date DESC LIMIT 1
    """, {"code": scheme_code, "algo": FUND_ALGO}, one=True)

    if not fund:
        raise HTTPException(404, f"no score for scheme {scheme_code}")

    holdings = query("""
        WITH latest AS (
            SELECT MAX(as_of_date) AS d FROM mf_holding
            WHERE scheme_code = %(code)s
        ),
        scores AS (
            SELECT DISTINCT ON (isin) isin, total_score,
                   adx_score, macd_score, rsi_score, bb_score, supertrend_score
            FROM stock_score WHERE algo_version = %(algo)s
            ORDER BY isin, as_of_date DESC
        )
        SELECT h.instrument_name,
               sm.symbol,
               h.isin,
               h.instrument_type,
               ROUND(h.pct_of_nav, 3)  AS pct_of_nav,
               sc.total_score,
               ROUND(h.pct_of_nav * sc.total_score, 2) AS contribution,
               sc.adx_score, sc.macd_score, sc.rsi_score,
               sc.bb_score, sc.supertrend_score
        FROM mf_holding h
        CROSS JOIN latest
        LEFT JOIN stock_master sm ON sm.isin = h.isin
        LEFT JOIN scores sc       ON sc.isin = h.isin
        WHERE h.scheme_code = %(code)s AND h.as_of_date = latest.d
        ORDER BY h.pct_of_nav DESC NULLS LAST
        LIMIT %(lim)s
    """, {"code": scheme_code, "algo": STOCK_ALGO, "lim": holdings_limit})

    # Trailing returns, against two yardsticks.
    #
    # BENCHMARK vs CATEGORY are different questions and the page should not
    # blur them. The benchmark is the index the fund measures itself
    # against; the category median is what a typical fund in the same peer
    # group returned. A fund can beat its benchmark and still sit in the
    # bottom half of its category, and that is worth seeing.
    #
    # bench_* is null where no benchmark is mapped -- the honest state for
    # a sectoral fund whose index we do not hold. The category median is
    # populated far more often, so for most funds it is the only
    # comparison available.
    #
    # NOTE ON THE TWO RANKS: category_rank here ranks by RETURN, per
    # period. mf_score.category_rank in the fund object above ranks by
    # HOLDINGS HEALTH. Same category, different question -- the page must
    # label them distinctly or a fund placed 3rd on one and 22nd on the
    # other reads as a bug.
    #
    # rank_meaningful is false for groups where a rank says nothing
    # (ETFs, Index Funds, Sectoral/Thematic). The count is still returned,
    # so the page can show the peer size without implying a placing.
    returns = query("""
        SELECT r.period,
               r.start_date, r.end_date,
               ROUND(r.years, 2)        AS years,
               ROUND(r.fund_cagr, 2)    AS fund_cagr,
               ROUND(r.bench_cagr, 2)   AS bench_cagr,
               ROUND(r.excess_cagr, 2)  AS excess_cagr,
               b.display_name           AS benchmark_name,
               r.match_type,
               r.category,
               r.category_rank          AS return_rank,
               r.category_count         AS return_peer_count,
               ROUND(cr.median_cagr, 2) AS category_median_cagr,
               ROUND(r.fund_cagr - cr.median_cagr, 2) AS vs_category,
               ROUND(cr.p25_cagr, 2)    AS category_p25_cagr,
               ROUND(cr.p75_cagr, 2)    AS category_p75_cagr,
               COALESCE(cr.rank_meaningful, false) AS rank_meaningful,
               (r.nav_scheme_code IS NOT NULL
                AND r.nav_scheme_code <> r.scheme_code) AS nav_substituted
        FROM mf_returns r
        LEFT JOIN benchmark_master b USING (benchmark_id)
        LEFT JOIN mf_category_return cr
               ON cr.category   = r.category
              AND cr.as_of_date = r.as_of_date
              AND cr.period     = r.period
        WHERE r.scheme_code = %(code)s
          AND r.as_of_date = (SELECT MAX(as_of_date) FROM mf_returns)
        ORDER BY r.years
    """, {"code": scheme_code})

    # Score history -- ONLY where we hold that month's portfolio.
    #
    # score_funds.py scores every fund at whatever date it is given, using
    # each fund's most recent portfolio at or before that date. So running
    # it for twelve months produces twelve rows for EVERY fund, even ones
    # whose holdings we only have for a single month. Those extra rows are
    # the same portfolio re-priced against later stock scores -- which is a
    # real number, but it is not "how this fund's portfolio changed", and
    # plotting it as such would be a straightforward misrepresentation.
    #
    # The 45-day test keeps a point only when the portfolio behind it was
    # disclosed for roughly that month. A fund with one portfolio gets one
    # point and no chart; a fund with twelve gets twelve.
    history = query("""
        SELECT s.as_of_date,
               ROUND(s.health_score_100, 1) AS score_100,
               ROUND(s.coverage_pct, 1)     AS coverage_pct,
               s.category_rank,
               s.holdings_as_of_date
        FROM mf_score s
        WHERE s.scheme_code = %(code)s
          AND s.algo_version = %(algo)s
          AND s.holdings_as_of_date IS NOT NULL
        ORDER BY s.as_of_date
    """, {"code": scheme_code, "algo": FUND_ALGO})

    # ONE POINT PER PORTFOLIO, NOT PER SCORING RUN.
    #
    # score_funds.py scores every fund at whatever date it is given, using
    # that fund's most recent portfolio at or before the date. Score it for
    # twelve months and a fund with ONE portfolio gets twelve rows -- the
    # same holdings re-priced as their stock indicators moved. That is a
    # real number, but it is not "how this fund's portfolio changed", and
    # plotting it as such would be a misrepresentation.
    #
    # A date gap alone cannot tell the two apart: a 30-day gap looks the
    # same whether this month's portfolio is not published yet or was
    # never loaded. Keying on the portfolio date can. Each distinct
    # holdings_as_of_date contributes exactly one point, at the earliest
    # date it was scored.
    seen, unique = set(), []
    for h in history:
        key = str(h["holdings_as_of_date"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(h)
    history = unique

    # LAB VIEW -- same query, experimental algo_version, allowlisted only.
    lab = []
    if user and (user.get("email") or "").lower() in LAB_EMAILS:
        lab = query("""
            SELECT s.as_of_date,
                   ROUND(s.health_score_100, 1) AS score_100,
                   ROUND(s.health_score, 1)     AS score_raw,
                   ROUND(s.coverage_pct, 1)     AS coverage_pct,
                   s.holdings_as_of_date
            FROM mf_score s
            WHERE s.scheme_code = %(code)s
              AND s.algo_version = %(algo)s
              AND s.holdings_as_of_date IS NOT NULL
            ORDER BY s.as_of_date
        """, {"code": scheme_code, "algo": LEGACY_FUND_ALGO})
        seen_l, uniq_l = set(), []
        for h in lab:
            k = str(h["holdings_as_of_date"])
            if k in seen_l:
                continue
            seen_l.add(k)
            uniq_l.append(h)
        lab = uniq_l

    # ROLLING RETURNS AND RISK.
    #
    # Free, like the trailing returns beside them. Both are derived from
    # published NAV history, which is public; the paid reading on this
    # page is the holdings score, and conflating "our analysis" with
    # "arithmetic on public data" would be charging for the wrong thing.
    #
    # Volatility, downside and drawdown are properties of the FUND, not
    # of a rolling window -- score_rolling.py stores them on every row
    # because the row is keyed by window, but they are identical across
    # all three. Returned ONCE here, from whichever window exists, so the
    # page cannot print the same measurement three times and invite the
    # reader to think it was taken three ways.
    rolling = query("""
        SELECT window_years, observations, first_start, last_start,
               avg_cagr, median_cagr, worst_cagr, best_cagr,
               p10_cagr, p25_cagr, p75_cagr, p90_cagr,
               pct_positive, pct_above_hurdle, hurdle_pct,
               volatility, downside_vol, max_drawdown,
               sharpe, sortino, risk_free_pct, as_of_date
        FROM mf_rolling
        WHERE scheme_code = %(code)s
          AND as_of_date = (SELECT MAX(as_of_date) FROM mf_rolling
                             WHERE scheme_code = %(code)s)
        ORDER BY window_years
    """, {"code": scheme_code})

    # The peer number beside each of ours. A Sharpe of 0.81 is neither
    # good nor bad on its own; against a category median of 0.62 it is a
    # statement a distributor can make out loud.
    peers = {r["window_years"]: r for r in query("""
        SELECT c.window_years, c.funds, c.avg_cagr, c.worst_cagr,
               c.p10_cagr, c.p25_cagr, c.p75_cagr, c.p90_cagr,
               c.pct_above_hurdle, c.volatility, c.downside_vol,
               c.max_drawdown, c.sharpe, c.sortino
        FROM mf_rolling_category c
        JOIN v_scheme_category vc ON vc.category = c.category
        WHERE vc.scheme_code = %(code)s
          AND c.as_of_date = (SELECT MAX(as_of_date) FROM mf_rolling_category)
    """, {"code": scheme_code})} if rolling else {}

    risk = None
    if rolling:
        r0 = rolling[0]
        risk = {
            "volatility": float(r0["volatility"]) if r0["volatility"] is not None else None,
            "downside_vol": float(r0["downside_vol"]) if r0["downside_vol"] is not None else None,
            "max_drawdown": float(r0["max_drawdown"]) if r0["max_drawdown"] is not None else None,
            "risk_free_pct": float(r0["risk_free_pct"]),
            "as_of": str(r0["as_of_date"]),
            "peer": ({
                "volatility": float(peers[r0["window_years"]]["volatility"])
                    if peers[r0["window_years"]]["volatility"] is not None else None,
                "downside_vol": float(peers[r0["window_years"]]["downside_vol"])
                    if peers[r0["window_years"]]["downside_vol"] is not None else None,
                "max_drawdown": float(peers[r0["window_years"]]["max_drawdown"])
                    if peers[r0["window_years"]]["max_drawdown"] is not None else None,
            } if r0["window_years"] in peers else None),
        }

    def _f(v):
        return float(v) if v is not None else None

    rolling = [{
        "window_years": r["window_years"],
        "observations": r["observations"],
        "first_start": str(r["first_start"]) if r["first_start"] else None,
        "last_start": str(r["last_start"]) if r["last_start"] else None,
        "avg_cagr": _f(r["avg_cagr"]), "median_cagr": _f(r["median_cagr"]),
        "worst_cagr": _f(r["worst_cagr"]), "best_cagr": _f(r["best_cagr"]),
        # THE SHAPE. Null on every row until score_rolling.py has been
        # re-run after add_rolling_percentiles.py, and the page draws
        # the range without the box rather than inventing one.
        "p10_cagr": _f(r["p10_cagr"]), "p25_cagr": _f(r["p25_cagr"]),
        "p75_cagr": _f(r["p75_cagr"]), "p90_cagr": _f(r["p90_cagr"]),
        "pct_positive": _f(r["pct_positive"]),
        "pct_above_hurdle": _f(r["pct_above_hurdle"]),
        "hurdle_pct": _f(r["hurdle_pct"]),
        # Per window, because the average return differs per window.
        "sharpe": _f(r["sharpe"]), "sortino": _f(r["sortino"]),
        # The middle fund of the same category, over the same window.
        # Absent rather than zero where the category has too few funds to
        # have a middle -- "no peer group" and "the peers did nothing"
        # are different facts.
        "peer": ({
            "funds": peers[r["window_years"]]["funds"],
            "avg_cagr": _f(peers[r["window_years"]]["avg_cagr"]),
            "worst_cagr": _f(peers[r["window_years"]]["worst_cagr"]),
            "p25_cagr": _f(peers[r["window_years"]]["p25_cagr"]),
            "p75_cagr": _f(peers[r["window_years"]]["p75_cagr"]),
            "pct_above_hurdle": _f(peers[r["window_years"]]["pct_above_hurdle"]),
            "sharpe": _f(peers[r["window_years"]]["sharpe"]),
            "sortino": _f(peers[r["window_years"]]["sortino"]),
        } if r["window_years"] in peers else None),
    } for r in rolling]

    # HOW IT BEHAVES AGAINST ITS INDEX.
    #
    # Free, like everything else derived from published NAV. Up and down
    # capture and the information ratio are arithmetic on the fund's own
    # month-ends against a published index; none of it is our reading of
    # anything, and charging for it would be charging for division.
    #
    # Three and five years only. build_fund_capture.py deliberately does
    # not compute a one-year figure -- twelve monthly returns split into
    # risers and fallers leave too few of one kind to say anything --
    # so a missing 1-year row here is the system working.
    capture = query_optional("""
        SELECT f.window_years, f.benchmark_id, f.bench_source, f.months,
               bm.display_name AS benchmark_name,
               f.up_months, f.down_months, f.up_capture, f.down_capture,
               f.information_ratio, f.tracking_error, f.r_squared,
               f.excess_cagr,
               f.fund_cagr, f.bench_cagr, f.first_month, f.last_month,
               f.as_of_date,
               c.up_capture AS peer_up, c.down_capture AS peer_down,
               c.information_ratio AS peer_ir,
               c.tracking_error AS peer_te, c.r_squared AS peer_r2,
               c.funds AS peer_funds
        FROM fund_capture f
        -- CAST, NOT USING. fund_capture.benchmark_id is text and
        -- benchmark_master.benchmark_id is an integer, so USING asks
        -- Postgres for text = integer, which it refuses outright. The
        -- refusal is not a missing table, so nothing caught it, and a
        -- join added to put a NAME on the page took the whole fund
        -- endpoint down with it.
        LEFT JOIN benchmark_master bm
               ON bm.benchmark_id::text = f.benchmark_id
        LEFT JOIN v_scheme_category vc ON vc.scheme_code = f.scheme_code
        LEFT JOIN fund_capture_category c
               ON c.category = vc.category
              AND c.window_years = f.window_years
        WHERE f.scheme_code = %(code)s
        ORDER BY f.window_years
    """, {"code": scheme_code})

    capture = [{
        "window_years": r["window_years"],
        "benchmark_id": r["benchmark_id"],
        # The name a reader recognises. The id is a primary key and
        # printing it put "measured against 5" on the fund page.
        "benchmark_name": r["benchmark_name"],
        # 'scheme' means this fund's own mapped index; 'category' means
        # the index most of its category points at. The page says which,
        # because "measured against its own benchmark" and "measured
        # against the one its neighbours use" are different claims.
        "bench_source": r["bench_source"],
        "months": r["months"],
        "up_months": r["up_months"], "down_months": r["down_months"],
        # Absent rather than zero where a side had too few months of its
        # own kind. Zero would say the fund captured nothing.
        "up_capture": _f(r["up_capture"]),
        "down_capture": _f(r["down_capture"]),
        "information_ratio": _f(r["information_ratio"]),
        "tracking_error": _f(r["tracking_error"]),
        # How much of the fund's monthly movement the index accounts
        # for, 0-100. Not the same thing as tracking error, and the
        # page says so: a leveraged index fund has a big tracking
        # error and an r_squared near 100.
        "r_squared": _f(r["r_squared"]),
        "excess_cagr": _f(r["excess_cagr"]),
        "fund_cagr": _f(r["fund_cagr"]), "bench_cagr": _f(r["bench_cagr"]),
        "first_month": str(r["first_month"]) if r["first_month"] else None,
        "last_month": str(r["last_month"]) if r["last_month"] else None,
        "as_of": str(r["as_of_date"]) if r["as_of_date"] else None,
        "peer": ({
            "funds": r["peer_funds"],
            "up_capture": _f(r["peer_up"]),
            "down_capture": _f(r["peer_down"]),
            "information_ratio": _f(r["peer_ir"]),
            "tracking_error": _f(r["peer_te"]),
            "r_squared": _f(r["peer_r2"]),
        } if r["peer_funds"] else None),
    } for r in capture]

    # HOW MUCH OF THE PORTFOLIO IS THE MANAGER'S OWN CHOICE.
    #
    # This is the holdings answer, and it is a different question from
    # the capture figures above, which are about price movement. A fund
    # can move almost exactly with its index while holding entirely
    # different stocks, and it can hold the index's stocks and move
    # differently because of what it weights. Only this one looks at
    # what is actually owned.
    #
    # The index side is a proxy -- a passive tracker's disclosed
    # portfolio standing in for the index's constituents, because NSE
    # publishes only the top ten weights. build_active_share.py has the
    # reasoning; source_name travels with the row so the page can say
    # whose portfolio it used.
    act = query_optional("""
        SELECT a.benchmark_id, b.display_name AS benchmark_name,
               a.as_of_date, a.index_as_of, a.overlap_pct, a.active_share,
               a.fund_names, a.index_names, a.shared_names,
               a.off_index_pct, a.off_index_names, a.equity_pct,
               c.scheme_name AS source_name,
               (SELECT ROUND(percentile_cont(0.5)
                       WITHIN GROUP (ORDER BY x.active_share)::numeric, 1)
                  FROM fund_active_share x
                 WHERE x.benchmark_id = a.benchmark_id) AS peer_active
        FROM fund_active_share a
        LEFT JOIN benchmark_master b USING (benchmark_id)
        LEFT JOIN v_fund_canonical c
               ON c.canonical_scheme_code::text = a.source_scheme::text
        WHERE a.scheme_code = %(code)s
    """, {"code": scheme_code})

    if act:
        a = act[0]
        # The per-stock positions, with real company names. An ISIN is
        # a correct answer to a question nobody asked.
        bets = query_optional("""
            SELECT t.isin, t.fund_pct, t.index_pct, t.diff,
                   m.company_name, m.symbol
            FROM fund_index_bet t
            LEFT JOIN stock_master m ON m.isin = t.isin
            WHERE t.scheme_code = %(code)s
            ORDER BY t.diff
        """, {"code": scheme_code})

        def _bet(r):
            return {"isin": r["isin"],
                    "name": r["company_name"] or r["symbol"] or r["isin"],
                    "fund_pct": _f(r["fund_pct"]),
                    "index_pct": _f(r["index_pct"]),
                    "diff": _f(r["diff"])}

        active_share = {
            "benchmark_name": a["benchmark_name"],
            "as_of": str(a["as_of_date"]),
            "index_as_of": str(a["index_as_of"]),
            "overlap_pct": _f(a["overlap_pct"]),
            "active_share": _f(a["active_share"]),
            "fund_names": a["fund_names"], "index_names": a["index_names"],
            "shared_names": a["shared_names"],
            "off_index_pct": _f(a["off_index_pct"]),
            "off_index_names": a["off_index_names"],
            # Shown so a cash call is never hidden inside a figure that
            # is only about stock selection.
            "equity_pct": _f(a["equity_pct"]),
            "source_name": a["source_name"],
            "peer_active": _f(a["peer_active"]),
            # Underweights first (most negative), overweights last.
            "under": [_bet(r) for r in bets if (r["diff"] or 0) < 0][:8],
            "over": [_bet(r) for r in reversed(bets)
                     if (r["diff"] or 0) > 0][:8],
        }
    else:
        active_share = None

    # PORTFOLIO TURNOVER, AS THE AMC PUBLISHED IT.
    #
    # Loaded from factsheets by load_turnover.py, never derived here. A
    # figure derived from our monthly holdings snapshots cannot see a
    # stock bought and sold between two of them -- which is exactly the
    # trading the ratio exists to reveal -- so it would read low on the
    # funds a client most needs warning about. Absent until somebody
    # loads a real one; the page then says so rather than showing a
    # number nobody can check against the factsheet.
    turnover = query_optional("""
        SELECT turnover_pct, as_of_date, source
        FROM fund_turnover
        WHERE scheme_code = %(code)s
        ORDER BY as_of_date DESC
        LIMIT 1
    """, {"code": scheme_code})
    turnover = ({
        "pct": _f(turnover[0]["turnover_pct"]),
        "as_of": str(turnover[0]["as_of_date"]),
        "source": turnover[0]["source"],
        # The category's middle fund, for the same reason every other
        # figure on this page carries one: 31% is neither high nor low
        # until you know what the rest of the shelf does.
        "peer": _f((query_optional("""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY t.turnover_pct)
                   AS med
            FROM fund_turnover t
            JOIN v_scheme_category vc ON vc.scheme_code = t.scheme_code
            WHERE vc.category = (SELECT category FROM v_scheme_category
                                  WHERE scheme_code = %(code)s)
              AND t.as_of_date >= %(since)s
            HAVING COUNT(DISTINCT t.scheme_code) >= 5
        """, {"code": scheme_code,
              "since": turnover[0]["as_of_date"]}) or [{}])[0].get("med")),
    } if turnover else None)

    if not has_score_access(user):
        # Three separate leaks on this one page: the fund's own score,
        # every holding's score and contribution, and the whole history
        # of past scores. All three go.
        fund = strip_fields(fund, FUND_SCORE_FIELDS)
        holdings = strip_fields(holdings, HOLDING_SCORE_FIELDS)
        history = []
        if not SCORES_ENABLED:
            # How much of the portfolio "the score covers" is score
            # metadata, meaningless with no score. The lab's history goes
            # too: it is the allowlist's view of the same thing.
            fund = strip_fields(fund, ("coverage_pct", "scored_holdings"))
            lab = []
        return {"fund": fund, "holdings": holdings, "returns": returns,
                "score_history": history, "score_locked": True,
                "scores_enabled": SCORES_ENABLED,
                "score_history_lab": lab,
                "rolling": rolling, "risk": risk,
                "capture": capture, "turnover": turnover,
                "active_share": active_share}

    return {"fund": fund, "holdings": holdings,
            "returns": returns, "score_history": history,
            "score_locked": False, "scores_enabled": True,
            "score_history_lab": lab,
            "rolling": rolling, "risk": risk,
            "capture": capture, "turnover": turnover,
                "active_share": active_share}


# =====================================================================
# BACKTEST
#
# The point of these two endpoints is to let the score be judged rather
# than asserted. They deliberately return the raw per-fund rows, not a
# verdict -- the page computes the correlation and the quartile spread
# from what it is given, so a weak result renders as a weak result.
#
# NO LOOK-AHEAD. The scores read here were written by
# score_stocks.py --date <snapshot> and score_funds.py --date <snapshot>,
# both of which select technical bars at or before that date. Returns are
# measured from the snapshot forward, so the two never overlap.
# =====================================================================

# NAV IS LOOKED UP PER FUND, NOT SCANNED BY DATE.
#
# The obvious shape -- DISTINCT ON (scheme_code) over a date window -- reads
# every scheme in the database (86k rows for a 15-day window across 11k
# schemes), sorts them on disk, and discards all but the dozen we asked
# for. Measured at ~6s per pass, three passes per request.
#
# Instead: resolve the fund set first, then one LATERAL lookup per fund.
# mf_nav_pkey is (scheme_code, nav_date), so each becomes a backward index
# seek returning a single row. Fourteen seeks beat an 86,000-row sort.
SNAPSHOT_SQL = """
WITH f AS (
    SELECT m.scheme_code, m.scheme_name, m.amc_name, c.category,
           ROUND(s.health_score_100, 1) AS score_100,
           ROUND(s.coverage_pct, 1)     AS coverage_pct,
           s.category_rank              AS score_rank
    FROM mf_score s
    JOIN mf_scheme m USING (scheme_code)
    JOIN v_scheme_category c USING (scheme_code)
    WHERE s.as_of_date   = %(snap)s
      AND s.algo_version = %(algo)s
      AND c.category     = %(cat)s
),
priced AS (
    SELECT f.*,
           n1.nav AS nav_then, n1.nav_date AS from_date,
           n2.nav AS nav_now,  n2.nav_date AS to_date,
           ROUND((n2.nav / n1.nav - 1) * 100, 2) AS fwd_return
    FROM f
    CROSS JOIN LATERAL (
        SELECT nav, nav_date FROM mf_nav
        WHERE scheme_code = f.scheme_code AND nav_date <= %(snap)s
        ORDER BY nav_date DESC LIMIT 1
    ) n1
    CROSS JOIN LATERAL (
        SELECT nav, nav_date FROM mf_nav
        WHERE scheme_code = f.scheme_code
        ORDER BY nav_date DESC LIMIT 1
    ) n2
    WHERE n1.nav > 0
)
SELECT p.scheme_code, p.scheme_name, p.amc_name, p.category,
       p.score_100, p.coverage_pct, p.score_rank,
       p.fwd_return, p.from_date, p.to_date,
       (SELECT ROUND(percentile_cont(0.5)
               WITHIN GROUP (ORDER BY fwd_return)::numeric, 2) FROM priced)
         AS category_median_return
FROM priced p
ORDER BY p.score_100 DESC
"""


@app.get("/api/backtest/categories", tags=["backtest"])
def backtest_categories():
    """Which categories have enough data for each section.

    The two sections need different things, so they get separate lists.
    The snapshot needs several funds scored at the historical date; the
    series needs at least one fund with three or more distinct monthly
    portfolios. Driving the dropdowns from this means an option never
    appears that would render an empty chart.
    """
    require_scores_on()
    snap = query("""SELECT MIN(as_of_date) AS d FROM mf_score
                     WHERE algo_version = %(algo)s""",
                 {"algo": FUND_ALGO}, one=True)["d"]
    if not snap:
        return {"snapshot_date": None, "snapshot": [], "series": []}

    snapshot = query("""
        SELECT c.category, COUNT(DISTINCT s.scheme_code) AS funds
        FROM mf_score s
        JOIN v_scheme_category c USING (scheme_code)
        WHERE s.as_of_date   = %(snap)s
          AND s.algo_version = %(algo)s
          AND c.category IS NOT NULL
        GROUP BY 1
        HAVING COUNT(DISTINCT s.scheme_code) >= 4
        ORDER BY 2 DESC, 1
    """, {"snap": snap, "algo": FUND_ALGO})

    series = query("""
        SELECT c.category, COUNT(*) AS funds
        FROM (
            SELECT scheme_code
            FROM mf_score
            WHERE algo_version = %(algo)s
              AND holdings_as_of_date IS NOT NULL
            GROUP BY scheme_code
            HAVING COUNT(DISTINCT holdings_as_of_date) >= 3
        ) x
        JOIN v_scheme_category c ON c.scheme_code = x.scheme_code
        WHERE c.category IS NOT NULL
        GROUP BY 1
        ORDER BY 2 DESC, 1
    """, {"algo": FUND_ALGO})

    return {"snapshot_date": snap, "snapshot": snapshot, "series": series}


@app.get("/api/backtest/snapshot", tags=["backtest"])
def backtest_snapshot(
    category: str = Query("Mid Cap Fund"),
    snapshot: Optional[str] = Query(None, description="YYYY-MM-DD; default oldest"),
):
    """Score at a past date against the return since.

    Defaults to the OLDEST fund-score date, which is the backtest snapshot;
    the newest is today's live scoring and would give a zero-length window.
    Switched off with the rest of the scores: see require_scores_on().
    """
    require_scores_on()
    if snapshot:
        snap = snapshot
    else:
        row = query("""SELECT MIN(as_of_date) AS d FROM mf_score
                        WHERE algo_version = %(algo)s""",
                    {"algo": FUND_ALGO}, one=True)
        snap = row["d"] if row else None
    if not snap:
        raise HTTPException(404, "no fund scores exist yet")

    latest = query("""SELECT MAX(as_of_date) AS d FROM mf_score
                       WHERE algo_version = %(algo)s""",
                   {"algo": FUND_ALGO}, one=True)["d"]
    if latest and str(snap) == str(latest):
        # Only one scoring date, so there is no "afterwards" to measure.
        raise HTTPException(404, "no historical snapshot to test against")

    funds = query(SNAPSHOT_SQL,
                  {"snap": snap, "algo": FUND_ALGO, "cat": category})
    return {
        "snapshot_date": snap,
        "measured_to": funds[0]["to_date"] if funds else None,
        "category": category,
        "fund_count": len(funds),
        "funds": funds,
    }


SERIES_SQL = """
WITH f AS (
    SELECT DISTINCT m.scheme_code, m.scheme_name
    FROM mf_score s
    JOIN mf_scheme m USING (scheme_code)
    JOIN v_scheme_category c USING (scheme_code)
    WHERE s.algo_version = %(algo)s AND c.category = %(cat)s
),
-- ONE BASE PER FUND, NOT ONE FOR THE WHOLE TABLE.
--
-- This used to take a single date -- MIN(as_of_date) across ALL of
-- mf_score -- and price every fund in every category from it. Scoring one
-- AMC's history back to Feb 2023 therefore moved the base three and a half
-- years for every category at once, and the page reported +90% where it had
-- reported +15%. The arithmetic was never wrong; the base was.
--
-- A per-CATEGORY base does not fix it either: mid cap now holds one fund
-- with 42 months beside 28 with twelve, so a category base would misdate
-- the 28 in exactly the same way.
--
-- So each fund is priced from ITS OWN first plotted point. The cost is that
-- the returns cover different windows and are not comparable BETWEEN funds
-- -- which is why the caption states the window and the table shows the
-- start month. Everything computed as a RATIO of two cum_return figures
-- (the month-by-month panel, the trough compare) is unaffected either way,
-- because the base cancels.
first_pt AS (
    SELECT f.scheme_code, f.scheme_name,
           (SELECT MIN(s2.as_of_date)
              FROM mf_score s2
             WHERE s2.scheme_code = f.scheme_code
               AND s2.algo_version = %(algo)s
               AND s2.holdings_as_of_date IS NOT NULL) AS d0
    FROM f
),
base AS (
    SELECT p.scheme_code, p.scheme_name, p.d0 AS base_date, nb.nav AS base_nav
    FROM first_pt p
    CROSS JOIN LATERAL (
        SELECT nav FROM mf_nav
        WHERE scheme_code = p.scheme_code AND nav_date <= p.d0
        ORDER BY nav_date DESC LIMIT 1
    ) nb
    WHERE p.d0 IS NOT NULL
)
SELECT b.scheme_code, b.scheme_name, b.base_date, s.as_of_date,
       s.holdings_as_of_date,
       ROUND(s.health_score_100, 1) AS score_100,
       ROUND(s.coverage_pct, 1)     AS coverage_pct,
       ROUND((na.nav / b.base_nav - 1) * 100, 2) AS cum_return
FROM base b
-- One row per portfolio, same reasoning as the fund page: DISTINCT ON
-- keeps the first scoring of each disclosed portfolio and drops the
-- re-pricings of it that follow.
JOIN LATERAL (
    SELECT DISTINCT ON (s2.holdings_as_of_date)
           s2.as_of_date, s2.holdings_as_of_date,
           s2.health_score_100, s2.coverage_pct
    FROM mf_score s2
    WHERE s2.scheme_code   = b.scheme_code
      AND s2.algo_version  = %(algo)s
      AND s2.holdings_as_of_date IS NOT NULL
    ORDER BY s2.holdings_as_of_date, s2.as_of_date
) s ON TRUE
CROSS JOIN LATERAL (
    SELECT nav FROM mf_nav
    WHERE scheme_code = b.scheme_code AND nav_date <= s.as_of_date
    ORDER BY nav_date DESC LIMIT 1
) na
WHERE b.base_nav > 0
ORDER BY b.scheme_name, s.as_of_date
"""


# The benchmark over the same window as the fund lines. One row, not a
# sixth line: an index has no holdings score, so it cannot be plotted on
# a chart of holdings scores. But "the fund returned 3.3% while its index
# returned X%" is the comparison that makes the return column mean
# something, and without it a reader has no scale for the numbers.
BENCHMARK_WINDOW = """
WITH bm AS (
    SELECT m.benchmark_id
    FROM mf_benchmark_map m
    JOIN v_scheme_category c ON c.scheme_code = m.scheme_code
    WHERE c.category = %(cat)s
      AND m.benchmark_id IS NOT NULL
    GROUP BY 1
    ORDER BY COUNT(*) DESC
    LIMIT 1
)
SELECT b.display_name,
       (SELECT n.index_value FROM benchmark_nav n
         WHERE n.benchmark_id = bm.benchmark_id AND n.index_date <= %(d0)s
         ORDER BY n.index_date DESC LIMIT 1) AS v0,
       (SELECT n.index_value FROM benchmark_nav n
         WHERE n.benchmark_id = bm.benchmark_id AND n.index_date <= %(d1)s
         ORDER BY n.index_date DESC LIMIT 1) AS v1
FROM bm JOIN benchmark_master b USING (benchmark_id)
"""


@app.get("/api/backtest/series", tags=["backtest"])
def backtest_series(
    category: str = Query("Mid Cap Fund"),
    limit: int = Query(30, ge=1, le=30),
):
    """Each fund's score at every date it was scored.

    Returns nothing useful until several monthly snapshots exist -- with
    two dates a 'trend' is a straight line between two points, which would
    look like evidence without being any.
    """
    require_scores_on()
    rows = query(SERIES_SQL, {"algo": FUND_ALGO, "cat": category})

    by_fund = {}
    for r in rows:
        f = by_fund.setdefault(r["scheme_code"],
                               {"scheme_code": r["scheme_code"],
                                "scheme_name": r["scheme_name"],
                                "base_date": str(r["base_date"]),
                                "points": []})
        # BOTH DATES, deliberately. as_of_date is when the score was
        # written; holdings_as_of_date is the month the portfolio it read
        # was disclosed for. They are usually NOT the same month, and a
        # reader who assumes they are will misread every forward figure.
        f["points"].append({"as_of": str(r["as_of_date"]),
                            "holdings_as_of": str(r["holdings_as_of_date"]),
                            "score_100": r["score_100"],
                            "coverage_pct": r["coverage_pct"],
                            "cum_return": r["cum_return"]})

    # Fewer than three points is a line between two dots. Say nothing.
    #
    # EVERYTHING WITH ENOUGH HISTORY IS RETURNED. Nothing is picked.
    # The old rule kept the top 6 by number of points, which was fine at
    # five funds and became arbitrary at fourteen: once most funds have
    # twelve monthly portfolios they all tie, and which six survived was
    # decided by whatever order the SQL happened to return. A subset
    # chosen after the data is in hand is how a result gets manufactured,
    # so the subset is gone.
    #
    # Sorted by points then name purely so the output is stable between
    # calls -- the order carries no meaning and nothing downstream may
    # read anything into it.
    funds = [f for f in by_fund.values() if len(f["points"]) >= 3]
    funds.sort(key=lambda f: (-len(f["points"]), f["scheme_name"]))
    funds = funds[:limit]

    # Measured over exactly the window the fund lines cover, so the
    # comparison is like for like.
    benchmark = None
    dates = [p["as_of"] for f in funds for p in f["points"]]
    if dates:
        row = query(BENCHMARK_WINDOW,
                    {"cat": category, "d0": base, "d1": max(dates)}, one=True)
        if row and row["v0"] and row["v1"] and float(row["v0"]) > 0:
            benchmark = {
                "name": row["display_name"],
                "from_date": str(base),
                "to_date": max(dates),
                "return_pct": round(
                    (float(row["v1"]) / float(row["v0"]) - 1) * 100, 2),
            }

    return {"category": category, "fund_count": len(funds),
            "benchmark": benchmark, "funds": funds}


# =====================================================================
@app.get("/api/categories", tags=["funds"])
def categories():
    """Categories that actually have scored funds -- for a dropdown.

    Deliberately not every category in mf_scheme: offering a filter that
    returns nothing is worse than not offering it."""
    rows = query("""
        SELECT c.category,
               COUNT(*) AS fund_count,
               ROUND(AVG(s.health_score), 2) AS avg_score,
               bool_and(c.rank_meaningful) AS rank_meaningful
        FROM mf_score s
        JOIN v_scheme_category c USING (scheme_code)
        WHERE s.algo_version = %(algo)s
          AND s.as_of_date = (SELECT MAX(as_of_date) FROM mf_score
                               WHERE algo_version = %(algo)s)
          AND c.category IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """, {"algo": FUND_ALGO})
    # The category average of a score is a score. This route had no gate at
    # all, so it leaked the number whether or not the paywall was on.
    return rows if SCORES_ENABLED else strip_fields(rows, ("avg_score",))


# =====================================================================
@app.get("/api/categories/returns", tags=["funds"])
def category_returns():
    """What each KIND of fund has returned, and what its index did.

    For the panel that opens when somebody picks a kind of fund on the
    plan page. The question it answers is the one asked before choosing a
    fund at all: does this whole category clear what the goal needs?

    THE MEDIAN, NOT THE AVERAGE. mf_category_return already stores the
    median, and that is the right middle for a distribution with a long
    right tail -- one spectacular small cap fund drags a mean upward and
    describes nothing anybody is likely to hold.

    THE BENCHMARK IS THE MOST COMMON ONE IN THE CATEGORY, named so it can
    be checked. Funds in a category do not all measure against the same
    index; picking the modal one and saying which it is beats averaging
    several indices into a number that is nobody's benchmark.
    """
    rows = query("""
        WITH latest AS (
            SELECT MAX(as_of_date) AS d FROM mf_category_return
        ),
        cat AS (
            SELECT cr.category, cr.period,
                   ROUND(cr.median_cagr, 2) AS median_cagr,
                   ROUND(cr.p25_cagr, 2)    AS p25_cagr,
                   ROUND(cr.p75_cagr, 2)    AS p75_cagr,
                   COALESCE(cr.rank_meaningful, false) AS rank_meaningful
            FROM mf_category_return cr, latest
            WHERE cr.as_of_date = latest.d
        ),
        -- The index most funds in the category measure against, and what
        -- it did over the same window.
        bench AS (
            SELECT DISTINCT ON (r.category, r.period)
                   r.category, r.period,
                   b.display_name AS benchmark_name,
                   ROUND(AVG(r.bench_cagr), 2) AS bench_cagr,
                   COUNT(*) AS funds
            FROM mf_returns r
            JOIN benchmark_master b USING (benchmark_id), latest
            WHERE r.as_of_date = latest.d AND r.bench_cagr IS NOT NULL
            GROUP BY r.category, r.period, b.display_name
            ORDER BY r.category, r.period, COUNT(*) DESC
        )
        SELECT cat.category, cat.period, cat.median_cagr,
               cat.p25_cagr, cat.p75_cagr, cat.rank_meaningful,
               bench.benchmark_name, bench.bench_cagr
        FROM cat
        LEFT JOIN bench ON bench.category = cat.category
                       AND bench.period  = cat.period
        WHERE cat.period IN ('1Y', '3Y', '5Y', '10Y')
        ORDER BY cat.category, cat.period
    """)

    out = {}
    for r in rows:
        out.setdefault(r["category"], {})[r["period"]] = {
            "median_cagr": float(r["median_cagr"]) if r["median_cagr"] is not None else None,
            "p25_cagr": float(r["p25_cagr"]) if r["p25_cagr"] is not None else None,
            "p75_cagr": float(r["p75_cagr"]) if r["p75_cagr"] is not None else None,
            "benchmark_name": r["benchmark_name"],
            "bench_cagr": float(r["bench_cagr"]) if r["bench_cagr"] is not None else None,
            "rank_meaningful": bool(r["rank_meaningful"]),
        }
    return out


# =====================================================================
@app.get("/api/stocks", tags=["stocks"])
def list_stocks(
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: Optional[dict] = Depends(current_user),
):
    """Stock scores, for drilling into what drives a fund."""
    unlocked = has_score_access(user)
    where = ["s.algo_version = %(algo)s",
             "s.as_of_date = (SELECT MAX(as_of_date) FROM stock_score "
             "                 WHERE algo_version = %(algo)s)"]
    params = {"algo": STOCK_ALGO, "limit": limit, "offset": offset}

    if search:
        # Same word-by-word, punctuation-blind matching as /api/funds.
        NORM = "regexp_replace(lower(%s), '[^a-z0-9]', '', 'g')"
        sym_n, name_n = NORM % "m.symbol", NORM % "m.company_name"
        words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in search.split()]
        for i, w in enumerate([w for w in words if w][:6]):
            key = "s%d" % i
            where.append("(%s LIKE %%(%s)s OR %s LIKE %%(%s)s)"
                         % (sym_n, key, name_n, key))
            params[key] = "%" + w + "%"

    clause = " AND ".join(where)

    # Ordering by score leaks the score as surely as printing it, and this
    # ORDER BY used to run for every caller. Locked callers get A-Z.
    order_by = "s.total_score DESC, m.symbol" if unlocked else "m.symbol"

    total = query(f"""SELECT COUNT(*) AS n FROM stock_score s
                      JOIN stock_master m USING (isin) WHERE {clause}""",
                  params, one=True)["n"]

    rows = query(f"""
        SELECT m.symbol, m.company_name, s.isin,
               s.total_score, s.timeframe_used,
               s.adx_score, s.macd_score, s.rsi_score,
               s.bb_score, s.supertrend_score,
               ROUND(s.adx, 1) AS adx, ROUND(s.rsi, 1) AS rsi,
               s.supertrend_dir, ROUND(s.close_price, 2) AS close_price,
               s.as_of_date
        FROM stock_score s JOIN stock_master m USING (isin)
        WHERE {clause}
        ORDER BY {order_by}
        LIMIT %(limit)s OFFSET %(offset)s
    """, params)

    if not unlocked:
        # The raw readings go too: ADX, RSI and the supertrend direction
        # are the score's ingredients, and the old code stripped only the
        # scores themselves.
        rows = strip_fields(rows, STOCK_SCORE_FIELDS + (
            "adx", "rsi", "supertrend_dir", "timeframe_used"))
        return {"total": total, "limit": limit, "offset": offset,
                "stocks": rows, "score_locked": True}

    return {"total": total, "limit": limit, "offset": offset,
            "stocks": rows, "score_locked": False}

# =====================================================================
# STOCK DETAIL
#
# Two endpoints, because the two halves have different shapes. The
# detail is one small object; the funds-holding list is 400+ rows for a
# large-cap name and has to page.
#
# WHAT IS LOCKED. The readings travel with the score, not with the free
# data. On their own "RSI 72, ADX 25 and rising" is public market fact
# -- but this page shows exactly the readings the rules test, in the
# order the rules test them, which is the score written out in longhand.
# Leaving them open would put the premium number back on the page in a
# different font. The stock's identity and its fund count stay free, so
# an anonymous visitor still gets a real page and a reason to sign up.
# =====================================================================
STOCK_READING_FIELDS = ("adx", "adx_prev", "adx_dir", "plus_di", "minus_di",
                        "rsi", "rsi_prev", "rsi_dir",
                        "macd_line", "macd_line_prev", "macd_line_dir",
                        "macd_signal", "macd_histogram", "macd_above_signal",
                        "bb_upper", "bb_middle", "bb_lower", "sma",
                        "close_price", "close_prev", "close_vs_sma",
                        "supertrend_value", "supertrend_value_prev",
                        "supertrend_dir", "supertrend_dir_prev")


def _direction(now, prev):
    """rising / falling / flat, or None when there is nothing to compare.

    None is not the same as flat and the page must not draw them the
    same way. Flat means we looked and it did not move; None means this
    stock has only one bar in this timeframe, so 'rising' is unknowable.
    """
    if now is None or prev is None:
        return None
    d = float(now) - float(prev)
    if d == 0:
        return "flat"
    return "rising" if d > 0 else "falling"


def _period_bars(isin, timeframe, as_of, n=2):
    """The last n bars for one stock in one timeframe -- ONE PER PERIOD.

    fetch_technicals.py writes a row every time it runs, so the current
    month's candle exists several times over: 25, 26 and 28 August are
    three snapshots of the SAME August bar. A naive "last two rows"
    would therefore compare August against August and report every
    indicator as flat, which is the worst kind of bug -- it produces a
    plausible answer rather than an error.

    DISTINCT ON the period collapses those snapshots to the freshest
    reading per calendar period, so "previous" genuinely means the month
    before. Prefix-matching the timeframe name keeps this working
    whether the column holds 'monthly', 'M' or 'MONTHLY'.
    """
    return query("""
        WITH stamped AS (
            SELECT t.*,
                   CASE
                     WHEN %(tf)s ILIKE 'm%%' THEN date_trunc('month', t.as_of_date)
                     WHEN %(tf)s ILIKE 'w%%' THEN date_trunc('week',  t.as_of_date)
                     ELSE t.as_of_date::timestamp
                   END AS period_key
            FROM stock_technical t
            WHERE t.isin = %(isin)s
              AND t.timeframe = %(tf)s
              AND t.as_of_date <= %(asof)s
        ),
        one_per_period AS (
            SELECT DISTINCT ON (period_key) *
            FROM stamped
            ORDER BY period_key DESC, as_of_date DESC
        )
        SELECT * FROM one_per_period
        ORDER BY period_key DESC
        LIMIT %(n)s
    """, {"isin": isin, "tf": timeframe, "asof": as_of, "n": n})


def _readings(isin, timeframe, as_of):
    """Indicator VALUES, plus which way each one moved.

    Read back from stock_technical rather than stock_score, because
    stock_score keeps the level (adx, rsi, macd_line) but not the
    previous bar it was compared against, and 'and rising' is the half
    that carries the meaning.
    """
    bars = _period_bars(isin, timeframe, as_of, 2)
    if not bars:
        return None
    cur = bars[0]
    prv = bars[1] if len(bars) > 1 else {}

    def g(row, key):
        v = row.get(key) if row else None
        return float(v) if v is not None else None

    adx, adx_p = g(cur, "adx"), g(prv, "adx")
    rsi, rsi_p = g(cur, "rsi"), g(prv, "rsi")
    macd, macd_p = g(cur, "macd_line"), g(prv, "macd_line")
    sig = g(cur, "macd_signal")
    close, close_p = g(cur, "close_price"), g(prv, "close_price")
    sma = g(cur, "sma")
    st, st_p = g(cur, "supertrend_value"), g(prv, "supertrend_value")

    return {
        "timeframe": timeframe,
        "bar_date": cur.get("as_of_date"),
        "prev_bar_date": prv.get("as_of_date") if prv else None,

        "adx": adx, "adx_prev": adx_p, "adx_dir": _direction(adx, adx_p),
        "plus_di": g(cur, "plus_di"), "minus_di": g(cur, "minus_di"),

        "rsi": rsi, "rsi_prev": rsi_p, "rsi_dir": _direction(rsi, rsi_p),

        "macd_line": macd, "macd_line_prev": macd_p,
        "macd_line_dir": _direction(macd, macd_p),
        "macd_signal": sig,
        "macd_histogram": g(cur, "macd_histogram"),
        "macd_above_signal": (None if macd is None or sig is None
                              else macd > sig),

        "bb_upper": g(cur, "bb_upper"),
        "bb_middle": g(cur, "bb_middle"),
        "bb_lower": g(cur, "bb_lower"),
        "sma": sma,
        "close_price": close, "close_prev": close_p,
        "close_vs_sma": (None if close is None or sma is None
                         else ("above" if close > sma else "below")),

        "supertrend_value": st, "supertrend_value_prev": st_p,
        "supertrend_dir": cur.get("supertrend_dir"),
        "supertrend_dir_prev": prv.get("supertrend_dir") if prv else None,

        # Moving averages. The raw values, plus the two readings that give
        # them meaning -- because "EMA 50 is 1,842" tells a reader nothing
        # on its own, and the page's job is to say what a number means.
        #
        # A period with too few bars behind it is absent rather than zero,
        # and the count below is out of the periods that EXIST, not out of
        # seven: "4 of 5 above" on a young stock is honest, "4 of 7" is not.
        **_ema_block(cur, close),
    }


# Long enough to deserve its own function, and separated because the EMA
# family will grow -- the whole point of storing them is that more get
# added.
EMA_PERIODS = (5, 10, 20, 26, 50, 100, 200)


def _ema_block(row, close):
    vals, above = {}, []
    for p in EMA_PERIODS:
        v = row.get("ema_%d" % p)
        v = float(v) if v is not None else None
        vals["ema_%d" % p] = v
        if v is not None and close is not None:
            above.append(close > v)

    have = [p for p in EMA_PERIODS if vals["ema_%d" % p] is not None]

    # Are the averages in order, shortest to longest? A stack in order is
    # the textbook shape of a sustained trend; out of order means the
    # short and long readings disagree, which is what a turn looks like
    # while it is happening.
    stack = None
    if len(have) >= 3:
        seq = [vals["ema_%d" % p] for p in have]
        if all(a > b for a, b in zip(seq, seq[1:])):
            stack = "up"
        elif all(a < b for a, b in zip(seq, seq[1:])):
            stack = "down"
        else:
            stack = "mixed"

    # The 50/200 relationship by its common names. Stated as what it IS --
    # one average above another -- never as a signal to act on.
    cross = None
    if vals["ema_50"] is not None and vals["ema_200"] is not None:
        cross = "above" if vals["ema_50"] > vals["ema_200"] else "below"

    return dict(
        vals,
        ema_periods_held=have,
        ema_above_count=sum(1 for a in above if a) if above else None,
        ema_count=len(above) if above else None,
        ema_stack=stack,
        ema_50_vs_200=cross,
    )


# ---------------------------------------------------------------------
# THE HOLDINGS FILTER, AND WHY IT IS NOT "the latest row for this stock".
#
# The obvious query -- DISTINCT ON (scheme_code) ... WHERE isin = X
# ORDER BY as_of_date DESC -- takes the latest disclosure IN WHICH THE
# FUND HELD THIS STOCK. A fund that sold out in July would still appear
# on the strength of its June row, and the page would tell a user that
# 431 funds hold Infosys today when some of them dumped it months ago.
#
# Pinning to the fund's OWN most recent portfolio fixes it: if the stock
# is absent from that portfolio, the fund is absent from this list.
# ---------------------------------------------------------------------
#
# HOLDING_RANK: where this stock sits inside the fund that owns it. A fund
# with 2% in a stock is barely expressing a view; the same 2% as a top-10
# position in a concentrated fund is a different statement, and only the
# rank tells them apart.
#
# Ranked among EQUITY holdings only -- the join to stock_master drops cash,
# TREPS, debt and unmatched ISINs. Ranking over the raw portfolio would let
# a 4% TREPS position occupy a top-10 slot and push a real equity holding
# out of the list, which is not what anyone means by "top 10 holdings".
#
_HELD_CTE = """
    WITH cand AS (
        SELECT h.scheme_code, h.pct_of_nav, h.as_of_date
        FROM mf_holding h
        WHERE h.isin = %(isin)s
          AND h.as_of_date = (SELECT MAX(h2.as_of_date)
                                FROM mf_holding h2
                               WHERE h2.scheme_code = h.scheme_code)
    ),
    held AS (
        SELECT c.scheme_code, c.pct_of_nav, c.as_of_date,
               (SELECT COUNT(*)
                  FROM mf_holding x
                  JOIN stock_master sm ON sm.isin = x.isin
                 WHERE x.scheme_code = c.scheme_code
                   AND x.as_of_date  = c.as_of_date
                   AND x.pct_of_nav  > c.pct_of_nav) + 1 AS holding_rank
        FROM cand c
    )
"""


@app.get("/api/stocks/{isin}", tags=["stocks"])
def stock_detail(isin: str, user: Optional[dict] = Depends(current_user)):
    """One stock: what the indicators read, and how widely it is held."""

    stock = query("""
        SELECT m.isin, m.symbol, m.company_name, m.sector, m.industry,
               m.is_active, m.description, m.description_source,
               cc.cap_class
        FROM stock_master m
        LEFT JOIN LATERAL (
            SELECT c.cap_class FROM stock_cap_class c
            WHERE c.isin = m.isin ORDER BY c.as_of_period DESC LIMIT 1
        ) cc ON true
        WHERE m.isin = %(isin)s
    """, {"isin": isin}, one=True)
    if not stock:
        raise HTTPException(404, f"no stock with isin {isin}")

    score = query("""
        SELECT s.total_score, s.adx_score, s.macd_score, s.rsi_score,
               s.bb_score, s.supertrend_score,
               s.timeframe_used, s.as_of_date,
               ROUND(s.close_price, 2) AS close_price
        FROM stock_score s
        WHERE s.isin = %(isin)s AND s.algo_version = %(algo)s
        ORDER BY s.as_of_date DESC
        LIMIT 1
    """, {"isin": isin, "algo": STOCK_ALGO}, one=True)

    # No score is a real state, not an error: ~115 stocks in holdings
    # have no technicals at all. The page should say so rather than 404.
    readings = None
    if score and score.get("timeframe_used"):
        readings = _readings(isin, score["timeframe_used"], score["as_of_date"])

    # ALL THREE TIMEFRAMES, not just the one the score happened to use.
    #
    # score_stocks picks monthly where it can and falls back to weekly,
    # then daily -- a sensible rule for producing ONE number, but it means
    # the page only ever showed a third of what is held. A stock can be
    # firmly up on the monthly candle and rolling over on the daily, and
    # that disagreement is the useful part; showing whichever one the
    # scorer chose hid it.
    #
    # Each timeframe is read at its OWN latest bar rather than at the
    # score's date: the daily bar is days fresher than the monthly one and
    # pinning them to a common date would throw that away.
    by_timeframe = {}
    for tf in ("MONTHLY", "WEEKLY", "DAILY"):
        latest = query("""
            SELECT MAX(as_of_date) AS d FROM stock_technical
            WHERE isin = %(isin)s AND timeframe ILIKE %(pfx)s
        """, {"isin": isin, "pfx": tf[0] + "%"}, one=True)
        if not latest or not latest["d"]:
            continue
        r = _readings(isin, tf, latest["d"])
        if r:
            by_timeframe[tf] = r

    held = query(_HELD_CTE + """
        SELECT COUNT(*)                                          AS fund_count,
               COUNT(*) FILTER (WHERE holding_rank <= 10)        AS top10_count,
               ROUND(MAX(pct_of_nav), 2)                         AS max_pct,
               ROUND(AVG(pct_of_nav), 2)                         AS avg_pct,
               MIN(holding_rank)                                 AS best_rank,
               MIN(as_of_date)                                   AS holdings_from,
               MAX(as_of_date)                                   AS holdings_to
        FROM held
    """, {"isin": isin}, one=True)

    # The denominator. A bare "431" means nothing; "431 of 957 funds
    # whose portfolio we hold" is a fact a reader can size.
    held["universe"] = query(
        "SELECT COUNT(DISTINCT scheme_code) AS n FROM mf_holding",
        one=True)["n"]

    if not has_score_access(user):
        # by_timeframe is withheld with the rest. These are the readings
        # the rules test, in the order the rules test them -- the score
        # written out in longhand -- so releasing them would put the
        # premium number back on the page in a different font.
        return {"stock": stock, "score": None, "readings": None,
                "by_timeframe": None,
                "held_by": held, "score_locked": True}

    return {"stock": stock, "score": score, "readings": readings,
            "by_timeframe": by_timeframe,
            "held_by": held, "score_locked": False}


@app.get("/api/stocks/{isin}/funds", tags=["stocks"])
def stock_funds(
    isin: str,
    sort: str = Query("pct", pattern="^(pct|score|name|rank)$"),
    top: int = Query(10, ge=0, le=200,
                     description="only funds where this stock is a top-N "
                                 "equity holding; 0 = every holder"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    exclude_etf: bool = Query(False, description="drop ETFs (incl. gold/silver/debt ETFs)"),
    exclude_index: bool = Query(False, description="drop Index Funds"),
    exclude_sectoral: bool = Query(False, description="drop Sectoral/Thematic funds"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: Optional[dict] = Depends(current_user),
):
    """Every fund holding this stock in its most recent portfolio.

    SUBSCRIPTION ONLY. Which funds own a stock is the reverse of what a
    fund page shows, and it is part of the paid product. The stock page's
    own summary (how many funds hold it, the largest position) stays open
    as the teaser, and a fund's holdings are still open on the fund page.
    """
    if not has_subscription(user):
        raise HTTPException(
            402, "This is part of the subscription. Sign in with a "
                 "subscribed account to see it.")

    unlocked = has_score_access(user)

    # Sorting by a column the caller cannot see leaks its order. Same
    # fallback the fund list uses, and the response says which sort was
    # actually applied so the UI shows a lock rather than lying.
    if not unlocked and sort == "score":
        sort, order = "pct", "desc"

    sort_column = {"pct": "hd.pct_of_nav",
                   "score": "s.health_score_100",
                   "name": "m.scheme_name",
                   "rank": "hd.holding_rank"}[sort]
    direction = "DESC" if order == "desc" else "ASC"

    # top=0 means no filter. Written as a SQL-side test rather than string
    # surgery so the query text stays one thing in one shape. An ETF or
    # index fund mechanically holds every constituent at a near-fixed
    # weight -- that's not a manager's conviction, and a sectoral/thematic
    # fund's "pick" is really a sector bet rather than a stock-level one --
    # so each checkbox drops its own category group independently rather
    # than being bundled into one all-or-nothing filter.
    where_top = "WHERE (%(top)s = 0 OR hd.holding_rank <= %(top)s)"
    excluded_categories = []
    if exclude_etf:
        excluded_categories.append("etf")
    if exclude_index:
        excluded_categories.append("index funds")
    if exclude_sectoral:
        excluded_categories.append("sectoral/thematic")
    if excluded_categories:
        where_top += " AND (c.category IS NULL OR c.category !~* %(excl_pat)s)"

    excl_pat = "|".join(re.escape(c) for c in excluded_categories)
    params = {"isin": isin, "algo": FUND_ALGO, "top": top,
              "excl_pat": excl_pat, "limit": limit, "offset": offset}

    total = query(_HELD_CTE + f"""
        SELECT COUNT(*) AS n FROM held hd
        LEFT JOIN v_scheme_category c USING (scheme_code)
        {where_top}
    """, {"isin": isin, "top": top, "excl_pat": excl_pat}, one=True)["n"]

    rows = query(_HELD_CTE + f"""
        SELECT hd.scheme_code,
               m.scheme_name,
               m.amc_name,
               c.category,
               c.rank_meaningful,
               ROUND(hd.pct_of_nav, 3)       AS pct_of_nav,
               hd.holding_rank,
               hd.as_of_date                 AS holdings_as_of,
               ROUND(s.health_score_100, 1)  AS score_100,
               s.category_rank
        FROM held hd
        JOIN mf_scheme m USING (scheme_code)
        LEFT JOIN v_scheme_category c USING (scheme_code)
        LEFT JOIN mf_score s
               ON s.scheme_code  = hd.scheme_code
              AND s.algo_version = %(algo)s
              AND s.as_of_date   = (SELECT MAX(as_of_date) FROM mf_score
                                     WHERE algo_version = %(algo)s)
        {where_top}
        ORDER BY {sort_column} {direction} NULLS LAST, m.scheme_name
        LIMIT %(limit)s OFFSET %(offset)s
    """, params)

    if not unlocked:
        rows = strip_fields(rows, ("score_100", "category_rank"))

    return {"total": total, "limit": limit, "offset": offset, "top": top,
            "funds": rows, "sort_applied": sort,
            "score_locked": not unlocked}


# The five kinds worth an "is this coming up" check. announcement,
# board_meeting, buyback and corp_action_other still get fetched and
# stored -- see fetch_stock_events.py -- but are not what this endpoint
# is for: this answers "what happens to my shares this week", and a
# credit-rating update or a routine company update is not that question.
EVENT_KINDS = ("dividend", "result", "split", "bonus", "rights")


@app.get("/api/stocks/{isin}/events", tags=["stocks"])
def stock_events(
    isin: str,
    kind: Optional[str] = Query(None, pattern="^(dividend|result|split|bonus|rights)$"),
    limit: int = Query(30, ge=1, le=200),
):
    """Dividends, results, splits, bonuses and rights issues due in the
    next 7 days (today included) -- read from BSE, not computed by us.

    A SHORT WINDOW ON PURPOSE. This is answering "does anything happen to
    my shares soon", not archiving history -- a dividend paid two years
    ago is not news to act on. Kept to just these five kinds for the same
    reason: a board meeting or a credit-rating note is a fact about the
    company, not something that changes what a holder needs to do this
    week.

    OPEN TO EVERYONE, no subscription check. The technical score is the
    paid product; a company's own public exchange filings are not ours to
    charge for, the same reasoning that already leaves sector/industry
    outside the paywall on /api/stocks/{isin}.
    """
    where = ("WHERE isin = %(isin)s AND kind = ANY(%(kinds)s) "
             "AND event_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 6")
    params = {"isin": isin, "limit": limit, "kinds": [kind] if kind else list(EVENT_KINDS)}
    rows = query(f"""
        SELECT kind, event_date, headline, detail, period, attachment_url,
               source
        FROM stock_event
        {where}
        ORDER BY event_date ASC
        LIMIT %(limit)s
    """, params)
    return {"isin": isin, "events": rows}
