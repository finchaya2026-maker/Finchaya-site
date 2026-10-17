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
import math
import os
import re
import secrets
import smtplib
import time
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
#
# The one exception is the allowlist in mf_scores_access (see "WHO SEES
# SCORES" below), which the owner fills in by hand with grant_scores.py to
# show scores to a chosen few. Nobody is on it until that script is run.
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

# The interactive API documentation (/docs, /redoc, /openapi.json) lists every endpoint, the admin ones included, to anybody. Useful
# while building, a free map for a stranger once the site is public: off when MF_ENV=production unless MF_PUBLIC_DOCS=true.
_DOCS = (not IS_PRODUCTION) or os.getenv("MF_PUBLIC_DOCS", "").strip().lower() in ("1", "true", "yes")

app = FastAPI(
    title="FinChaya Fund Health API",
    description="Read-only access to daily fund and stock health scores.",
    version="1.0.0",
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)


@app.middleware("http")
async def _security_headers(request, call_next):
    """Headers every response should carry: no content-type sniffing, no framing by another site, a tamer Referer, and (in
    production, where Caddy serves the site over HTTPS) a year of 'always use https'. A Content-Security-Policy is deliberately not
    set here: the pages use inline scripts, Google sign-in and Razorpay checkout, and a policy that breaks sign-in is worse than none."""
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if IS_PRODUCTION:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response

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


