"""
tradeplan_alerts.py -- "email me when this condition is met".
---------------------------------------------------------------------------
Two halves in one file, because they share the same rules:

  1. the CHECKER, run on a schedule on the server (cron):

         cd /opt/mfapi && venv/bin/python3 tradeplan_alerts.py              # one pass, quietly
         venv/bin/python3 tradeplan_alerts.py --dry-run                     # what WOULD fire, nothing changed or sent
         venv/bin/python3 tradeplan_alerts.py --status                      # is email set up, when did it last run
         venv/bin/python3 tradeplan_alerts.py --test-email you@example.com  # send ONE test email and say what happened

  2. the pieces the web page's API uses (tradeplan_api.py): the email settings check and the email wording.

WHAT AN ALERT IS
    A person looks at a plan's result on a stock and sees a condition is NOT met ("Daily MACD is rising" -- no). They
    press "Alert me when this is met". We store that one condition (or the whole plan) exactly as it was evaluated,
    already turned round if they were reading it for the other view, and the checker looks again whenever there is new
    data. The first time it is met, ONE email goes out and the alert is finished. It does not repeat daily and it does
    not follow the price in and out of the condition: one condition met, one email. They can set another.

    The condition is read on a FINISHED candle by default. A weekly or monthly candle that is still forming can be
    "met" on a Tuesday and not on the Friday close, and an email that is wrong by the weekend is worse than none.
    The person can untick that.

THE RULES THAT KEEP THIS HONEST (the fund alerts were once built as switches that did nothing; this is not that)
    * IT SAYS WHAT IS ACTUALLY SET UP. The checker leaves a row in trade_alert_run each time it runs, and the page reads
      it: "last checked ...", or "the checker has never run", or "has not run for N hours". Email settings are read the
      same way. If either is missing the page says so in full weight and still saves the alert -- but never lets a
      person think they are covered.
    * A TRIGGER IS RECORDED BEFORE THE EMAIL IS TRIED. A met condition is saved as 'triggered' with what it read, and
      only then is the email sent. If the email fails (it can: see below) the alert still shows on the page as triggered
      with "email not sent", and the checker retries (up to MAX_EMAIL_ATTEMPTS), so a mail outage loses nothing.
    * NOTHING FIRES ON GUESSWORK. A condition with no data is "no data", not "met". An alert is refused at creation if
      its condition is already met or cannot be read at all (it could never fire).
    * LIMITS. 20 alerts per person, 20 emails per person per day (the rest wait for tomorrow), one email per alert.

EMAIL, AND WHY IT NEEDS CHECKING BEFORE ANYBODY RELIES ON IT
    Sending is mailer.py (shared with stock alerts and payment receipts): Resend over HTTPS (MF_MAIL_PROVIDER=resend,
    RESEND_API_KEY, MF_MAIL_FROM), or SMTP from the settings the site already has (MF_SMTP_USER / MF_SMTP_PASS). The
    settings are explained there. DigitalOcean BLOCKS outbound SMTP (ACCOUNTS_AND_AUTH.md), so on the droplet it is Resend.

    Run --test-email first. It sends one real message and prints either "sent" or the exact reason it could not.

WORDING
    FinChaya is not a SEBI-registered Research Analyst or Investment Adviser. The email says a condition THE PERSON
    CHOSE is met; it says nothing about what to do, and carries the same disclaimer as the page.
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

MAX_ALERTS = 20                 # active or paused, per person
MAX_EMAIL_ATTEMPTS = 6          # a failing send is retried on later runs, this many times in all
MAX_EMAILS_PER_DAY = 20         # per person; the rest wait for the next day
STALE_CHECK_HOURS = 36          # the checker is "not running" if its last run is older than this
LOCK_KEY = 727410               # advisory lock: two checkers at once would send the same email twice


# =====================================================================
# SENDING EMAIL -- mailer.py, the one sender the whole site uses (stock alerts and payment receipts too)
# =====================================================================
from mailer import _clean_header, mail_config, send_email, site_url      # noqa: E402,F401  (re-exported: the page and the tests call them from here)


# =====================================================================
# THE EMAIL
# =====================================================================
DISCLAIMER = ("This is a condition you chose, checked against the latest candles we hold. It is not a recommendation "
              "to buy, sell or hold anything, and an indicator describes the price so far; it does not predict what "
              "comes next. Educational content only, not investment advice. FinChaya is not currently registered "
              "with SEBI as an Investment Adviser or Research Analyst.")


def _j(v):
    """A json column as Python: psycopg gives a list or dict, other drivers give text."""
    if isinstance(v, (str, bytes)):
        return json.loads(v)
    return v


def build_email(alert, reading, env=None):
    """(subject, body) for a met alert. Plain text, nothing from the person's own input is placed anywhere it could be
    read as markup or a header. `reading` is the trigger_reading list: [{text, reading, as_of, tf_label}, ...]."""
    who = alert.get("symbol") or alert.get("company_name") or alert.get("instrument") or "your instrument"
    name = alert.get("company_name")
    target = "%s (%s)" % (name, who) if name and alert.get("symbol") else who
    sentence = alert["sentence"]
    head = {"condition": "Your condition is now met", "set": "Your conditions are now all met"}.get(
        alert["scope"], "Your plan's conditions are now met")
    noun = {"condition": "condition", "set": "set of conditions"}.get(alert["scope"], "plan")
    subject = _clean_header("%s: %s - %s" % (head, sentence, target))
    if len(subject) > 160:
        subject = subject[:157] + "..."
    lines = ["Hello,", "", "A %s you asked us to watch is now met%s." % (
        noun, ": every one of them holds at the same time" if alert["scope"] != "condition" else ""), "",
             "  " + sentence, "  %s, read for a %s view" % (target, alert["for_view"]), ""]
    for r in reading or []:
        if r.get("reading"):
            # a chart reading names its candle; a company figure (profit, P/E, shareholding) names the period or the day it is as of
            lines.append("  %s" % r["reading"] + (("  (%s)" % r["as_of"] if r.get("tf_label") == "Company data" else "  (%s candle of %s)" % (r["tf_label"].lower(), r["as_of"]))
                                                  if r.get("as_of") else ""))
    lines += ["", "Plan: %s" % alert["plan_name"], "",
              "This was a one-time alert, so you will not get another email for it. To see it, set another, or remove "
              "your alerts: %s/tradeplan#alerts" % site_url(env), "", DISCLAIMER, ""]
    return subject, "\n".join(lines)


# =====================================================================
# THE CHECKER
# =====================================================================
_SELECT = ("SELECT alert_id, user_id, plan_name, scope, instrument, isin, symbol, company_name, for_view, spec, sentence, "
           "status, last_stamp, last_state, triggered_at, trigger_reading, email_status, email_attempts "
           "FROM trade_alert ")


def _plan_of(alert):
    """The alert's stored conditions as a plan the engine can evaluate. direction == the view it was set for, so the
    engine does NOT turn it round again: it was already turned round when the alert was made."""
    import tradeplan_engine as te
    spec = _j(alert["spec"])
    return te.clean_plan({"name": "Alert", "instrument": alert["instrument"],
                          "instrument_label": "alert" if alert["instrument"] == "other" else None,
                          "direction": alert["for_view"], "trade_tf": "D", "closed_only": bool(spec.get("closed_only", True)),
                          "notes": "", "conditions": spec["conditions"]})


def evaluate_alert(alert, frames, today, now=None):
    """The engine's answer for one stored alert against candles already loaded."""
    import tradeplan_engine as te
    return te.evaluate_plan(_plan_of(alert), frames, alert["for_view"], today=today, now=now)


