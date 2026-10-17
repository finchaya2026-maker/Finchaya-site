"""
trial.py -- a free trial of seven days, and the two emails around its end.
---------------------------------------------------------------------------
    cd /opt/mfapi
    venv/bin/python3 trial.py --run               # the daily job: expire ended trials, send the day-6 reminders and the day-8 farewells
    venv/bin/python3 trial.py --dry-run           # ... say who would be emailed, send and save nothing
    venv/bin/python3 trial.py --status            # trials by state, and whether email is set up
    venv/bin/python3 trial.py --sample you@x.com  # send ONE sample of each email to this address (nothing is saved)

WHAT A TRIAL IS
    A signed-in person with no plan picks what to try (Mutual funds, Stocks, or both) and gets that plan for seven days, with no card and no payment. It is
    the same switch every paid account uses: mf_user.is_premium / premium_until / premium_scope, so every gate on the site already understands it and
    nothing else had to change. The day it ends, premium_until has passed and the gates close by themselves; the daily job then also clears is_premium.

    Day 1 is the day it was started and day 7 the last day of access (premium_until = started + 6). The access ends with that day.
    DAY 6  reminder: "your trial ends tomorrow", with the prices and a link that opens the payment window (subscribe, or upgrade to a bigger plan).
    DAY 8  farewell: "we are sorry to let you go ... you can come back and subscribe at any time".
    Neither is sent to somebody who has subscribed in the meantime (a paid plan ends later than the trial), and each is sent once.

ONE TRIAL PER PERSON, NOT PER ACCOUNT
    billing_trial.user_id is unique, and so is email_key: the address with a Gmail address's dots and "+tags" removed, so name@gmail.com, n.ame@gmail.com
    and name+x@gmail.com are one person. Somebody who has had any plan before (a paid one, or one given by hand) is not offered it.

PAYING
    Nothing in here takes money or grants a paid plan. The reminder and the farewell link to  <site>/?subscribe=<tier>, which opens the Subscription window
    (finchaya.js), and payment is exactly what payments.py already does; its webhook then overrides the trial's scope and end date (GREATEST of the two).

SETTINGS (all optional, in /opt/mfapi/.env)
    MF_TRIAL_ENABLED=false   switch the trial off (nobody is offered it; trials already running carry on)
    MF_TRIAL_DAYS=7          how long it lasts (1 to 30); the reminder is sent on the day before the last day, the farewell the day after it
    FINCHAYA_SITE_URL        where the links in the emails go (default https://mf.finchaya.com)

THE TABLE   create_trial_table.py (once, by hand, with --admin doadmin on the live database).  Mounted by payments.py as /api/pay/trial.
"""

import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

try:                                                    # the job and the tests run without the web app
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    load_dotenv("/opt/mfapi/.env")
except Exception:                                       # noqa: BLE001
    pass

import psycopg
from psycopg.rows import dict_row

SCOPES = ("mf", "stocks", "all")
TITLES = {"mf": "Mutual funds", "stocks": "Stocks", "all": "Mutual funds + stocks"}
DEFAULT_PRICES = {"mf": ("₹149 / month", "₹999 / year"), "stocks": ("₹399 / month", "₹2,999 / year"), "all": ("₹599 / month", "₹3,499 / year")}


def _int_env(name, default, lo, hi):
    try:
        v = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