# Crawlers and browsers ask for these on every visit; a 404 for each is noise in the log and, for the icon, a blank tab.
@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("User-agent: *\nDisallow: /api/\nDisallow: /admin\nDisallow: /clients\nDisallow: /allocation\n",
                             headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Every page names its own icon (finchaya.js adds it); this only answers the request browsers make when none is named.
    return Response(status_code=204, headers={"Cache-Control": "public, max-age=86400"})


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

# A personal referral link for each member, and the credit when a friend
# joins through it. No rewards and nothing is sent on anyone's behalf; see
# the module docstring.
_mount_optional("referral_api", "referrals")

# "Check your view": say bullish or bearish on a stock and see which chart
# readings line up with that and which run against it. Premium members and
# admins only, enforced inside the module on every call.
_mount_optional("viewcheck_api", "check-your-view")

# The Snapshot tab's year-by-year placings (calendar-year return and rank in category), computed from NAV history.
# Open to everyone: arithmetic on published NAV. Without this module only that one card is missing.
_mount_optional("snapshot_api", "snapshot")


def viewcheck_allowed(user) -> bool:
    """May this caller use /viewcheck? Asked of the module that owns the
    rule, so the menu and the endpoint can never disagree. Fails CLOSED:
    if the module is not on the server, nobody is offered the link."""
    try:
        from viewcheck_api import allowed
        return bool(allowed(user))
    except Exception:                                    # noqa: BLE001
        return False


@app.get("/viewcheck", include_in_schema=False)
def viewcheck_page():
    # Served to anyone, like /admin: it is a static file, and the API behind
    # it is what refuses. The page shows a sign-in / subscription notice to
    # anyone who is not allowed.
    return _static_page("viewcheck.html")


# "Back-test your rules": describe an entry and an exit in indicators joined
# with AND / OR, and see what they would have done over up to ten years.
# Premium members and admins only, enforced inside the module on every call.
_mount_optional("backtest_api", "back-testing")


def viewcheck_audience() -> str:
    """"admin" while check-your-view is still being finished, "premium"
    once released. The page uses it to say "not open yet" rather than
    offering a plan that does not include the tool."""
    try:
        from viewcheck_api import mode
        return mode()
    except Exception:                                    # noqa: BLE001
        return "admin"


def backtest_allowed(user) -> bool:
    """May this caller use /backtest? Asked of the module that owns the rule,
    so the menu and the endpoint can never disagree. Fails CLOSED: if the
    module (or pandas) is not on the server, nobody is offered the link."""
    try:
        from backtest_api import allowed
        return bool(allowed(user))
    except Exception:                                    # noqa: BLE001
        return False


@app.get("/backtest", include_in_schema=False)
def backtest_page():
    # Served to anyone, like /viewcheck: a static file, and the API behind it
    # is what refuses.
    return _static_page("backtest.html")


# "My trade plan": write a plan once (instrument, time frames, conditions joined
# with AND / OR), save it, then check it on a stock. Admin-only until
# MF_TRADEPLAN_ACCESS says otherwise (see tradeplan_api.py). It reuses the
# back-tester's candle reader and indicator code, so it needs those files too.
_mount_optional("tradeplan_api", "trade plan")


@app.get("/tradeplan", include_in_schema=False)
def tradeplan_page():
    # Served to anyone, like /backtest: a static file, and the API behind it
    # is what refuses.
    return _static_page("tradeplan.html")


# Quarterly results, week by week: who has filed (sales, operating profit, net profit, EPS against the quarter
# before and a year ago, with market cap and the P/E and PEG moves) and who is due next. FOR STOCKS SUBSCRIBERS (and
# administrators): stock_access.py checks every call, and shows a sign-in or subscribe notice to anyone else. It reuses
# build_ratios() above, so the page and the stock page can never disagree. MF_RESULTS_ENABLED=false switches it off;
# MF_RESULTS_ACCESS=signed_in | everyone | admin changes who may see it.
_mount_optional("results_api", "quarterly results")


@app.get("/results", include_in_schema=False)
def results_page():
    # Served to anyone: a static file that asks /api/results/access first. The data endpoint is what refuses.
    return _static_page("results.html")


# Stock alerts: "Set alert" on a stock page (a price level, a close above a recent high, a pullback to a moving average, a
# change of trend, an indicator, the next results) and the "My stock alerts" page. One email, once, when it happens.
# Stocks subscribers and administrators only (stock_access.py). The checking and the emailing are stockalerts_checker.py,
# run by cron; mailer.py is what sends. The page says plainly when either is not set up.
_mount_optional("stockalerts_api", "stock alerts")


@app.get("/stock-alerts", include_in_schema=False)
def stockalerts_page():
    # Served to anyone, like /tradeplan: a static file whose API refuses.
    return _static_page("stockalerts.html")


@app.get("/stockalerts.js", include_in_schema=False)
def stockalerts_script():
    path = os.path.join(BASE_DIR, "stockalerts.js")
    if not os.path.exists(path):
        raise HTTPException(404, "stockalerts.js is missing from %s" % BASE_DIR)
    return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


# Build your own screener: a person combines chart patterns, indicators, mutual-fund activity, who owns the company and its ratios
# with AND / OR, runs it over every stock, saves it, and can ask for a measure the list lacks. Stocks subscribers and administrators
# only (stock_access.py). It reads the same readings as the Screener page and the stock page, so they cannot disagree.
# MF_SCREENBUILDER_ENABLED=false switches it off; MF_SCREENBUILDER_ACCESS=signed_in | everyone | admin changes who may use it.
_mount_optional("screenbuilder_api", "screener builder")


@app.get("/screen-builder", include_in_schema=False)
def screenbuilder_page():
    # Served to anyone: a static file that asks /api/screenbuilder/access first. The data endpoints are what refuse.
    return _static_page("screenbuilder.html")


# IPOs: what is coming, open and recently listed, drafts filed with SEBI, and a page for each issue with what its own offer document
# says (promoters, business, financials, what the money is for, the risk factors it lists). INFORMATION ONLY: no score, rank or verdict.
# The facts are read once a day from the NSE and SEBI by ipo_refresh.py (cron). Open to everyone; MF_IPO_ACCESS=admin | signed_in | premium
# narrows it and MF_IPO_ENABLED=false switches it off. One page per company: once an issue has listed, /ipo/<name> goes to the stock page,
# which carries the IPO as a tab (ipo_api.py). See DEPLOY_IPO.md.
_mount_optional("ipo_api", "IPO pages")


@app.get("/ipo", include_in_schema=False)
def ipo_page():
    # Served to anyone: a static file that asks /api/ipo/access first. The data endpoints are what refuse.
    return _static_page("ipo.html")


@app.get("/ipo/{slug}", include_in_schema=False)
def ipo_issue_page(slug: str):
    if not re.fullmatch(r"[a-z0-9-]{1,90}", slug):
        raise HTTPException(404, "No such issue.")
    # One page per company: a LISTED issue whose stock is on the site goes to the stock page, where the IPO is a tab (ipo_api.stock_page_url decides).
    try:
        import ipo_api
        target = ipo_api.stock_page_url(slug)
    except ImportError:
        target = None
    if target:
        return RedirectResponse(target, status_code=302)
    return _static_page("ipo_issue.html")


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
               u.is_premium, u.premium_until, u.premium_scope, u.full_name
        FROM mf_session s
        JOIN mf_user u USING (user_id)
        WHERE s.token_hash = %(h)s
          AND s.revoked_at IS NULL
          AND s.expires_at > now()
          AND u.is_active
    """, {"h": sha256_hex(token)}, one=True)


AREAS = ("mf", "stocks")


def scope_of(user: Optional[dict]) -> str:
    """Which tier the account's subscription covers: "mf", "stocks" or
    "all". An account with no scope on record -- granted by hand before
    tiers existed, or a legacy plan -- has everything, so nobody who was
    paying loses a page when this ships."""
    s = (user or {}).get("premium_scope") if user else None
    return s if s in ("mf", "stocks", "all") else "all"


_ADMIN_TTL = 30
_admin_seen = {"ids": frozenset(), "at": None}


def is_admin_user(user: Optional[dict]) -> bool:
    """Is this signed-in account in the app_admin table? The ids are kept for 30 seconds (this is asked on almost every request)
    and an error leaves the list EMPTY: fail closed, so a database hiccup can only ever mean fewer people get in."""
    if not user:
        return False
    now = time.monotonic()
    if _admin_seen["at"] is None or now - _admin_seen["at"] > _ADMIN_TTL:
        try:
            ids = frozenset(r["user_id"] for r in query("SELECT user_id FROM app_admin"))
        except Exception:                                    # noqa: BLE001
            ids = frozenset()
        _admin_seen.update(ids=ids, at=now)
    return user.get("user_id") in _admin_seen["ids"]


def has_subscription(user: Optional[dict], area: Optional[str] = None) -> bool:
    """Premium gate. With the paywall off, everyone passes -- which is
    what makes this file safe to deploy before login exists.

    ADMINISTRATORS ALWAYS PASS, in every area, with no payment: the owner has to be able to see and test everything the site
    sells. (Decided 11 Oct 2026; before that an admin without a paid plan met the paywall like anyone else.) This is only the
    PAYWALL. SCORES stay governed by MF_SCORES_ENABLED / the allowlist (has_score_access), which is a regulatory switch, not a
    price: an admin does not see scores unless they are on for that person.

    This is the answer for the PAID PRODUCTS. `area` is which side of the
    site is asking -- "mf" (look-through, fund screener, plan, overlap,
    review) or "stocks" (stock screens, which funds own a stock and what
    changed, check your view) -- and the subscription's tier must cover it.
    No area means "any live subscription will do" (scores, the account
    menu). Whether SCORES are shown is a separate question:
    has_score_access()."""
    if not PAYWALL_ENABLED:
        return True
    if is_admin_user(user):
        return True
    if not user or not user["is_premium"]:
        return False
    until = user["premium_until"]
    if until is not None and until < date.today():
        return False
    if area is None:
        return True
    s = scope_of(user)
    return s == "all" or s == area


# ---------------------------------------------------------------------
# WHO SEES SCORES
#
# MF_SCORES_ENABLED is the PUBLIC switch: true shows scores to everyone,
# false hides them from everyone. On top of it sits a short allowlist,
# the table mf_scores_access, so scores can be shown to a chosen few
# (say 10 of 100 subscribers) while the public switch stays off. Manage
# the list with grant_scores.py.
#
# The allowlist can only ADD people. It never hides scores from anyone
# the public switch already shows them to.
#
# Read through a short cache so this is not one extra query on every
# request. The price is that a grant or a revoke takes up to
# _SCORES_ALLOW_TTL seconds to be noticed.
#
# FAILS CLOSED. If the table is missing (grant_scores.py creates it the
# first time it runs) or the database hiccups, nobody is on the list.
# An error here can only ever mean FEWER people see scores, never more --
# which is the right direction to be wrong in while scores are withheld
# pending registration.
# ---------------------------------------------------------------------
_SCORES_ALLOW_TTL = 30
_scores_allow = {"ids": frozenset(), "at": None}


def _scores_allowlist() -> frozenset:
    now = time.monotonic()
    at = _scores_allow["at"]
    if at is None or now - at > _SCORES_ALLOW_TTL:
        try:
            ids = frozenset(r["user_id"] for r in
                            query("SELECT user_id FROM mf_scores_access"))
        except Exception:                                # noqa: BLE001
            ids = frozenset()
        _scores_allow.update(ids=ids, at=now)
    return _scores_allow["ids"]


def scores_on_for(user: Optional[dict]) -> bool:
    """Are scores switched on for THIS caller? True for everyone when the
    public switch is on; otherwise only for a signed-in user on the
    allowlist. Signed-out visitors never are, unless scores are public."""
    if SCORES_ENABLED:
        return True
    return bool(user) and user["user_id"] in _scores_allowlist()


def has_score_access(user: Optional[dict]) -> bool:
    """May this caller see a score-type field? Only if scores are switched
    on FOR THEM and the caller is subscribed. Every place that strips score
    fields already calls this, so switching scores off strips them for
    everyone, subscribers included, without touching those call sites."""
    return scores_on_for(user) and has_subscription(user)


def require_scores_on(user: Optional[dict] = None):
    """For routes that are score-derived through and through and have no
    gate of their own (the 'does the score work' evidence endpoints). With
    scores off for this caller there is nothing honest to return, so they
    404."""
    if not scores_on_for(user):
        raise HTTPException(404, "Not available yet.")


def send_otp_email(to_addr: str, code: str) -> None:
    # mailer.py is the one sender the whole site uses (Resend over HTTPS, which works from the droplet, or SMTP, which DigitalOcean
    # blocks). Used when it is installed; otherwise the SMTP-only code below, exactly as before.
    try:
        import mailer
    except ImportError:
        mailer = None
    if mailer is not None:
        ok, detail = mailer.send_email(
            to_addr, "Your FinChaya sign-in code",
            "Your FinChaya sign-in code is %s.\n\nIt expires in %d minutes. If you did not ask for it, ignore "
            "this email -- nobody can sign in without it.\n" % (code, OTP_TTL_MIN))
        if not ok:
            raise RuntimeError(detail)
        return
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
    # 254 is the longest address that exists; a longer one overflowed the database column and came back as a 500.
    if len(email) > 254 or not EMAIL_RE.match(email):
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
        SELECT user_id, email, account_type, is_premium, premium_until,
               premium_scope, full_name
        FROM mf_user WHERE user_id = %(u)s
    """, {"u": user_id}, one=True)
    return {"user": me, "token": token,
            "score_access": has_score_access(me),
            "subscribed": has_subscription(me),
            "subscribed_mf": has_subscription(me, "mf"),
            "subscribed_stocks": has_subscription(me, "stocks"),
            "scope": scope_of(me) if has_subscription(me) else None,
            "scores_enabled": scores_on_for(me)}


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
        SELECT user_id, email, account_type, is_premium, premium_until,
               premium_scope, full_name
        FROM mf_user WHERE user_id = %(u)s
    """, {"u": user_id}, one=True)
    return {"user": me, "token": token,
            "score_access": has_score_access(me),
            "subscribed": has_subscription(me),
            "subscribed_mf": has_subscription(me, "mf"),
            "subscribed_stocks": has_subscription(me, "stocks"),
            "scope": scope_of(me) if has_subscription(me) else None,
            "scores_enabled": scores_on_for(me)}


@app.get("/api/auth/me", tags=["auth"])
def auth_me(user: Optional[dict] = Depends(current_user)):
    """Who am I, and can I see scores. The page calls this on load."""
    return {"signed_in": user is not None,
            "user": user,
            "paywall_enabled": PAYWALL_ENABLED,
            "score_access": has_score_access(user),
            # subscribed = has SOME live subscription. subscribed_mf /
            # subscribed_stocks = may use that side's paid products; the
            # tier ("scope") decides. scores_enabled = scores exist at all
            # right now. The page needs all of these: "locked", "wrong
            # plan" and "coming soon" are different messages.
            "subscribed": has_subscription(user),
            "subscribed_mf": has_subscription(user, "mf"),
            "subscribed_stocks": has_subscription(user, "stocks"),
            "scope": scope_of(user) if has_subscription(user) else None,
            "scores_enabled": scores_on_for(user),
            # Premium or admin: whether to offer "Check your view". The
            # endpoint decides again on every call; this only drives the menu.
            "viewcheck": viewcheck_allowed(user),
            "viewcheck_audience": viewcheck_audience(),
            # Same rule for "Back-test your rules".
            "backtest": backtest_allowed(user),
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


# The newest stored close for each of a handful of stocks, and the one before
# it. Read from the candles themselves (stock_ohlc_daily), NOT from
# stock_score: a stock needs about 60 days of candles before it is scored at
# all, and while scores are off nothing is shown from that table anyway. This
# is the number to check "is the data up to date" against. LATERAL with a
# LIMIT of 2 per stock uses the (isin, as_of_date DESC) index, so a page of 50
# costs 50 short index reads, not a scan of 4 million candles.
_LATEST_PRICES = """
SELECT i.isin,
       (array_agg(o.close ORDER BY o.as_of_date DESC))[1]       AS close,
       (array_agg(o.as_of_date ORDER BY o.as_of_date DESC))[1]  AS as_of_date,
       (array_agg(o.close ORDER BY o.as_of_date DESC))[2]       AS prev_close