def reading_of(result):
    return [{"text": c["text"], "reading": c.get("reading"), "as_of": c.get("as_of"), "tf_label": c["tf_label"]}
            for c in result["conditions"] if c["state"] == "met"]


def _utc(now):
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


def run_once(query, execute, send=None, load=None, today=None, now=None, dry_run=False, log=print, env=None):
    """One pass over every alert that needs a look. Everything it needs from outside is passed in, so it runs the same
    against the real database, the test database and a fake mail server.

      query(sql, params=None, one=False) / execute(sql, params=None, returning=False)   the database
      send(to, subject, body) -> (ok, detail)                                           mail
      load(query, instrument, isin, tfs) -> (frames, notes)                              candles

    Returns the counts. Never raises for one bad alert: it is logged and the rest are still looked at."""
    if send is None:
        send = lambda to, subject, body: send_email(to, subject, body, env=env)       # noqa: E731
    if load is None:
        import tradeplan_api
        load = tradeplan_api.load_frames
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    out = {"checked": 0, "skipped": 0, "fired": 0, "emailed": 0, "failed": 0, "paused": 0, "errors": []}

    rows = query(_SELECT + "WHERE status = 'active' OR (status = 'triggered' AND email_status <> 'sent' "
                           "AND email_attempts < %(max)s) ORDER BY alert_id", {"max": MAX_EMAIL_ATTEMPTS})
    if not rows:
        if not dry_run:
            _heartbeat(execute, out)
        return out

    # who to write to, and who has had their share of emails today
    uids = sorted({r["user_id"] for r in rows})
    emails = {r["user_id"]: r for r in query(
        "SELECT user_id, email, is_active FROM mf_user WHERE user_id = ANY(%(u)s)", {"u": uids})}
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    sent_today = {r["user_id"]: r["n"] for r in query(
        "SELECT user_id, COUNT(*) AS n FROM trade_alert WHERE email_status = 'sent' AND email_sent_at >= %(since)s "
        "GROUP BY user_id", {"since": day_start})}

    def email_for(alert, reading):
        """Try the email for a triggered alert and record what happened. Returns True if it went."""
        u = emails.get(alert["user_id"])
        if not u or not u.get("is_active", True) or not u.get("email"):
            ok, detail, final = False, "no active account with an email address", True
        elif sent_today.get(alert["user_id"], 0) >= MAX_EMAILS_PER_DAY:
            return False                                     # over today's share: it waits, untouched, for tomorrow
        else:
            subject, body = build_email(alert, reading, env)
            if dry_run:
                log("  [dry run] would email %s: %s" % (u["email"], subject))
                return False
            ok, detail = send(u["email"], subject, body)
            final = False
        if dry_run:
            return False
        if ok:
            execute("UPDATE trade_alert SET email_status = 'sent', email_attempts = email_attempts + 1, email_error = NULL, "
                    "email_sent_at = %(at)s WHERE alert_id = %(id)s", {"at": _utc(now), "id": alert["alert_id"]})
            sent_today[alert["user_id"]] = sent_today.get(alert["user_id"], 0) + 1
            out["emailed"] += 1
            log("alert %s: emailed %s" % (alert["alert_id"], u["email"]))
            return True
        execute("UPDATE trade_alert SET email_status = 'failed', email_attempts = %(a)s, email_error = %(e)s "
                "WHERE alert_id = %(id)s",
                {"a": MAX_EMAIL_ATTEMPTS if final else alert["email_attempts"] + 1, "e": detail[:300], "id": alert["alert_id"]})
        out["failed"] += 1
        log("alert %s: email FAILED (%s)" % (alert["alert_id"], detail))
        return False

    # 1. triggered alerts whose email did not go: try again, with what they read at the time
    for a in [r for r in rows if r["status"] == "triggered"]:
        try:
            email_for(a, _j(a["trigger_reading"]) if a["trigger_reading"] is not None else [])
        except Exception as e:                               # noqa: BLE001
            out["errors"].append("alert %s retry: %s: %s" % (a["alert_id"], type(e).__name__, e))
            log(out["errors"][-1])

    # 2. alerts still waiting: look at each stock once
    waiting = [r for r in rows if r["status"] == "active"]
    by_stock = {}
    for a in waiting:
        by_stock.setdefault((a["instrument"], a["isin"]), []).append(a)
    isins = sorted({k[1] for k in by_stock if k[1]})
    newest = {}
    if isins:
        newest = {r["isin"]: r["d"] for r in query(
            "SELECT isin, max(as_of_date) AS d FROM stock_ohlc_daily WHERE isin = ANY(%(i)s) GROUP BY isin", {"i": isins})}

    for (inst, isin), alerts in by_stock.items():
        stamp = "%s|%s" % (newest.get(isin), today)            # a new candle, or a new day (a week can finish without one)
        todo = [a for a in alerts if a["last_stamp"] != stamp]
        out["skipped"] += len(alerts) - len(todo)
        if not todo:
            continue
        plans, valid = {}, []
        for a in todo:                                       # one at a time: a bad alert must only ever pause ITSELF
            try:
                plans[a["alert_id"]] = _plan_of(a)
                valid.append(a)
            except Exception:                                # noqa: BLE001
                out["paused"] += 1
                if not dry_run:
                    execute("UPDATE trade_alert SET status = 'paused', note = %(n)s WHERE alert_id = %(id)s",
                            {"n": "Paused: this alert uses something that is no longer available.", "id": a["alert_id"]})
        if not valid:
            continue
        tfs = {c["tf"] for a in valid for c in plans[a["alert_id"]]["conditions"]}
        try:
            frames, _notes = load(query, inst, isin, tfs)
        except Exception as e:                               # noqa: BLE001
            out["errors"].append("%s %s: could not load candles: %s: %s" % (inst, isin, type(e).__name__, e))
            log(out["errors"][-1])
            continue
        for a in valid:
            try:
                import tradeplan_engine as te
                res = te.evaluate_plan(plans[a["alert_id"]], frames, a["for_view"], today=today)
                state = res["overall"]["state"]
                out["checked"] += 1
                last = {"met": "met", "not_met": "not_met", "unknown": "no_data"}[state]
                if state != "met":
                    if not dry_run:
                        execute("UPDATE trade_alert SET last_checked_at = %(at)s, last_stamp = %(s)s, last_state = %(st)s "
                                "WHERE alert_id = %(id)s", {"at": _utc(now), "s": stamp, "st": last, "id": a["alert_id"]})
                    continue
                reading = reading_of(res)
                out["fired"] += 1
                log("alert %s: MET  %s  (%s)" % (a["alert_id"], a["sentence"], isin))
                if dry_run:
                    email_for(dict(a, **{"email_attempts": 0}), reading)
                    continue
                # recorded as triggered BEFORE the email is tried, so a mail failure loses nothing
                execute("UPDATE trade_alert SET status = 'triggered', triggered_at = %(at)s, last_checked_at = %(at)s, "
                        "last_stamp = %(s)s, last_state = 'met', trigger_reading = %(r)s::jsonb WHERE alert_id = %(id)s",
                        {"at": _utc(now), "s": stamp, "r": json.dumps(reading), "id": a["alert_id"]})
                email_for(a, reading)
            except Exception as e:                           # noqa: BLE001
                out["errors"].append("alert %s: %s: %s" % (a["alert_id"], type(e).__name__, e))
                log(out["errors"][-1])

    if not dry_run:
        _heartbeat(execute, out)
    return out