TRIAL_DAYS = _int_env("MF_TRIAL_DAYS", 7, 1, 30)
ENABLED = os.environ.get("MF_TRIAL_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
FAREWELL_WINDOW_DAYS = 3                                # a farewell later than this after the trial is not sent (it would read oddly)

REASONS = {
    "sign_in": "Sign in to start a free trial.",
    "off": "The free trial is not on offer right now.",
    "admin": "Administrator accounts already have every plan.",
    "has_access": "Your account already has a plan, so there is nothing to try.",
    "had_plan": "The free trial is for new accounts; this one has had a plan before.",
    "used": "You have already had your free trial.",
    "email_used": "A free trial has already been used with this email address.",
}


class TrialError(Exception):
    """A trial that cannot be started, with the reason as a key of REASONS."""

    def __init__(self, reason):
        super().__init__(REASONS.get(reason, reason))
        self.reason = reason


# =====================================================================
# DATES AND KEYS
# =====================================================================
def ends_on(started: date) -> date:
    """The last day of access: day 1 is the day it was started."""
    return started + timedelta(days=TRIAL_DAYS - 1)


def reminder_day(started: date) -> date:
    """The day the reminder goes: the day before the last (day 6 of 7)."""
    return ends_on(started) - timedelta(days=1)


def farewell_day(started: date) -> date:
    """The day the farewell goes: the day after the last (day 8 of 7)."""
    return ends_on(started) + timedelta(days=1)


def days_left(end: date, today: date) -> int:
    """Days of access left, today included (the last day says 1); 0 once it is over."""
    return max(0, (end - today).days + 1) if today <= end else 0


def email_key(email: Optional[str], user_id=None) -> str:
    """One key per person: lower case, the "+tag" dropped, and for Gmail the dots too (name@gmail.com = n.ame@googlemail.com)."""
    e = (email or "").strip().lower()
    if "@" not in e:
        return "uid:%s" % user_id
    local, domain = e.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        domain = "gmail.com"
        local = local.replace(".", "")
    return "%s@%s" % (local, domain) if local else "uid:%s" % user_id


def _q(cur, sql, params=None, one=False):
    cur.execute(sql, params or {})
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def _table_missing(e) -> bool:
    return isinstance(e, psycopg.errors.UndefinedTable)


# =====================================================================
# WHAT THE ACCOUNT CAN HAVE
# =====================================================================
def _paid_live(cur, user_id) -> bool:
    """A subscription that is being billed (not an abandoned checkout)."""
    try:
        return _q(cur, "SELECT 1 AS x FROM billing_subscription WHERE user_id = %(u)s AND status IN ('authenticated', 'active', 'pending') LIMIT 1",
                  {"u": user_id}, one=True) is not None
    except psycopg.errors.UndefinedTable:
        cur.connection.rollback()
        return False


def _had_plan(cur, user) -> bool:
    """Has this account ever had a plan? A subscription that got past 'created', or an end date from a hand-made grant."""
    if user.get("premium_until") is not None:
        return True
    try:
        return _q(cur, "SELECT 1 AS x FROM billing_subscription WHERE user_id = %(u)s AND status <> 'created' LIMIT 1", {"u": user["user_id"]}, one=True) is not None
    except psycopg.errors.UndefinedTable:
        cur.connection.rollback()
        return False


def converted(row, paid_live: bool) -> bool:
    """A trial whose owner has subscribed since: a paid plan ends later than the trial did, or one is being billed."""
    pu = row.get("premium_until")
    return bool(paid_live or (pu is not None and pu > row["ends_on"]))


def state(cur, user, is_admin=False, today=None) -> dict:
    """What the page needs: whether a trial can be started, why not, and where a running or finished one stands. Never raises for a missing table
    (the trial is then simply not on offer)."""
    today = today or date.today()
    out = {"enabled": ENABLED, "days": TRIAL_DAYS, "available": False, "reason": None, "message": None, "used": False, "active": None, "ended": None}

    def no(reason):
        out["reason"], out["message"] = reason, REASONS.get(reason)
        return out
    if not user:
        return no("sign_in")
    try:
        row = _q(cur, "SELECT * FROM billing_trial WHERE user_id = %(u)s", {"u": user["user_id"]}, one=True)
    except psycopg.errors.UndefinedTable:
        cur.connection.rollback()
        out["enabled"] = False
        return no("off")
    if row:
        out["used"] = True
        conv = converted(dict(row, premium_until=user.get("premium_until")), _paid_live(cur, user["user_id"]))
        info = {"scope": row["scope"], "title": TITLES[row["scope"]], "started_on": row["started_on"].isoformat(), "ends_on": row["ends_on"].isoformat()}
        if not conv and today <= row["ends_on"]:
            out["active"] = dict(info, days_left=days_left(row["ends_on"], today))
        elif not conv:
            out["ended"] = info
        return no("used")
    if is_admin:
        return no("admin")
    if not ENABLED:
        return no("off")
    if user.get("is_premium") and (user.get("premium_until") is None or user["premium_until"] >= today):
        return no("has_access")
    if _had_plan(cur, user):
        return no("had_plan")
    if _q(cur, "SELECT 1 AS x FROM billing_trial WHERE email_key = %(k)s", {"k": email_key(user.get("email"), user["user_id"])}, one=True):
        return no("email_used")
    out["available"] = True
    return out


def start(cur, user, scope, is_admin=False, today=None) -> dict:
    """Start the trial: one row in billing_trial and the account switched on, in the caller's transaction. Raises TrialError."""
    today = today or date.today()
    if scope not in SCOPES:
        raise TrialError("Choose what to try: Mutual funds, Stocks, or both.")
    st = state(cur, user, is_admin=is_admin, today=today)
    if not st["available"]:
        raise TrialError(st["reason"])
    try:
        cur.execute("INSERT INTO billing_trial (user_id, email_key, scope, started_on, ends_on) VALUES (%(u)s, %(k)s, %(s)s, %(a)s, %(b)s)",
                    {"u": user["user_id"], "k": email_key(user.get("email"), user["user_id"]), "s": scope, "a": today, "b": ends_on(today)})
    except psycopg.errors.UniqueViolation as e:                                     # two clicks at once, or the same person on two accounts
        cur.connection.rollback()
        raise TrialError("used" if "user_id" in str(e) else "email_used")
    cur.execute("UPDATE mf_user SET is_premium = true, premium_scope = %(s)s, premium_until = %(b)s WHERE user_id = %(u)s",
                {"s": scope, "b": ends_on(today), "u": user["user_id"]})
    user = dict(user, is_premium=True, premium_scope=scope, premium_until=ends_on(today))
    return state(cur, user, today=today)


# =====================================================================
# THE EMAILS
# =====================================================================
def _site():
    return (os.environ.get("FINCHAYA_SITE_URL") or "https://mf.finchaya.com").rstrip("/")


def _prices():
    """{scope: (monthly, yearly)} as the payment page shows them (payments.PRICES), else the list prices."""
    try:
        import payments
        return {s: (payments.PRICES[s + "_monthly"], payments.PRICES[s + "_yearly"]) for s in SCOPES}
    except Exception:                                   # noqa: BLE001
        return dict(DEFAULT_PRICES)


def _first(name, email):
    n = (name or "").strip().split(" ")[0] if (name or "").strip() else ""
    return n or ((email or "").split("@")[0] if email else "there")


def _fmt(d: date) -> str:
    return "%d %s %d" % (d.day, d.strftime("%B"), d.year)


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


DISCLAIMER = ("Educational content only, not investment advice. FinChaya is not currently registered with SEBI as an Investment Adviser or Research Analyst.")


def _plan_lines(scope, prices):
    """(text lines, html rows) for the three plans, the one that was tried marked."""
    text, rows = [], []
    for s in SCOPES:
        m, y = prices[s]
        mark = "  (what you tried)" if s == scope else ""
        text.append("  %-24s %s   or   %s%s" % (TITLES[s], m, y, mark))
        rows.append('<tr><td style="padding:6px 14px 6px 0"><b>%s</b>%s</td><td style="padding:6px 14px 6px 0">%s</td><td style="padding:6px 0">%s</td></tr>'
                    % (_esc(TITLES[s]), ' <span style="color:#51615a">(what you tried)</span>' if s == scope else "", _esc(m), _esc(y)))
    return text, '<table style="border-collapse:collapse;font-size:15px">' + "".join(rows) + "</table>"


def _frame(paragraphs_html, button_text, link, foot_html):
    return ("<html><body style=\"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#1b2b24;font-size:15px;line-height:1.55\">"
            + paragraphs_html
            + '<p style="margin:20px 0"><a href="%s" style="background:#0f7a56;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:700">%s</a></p>'
            % (_esc(link), _esc(button_text))
            + foot_html + '<p style="color:#51615a;font-size:12.5px">%s</p></body></html>' % _esc(DISCLAIMER))


def reminder_email(row, prices=None, site=None):
    """(subject, text, html): the trial ends tomorrow. `row` has email, full_name, scope, ends_on."""
    prices, site = prices or _prices(), site or _site()
    name, scope, end = _first(row.get("full_name"), row.get("email")), row["scope"], row["ends_on"]
    link = "%s/?subscribe=%s" % (site, scope)
    lines, table = _plan_lines(scope, prices)
    subject = "Your FinChaya free trial ends tomorrow"
    text = "\n".join([
        "Hello %s," % name, "",
        "Your free trial of FinChaya (%s) ends tomorrow, %s. After that the paid parts of the site close again; your account and everything you saved stay as they are." % (TITLES[scope], _fmt(end)),
        "", "To keep going, choose a plan. You can keep what you tried, or upgrade to a bigger one: the new plan starts the moment you pay, and nothing is charged before that.", "",
        *lines, "",
        "Subscribe or upgrade (this opens the payment window):", link, "",
        "No card is on file, so nothing is charged unless you choose to subscribe. Payment is handled by Razorpay, and you can cancel any time.",
        "If something was missing or confusing during the trial, just reply to this email: we read every reply.", "", "FinChaya", "", DISCLAIMER])
    html = _frame(
        "<p>Hello %s,</p><p>Your free trial of FinChaya (<b>%s</b>) <b>ends tomorrow, %s</b>. After that the paid parts of the site close again; your account and everything you saved stay as they are.</p>"
        "<p>To keep going, choose a plan. You can keep what you tried, or upgrade to a bigger one: the new plan starts the moment you pay, and nothing is charged before that.</p>%s"
        % (_esc(name), _esc(TITLES[scope]), _esc(_fmt(end)), table),
        "Subscribe or upgrade", link,
        "<p style='color:#51615a'>No card is on file, so nothing is charged unless you choose to subscribe. Payment is handled by Razorpay, and you can cancel any time. "
        "If something was missing or confusing during the trial, just reply to this email: we read every reply.</p><p>FinChaya</p>")
    return subject, text, html


def farewell_email(row, prices=None, site=None):
    """(subject, text, html): the trial is over and was not renewed; the door stays open."""
    prices, site = prices or _prices(), site or _site()
    name, scope, end = _first(row.get("full_name"), row.get("email")), row["scope"], row["ends_on"]
    link = "%s/?subscribe=%s" % (site, scope)
    lines, table = _plan_lines(scope, prices)
    subject = "We are sorry to see you go"
    text = "\n".join([
        "Hello %s," % name, "",
        "Your free trial of FinChaya (%s) ended on %s, and as you have not subscribed, we are sorry to let you go. Your account is still here exactly as it was; only the paid parts are closed again." % (TITLES[scope], _fmt(end)),
        "", "You are welcome back at any point, and you can renew whenever you like, whichever plan you want:", "", *lines, "",
        "Come back and subscribe (this opens the payment window):", link, "",
        "If you left because something was missing, hard to find or not what you expected, a one-line reply to this email would help us a great deal.", "", "FinChaya", "", DISCLAIMER])
    html = _frame(
        "<p>Hello %s,</p><p>Your free trial of FinChaya (<b>%s</b>) ended on <b>%s</b>, and as you have not subscribed, <b>we are sorry to let you go</b>. Your account is still here exactly as it was; only the paid parts are closed again.</p>"
        "<p>You are welcome back at any point, and you can renew whenever you like, whichever plan you want:</p>%s" % (_esc(name), _esc(TITLES[scope]), _esc(_fmt(end)), table),
        "Come back and subscribe", link,
        "<p style='color:#51615a'>If you left because something was missing, hard to find or not what you expected, a one-line reply to this email would help us a great deal.</p><p>FinChaya</p>")
    return subject, text, html


# =====================================================================
# THE DAILY JOB
# =====================================================================
def run(cur, today=None, send=None, dry=False, log=print) -> dict:
    """Expire ended trials, send the day-6 reminders and the day-8 farewells. `send(to, subject, text, html)` -> (ok, detail); `dry` sends and saves nothing.
    Safe to run any number of times a day: each email is sent once. Returns the counts."""
    today = today or date.today()
    n = {"trials": 0, "expired": 0, "converted": 0, "reminders": 0, "farewells": 0, "failed": 0, "skipped": 0}
    prices = _prices()
    rows = _q(cur, """SELECT t.*, u.email, u.full_name, u.is_active, u.is_premium, u.premium_until, u.premium_scope
                      FROM billing_trial t JOIN mf_user u USING (user_id)
                      WHERE t.expired_on IS NULL OR t.ends_on >= %(d)s - 10
                      ORDER BY t.trial_id""", {"d": today})
    for row in rows:
        n["trials"] += 1
        conv = converted(row, _paid_live(cur, row["user_id"]))
        # ---- ended: switch the account off (unless it has paid), once
        if row["expired_on"] is None and today > row["ends_on"]:
            if conv:
                n["converted"] += 1
                if not dry:
                    cur.execute("UPDATE billing_trial SET converted_on = COALESCE(converted_on, %(d)s), expired_on = %(d)s WHERE trial_id = %(t)s", {"d": today, "t": row["trial_id"]})
            else:
                n["expired"] += 1
                if not dry:
                    # only the trial's own grant: a plan bought since has a later end date and is left alone
                    cur.execute("UPDATE mf_user SET is_premium = false WHERE user_id = %(u)s AND is_premium AND premium_until = %(e)s", {"u": row["user_id"], "e": row["ends_on"]})
                    cur.execute("UPDATE billing_trial SET expired_on = %(d)s WHERE trial_id = %(t)s", {"d": today, "t": row["trial_id"]})
        if conv:
            if not dry and row["converted_on"] is None and today >= row["started_on"]:
                cur.execute("UPDATE billing_trial SET converted_on = %(d)s WHERE trial_id = %(t)s", {"d": today, "t": row["trial_id"]})
            continue
        # ---- the two emails
        for kind, col, due in (("reminder", "reminder_sent_at", row["reminder_sent_at"] is None and reminder_day(row["started_on"]) <= today <= row["ends_on"]),
                               ("farewell", "farewell_sent_at", row["farewell_sent_at"] is None and farewell_day(row["started_on"]) <= today <= farewell_day(row["started_on"]) + timedelta(days=FAREWELL_WINDOW_DAYS - 1))):
            if not due:
                continue
            if not row["is_active"] or not row["email"]:
                n["skipped"] += 1
                continue
            subject, text, html = (reminder_email if kind == "reminder" else farewell_email)(row, prices)
            if dry:
                log("  would send the %s to %s (%s trial, ends %s)" % (kind, row["email"], row["scope"], row["ends_on"]))
                n["reminders" if kind == "reminder" else "farewells"] += 1
                continue
            ok, detail = send(row["email"], subject, text, html)
            note = (detail or "")[:300]
            if ok:
                cur.execute("UPDATE billing_trial SET %s = now(), %s = %%(n)s WHERE trial_id = %%(t)s" % (col, col.replace("_sent_at", "_note")), {"n": note, "t": row["trial_id"]})
                n["reminders" if kind == "reminder" else "farewells"] += 1
                log("  sent the %s to %s" % (kind, row["email"]))
            else:
                cur.execute("UPDATE billing_trial SET %s = %%(n)s WHERE trial_id = %%(t)s" % col.replace("_sent_at", "_note"), {"n": note, "t": row["trial_id"]})
                n["failed"] += 1
                log("  could NOT send the %s to %s: %s (tried again on the next run)" % (kind, row["email"], note))
    return n


# =====================================================================
# THE API (mounted by payments.py under /api/pay)
# =====================================================================
try:
    from fastapi import APIRouter, HTTPException, Request
    from pydantic import BaseModel

    router = APIRouter(prefix="/trial", tags=["trial"])

    class StartBody(BaseModel):
        scope: str = "all"

    def _who(request):
        from api import current_user, is_admin_user
        u = current_user(request)
        return u, bool(u and is_admin_user(u))

    @router.get("")
    def trial_status(request: Request):
        """Where this visitor stands with the free trial. Answers 200 for a visitor who is not signed in too (the page then offers to sign in first)."""
        u, adm = _who(request)
        with psycopg.connect(os.environ["FINCHAYA_DB"], row_factory=dict_row) as conn, conn.cursor() as cur:
            st = state(cur, u, is_admin=adm)
            conn.rollback()
        st["signed_in"] = bool(u)
        return st

    @router.post("")
    def trial_start(request: Request, body: StartBody):
        """Start the free trial of the tier chosen. The browser sends only the tier; who it is, whether they may, and the dates are all decided here."""
        u, adm = _who(request)
        if not u:
            raise HTTPException(401, REASONS["sign_in"])
        with psycopg.connect(os.environ["FINCHAYA_DB"], row_factory=dict_row) as conn, conn.cursor() as cur:
            try:
                cur.execute("SELECT user_id FROM mf_user WHERE user_id = %(u)s FOR UPDATE", {"u": u["user_id"]})     # two clicks at once queue here
                st = start(cur, u, body.scope.strip().lower(), is_admin=adm)
                conn.commit()
            except TrialError as e:
                conn.rollback()
                raise HTTPException(409 if e.reason in REASONS else 400, str(e))
            except psycopg.errors.UndefinedTable:
                conn.rollback()
                raise HTTPException(503, "The free trial is not set up on this server yet.")
        st["signed_in"] = True
        a = st["active"]
        st["message"] = ("Your free trial of %s has started. It runs to the end of %s." % (a["title"], _fmt(date.fromisoformat(a["ends_on"])))) if a else None
        return st
except ImportError:                                     # the job and the tests do not need the web framework
    router = None


# =====================================================================
# COMMAND LINE
# =====================================================================
def _sender():
    import mailer
    return lambda to, subject, text, html: mailer.send_email(to, subject, text, html=html)


def main(argv):
    dsn = os.environ.get("FINCHAYA_DB")
    if not dsn:
        sys.exit("FINCHAYA_DB is not set. Is /opt/mfapi/.env present?")
    if "--sample" in argv:
        to = argv[argv.index("--sample") + 1]
        end = date.today() + timedelta(days=1)
        row = {"email": to, "full_name": "", "scope": "stocks", "ends_on": end}
        send = _sender()
        for fn in (reminder_email, farewell_email):
            subject, text, html = fn(row)
            ok, detail = send(to, "[sample] " + subject, text, html)
            print("%-34s %s %s" % (subject, "sent" if ok else "NOT sent:", detail))
        return
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        if "--status" in argv:
            import mailer
            c = mailer.mail_config()
            print("email: %s" % ("ready (%s)" % c["transport"] if c["configured"] else "NOT set up: %s" % c["why"]))
            print("trial: %s, %d days" % ("on" if ENABLED else "OFF (MF_TRIAL_ENABLED)", TRIAL_DAYS))
            for r in _q(cur, """SELECT scope, count(*) AS n, count(*) FILTER (WHERE ends_on >= CURRENT_DATE) AS running, count(reminder_sent_at) AS reminded,
                                       count(farewell_sent_at) AS farewelled, count(converted_on) AS converted FROM billing_trial GROUP BY scope ORDER BY scope"""):
                print("  %-7s started %d, running %d, reminded %d, farewell sent %d, subscribed since %d" % (r["scope"], r["n"], r["running"], r["reminded"], r["farewelled"], r["converted"]))
            return
        dry = "--dry-run" in argv
        if not dry and "--run" not in argv:
            print(__doc__)
            return
        print("%s trial job, %s" % ("DRY RUN of the" if dry else "Running the", date.today()))
        n = run(cur, send=None if dry else _sender(), dry=dry)
        conn.commit()
        print("trials looked at %(trials)d | reminders %(reminders)d | farewells %(farewells)d | ended %(expired)d | subscribed since %(converted)d | could not send %(failed)d | skipped %(skipped)d" % n)


if __name__ == "__main__":
    main(sys.argv[1:])