FROM unnest(%(isins)s::text[]) AS i(isin)
CROSS JOIN LATERAL (
    SELECT x.close, x.as_of_date FROM stock_ohlc_daily x
    WHERE x.isin = i.isin ORDER BY x.as_of_date DESC LIMIT 2
) o
GROUP BY i.isin
"""


def latest_prices(isins):
    """{isin: {"close", "date", "change_pct"}} for the stocks that have a
    candle. A stock with none is simply absent, which the page shows as a
    dash, not as zero. Prices are a market fact, not a FinChaya score, so
    this is open to everyone, signed in or not."""
    isins = [i for i in dict.fromkeys(isins) if i]
    if not isins:
        return {}
    out = {}
    for r in query(_LATEST_PRICES, {"isins": isins}):
        close = float(r["close"]) if r["close"] is not None else None
        prev = float(r["prev_close"]) if r["prev_close"] is not None else None
        out[r["isin"]] = {
            "close": close,
            "date": str(r["as_of_date"]),
            "change_pct": (round((close / prev - 1) * 100, 2)
                           if close and prev else None),
        }
    return out


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
                         or (sort == "coverage" and not scores_on_for(user))):
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
        if not scores_on_for(user):
            rows = strip_fields(rows, ("coverage_pct", "scored_holdings"))

    return {"total": total, "limit": limit, "offset": offset,
            "funds": rows,
            "score_locked": not unlocked,
            "scores_enabled": scores_on_for(user),
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
        if not scores_on_for(user):
            # How much of the portfolio "the score covers" is score
            # metadata, meaningless with no score. The lab's history goes
            # too: it is the allowlist's view of the same thing.
            fund = strip_fields(fund, ("coverage_pct", "scored_holdings"))
            lab = []
        return {"fund": fund, "holdings": holdings, "returns": returns,
                "score_history": history, "score_locked": True,
                "scores_enabled": scores_on_for(user),
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
def backtest_categories(user: Optional[dict] = Depends(current_user)):
    """Which categories have enough data for each section.

    The two sections need different things, so they get separate lists.
    The snapshot needs several funds scored at the historical date; the
    series needs at least one fund with three or more distinct monthly
    portfolios. Driving the dropdowns from this means an option never
    appears that would render an empty chart.
    """
    require_scores_on(user)
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
    user: Optional[dict] = Depends(current_user),
):
    """Score at a past date against the return since.

    Defaults to the OLDEST fund-score date, which is the backtest snapshot;
    the newest is today's live scoring and would give a zero-length window.
    Switched off with the rest of the scores: see require_scores_on().
    """
    require_scores_on(user)
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
    user: Optional[dict] = Depends(current_user),
):
    """Each fund's score at every date it was scored.

    Returns nothing useful until several monthly snapshots exist -- with
    two dates a 'trend' is a straight line between two points, which would
    look like evidence without being any.
    """
    require_scores_on(user)
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
def categories(user: Optional[dict] = Depends(current_user)):
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
    return rows if scores_on_for(user) else strip_fields(rows, ("avg_score",))


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
    """Every listed stock we track, with its latest price and that price's date.

    BUILT FROM stock_master, NOT stock_score. It used to list only stocks
    that had a score, and a stock is scored only after about 60 days of
    candles. So a stock added to the master tonight -- a new listing, or one
    the old price feed never delivered -- was invisible here for months, even
    with candles stored. The list is now the master, and a score is an
    optional extra that is present for the stocks that have one.

    The price is the newest candle in stock_ohlc_daily, with its date, so the
    list itself shows whether the data is up to date."""
    unlocked = has_score_access(user)
    where = ["m.is_active"]
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
    # ORDER BY used to run for every caller. Locked callers get A-Z. Unscored
    # stocks sort after the scored ones for those who can see scores.
    order_by = ("s.total_score DESC NULLS LAST, m.symbol" if unlocked
                else "m.symbol")

    total = query(f"SELECT COUNT(*) AS n FROM stock_master m WHERE {clause}",
                  params, one=True)["n"]

    rows = query(f"""
        SELECT m.symbol, m.company_name, m.isin,
               s.total_score, s.timeframe_used,
               s.adx_score, s.macd_score, s.rsi_score,
               s.bb_score, s.supertrend_score,
               ROUND(s.adx, 1) AS adx, ROUND(s.rsi, 1) AS rsi,
               s.supertrend_dir, s.as_of_date
        FROM stock_master m
        LEFT JOIN stock_score s
               ON s.isin = m.isin AND s.algo_version = %(algo)s
              AND s.as_of_date = (SELECT MAX(as_of_date) FROM stock_score
                                   WHERE algo_version = %(algo)s)
        WHERE {clause}
        ORDER BY {order_by}
        LIMIT %(limit)s OFFSET %(offset)s
    """, params)

    # as_of_date above is the SCORE's date and is null for an unscored stock.
    # The price and its date come from the candles.
    prices = latest_prices([r["isin"] for r in rows])
    for r in rows:
        p = prices.get(r["isin"]) or {}
        r["close_price"] = p.get("close")
        r["price_date"] = p.get("date")
        r["change_pct"] = p.get("change_pct")

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

    # The latest price and its date, from the candles. Open to everyone and
    # independent of scores: it is the figure to check data freshness by, and
    # the page used to show a price only through the score object, so it
    # showed none at all while scores are off.
    px = latest_prices([isin]).get(isin) or {}
    stock["last_price"] = px.get("close")
    stock["last_price_date"] = px.get("date")
    stock["day_change_pct"] = px.get("change_pct")

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
    if not has_subscription(user, "stocks"):
        raise HTTPException(
            402, "This is part of the Stocks plan. Sign in with an account "
                 "on the Stocks or Mutual funds + stocks plan to see it.")

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


FINANCIALS_QUARTERS = 4         # the latest four, as the page shows them; comparisons still look further back
FINANCIALS_YEARS = 4

# Has the filing-date column been added yet (add_stock_financials_filed_on.py)? Asked of the
# database rather than assumed, so the site works whether the migration or the new code arrives
# first. The query lists its columns by name instead of SELECT *, so adding the column later
# cannot upset a statement the database has already prepared. A "no" is re-checked after a minute.
_FIN_FILED = {"has": False, "checked": 0.0}


def _fin_has_filed_on():
    if _FIN_FILED["has"]:
        return True
    if time.monotonic() - _FIN_FILED["checked"] < 60:
        return False
    _FIN_FILED["checked"] = time.monotonic()
    found = query("""SELECT 1 AS x FROM information_schema.columns
                     WHERE table_name = 'stock_financials' AND column_name = 'filed_on' LIMIT 1""")
    _FIN_FILED["has"] = bool(found)
    return _FIN_FILED["has"]


@app.get("/api/stocks/{isin}/financials", tags=["stocks"])
def stock_financials(isin: str):
    """Revenue, profit and EPS for the latest quarters and years, as the company
    filed them with the stock exchange (stored by result_watcher.py and
    fetch_stock_financials.py).

    Only periods that actually have figures are returned. The table also holds
    dates-only rows for filings whose figures could not be read; those are left
    out rather than shown as blanks.

    "vs a year ago" compares the SAME period a year earlier on the SAME basis
    (consolidated against consolidated). It is null when that earlier figure is
    not on file, is on a different basis, or is zero or a loss, where a percentage
    change means nothing.

    OPEN TO EVERYONE, like /events: a company's own filing is not ours to charge
    for, and this is a table of reported figures, not a score.
    """
    filed = "filed_on" if _fin_has_filed_on() else "NULL::date AS filed_on"
    rows = query(f"""
        SELECT period_type, period_end, period_label, revenue, profit, eps, debt,
               consolidated, {filed}
        FROM stock_financials
        WHERE isin = %(isin)s AND (revenue IS NOT NULL OR profit IS NOT NULL)
        ORDER BY period_end DESC
    """, {"isin": isin})
    return {"isin": isin, **build_financials(rows)}


# ---- how the Financials tab reads the figures ------------------------------
# Pure functions (no database, no web framework) so they can be tested on their
# own. Kept in this file rather than a new module: one file fewer to forget to
# upload, and a missing module here would stop the whole site starting.
#
# The wording is deliberately flat: "up", "down", "little changed". Nothing here
# says a result is good or bad; that is for the reader to decide.
_RUPEE = chr(0x20B9)                # the rupee sign, written as a code so this file stays plain ASCII
_SHARE_COUNT_TOLERANCE = 0.15       # implied share count may differ this much and still be "the same"


def _fin_f(v):
    return None if v is None else float(v)


def _fin_group(x, dp):
    """Indian digit grouping (1,23,456.7) of a non-negative number."""
    whole, dot, frac = f"{x:.{dp}f}".partition(".")
    if len(whole) > 3:
        head, tail, parts = whole[:-3], whole[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return whole + dot + frac


def _fin_money(crore):
    c = abs(crore)
    return _RUPEE + _fin_group(c, 0 if c >= 1000 else 1 if c >= 100 else 2) + " crore"


def _fin_pct(now, before):
    """Percent change, or None when it would mean nothing (no earlier figure, or it was zero or a loss)."""
    if now is None or before is None or before <= 0:
        return None
    return round((now - before) / before * 100, 1)


def _fin_move(pct):
    if abs(pct) < 1:
        sign = "" if round(pct, 1) == 0 else "+" if pct > 0 else "-"      # no sign on a change that rounds to 0.0
        return "little changed (%s%.1f%%)" % (sign, abs(pct))
    return ("up %.1f%%" if pct > 0 else "down %.1f%%") % abs(pct)


def _fin_neighbour(rows, r, lo_days, hi_days):
    """The row of the same kind and basis whose period ended between lo and hi days before r's."""
    for o in rows:
        if o is r or o["period_type"] != r["period_type"] or o["consolidated"] != r["consolidated"]:
            continue
        if lo_days <= (r["period_end"] - o["period_end"]).days <= hi_days:
            return o
    return None


def _fin_eps_comparable(now, then):
    """Is earnings per share like-for-like between two periods? A bonus issue or split changes the
    number of shares, and EPS then falls (or rises) without the company doing anything. Profit
    divided by EPS is the share count; if it moved by more than the tolerance, do not compare.
    None = cannot tell (a figure is missing)."""
    try:
        a, b = abs(now["profit"] / now["eps"]), abs(then["profit"] / then["eps"])
        return abs(a / b - 1) <= _SHARE_COUNT_TOLERANCE
    except (TypeError, ZeroDivisionError):
        return None


def _fin_revenue_phrase(now, o, when):
    b = o["revenue"]
    pct = _fin_pct(now, b)
    return None if pct is None else f"{_fin_move(pct)} from {_fin_money(b / 1e7)} {when}"