def _heartbeat(execute, out):
    """The row the page reads to say alerts are really being looked at."""
    execute("INSERT INTO trade_alert_run (checked, skipped, fired, emailed, failed, error) "
            "VALUES (%(c)s, %(s)s, %(f)s, %(e)s, %(x)s, %(err)s) RETURNING run_id",
            {"c": out["checked"], "s": out["skipped"], "f": out["fired"], "e": out["emailed"], "x": out["failed"],
             "err": ("; ".join(out["errors"])[:500] or None)}, returning=True)


def checker_status(query, now=None):
    """What the page needs to say whether alerts are being checked: when the checker last ran, and whether that is too
    long ago. A failed read is reported as 'unknown', never as healthy."""
    now = now or datetime.now(timezone.utc)
    try:
        row = query("SELECT started_at, checked, fired, emailed, failed, error FROM trade_alert_run "
                    "ORDER BY run_id DESC LIMIT 1", None, one=True)
    except Exception:                                        # noqa: BLE001
        return {"known": False, "never_run": None, "last_run_at": None, "stale": None}
    if not row:
        return {"known": True, "never_run": True, "last_run_at": None, "stale": True}
    at = row["started_at"]
    if isinstance(at, str):
        at = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
    stale = (_utc(now) - _utc(at)) > timedelta(hours=STALE_CHECK_HOURS)
    return {"known": True, "never_run": False, "last_run_at": at, "stale": stale,
            "checked": row["checked"], "fired": row["fired"], "emailed": row["emailed"], "failed": row["failed"],
            "error": row["error"]}


