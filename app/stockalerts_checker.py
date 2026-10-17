"""
stockalerts_checker.py -- "email me when this happens to this stock": the scheduled check, the email, and the CLI.
---------------------------------------------------------------------------
    cd /opt/mfapi && venv/bin/python3 stockalerts_checker.py              # one pass, quietly (this is what cron runs)
    venv/bin/python3 stockalerts_checker.py --dry-run                     # what WOULD fire; nothing changed, nothing sent
    venv/bin/python3 stockalerts_checker.py --status                      # is email set up, when did it last run, how many alerts
    venv/bin/python3 stockalerts_checker.py --test-email you@example.com  # send ONE real email and say what happened

WHAT RUNS WHEN
    Prices arrive once a day (the nightly price job), results arrive through the day (result_watcher.py, every 30 minutes
    in the season). So the checker runs every 30 minutes and does very little most of the time: for each stock it
    remembers the newest candle it looked at ("stamp"), and a stock with nothing new is skipped. Alerts that watch a
    company's FILINGS (results) are looked at every run because a filing can land at any minute.

THE RULES THAT KEEP THIS HONEST (the same ones the trade-plan alerts follow; the fund alerts were once switches that did nothing)
    * IT SAYS WHAT IS ACTUALLY SET UP. Every run leaves a row in stock_alert_run and the alerts page reads it: "last checked
      ...", "has never run", "has not run for N hours". Email settings are read the same way. If either is missing the page
      says so in full weight, and still saves the alert -- but never lets anybody think they are covered.
    * A TRIGGER IS RECORDED BEFORE THE EMAIL IS TRIED. A met alert is saved as 'triggered' with what it read, then the email
      is sent. If the email fails the alert still shows as triggered with "email not sent", and later runs retry up to
      MAX_EMAIL_ATTEMPTS times, so a mail outage loses nothing.
    * NOTHING FIRES ON GUESSWORK. No data is "no data", not "met". A price alert is PAUSED, with a note, if the company
      split or issued bonus shares after it was set: the rupee level no longer means what it did.
    * LIMITS. MAX_ALERTS per person, MAX_PER_STOCK per stock, MAX_EMAILS_PER_DAY per person (the rest wait for tomorrow),
      one email per alert.
    * Two checkers at once would send the same email twice, so a database advisory lock keeps it to one.
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

MAX_ALERTS = 50                 # active or paused, per person
MAX_PER_STOCK = 10              # per person per stock
MAX_EMAIL_ATTEMPTS = 6          # a failing send is retried on later runs, this many times in all
MAX_EMAILS_PER_DAY = 20         # per person; the rest wait for the next day
STALE_CHECK_HOURS = 3           # the checker is "not running" if its last run is older than this (cron is every 30 minutes)
LOCK_KEY = 727420               # advisory lock: two checkers at once would send the same email twice

_SELECT = ("SELECT alert_id, user_id, isin, symbol, company_name, kind, spec, sentence, status, note, last_stamp, last_state, "
           "triggered_at, trigger_reading, trigger_as_of, email_status, email_attempts, created_at FROM stock_alert ")


def _j(v):
    """A json column as Python: psycopg gives a list or dict, other drivers give text."""
    if isinstance(v, (str, bytes)):
        return json.loads(v)
    return v


def _utc(now):
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


def site_url(env=None):
    return (env or os.environ).get("FINCHAYA_SITE_URL", "https://mf.finchaya.com").rstrip("/")


# =====================================================================
# WHAT THE CHECKER READS FOR ONE STOCK
# =====================================================================
def tfs_of(spec):
    """The chart time frames an alert needs (the daily one always: it carries the baseline)."""
    out = {"D"}
    if spec["kind"] in ("ema_support", "trend_change"):
        out.add(spec["tf"])
    elif spec["kind"] == "indicator":
        out.add(spec["condition"]["tf"])
    return out


def load_stock(query, isin, tfs, with_results):
    """(frames, ctx) for one stock: candles by time frame, and the rows the results alerts and the split guard need.
    Candles are read exactly as the back-tester and the trade plan read them (same split/bonus adjustment)."""
    import tradeplan_api
    frames, _notes = tradeplan_api.load_frames(query, "stock", isin, tfs)
    ctx = {"actions": query("SELECT event_date FROM stock_event WHERE isin = %(i)s AND kind IN ('split', 'bonus') "
                            "ORDER BY event_date", {"i": isin})}
    if with_results:
        ctx["fin"] = query("SELECT isin, period_type, period_end, period_label, revenue, profit, eps, consolidated, filed_on "
                           "FROM stock_financials WHERE isin = %(i)s AND period_type = 'quarterly' ORDER BY period_end DESC "
                           "LIMIT 40", {"i": isin})
        ctx["events"] = query("SELECT event_date, headline, detail FROM stock_event WHERE isin = %(i)s AND kind = 'result' "
                              "AND event_date >= %(d)s ORDER BY event_date", {"i": isin, "d": date.today() - timedelta(days=1)})
    return frames, ctx


# =====================================================================
# THE CHECKER
# =====================================================================
def run_once(query, execute, send=None, load=None, today=None, now=None, dry_run=False, log=print, env=None):
    """One pass over every alert that needs a look. Everything it needs from outside is passed in, so it runs the same
    against the real database, the test database and a fake mail server.

      query(sql, params=None, one=False) / execute(sql, params=None, returning=False)   the database
      send(to, subject, text, html) -> (ok, detail)                                      mail
      load(query, isin, tfs, with_results) -> (frames, ctx)                              candles and rows

    Returns the counts. Never raises for one bad alert: it is logged and the rest are still looked at."""
    import stockalerts_engine as se
    if send is None:
        import mailer
        send = lambda to, subject, text, html: mailer.send_email(to, subject, text, html=html, env=env)    # noqa: E731
    load = load or load_stock
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    site = site_url(env)
    out = {"checked": 0, "skipped": 0, "fired": 0, "emailed": 0, "failed": 0, "paused": 0, "errors": []}

    rows = query(_SELECT + "WHERE status = 'active' OR (status = 'triggered' AND email_status <> 'sent' "
                           "AND email_attempts < %(max)s) ORDER BY alert_id", {"max": MAX_EMAIL_ATTEMPTS})
    if not rows:
        if not dry_run:
            _heartbeat(execute, out)
        return out

    uids = sorted({r["user_id"] for r in rows})
    users = {r["user_id"]: r for r in query(
        "SELECT user_id, email, is_active FROM mf_user WHERE user_id = ANY(%(u)s)", {"u": uids})}
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    sent_today = {r["user_id"]: r["n"] for r in query(
        "SELECT user_id, COUNT(*) AS n FROM stock_alert WHERE email_status = 'sent' AND email_sent_at >= %(since)s "
        "GROUP BY user_id", {"since": day_start})}

    def email_for(alert, reading):
        """Try the email for a triggered alert and record what happened. Returns True if it went."""
        u = users.get(alert["user_id"])
        if not u or not u.get("is_active", True) or not u.get("email"):
            ok, detail, final = False, "no active account with an email address", True
        elif sent_today.get(alert["user_id"], 0) >= MAX_EMAILS_PER_DAY:
            return False                                     # over today's share: it waits, untouched, for tomorrow
        else:
            subject, text, html = se.build_email(alert, reading, site)
            if dry_run:
                log("  [dry run] would email %s: %s" % (u["email"], subject))
                return False
            ok, detail = send(u["email"], subject, text, html)
            final = False
        if dry_run:
            return False
        if ok:
            execute("UPDATE stock_alert SET email_status = 'sent', email_attempts = email_attempts + 1, email_error = NULL, "
                    "email_sent_at = %(at)s WHERE alert_id = %(id)s", {"at": _utc(now), "id": alert["alert_id"]})
            sent_today[alert["user_id"]] = sent_today.get(alert["user_id"], 0) + 1
            out["emailed"] += 1
            log("alert %s: emailed %s" % (alert["alert_id"], u["email"]))
            return True
        execute("UPDATE stock_alert SET email_status = 'failed', email_attempts = %(a)s, email_error = %(e)s "
                "WHERE alert_id = %(id)s",
                {"a": MAX_EMAIL_ATTEMPTS if final else alert["email_attempts"] + 1, "e": detail[:300], "id": alert["alert_id"]})
        out["failed"] += 1
        log("alert %s: email FAILED (%s)" % (alert["alert_id"], detail))
        return False

    # 1. triggered alerts whose email did not go: try again, with what they read at the time
    for a in [r for r in rows if r["status"] == "triggered"]:
        try:
            email_for(a, a["trigger_reading"])
        except Exception as e:                               # noqa: BLE001
            out["errors"].append("alert %s retry: %s: %s" % (a["alert_id"], type(e).__name__, e))
            log(out["errors"][-1])

    # 2. alerts still waiting: look at each stock once
    by_stock = {}
    for a in (r for r in rows if r["status"] == "active"):
        by_stock.setdefault(a["isin"], []).append(a)
    isins = sorted(by_stock)
    newest = {r["isin"]: r["d"] for r in query(
        "SELECT isin, max(as_of_date) AS d FROM stock_ohlc_daily WHERE isin = ANY(%(i)s) GROUP BY isin", {"i": isins})} if isins else {}

    for isin, alerts in by_stock.items():
        stamp = "%s|%s" % (newest.get(isin), today)            # a new candle, or a new day (a week can finish without one)
        todo = [a for a in alerts if a["kind"] == "results" or a["last_stamp"] != stamp]
        out["skipped"] += len(alerts) - len(todo)
        if not todo:
            continue
        valid = []
        for a in todo:                                       # one at a time: a bad alert must only ever pause ITSELF
            try:
                spec = _j(a["spec"])
                se.describe(spec)
                a["_spec"] = spec
                valid.append(a)
            except Exception:                                # noqa: BLE001
                _pause(execute, a, "Paused: this alert uses something that is no longer available.", out, dry_run)
        if not valid:
            continue
        tfs = set()
        for a in valid:
            tfs |= tfs_of(a["_spec"])
        try:
            frames, ctx = load(query, isin, tfs, any(a["kind"] == "results" for a in valid))
        except Exception as e:                               # noqa: BLE001
            out["errors"].append("%s: could not load candles: %s: %s" % (isin, type(e).__name__, e))
            log(out["errors"][-1])
            continue
        for a in valid:
            try:
                spec = a["_spec"]
                if spec["kind"] == "price":
                    since = a["created_at"].date() if hasattr(a["created_at"], "date") else a["created_at"]
                    acted = [x for x in ctx.get("actions", []) if since < x["event_date"] <= today]
                    if acted:
                        _pause(execute, a, "Paused: the company issued a split or bonus on %s, after you set this alert, so "
                                           "the price level no longer means what it did. Set it again at the new price."
                               % se.dmy(acted[0]["event_date"]), out, dry_run)
                        continue
                res = se.check(spec, frames, today, ctx)
                out["checked"] += 1
                last = {"met": "met", "not_met": "not_met", "no_data": "no_data"}[res["state"]]
                if res["state"] != "met":
                    if not dry_run:
                        params = {"at": _utc(now), "s": stamp, "st": last, "id": a["alert_id"]}
                        if "new_baseline_period" in res:
                            # a results alert with a condition that this filing did not meet: wait for the NEXT quarter
                            params["spec"] = json.dumps(dict(spec, baseline_period=res["new_baseline_period"]))
                            execute("UPDATE stock_alert SET last_checked_at = %(at)s, last_stamp = %(s)s, last_state = %(st)s, "
                                    "spec = %(spec)s::jsonb, note = %(n)s WHERE alert_id = %(id)s",
                                    dict(params, n=("Latest results did not match: %s. Waiting for the next quarter."
                                                    % (res.get("reading") or "")).strip()[:300]))
                        else:
                            execute("UPDATE stock_alert SET last_checked_at = %(at)s, last_stamp = %(s)s, last_state = %(st)s "
                                    "WHERE alert_id = %(id)s", params)
                    continue
                out["fired"] += 1
                log("alert %s: MET  %s %s  (%s)" % (a["alert_id"], a.get("symbol") or isin, a["sentence"], res.get("reading")))
                if dry_run:
                    email_for(dict(a, email_attempts=0), res["reading"])
                    continue
                # recorded as triggered BEFORE the email is tried, so a mail failure loses nothing
                execute("UPDATE stock_alert SET status = 'triggered', triggered_at = %(at)s, last_checked_at = %(at)s, "
                        "last_stamp = %(s)s, last_state = 'met', trigger_reading = %(r)s, trigger_as_of = %(d)s "
                        "WHERE alert_id = %(id)s", {"at": _utc(now), "s": stamp, "r": res["reading"],
                                                    "d": res["as_of"], "id": a["alert_id"]})
                email_for(a, res["reading"])
            except Exception as e:                           # noqa: BLE001
                out["errors"].append("alert %s: %s: %s" % (a["alert_id"], type(e).__name__, e))
                log(out["errors"][-1])

    if not dry_run:
        _heartbeat(execute, out)
    return out


def _pause(execute, alert, note, out, dry_run):
    out["paused"] += 1
    if not dry_run:
        execute("UPDATE stock_alert SET status = 'paused', note = %(n)s WHERE alert_id = %(id)s",
                {"n": note, "id": alert["alert_id"]})


def _heartbeat(execute, out):
    """The row the page reads to say alerts are really being looked at."""
    execute("INSERT INTO stock_alert_run (checked, skipped, fired, emailed, failed, error) "
            "VALUES (%(c)s, %(s)s, %(f)s, %(e)s, %(x)s, %(err)s) RETURNING run_id",
            {"c": out["checked"], "s": out["skipped"], "f": out["fired"], "e": out["emailed"], "x": out["failed"],
             "err": ("; ".join(out["errors"])[:500] or None)}, returning=True)


def checker_status(query, now=None):
    """What the page needs to say whether alerts are being checked: when the checker last ran, and whether that is too
    long ago. A failed read is reported as 'unknown', never as healthy."""
    now = now or datetime.now(timezone.utc)
    try:
        row = query("SELECT started_at, checked, fired, emailed, failed, error FROM stock_alert_run "
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
        import mailer
        sys.exit(mailer.main(["--test", argv[i + 1] if i + 1 < len(argv) else ""]))

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
        import mailer
        print("\n".join(mailer.describe()))
        st = checker_status(query)
        print("checker: %s" % ("never run" if st.get("never_run") else "last ran %s%s" % (
            st.get("last_run_at"), "  (TOO LONG AGO)" if st.get("stale") else "")))
        for r in query("SELECT status, email_status, COUNT(*) AS n FROM stock_alert GROUP BY 1, 2 ORDER BY 1, 2"):
            print("  alerts %-10s email %-8s %d" % (r["status"], r["email_status"], r["n"]))
        return

    dry = "--dry-run" in argv
    lock = conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (LOCK_KEY + (1 if dry else 0),)).fetchone()["got"]
    if not lock:
        print("another stock-alert checker is already running; nothing done")
        return
    out = run_once(query, execute, dry_run=dry)
    print("%schecked %d, unchanged %d, met %d, emailed %d, email failed %d, paused %d%s"
          % ("[dry run] " if dry else "", out["checked"], out["skipped"], out["fired"], out["emailed"], out["failed"], out["paused"],
             ("; ERRORS: " + " | ".join(out["errors"])) if out["errors"] else ""))
    sys.exit(1 if out["errors"] else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