def _fin_profit_phrase(now, o, when):
    b = o["profit"]
    if now is None or b is None or b == 0:
        return None
    if b > 0:
        if now > 0:
            return f"{_fin_move(_fin_pct(now, b))} from {_fin_money(b / 1e7)} {when}"
        return f"{'no profit' if now == 0 else 'a loss'}, against a profit of {_fin_money(b / 1e7)} {when}"
    if now >= 0:
        return f"a profit, against a loss of {_fin_money(-b / 1e7)} {when}"
    if now == b:
        return f"the same loss as {when}"
    return f"a {'smaller' if now > b else 'bigger'} loss than the {_fin_money(-b / 1e7)} loss {when}"


def _fin_eps_phrase(r, o, when):
    """(phrase or None, True if skipped because the share count changed)."""
    if r["eps"] is None or o["eps"] is None:
        return None, False
    like = _fin_eps_comparable(r, o)
    if like is None:
        return None, False
    if not like:
        return None, True
    now, b = r["eps"], o["eps"]
    if b <= 0:
        return None, False
    if now <= 0:
        return f"a loss per share, against {_RUPEE}{b:.2f} {when}", False
    return f"{_fin_move(_fin_pct(now, b))} from {_RUPEE}{b:.2f} {when}", False


def _fin_profit_short(now, b):
    """Profit against an earlier period, in a few words for the headline."""
    if now is None or b is None or b == 0:
        return None
    if b > 0:
        if now > 0:
            return "profit " + _fin_move(_fin_pct(now, b))
        return "no profit" if now == 0 else "profit turned to a loss"
    if now > 0:
        return "profit after a loss"
    if now == 0:
        return "break-even after a loss"
    return "loss narrowed" if now > b else "loss widened" if now < b else "loss unchanged"


def _fin_headline(r, year_ago, prev):
    """One line for the top of the stock page: the newest quarter against a year earlier
    (or, if that is not on file, the quarter before). Facts only, same flat wording as the
    longer summary. With nothing to compare against, it just states the figures."""
    d = r["period_end"]
    head = f"{r['period_label']} ({d.strftime('%b %Y')})"
    for ref, scope in ((year_ago, "vs a year ago"), (prev, "vs the previous quarter")):
        if ref is None:
            continue
        parts = []
        pct = _fin_pct(r["revenue"], ref["revenue"])
        if pct is not None:
            parts.append("revenue " + _fin_move(pct))
        profit = _fin_profit_short(r["profit"], ref["profit"])
        if profit:
            parts.append(profit)
        if r["eps"] is not None and ref["eps"] is not None and _fin_eps_comparable(r, ref) is True:
            pct = _fin_pct(r["eps"], ref["eps"])
            if pct is not None:
                parts.append("EPS " + _fin_move(pct))
        if parts:
            return f"{head} {scope}: " + ", ".join(parts)
    facts = []
    if r["revenue"] is not None:
        facts.append("revenue " + _fin_money(r["revenue"] / 1e7))
    if r["profit"] is not None:
        facts.append(("profit " if r["profit"] >= 0 else "loss ") + _fin_money(abs(r["profit"]) / 1e7))
    return f"{head}: " + ", ".join(facts) if facts else None


def _fin_sentence(lead, phrases, tail=""):
    return lead + (": " + " and ".join(phrases) if phrases else "") + "." + tail


def _fin_summary(rows, kind, today):
    """The plain-English reading of the newest period of one kind, or None."""
    r = next((x for x in rows if x["period_type"] == kind), None)
    if r is None:
        return None
    year_ago = _fin_neighbour(rows, r, 350, 380)
    prev = _fin_neighbour(rows, r, 80, 100) if kind == "quarterly" else None
    if kind == "quarterly":
        compare = [(year_ago, "a year ago"), (prev, "in the previous quarter")]
    else:
        compare = [(year_ago, f"in {year_ago['period_label']}" if year_ago else "")]
    compare = [(o, w) for o, w in compare if o is not None]

    lines = []
    if r["revenue"] is not None:
        lines.append(_fin_sentence(f"Revenue was {_fin_money(r['revenue'] / 1e7)}",
                                   [p for p in (_fin_revenue_phrase(r["revenue"], o, w) for o, w in compare) if p]))
    if r["profit"] is not None:
        lead = (f"Profit was {_fin_money(r['profit'] / 1e7)}" if r["profit"] >= 0
                else f"The company made a loss of {_fin_money(-r['profit'] / 1e7)}")
        lines.append(_fin_sentence(lead, [p for p in (_fin_profit_phrase(r["profit"], o, w) for o, w in compare) if p]))
    if r["eps"] is not None:
        results = [_fin_eps_phrase(r, o, w) for o, w in compare]
        lead = (f"Earnings per share was {_RUPEE}{r['eps']:.2f}" if r["eps"] >= 0
                else f"Loss per share was {_RUPEE}{-r['eps']:.2f}")
        tail = (" Not compared with an earlier period where the share count changed, for example "
                "through a bonus issue or split." if any(skipped for _, skipped in results) else "")
        lines.append(_fin_sentence(lead, [p for p, _ in results if p], tail))
    d, filed = r["period_end"], r.get("filed_on")
    title = "%s, %s figures for the %s ended %d %s %d" % (
        r["period_label"], "consolidated" if r["consolidated"] else "standalone",
        "quarter" if kind == "quarterly" else "year", d.day, d.strftime("%b"), d.year)
    if filed:
        title += ", filed %d %s %d" % (filed.day, filed.strftime("%b"), filed.year)
    return {
        "title": title,
        "lines": lines,
        "headline": _fin_headline(r, year_ago, prev) if kind == "quarterly" else None,
        "filed_on": filed,                                            # the day it was filed with the exchange, if known
        "days_since_filed": (today - filed).days if filed else None,  # lets the page flag a fresh result
    }


def build_financials(db_rows, today=None):
    """Tables, year-on-year and quarter-on-quarter changes, and the plain-English summary.
    Comparisons are made against ALL the rows given; only the newest few are returned."""
    today = today or date.today()
    rows = [{**r, "revenue": _fin_f(r["revenue"]), "profit": _fin_f(r["profit"]),
             "eps": _fin_f(r["eps"]), "debt": _fin_f(r["debt"])} for r in db_rows]     # newest first

    def crore(v):
        return None if v is None else round(v / 1e7, 2)

    def eps_change(r, o):
        if o is None or _fin_eps_comparable(r, o) is not True:
            return None
        return _fin_pct(r["eps"], o["eps"])

    def eps_changed(r, *others):
        """True if EPS was withheld from a comparison only because the share count changed."""
        return any(o is not None and _fin_eps_comparable(r, o) is False for o in others)

    out = {"quarterly": [], "yearly": []}
    limit = {"quarterly": FINANCIALS_QUARTERS, "yearly": FINANCIALS_YEARS}
    for r in rows:
        kind = r["period_type"]
        if kind not in out or len(out[kind]) >= limit[kind]:
            continue
        ya = _fin_neighbour(rows, r, 350, 380)
        pv = _fin_neighbour(rows, r, 80, 100) if kind == "quarterly" else None
        item = {
            "period_label": r["period_label"],
            "period_end": r["period_end"],
            "revenue_cr": crore(r["revenue"]),
            "profit_cr": crore(r["profit"]),
            "eps": r["eps"],
            "debt_cr": crore(r["debt"]),
            "basis": "Consolidated" if r["consolidated"] else "Standalone",
            "filed_on": r.get("filed_on"),
            "revenue_vs_year_ago_pct": _fin_pct(r["revenue"], ya["revenue"]) if ya else None,
            "profit_vs_year_ago_pct": _fin_pct(r["profit"], ya["profit"]) if ya else None,
            "eps_vs_year_ago_pct": eps_change(r, ya),
            "eps_share_count_changed": eps_changed(r, ya, pv),
        }
        if kind == "quarterly":
            item.update({
                "revenue_vs_prev_pct": _fin_pct(r["revenue"], pv["revenue"]) if pv else None,
                "profit_vs_prev_pct": _fin_pct(r["profit"], pv["profit"]) if pv else None,
                "eps_vs_prev_pct": eps_change(r, pv),
            })
        out[kind].append(item)
    out["summary"] = {"quarter": _fin_summary(rows, "quarterly", today), "year": _fin_summary(rows, "yearly", today)}
    return out


# ---- the price chart ---------------------------------------------------------
# stock_ohlc_daily holds prices as they traded. The day a share splits or a bonus is paid the price
# drops by the ratio, which on a chart looks like a crash. Earlier prices are therefore divided by
# the ratio (what every charting site does), but ONLY for an action that
#   (a) stock_event gives a ratio for (a split's old and new face value, a bonus's A:B), and
#   (b) the prices themselves confirm: the close before the ex-date was roughly `ratio` times the
#       close on it. The ex-date in the table may be a day or two out, so the nearest day that
#       fits is used.
# An action the prices do not confirm is left alone (the price series may already be adjusted, or
# the headline may have been misread), and a fall of 38% or more that no action explains is
# reported as a note rather than quietly smoothed over: demergers and real crashes are NOT
# adjusted. Pure functions again, so they can be tested without a database.
CHART_FLOOR = date(2024, 8, 1)      # see _chart_floor()
PRICE_RANGES = (("1M", 31), ("3M", 92), ("6M", 183), ("1Y", 366), ("2Y", 731),
                ("3Y", 1096), ("5Y", 1827), ("10Y", 3653))