# =====================================================================
# COMMAND LINE
# =====================================================================
def _connect():
    import psycopg
    from dotenv import load_dotenv
    from psycopg.rows import dict_row
    here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(here, ".env"))
    load_dotenv("/opt/mfapi/.env")
    dsn = os.environ.get("FINCHAYA_DB")
    if not dsn:
        sys.exit("FINCHAYA_DB is not set.")
    return psycopg.connect(dsn, row_factory=dict_row)


def main(argv):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    load_dotenv("/opt/mfapi/.env")

    if "--test-email" in argv:
        i = argv.index("--test-email")
        to = argv[i + 1] if i + 1 < len(argv) else ""
        cfg = mail_config()
        print("mail transport: %s%s" % (cfg["transport"] or "none", "" if cfg["configured"] else "  (NOT configured: %s)" % cfg["why"]))
        print("sending from: %s" % (cfg["from"] or "(unset)"))
        t0 = time.monotonic()
        ok, detail = send_email(to, "FinChaya test email", "If you can read this, FinChaya can email you.\n\n" + DISCLAIMER + "\n")
        print("%s: %s (%.1fs)" % ("SENT" if ok else "NOT SENT", detail, time.monotonic() - t0))
        sys.exit(0 if ok else 1)

    conn = _connect()

    def query(sql, params=None, one=False):
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            rows = cur.fetchall()
        conn.commit()
        return (rows[0] if rows else None) if one else rows

    def execute(sql, params=None, returning=False):
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            row = cur.fetchone() if returning else None
        conn.commit()
        return row

    if "--status" in argv:
        cfg = mail_config()
        print("mail: %s %s" % (cfg["transport"] or "none", "configured" if cfg["configured"] else "NOT configured (%s)" % cfg["why"]))
        st = checker_status(query)
        print("checker: %s" % ("never run" if st.get("never_run") else "last ran %s%s" % (st.get("last_run_at"), "  (TOO LONG AGO)" if st.get("stale") else "")))
        for r in query("SELECT status, email_status, COUNT(*) AS n FROM trade_alert GROUP BY 1, 2 ORDER BY 1, 2"):
            print("  alerts %-10s email %-8s %d" % (r["status"], r["email_status"], r["n"]))
        return

    dry = "--dry-run" in argv
    lock = conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (LOCK_KEY + (1 if dry else 0),)).fetchone()["got"]
    if not lock:
        print("another trade-alert checker is already running; nothing done")
        return
    out = run_once(query, execute, dry_run=dry)
    print("%schecked %d, unchanged %d, met %d, emailed %d, email failed %d, paused %d%s"
          % ("[dry run] " if dry else "", out["checked"], out["skipped"], out["fired"], out["emailed"], out["failed"], out["paused"],
             ("; ERRORS: " + " | ".join(out["errors"])) if out["errors"] else ""))
    sys.exit(1 if out["errors"] else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