_ACTION_SKIP = re.compile(r"ncrps|preference|debenture|warrant|\bncd", re.I)
_SPLIT_RE = re.compile(r"From\s+R(?:s|e)s?\.?\s*([\d.]+)\s*/?-?\s*(?:Per Share|each)?\s*To\s+R(?:s|e)s?\.?\s*([\d.]+)", re.I)
_BONUS_RE = re.compile(r"(\d+)\s*:\s*(\d+)")
_DEMERGER_RE = re.compile(r"demerger|arrangement|spin|scheme of|hive", re.I)
_EX_DATE_SLACK_DAYS = 3             # how far from the listed ex-date the price drop may sit
_BIG_FALL = 1.6                     # yesterday's close / today's close that deserves a note


def _chart_floor():
    """The first day the chart will show. The price history from before August 2024 came from Zerodha's
    Kite data feed, whose terms restrict showing it to the public on another site, so by default the
    chart starts on 1 Aug 2024, the day we began storing NSE's own end-of-day file (the values agree
    with NSE's file where the two overlap). Set MF_CHART_FULL_HISTORY=1 once that is settled."""
    return None if os.getenv("MF_CHART_FULL_HISTORY", "").strip() == "1" else CHART_FLOOR


def action_factor(kind, headline):
    """How many old shares one old share became: 2.0 for a 1:1 bonus or a Rs10 -> Rs5 split, 0.1 for a
    1-for-10 consolidation. None when the headline gives no usable ratio or is not about equity shares."""
    h = headline or ""
    if _ACTION_SKIP.search(h):
        return None
    try:
        if kind == "split":
            m = _SPLIT_RE.search(h)
            old, new = (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)
            f = old / new if old > 0 and new > 0 else None
        elif kind == "bonus":
            m = _BONUS_RE.search(h)
            a, b = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            f = (a + b) / b if a > 0 and b > 0 else None
        else:
            return None
    except ValueError:
        return None
    return f if f and abs(f - 1) > 1e-6 and 0.001 < f < 1000 else None


def _action_label(kind, headline):
    h = headline or ""
    if kind == "split":
        m = _SPLIT_RE.search(h)
        if m:
            return "split, face value Rs %s to Rs %s" % (m.group(1).rstrip("."), m.group(2).rstrip("."))
        return "share split"
    m = _BONUS_RE.search(h)
    return "bonus issue %s:%s" % (m.group(1), m.group(2)) if m else "bonus issue"


def collect_actions(event_rows):
    """[{"date", "factor", "kind", "label"}] from stock_event rows, oldest first. The same action is on file
    from BSE and from NSE; two readings of one day with (nearly) the same ratio count once."""
    out = []
    for e in event_rows:
        f = action_factor(e["kind"], e["headline"])
        if f is None:
            continue
        if any(o["date"] == e["event_date"] and o["kind"] == e["kind"] and abs(o["factor"] / f - 1) < 0.01
               for o in out):
            continue
        out.append({"date": e["event_date"], "factor": f, "kind": e["kind"],
                    "label": _action_label(e["kind"], e["headline"])})
    return sorted(out, key=lambda a: (a["date"], a["factor"]))


def _ratio_confirms(f, observed):
    """Does an observed fall of `observed` (close before / close on the day) fit a ratio of `f`? Prices move on
    the day as well, so the match is loose: within roughly a third of the ratio for a big one, and a
    clear step of at least 40% of the way for a small one such as a 1:4 bonus (1.25). A ratio in the 1.5 to 1.85 range (a 1:2 bonus
    is 1.5) must show a real fall (at least 1.2): 0.65 x 1.5 is 0.975, under which a price that did not fall at all would "confirm" it."""
    if observed is None or observed <= 0:
        return False
    if f < 1:
        return _ratio_confirms(1 / f, 1 / observed)
    if f >= 1.5:
        return max(0.65 * f, 1.2) <= observed <= 1.5 * f
    return observed >= 1 + 0.4 * (f - 1)


def confirm_actions(actions, bars):
    """Split `actions` into those the prices confirm and those they do not.
    `bars`: [{"d": date, "close": float}, ...] oldest first.
    Returns (applied, unconfirmed); an applied item has "idx" (the first bar at the new, lower price),
    "date" (that bar's date), "factor" and "label"."""
    n = len(bars)
    applied, unconfirmed, used = [], [], set()

    def observed(j):
        prev, cur = bars[j - 1]["close"], bars[j]["close"]
        return prev / cur if prev and cur and prev > 0 and cur > 0 else None

    def best_bar(day, f):
        near = [j for j in range(1, n) if abs((bars[j]["d"] - day).days) <= _EX_DATE_SLACK_DAYS
                and j not in used and observed(j) is not None]
        if not near:
            return None
        return min(near, key=lambda j: (abs(math.log(observed(j) / f)), abs((bars[j]["d"] - day).days)))

    groups = {}
    for a in actions:
        groups.setdefault(a["date"], []).append(a)
    for day in sorted(groups):
        items = groups[day]
        if n < 2 or day <= bars[0]["d"] or day > bars[-1]["d"] + timedelta(days=_EX_DATE_SLACK_DAYS):
            unconfirmed.extend(dict(a, why="outside the price history") for a in items)
            continue
        product = 1.0
        for a in items:
            product *= a["factor"]
        # the whole day's actions together (a split and a bonus can share an ex-date), else the best single one
        tries = [(product, items)] + ([(a["factor"], [a]) for a in items] if len(items) > 1 else [])
        done = None
        for f, part in tries:
            j = best_bar(day, f)
            if j is not None and _ratio_confirms(f, observed(j)):
                done = (j, f, part)
                break
        if done is None:
            unconfirmed.extend(dict(a, why="prices do not show it") for a in items)
            continue
        j, f, part = done
        used.add(j)
        applied.append({"idx": j, "date": bars[j]["d"], "factor": f,
                        "label": " and ".join(a["label"] for a in part), "kind": part[0]["kind"]})
        unconfirmed.extend(dict(a, why="not the one the prices show") for a in items if a not in part)
    return sorted(applied, key=lambda a: a["idx"]), unconfirmed


def price_notes(bars, applied, unconfirmed, demergers, since):
    """Plain sentences for what the reader should know about the chart, for dates on or after `since`."""
    notes = []
    for a in applied:
        if a["date"] >= since:
            notes.append("Prices before %s are adjusted for the %s." % (_fmt_day(a["date"]), a["label"]))
    covered = {a["idx"] for a in applied}
    for j in range(1, len(bars)):
        if bars[j]["d"] < since or j in covered or not bars[j]["close"] or not bars[j - 1]["close"]:
            continue
        if bars[j - 1]["close"] / bars[j]["close"] < _BIG_FALL:
            continue
        drop = round((1 - bars[j]["close"] / bars[j - 1]["close"]) * 100)
        if any(abs((bars[j]["d"] - dm).days) <= 4 for dm in demergers):
            notes.append("A demerger took effect on %s and the price fell %d%% that day. Prices before it are not "
                         "adjusted for it." % (_fmt_day(bars[j]["d"]), drop))
        else:
            notes.append("The price fell %d%% on %s. No split or bonus is on record for that day, so it is shown "
                         "as traded." % (drop, _fmt_day(bars[j]["d"])))
    return notes[:6]


def _fmt_day(d):
    return "%d %s" % (d.day, d.strftime("%b %Y"))


def adjust_bars(rows, applied):
    """Prices divided, and volumes multiplied, by every applied ratio that came AFTER the bar.
    rows: [{"d", "open", "high", "low", "close", "volume"}, ...] oldest first."""
    out = [None] * len(rows)
    factor = 1.0
    cut = {a["idx"]: a["factor"] for a in applied}
    for i in range(len(rows) - 1, -1, -1):
        if i + 1 in cut:
            factor *= cut[i + 1]
        r = rows[i]
        out[i] = {"d": r["d"], "factor": factor,
                  "open": r["open"] / factor if r["open"] is not None else None,
                  "high": r["high"] / factor if r["high"] is not None else None,
                  "low": r["low"] / factor if r["low"] is not None else None,
                  "close": r["close"] / factor if r["close"] is not None else None,
                  "volume": int(round(r["volume"] * factor)) if r["volume"] is not None else None}
    return out


WICK_LIMIT = 1.4        # a high above 1.4x, or a low below 1/1.4 of, the bar's own open and close is a bad tick
                        # (the same limit the chart scanners use, so the candles and the scans read the same bars)


def sane_wicks(b):
    """The bar with its high and low made consistent with its own open and close: never inside the body, and a
    wick far beyond it (a bad tick in the feed) held back. A bar with no high or low keeps None; the page draws
    it from the close. Nothing else about the bar changes."""
    c, o = b["close"], b["open"]
    if c is None:
        return b
    body_hi = max(c, o) if o else c
    body_lo = min(c, o) if o else c
    h, l = b["high"], b["low"]
    if h is not None:
        h = min(max(h, body_hi), body_hi * WICK_LIMIT)
    if l is not None:
        l = max(min(l, body_lo), body_lo / WICK_LIMIT)
    return dict(b, high=h, low=l)


def build_prices(price_rows, event_rows, range_key="1Y", today=None, floor=None):
    """The chart's data: adjusted daily bars for the chosen range, what changed over it, and notes.
    price_rows: dicts with as_of_date, open, high, low, close, volume, oldest first.
    event_rows: dicts with kind, event_date, headline (splits, bonuses, and other corporate actions).
    floor: the earliest day the chart is allowed to show, echoed back so the page can say so."""
    today = today or date.today()
    raw = [{"d": r["as_of_date"], "open": _fin_f(r["open"]), "high": _fin_f(r["high"]), "low": _fin_f(r["low"]),
            "close": _fin_f(r["close"]), "volume": r["volume"]} for r in price_rows if r["close"] is not None]
    if not raw:
        return {"empty": True, "points": [], "ranges": [], "stats": None, "notes": [], "adjustments": []}
    actions = collect_actions([e for e in event_rows if e["kind"] in ("split", "bonus")])
    applied, unconfirmed = confirm_actions(actions, raw)
    bars = [sane_wicks(b) for b in adjust_bars(raw, applied)]
    first, last = bars[0]["d"], bars[-1]["d"]
    span = (last - first).days
    ranges = [k for k, days in PRICE_RANGES if days <= span * 1.05 + 5]
    if not ranges:
        ranges = [PRICE_RANGES[0][0]]
    days = dict(PRICE_RANGES).get(range_key)
    if range_key == "ALL" or days is None:
        start = first
    else:
        start = max(first, last - timedelta(days=days))
    shown = [b for b in bars if b["d"] >= start]
    closes = [b["close"] for b in shown if b["close"] is not None]
    hi = max(shown, key=lambda b: b["close"])
    lo = min(shown, key=lambda b: b["close"])
    prev = bars[-2] if len(bars) > 1 else None
    demergers = [e["event_date"] for e in event_rows if e["kind"] == "corp_action_other"
                 and _DEMERGER_RE.search(e.get("headline") or "")]
    stats = {
        "last": round(bars[-1]["close"], 2), "last_date": last,
        "day_change_pct": round((bars[-1]["close"] / prev["close"] - 1) * 100, 2) if prev and prev["close"] else None,
        "range_start": shown[0]["d"], "range_start_close": round(shown[0]["close"], 2),
        "range_change_pct": round((closes[-1] / closes[0] - 1) * 100, 2) if len(closes) > 1 and closes[0] else None,
        "high": round(hi["close"], 2), "high_date": hi["d"], "low": round(lo["close"], 2), "low_date": lo["d"],
    }
    dp = lambda v: None if v is None else round(v, 2 if v >= 10 else 3)
    return {
        "range": range_key, "ranges": ranges, "history_from": first, "floor": floor, "stale_days": (today - last).days,
        "points": [[b["d"].isoformat(), dp(b["open"]), dp(b["high"]), dp(b["low"]), dp(b["close"]), b["volume"]]
                   for b in shown],
        "stats": stats,
        "adjustments": [{"date": a["date"], "ratio": round(a["factor"], 4), "what": a["label"]}
                        for a in applied if a["date"] >= start],
        "notes": price_notes(raw, applied, unconfirmed, demergers, start),
        "empty": False,
    }


@app.get("/api/stocks/{isin}/prices", tags=["stocks"])
def stock_prices(isin: str, range: str = Query("1Y", pattern="^(1M|3M|6M|1Y|2Y|3Y|5Y|10Y|ALL)$")):
    """End-of-day prices for the price chart, adjusted for splits and bonuses (see the notes above
    build_prices). Daily open, high, low, close and volume as [date, o, h, l, c, v].

    OPEN TO EVERYONE: a price is a market fact, not a FinChaya score (the same reasoning as
    /api/stocks/{isin}/financials and latest_prices)."""
    chart_floor = _chart_floor()
    floor = chart_floor or date(1990, 1, 1)
    prices = query("""
        SELECT as_of_date, open, high, low, close, volume
        FROM stock_ohlc_daily
        WHERE isin = %(isin)s AND as_of_date >= %(floor)s
        ORDER BY as_of_date
    """, {"isin": isin, "floor": floor})
    events = query("""
        SELECT kind, event_date, headline
        FROM stock_event
        WHERE isin = %(isin)s AND kind IN ('split', 'bonus', 'corp_action_other') AND event_date >= %(floor)s
        ORDER BY event_date
    """, {"isin": isin, "floor": floor}) if prices else []
    return {"isin": isin, **build_prices(prices, events, range, floor=chart_floor)}


# ---- ratios built from the filings and the price -------------------------------
# Trailing P/E and PEG, worked out here from the results we store (stock_financials) and the latest close;
# nothing is taken from a third party. Pure functions again, tested without a database.
#
# P/E = latest close / earnings per share over the last four quarters. The four quarters must be
# consecutive and on one basis (consolidated, else standalone), and recent.
#
# A BONUS OR SPLIT changes what an EPS figure means: the price is always in today's shares, an EPS filed
# before the action is in the old ones. Each quarter's EPS is divided by every split/bonus ratio whose
# ex-date came AFTER that quarter was filed (and on or before the price's date), putting all of them in
# today's shares. Where quarters sit on both sides of an action that is CHECKED rather than trusted: profit /
# EPS is the share count, and if it still jumps between the quarters (a misread ratio, a buyback, a rights
# issue) no P/E is shown, and the reason says so. Where an action came after ALL the quarters used there is no
# later filing to check against, so the exchange's own ratio is relied on and the working says so. A ratio we
# cannot stand behind is left out, never estimated.
#
# PEG = P/E divided by how fast that 12-month EPS grew over the year before it (per cent), so it needs
# eight consecutive quarters. It looks BACKWARD at reported growth; the usual PEG uses analysts' forecasts,
# which we do not hold. It is left out when earnings did not grow.
RATIO_MAX_QUARTER_AGE_DAYS = 200        # the newest quarter must have ended within this long ago
_QUARTER_GAP_DAYS = (80, 100)           # consecutive quarter-ends are about 91 days apart
_FILING_LAG_DAYS = 45                   # when a row has no filing date, assume it was filed this long after its quarter
_FINANCIALS_ROWS = 40


def _consecutive_quarters(rows, consolidated):
    """The newest unbroken run of quarterly rows with an EPS on one basis, newest first."""
    own = sorted((r for r in rows if r["period_type"] == "quarterly" and r["consolidated"] == consolidated
                  and r["eps"] is not None), key=lambda r: r["period_end"], reverse=True)
    run = []
    for r in own:
        if run:
            gap = (run[-1]["period_end"] - r["period_end"]).days
            if gap < 20:
                continue                # the same quarter twice
            if not _QUARTER_GAP_DAYS[0] <= gap <= _QUARTER_GAP_DAYS[1]:
                break
        run.append(r)
    return run


def _restate(r, actions, cutoff):
    """(EPS in today's shares, the divisor used) for one quarter."""
    filed = r.get("filed_on") or (r["period_end"] + timedelta(days=_FILING_LAG_DAYS))
    f = 1.0
    for a in actions:
        if filed < a["date"] <= cutoff:
            f *= a["factor"]
    return r["eps"] / f, f


def _shares_hold_steady(quarters, restated):
    """True/False: does profit / restated EPS (the share count) stay within the tolerance across the quarters?
    None when it cannot be told (fewer than two quarters have both figures)."""
    counts = []
    for r, e in zip(quarters, restated):
        if r["profit"] is not None and e:
            counts.append(abs(r["profit"] / e))
    if len(counts) < 2 or min(counts) <= 0:
        return None
    return max(counts) / min(counts) <= (1 + _SHARE_COUNT_TOLERANCE) / (1 - _SHARE_COUNT_TOLERANCE)


def _consistent(quarters, pairs):
    """Can these quarters be added up and compared? `pairs` is _restate()'s (EPS, divisor) for each.
    No if the share count still jumps after restating. If it cannot be told (a profit figure is missing),
    only if every quarter was restated by the same amount, so that none is on a different footing from
    another."""
    steady = _shares_hold_steady(quarters, [e for e, f in pairs])
    if steady is False:
        return False
    return steady is True or len({round(f, 6) for e, f in pairs}) == 1


def _actions_in_effect(quarters, actions, cutoff):
    """The split/bonus actions that changed at least one of these quarters' EPS, oldest first."""
    out = []
    for a in actions:
        for r in quarters:
            filed = r.get("filed_on") or (r["period_end"] + timedelta(days=_FILING_LAG_DAYS))
            if filed < a["date"] <= cutoff:
                out.append(a)
                break
    return out


def _rupees(v, dp=2):
    return _RUPEE + _fin_group(abs(v), dp) if v >= 0 else "-" + _RUPEE + _fin_group(abs(v), dp)


def _build_pe_peg(fin_rows, event_rows, price, today=None):
    """P/E and PEG for one stock.
    fin_rows: stock_financials rows (period_type, period_end, eps, profit, consolidated, filed_on).
    event_rows: stock_event rows (kind, event_date, headline) for splits and bonuses.
    price: {"close", "date"} or None. Each ratio comes back as {"value", "reason", "detail"}: a value with the
    working shown, or no value and the reason in plain words."""
    today = today or date.today()
    rows = [{**r, "eps": _fin_f(r["eps"]), "profit": _fin_f(r["profit"])} for r in fin_rows]

    def none(reason):
        return {"value": None, "reason": reason, "detail": None}

    out = {"pe": none(None), "peg": none(None), "eps_ttm": none(None), "quarters_to": None, "basis": None, "price": None}
    if not price or not price.get("close"):
        out["pe"] = none("No recent price on file.")
        return out
    close, price_day = float(price["close"]), price["date"]
    if isinstance(price_day, str):
        price_day = date.fromisoformat(price_day)
    out["price"] = {"close": close, "date": price_day}

    runs = [(c, _consecutive_quarters(rows, c)) for c in (True, False)]
    runs = [(c, run) for c, run in runs if run]
    if not runs:
        out["pe"] = none("No quarterly results on file.")
        return out
    # the basis with the newest quarter; consolidated on a tie, unless it has fewer quarters
    basis, run = max(runs, key=lambda cr: (cr[1][0]["period_end"], len(cr[1][:8]), cr[0]))
    out["basis"] = "Consolidated" if basis else "Standalone"
    out["quarters_to"] = run[0]["period_end"]
    if len(run) < 4:
        out["pe"] = none("Fewer than four consecutive quarters of results are on file.")
        return out
    if (price_day - run[0]["period_end"]).days > RATIO_MAX_QUARTER_AGE_DAYS:
        out["pe"] = none("The latest results on file are for the quarter ended %s, too old to use." % _fmt_day(run[0]["period_end"]))
        return out

    actions = collect_actions([e for e in event_rows if e["kind"] in ("split", "bonus")])
    pairs = [_restate(r, actions, price_day) for r in run[:8]]
    if not _consistent(run[:4], pairs[:4]):
        out["pe"] = none("The share count changed within these four quarters (a bonus issue, split or buyback) in a way "
                         "we could not adjust for, so a P/E would not be like for like.")
        return out
    ttm = sum(e for e, f in pairs[:4])
    moved = _actions_in_effect(run[:4], actions, price_day)
    adjusted = (" Earnings per share filed before the %s (ex-date %s) are restated to today's shares."
                % (" and the ".join(a["label"] for a in moved), " and ".join(_fmt_day(a["date"]) for a in moved))) if moved else ""
    out["eps_ttm"] = {"value": round(ttm, 2), "reason": None,
                      "detail": "Earnings per share added up over the four quarters to %s (%s), in today's shares.%s"
                                % (_fmt_day(run[0]["period_end"]), out["basis"].lower(), adjusted)}
    if ttm <= 0:
        out["pe"] = none("Not meaningful: earnings per share over the last 12 months were a loss.")
        out["peg"] = none("Not meaningful: earnings per share over the last 12 months were a loss.")
        return out
    pe = close / ttm
    out["pe"] = {"value": round(pe, 1), "reason": None,
                 "detail": "%s price on %s divided by %s earnings per share over the four quarters to %s (%s).%s"
                           % (_rupees(close), _fmt_day(price_day), _rupees(ttm), _fmt_day(run[0]["period_end"]), out["basis"].lower(), adjusted)}

    if len(run) < 8:
        out["peg"] = none("Needs eight consecutive quarters of results to measure growth; %d are on file." % len(run))
        return out
    if not _consistent(run[:8], pairs[:8]):
        out["peg"] = none("The share count changed over these two years in a way we could not adjust for, so growth could not "
                          "be measured like for like.")
        return out
    before = sum(e for e, f in pairs[4:8])
    if before <= 0:
        out["peg"] = none("Not meaningful: earnings per share were a loss in the 12 months before.")
        return out
    growth = (ttm / before - 1) * 100
    if growth <= 0:
        shown = round(growth, 1) or 0.0                         # no "-0.0"
        out["peg"] = none("Not meaningful: earnings per share did not grow over the last 12 months (%.1f%%)." % shown)
        return out
    out["peg"] = {"value": round(pe / growth, 2), "reason": None,
                  "detail": "P/E of %.1f divided by %.1f%%, the growth in 12-month earnings per share (%s, against %s a year earlier). "
                            "This looks back at reported growth; it is not based on forecasts."
                            % (pe, growth, _rupees(ttm), _rupees(before))}
    return out


# ---- ratios from the balance sheet: P/B, book value, market cap, ROE, debt to equity, interest cover ----
# From stock_balance_sheet (read by fetch_stock_financials.balance_sheet_figures, filled by result_watcher.py):
# the annual and September balance sheets. The share count is the one on the balance-sheet date, carried to
# today by the splits and bonuses since (price is in today's shares); P/B = price / (net worth / shares), so
# both sides are on the same date. Banks and insurers have no debt-to-equity or interest cover (their
# borrowing is the business) and say so. A figure the filing did not give is a reason, never a guess.
BS_MAX_AGE_DAYS = 400               # a balance sheet older than this is too old to price against
ROE_MAX_AGE_DAYS = 640              # the year's profit may be up to this old (the latest year is filed by June)


def _fy_label(d):
    return "FY%d-%02d" % (d.year - 1, d.year % 100) if d.month == 3 else "the year to " + _fmt_day(d)


def _product(xs):
    p = 1.0
    for x in xs:
        p *= x
    return p


def _shares_today(b, actions, price_day, master_face_value):
    """(shares in today's terms, note, None) or (None, None, reason) for one balance-sheet row. Splits and
    bonuses with an ex-date after the balance-sheet date and up to the price date are applied."""
    since = [a for a in actions if b["period_end"] < a["date"] <= price_day]
    note = ""
    if since:
        note = " Share count carried forward for the %s (ex-date %s)." % (
            " and the ".join(a["label"] for a in since), " and ".join(_fmt_day(a["date"]) for a in since))
    if b["shares"]:
        return b["shares"] * _product(a["factor"] for a in since), note, None
    # Insurers' filings give no face value. Paid-up capital over the face value held today is the share count
    # today for a split (the face value we hold already reflects it); a bonus is applied on top.
    if b["paid_up"] and master_face_value and master_face_value > 0:
        bonuses = [a for a in since if a["kind"] == "bonus"]
        return (b["paid_up"] / master_face_value * _product(a["factor"] for a in bonuses),
                " Share count worked out from the paid-up capital and the face value we hold." + (note if bonuses else ""), None)
    return None, None, "The filing does not give the number of shares."


def _add_balance_sheet_ratios(out, bs_rows, event_rows, master_face_value=None):
    def none(reason):
        return {"value": None, "reason": reason, "detail": None}

    for k in ("pb", "bvps", "market_cap", "roe", "de", "interest_cover"):
        out[k] = none(None)
    out["balance_sheet_to"] = None
    px = out.get("price")
    rows = [{**r, **{c: _fin_f(r.get(c)) for c in ("net_worth", "total_equity", "paid_up", "face_value", "shares", "debt",
                                                    "total_assets", "profit_owners", "profit_before_tax", "finance_costs")}}
            for r in bs_rows]
    rows.sort(key=lambda r: r["period_end"], reverse=True)
    if not px:
        for k in out["pb"], out["bvps"], out["market_cap"]:
            k["reason"] = "No recent price on file."
    if not rows:
        for k in ("pb", "bvps", "market_cap", "roe", "de", "interest_cover"):
            if out[k]["reason"] is None:
                out[k]["reason"] = "No balance sheet on file yet."
        return
    price_day = px["date"] if px else rows[0]["period_end"]
    actions = collect_actions([e for e in event_rows if e["kind"] in ("split", "bonus")])
    latest = rows[0]
    out["balance_sheet_to"] = latest["period_end"]
    when = _fmt_day(latest["period_end"])
    basis = "consolidated" if latest["consolidated"] else "standalone"

    # --- P/B, book value per share, market cap
    if px and (price_day - latest["period_end"]).days > BS_MAX_AGE_DAYS:
        for k in ("pb", "bvps", "market_cap"):
            out[k] = none("The latest balance sheet on file is for %s, too old to use." % when)
    elif px:
        shares, note, why = _shares_today(latest, actions, price_day, master_face_value)
        nw = latest["net_worth"]
        if nw is None:
            for k in ("pb", "bvps", "market_cap"):
                out[k] = none("The balance sheet on file gives no net worth.")
        elif shares is None or shares <= 0:
            for k in ("pb", "bvps", "market_cap"):
                out[k] = none(why or "The number of shares could not be worked out.")
        else:
            bvps = nw / shares                  # book value per share in TODAY's shares
            out["market_cap"] = {"value": round(px["close"] * shares / 1e7), "reason": None,
                                 "detail": "%s price on %s times about %s crore shares.%s" % (
                                     _rupees(px["close"]), _fmt_day(price_day), _fin_group(shares / 1e7, 2), note)}
            out["bvps"] = {"value": round(bvps, 2), "reason": None,
                           "detail": "Net worth of %s crore (%s, at %s) over about %s crore shares.%s" % (
                               _rupees(nw / 1e7, 0), basis, when, _fin_group(shares / 1e7, 2), note)}
            if nw <= 0:
                out["pb"] = none("Not meaningful: the company's net worth is negative.")
            else:
                out["pb"] = {"value": round(px["close"] / bvps, 2), "reason": None,
                             "detail": "%s price on %s divided by book value per share of %s: the net worth of shareholders "
                                       "(%s crore, %s, at %s) over the share count.%s" % (
                                           _rupees(px["close"]), _fmt_day(price_day), _rupees(bvps),
                                           _rupees(nw / 1e7, 0), basis, when, note)}

    # --- return on equity, from the latest full year
    years = [r for r in rows if r["period_type"] == "yearly" and r["profit_owners"] is not None and r["net_worth"] is not None]
    if not years:
        out["roe"] = none("No full-year balance sheet and profit on file yet.")
    else:
        y = years[0]
        if (price_day - y["period_end"]).days > ROE_MAX_AGE_DAYS:
            out["roe"] = none("The latest full year on file is %s, too old to use." % _fy_label(y["period_end"]))
        else:
            prev = next((r for r in rows if r["period_type"] == "yearly" and r["consolidated"] == y["consolidated"]
                         and r["net_worth"] is not None and 330 <= (y["period_end"] - r["period_end"]).days <= 400), None)
            avg = (y["net_worth"] + prev["net_worth"]) / 2 if prev else y["net_worth"]
            if avg <= 0:
                out["roe"] = none("Not meaningful: shareholders' equity was negative or nil.")
            else:
                roe = y["profit_owners"] / avg * 100
                eq = ("their average equity of %s crore (%s at the start of the year and %s at the end)"
                      % (_rupees(avg / 1e7, 0), _rupees(prev["net_worth"] / 1e7, 0), _rupees(y["net_worth"] / 1e7, 0)) if prev else
                      "their equity of %s crore at the year end (no earlier year-end balance sheet on file)" % _rupees(y["net_worth"] / 1e7, 0))
                out["roe"] = {"value": round(roe, 1), "reason": None,
                              "detail": "Profit attributable to shareholders of %s crore for %s (%s) divided by %s." % (
                                  _rupees(y["profit_owners"] / 1e7, 0), _fy_label(y["period_end"]),
                                  "consolidated" if y["consolidated"] else "standalone", eq)}

    # --- debt to equity, interest cover
    kind = latest["kind"]
    if kind in ("bank", "insurer"):
        word = "banks" if kind == "bank" else "insurers"
        out["de"] = none("Not used for %s: borrowing and deposits are their business." % word)
        out["interest_cover"] = none("Not used for %s." % word)
    else:
        te = latest["total_equity"]
        if latest["debt"] is None:
            out["de"] = none("The filing gives no borrowings figure.")
        elif te is None or te <= 0:
            out["de"] = none("Not meaningful: total equity is negative or nil.")
        else:
            out["de"] = {"value": round(latest["debt"] / te, 2), "reason": None,
                         "detail": "Borrowings of %s crore divided by total equity of %s crore (%s, at %s)%s." % (
                             _rupees(latest["debt"] / 1e7, 0), _rupees(te / 1e7, 0), basis, when,
                             "; for a finance company this adds borrowings, debt securities, subordinated debt and deposits"
                             if kind == "nbfc" else "")}
        if kind == "nbfc":
            out["interest_cover"] = none("Not used for finance companies: interest is their main cost.")
        else:
            c = next((r for r in years if r["profit_before_tax"] is not None and r["finance_costs"] is not None), None)
            if c is None or (price_day - c["period_end"]).days > ROE_MAX_AGE_DAYS:
                out["interest_cover"] = none("No recent full-year profit and finance costs on file.")
            elif c["finance_costs"] <= 0:
                out["interest_cover"] = none("The company reported no finance costs for %s." % _fy_label(c["period_end"]))
            elif c["profit_before_tax"] + c["finance_costs"] <= 0:
                out["interest_cover"] = none("Not meaningful: the company made a loss before interest and tax in %s." % _fy_label(c["period_end"]))
            else:
                out["interest_cover"] = {"value": round((c["profit_before_tax"] + c["finance_costs"]) / c["finance_costs"], 1),
                                         "reason": None,
                                         "detail": "Profit before tax of %s crore plus finance costs of %s crore, divided by finance costs, for %s." % (
                                             _rupees(c["profit_before_tax"] / 1e7, 0), _rupees(c["finance_costs"] / 1e7, 0), _fy_label(c["period_end"]))}


# ---- who owns the company: the shareholding pattern -----------------------------------------------
# From stock_shareholding, filled by fetch_shareholding.py from NSE's quarterly shareholding-pattern filings.
# Percentages are 0-100. The detail columns (mutual funds, foreign institutions, individuals ...) are empty
# until that filing's XBRL has been read (detail_loaded), and then the page shows only promoters and public.
# A promoter holding of 0 means the company has no promoter, not that the figure is missing.
_SHP_COLUMNS = ("promoter_pct", "public_pct", "employee_trust_pct", "mf_pct", "insurance_pct", "bank_pct", "other_dii_pct",
                "fii_pct", "retail_pct", "bodies_corporate_pct", "nri_pct", "other_public_pct", "promoter_pledged_pct")
_SHP_CHANGES = ("promoter", "foreign_institutions", "domestic_institutions", "mutual_funds", "insurers", "banks",
                "other_domestic", "individuals", "companies", "nri", "other", "employee_trusts", "pledged")


def _fy_quarter_label(d):
    q = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[d.month]
    return "Q%d FY%02d" % (q, (d.year + 1 if d.month >= 4 else d.year) % 100)


def build_shareholding(db_rows, quarters=8):
    """The latest `quarters` filings, newest first, each with who owns what and how each of the main holdings moved
    since the quarter before (in percentage points; None unless that quarter is the very next one back)."""
    rows = sorted(db_rows, key=lambda r: r["quarter_end"], reverse=True)[:quarters]
    out = []
    for r in rows:
        q = {c: _fin_f(r.get(c)) for c in _SHP_COLUMNS}
        detail = bool(r.get("detail_loaded"))
        parts = [q["mf_pct"], q["insurance_pct"], q["bank_pct"], q["other_dii_pct"]]
        out.append({
            "quarter_end": r["quarter_end"], "label": _fy_quarter_label(r["quarter_end"]),
            "promoter": q["promoter_pct"], "public": q["public_pct"], "employee_trusts": q["employee_trust_pct"],
            "mutual_funds": q["mf_pct"] if detail else None, "insurers": q["insurance_pct"] if detail else None,
            "banks": q["bank_pct"] if detail else None, "other_domestic": q["other_dii_pct"] if detail else None,
            "domestic_institutions": round(sum(parts), 4) if detail and all(p is not None for p in parts) else None,
            "foreign_institutions": q["fii_pct"] if detail else None, "individuals": q["retail_pct"] if detail else None,
            "companies": q["bodies_corporate_pct"] if detail else None, "nri": q["nri_pct"] if detail else None,
            "other": q["other_public_pct"] if detail else None,
            "pledged": q["promoter_pledged_pct"] if detail else None,
            "submitted_on": r.get("submitted_on"), "revised": bool(r.get("revised")), "detail": detail,
        })
    for i, item in enumerate(out):
        prev = out[i + 1] if i + 1 < len(out) else None
        adjacent = prev is not None and 80 <= (item["quarter_end"] - prev["quarter_end"]).days <= 100
        item["change"] = {k: (round(item[k] - prev[k], 2) if adjacent and item[k] is not None and prev[k] is not None else None)
                          for k in _SHP_CHANGES}
    return {"quarters": out, "latest": out[0] if out else None, "has_detail": any(q["detail"] for q in out)}


@app.get("/api/stocks/{isin}/shareholding", tags=["stocks"])
def stock_shareholding(isin: str, quarters: int = Query(8, ge=1, le=20)):
    """Who owns the company, quarter by quarter, as the company filed it with NSE. Empty until
    create_stock_shareholding_table.py and fetch_shareholding.py have been run (query_optional).
    OPEN TO EVERYONE: a company's own filing, not a FinChaya score."""
    rows = query_optional("""
        SELECT quarter_end, promoter_pct, public_pct, employee_trust_pct, mf_pct, insurance_pct, bank_pct, other_dii_pct,
               fii_pct, retail_pct, bodies_corporate_pct, nri_pct, other_public_pct, promoter_pledged_pct,
               submitted_on, revised, detail_loaded
        FROM stock_shareholding
        WHERE isin = %(isin)s
        ORDER BY quarter_end DESC
        LIMIT %(n)s
    """, {"isin": isin, "n": quarters})
    return {"isin": isin, **build_shareholding(rows, quarters)}


def build_ratios(fin_rows, event_rows, price, today=None, bs_rows=None, face_value=None):
    """Every ratio for one stock: P/E and PEG from the quarterly results (see _build_pe_peg), and from the
    balance sheet P/B, book value per share, market cap, return on equity, debt to equity and interest cover.
    Each is {"value", "reason", "detail"}: the number with its working, or no number and why."""
    out = _build_pe_peg(fin_rows, event_rows, price, today)
    _add_balance_sheet_ratios(out, bs_rows or [], event_rows, _fin_f(face_value))
    return out


@app.get("/api/stocks/{isin}/ratios", tags=["stocks"])
def stock_ratios(isin: str):
    """Ratios for the stock page, each with its working or the reason it is missing.
    OPEN TO EVERYONE, like /financials: reported figures divided by a price, not a score."""
    filed = "filed_on" if _fin_has_filed_on() else "NULL::date AS filed_on"
    fin = query(f"""
        SELECT period_type, period_end, eps, profit, consolidated, {filed}
        FROM stock_financials
        WHERE isin = %(isin)s AND period_type = 'quarterly'
        ORDER BY period_end DESC
        LIMIT %(n)s
    """, {"isin": isin, "n": _FINANCIALS_ROWS})
    px = latest_prices([isin]).get(isin)
    events = query("""
        SELECT kind, event_date, headline FROM stock_event
        WHERE isin = %(isin)s AND kind IN ('split', 'bonus') AND event_date >= CURRENT_DATE - 1100
        ORDER BY event_date
    """, {"isin": isin})
    # the balance-sheet table is built by create_stock_balance_sheet.py; until it exists these ratios just say so
    bs = query_optional("""
        SELECT period_end, period_type, kind, consolidated, net_worth, total_equity, paid_up, face_value, shares, debt,
               total_assets, profit_owners, profit_before_tax, finance_costs, depreciation
        FROM stock_balance_sheet
        WHERE isin = %(isin)s
        ORDER BY period_end DESC
        LIMIT 12
    """, {"isin": isin})
    fv = query("SELECT face_value FROM stock_master WHERE isin = %(isin)s", {"isin": isin})
    return {"isin": isin, **build_ratios(fin, events, px, bs_rows=bs, face_value=fv[0]["face_value"] if fv else None)}
