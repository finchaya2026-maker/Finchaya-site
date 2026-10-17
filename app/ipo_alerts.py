"""
ipo_alerts.py -- "email me when a NEW stock matches the IPO trend screen": the settings, the check, the email, and the command line.
---------------------------------------------------------------------------
    cd /opt/mfapi && venv/bin/python3 ipo_alerts.py              # one pass, quietly (this is what cron runs, every 30 minutes)
    venv/bin/python3 ipo_alerts.py --dry-run                     # what WOULD be emailed; nothing saved, nothing sent
    venv/bin/python3 ipo_alerts.py --status                      # is email set up, when did it last run, how many alerts
    venv/bin/python3 ipo_alerts.py --sample you@example.com      # ONE real email built the way an alert's is, from made-up figures: proves delivery

WHAT IT IS
    On the Screener page, "IPO trend after listing" has a box: "Email me when a new stock matches". It saves the screen's own settings (which scenario, which bars,
    line or candlesticks, how recently listed, how big the first move, the swing size). From then on, each time new prices arrive, this checker runs that screen,
    and the stocks that are on it now and were not before are emailed to the member, in ONE email per alert.

WHAT COUNTS AS "NEW"
    A stock is new when its signal (the bar that went through the level, for that scenario) is one this alert has not seen. When the alert is made, every stock
    already on the screen is marked as seen (the page says how many), so the first email is about something that happened AFTER the alert was set. The same stock
    can come back later with a different signal (a later higher low and a new high): that is a new match. The same signal is never emailed twice.
    A weekly or monthly bar that is still forming is not used: it can disappear before it closes, so the email comes once the bar has closed.

THE RULES THAT KEEP THIS HONEST (the ones the stock alerts follow)
    * A match is SAVED BEFORE the email is tried. If the email fails the match stays saved as "not emailed" and later runs retry (MAX_EMAIL_ATTEMPTS), so a mail
      outage loses nothing.
    * It says what is set up: every run leaves a row in ipo_trend_alert_run, and the page says "last checked ..." or "has not run for N hours".
    * LIMITS: MAX_ALERTS per person, MAX_LINES stocks named in one email (the rest are counted), MAX_EMAILS_PER_DAY per person.
    * Two checkers at once would send the same email twice, so a database advisory lock keeps it to one.
    * Nothing here says buy or sell. It reports that a pattern has happened.
"""

import html as _html
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

MAX_ALERTS = 5                  # per person
MAX_LINES = 25                  # stocks named in one email
MAX_EMAIL_ATTEMPTS = 6          # a failing send is retried on later runs, this many times in all
MAX_EMAILS_PER_DAY = 10         # per person; the rest wait for the next day
STALE_CHECK_HOURS = 3           # the checker is "not running" if its last run is older than this (cron is every 30 minutes)
LOCK_KEY = 727421               # advisory lock
SETTING_KEYS = ("timeframe", "basis", "scenario", "listed_within", "move", "swing")
# A signal made within this many bars of the latest one is looked at (a missed day is caught up); anything older is history, not news.
LOOKBACK = {"DAILY": 5, "WEEKLY": 2, "MONTHLY": 2}
SCENARIO_WORDS = {"EITHER": "either scenario (a higher low then a new high, or a downtrend broken after a higher low)",
                  "HOLD": "scenario 1 (the price rose after listing, pulled back to a higher low, then went above the high before the pullback)",
                  "DOWN": "scenario 2 (the price kept falling after listing, formed a higher low, then went above the last lower high)"}
TF_WORDS = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly"}
DISCLAIMER = ("This reports that a chart pattern has already happened to a recently listed stock, on the latest end-of-day prices we hold. It is not a recommendation "
              "to buy, sell or hold anything, and a pattern can fail. Educational content only, not investment advice. FinChaya is not currently registered with SEBI "
              "as an Investment Adviser or Research Analyst.")


class AlertError(ValueError):
    """An alert that cannot be made, in words the person can act on."""


# =====================================================================
# THE SETTINGS
# =====================================================================
def _controls():
    import ipo_trend
    return {c["param"]: c for c in ipo_trend.SCREEN["controls"] if c["param"] in SETTING_KEYS}


def clean_settings(raw):
    """The settings of an alert, checked: every value must be one the screen offers (an alert must never quietly watch something other than what was asked). Missing
    settings take the screen's own defaults. Returns a dict with exactly SETTING_KEYS."""
    if not isinstance(raw, dict):
        raise AlertError("The alert is not in a form I can read.")
    out = {}
    for key, c in _controls().items():
        v = raw.get(key)
        if v is None or v == "":
            out[key] = c["default"]
            continue
        if isinstance(v, bool):
            raise AlertError("Choose one of the offered values for '%s'." % c["label"])
        try:
            v = type(c["default"])(v)
        except (TypeError, ValueError):
            raise AlertError("Choose one of the offered values for '%s'." % c["label"])
        if v not in [o[0] for o in c["options"]]:
            raise AlertError("Choose one of the offered values for '%s'." % c["label"])
        out[key] = v
    return out


def settings_key(settings):
    return json.dumps({k: settings[k] for k in SETTING_KEYS}, sort_keys=True)


def sentence(settings):
    """What the alert watches for, in one plain sentence."""
    s = settings
    move = float(s["move"])
    swing = int(s["swing"])
    return ("A new IPO stock on the \"IPO trend after listing\" screen: %s; %s bars; %s; listed in the last %s days%s; %s."
            % (SCENARIO_WORDS[s["scenario"]], TF_WORDS[s["timeframe"]], "candlesticks (highs and lows)" if s["basis"] == "CANDLE" else "line chart (closing prices)",
               int(s["listed_within"]), ", first move at least %g%%" % move if move else "",
               "every swing counts" if swing == 1 else "a swing is a bar higher or lower than %d bars each side" % swing))


def event_key(row):
    """The identity of one match: the stock, the scenario and the bar that went through the level."""
    return (row["isin"], row["scenario"], str(row["alert_date"]))


def snapshot(row):
    """What an email needs about a match, kept with it (so a retried email says what was true at the time)."""
    keep = ("isin", "symbol", "name", "scenario", "scenario_label", "timeframe", "alert_date", "trigger", "level", "level_date", "higher_low", "higher_low_date",
            "base", "base_date", "top", "top_date", "move_pct", "listing_date", "issue_price", "close", "slug")
    return {k: row.get(k) for k in keep}


# =====================================================================
# THE EMAIL
# =====================================================================
def _dmy(iso):
    try:
        d = date.fromisoformat(str(iso)[:10])
        return "%d %s %d" % (d.day, d.strftime("%b"), d.year)
    except (TypeError, ValueError):
        return ""


def _rs(v):
    return "" if v is None else "Rs %s" % "{:,.2f}".format(float(v))


def _line(r):
    """One match in words."""
    listed = "Listed %s%s." % (_dmy(r.get("listing_date")), " at an issue price of %s" % _rs(r["issue_price"]) if r.get("issue_price") else "")
    if r.get("scenario") == "HOLD":
        return ("Went above the earlier high of %s (made %s) on %s, reaching %s, after a pullback to a higher low of %s on %s. The rise began from a low of %s on %s. %s"
                % (_rs(r.get("level")), _dmy(r.get("level_date")), _dmy(r.get("alert_date")), _rs(r.get("trigger")), _rs(r.get("higher_low")), _dmy(r.get("higher_low_date")),
                   _rs(r.get("base")), _dmy(r.get("base_date")), listed))
    return ("Went above the last lower high of %s (made %s) on %s, reaching %s, after a higher low of %s on %s. The fall began at %s on %s and bottomed at %s on %s. %s"
            % (_rs(r.get("level")), _dmy(r.get("level_date")), _dmy(r.get("alert_date")), _rs(r.get("trigger")), _rs(r.get("higher_low")), _dmy(r.get("higher_low_date")),
               _rs(r.get("top")), _dmy(r.get("top_date")), _rs(r.get("base")), _dmy(r.get("base_date")), listed))


def build_email(alert, rows, site):
    """(subject, text, html) for the new matches of one alert. Everything that came from outside is escaped in the html and kept on one line in the subject."""
    n = len(rows)
    subject = "FinChaya: %d new IPO stock%s on your IPO trend screen" % (n, "" if n == 1 else "s")
    shown, more = rows[:MAX_LINES], max(0, n - MAX_LINES)
    screen = "%s/#/screener/ipo_trend" % site.rstrip("/")
    lines = ["Hello,", "", "%d new stock%s matched the alert you set on the IPO trend screen:" % (n, "" if n == 1 else "s"), "", "  " + alert["sentence"], ""]
    h = ["<html><body style=\"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#1b2b24;font-size:15px;line-height:1.5\">",
         "<p>Hello,</p><p><b>%d new stock%s</b> matched the alert you set on the IPO trend screen:</p>" % (n, "" if n == 1 else "s"),
         "<p style='color:#51615a'>%s</p>" % _html.escape(alert["sentence"]), "<ol style='padding-left:20px'>"]
    for i, r in enumerate(shown, 1):
        name = "%s (%s)" % (r.get("name") or r.get("symbol") or r["isin"], r.get("symbol") or r["isin"])
        link = "%s/#/stock/%s" % (site.rstrip("/"), r["isin"])
        lines += ["%d. %s - %s" % (i, name, r.get("scenario_label") or ""), "   " + _line(r), "   " + link, ""]
        h.append("<li style='margin-bottom:10px'><b>%s</b> &ndash; %s<br><span style='color:#51615a'>%s</span><br><a href=\"%s\">Open the stock page</a></li>"
                 % (_html.escape(name), _html.escape(r.get("scenario_label") or ""), _html.escape(_line(r)), _html.escape(link)))
    h.append("</ol>")
    if more:
        lines += ["...and %d more. See them all on the screen." % more, ""]
        h.append("<p>...and %d more. See them all on the screen.</p>" % more)
    lines += ["See the whole screen: " + screen, "To stop these emails, open the screen and press Stop beside this alert.", "", DISCLAIMER, ""]
    h.append("<p><a href=\"%s\">See the whole screen</a>. To stop these emails, open the screen and press Stop beside this alert.</p>"
             "<p style='color:#6b7c76;font-size:12.5px'>%s</p></body></html>" % (_html.escape(screen), _html.escape(DISCLAIMER)))
    return re.sub(r"[\r\n\x00]+", " ", subject).strip(), "\n".join(lines), "".join(h)


# =====================================================================
# THE CHECKER
# =====================================================================
def _j(v):
    return json.loads(v) if isinstance(v, (str, bytes)) else v


def _utc(now):
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


def usable(row):
    """A match worth telling about: not on a weekly or monthly bar that is still forming (it can vanish before it closes)."""
    return not row.get("forming")


def baseline(execute, alert_id, rows):
    """Mark the stocks that match NOW as seen without emailing them (called when the alert is made). Returns how many."""
    n = 0
    for r in rows:
        if usable(r):
            execute("INSERT INTO ipo_trend_alert_seen (alert_id, isin, scenario, signal_date, symbol, company_name, reading, baseline, emailed) "
                    "VALUES (%(a)s, %(i)s, %(s)s, %(d)s, %(sym)s, %(n)s, %(r)s::jsonb, true, true) ON CONFLICT DO NOTHING",
                    {"a": alert_id, "i": r["isin"], "s": r["scenario"], "d": r["alert_date"], "sym": r.get("symbol"), "n": r.get("name"), "r": json.dumps(snapshot(r), default=str)})
            n += 1
    return n


def run_once(query, execute, scan, market_date=None, send=None, today=None, now=None, dry_run=False, log=print, env=None):
    """One pass over every active alert. Everything it needs from outside is passed in, so it runs the same against the real database, the test database and a fake
    mail server.

      query(sql, params=None, one=False) / execute(sql, params=None, returning=False)   the database
      scan(settings) -> list of match rows (ipo_trend.run_screen's rows, with `forming`)  the screen, run with these settings
      market_date                                                                          the newest trading day with prices (None: no skipping)
      send(to, subject, text, html) -> (ok, detail)                                        mail

    Returns the counts. Never raises for one bad alert: it is logged and the rest are still looked at."""
    if send is None:
        import mailer
        send = lambda to, subject, text, html: mailer.send_email(to, subject, text, html=html, env=env)    # noqa: E731
        site = mailer.site_url(env)
    else:
        site = (env or os.environ).get("FINCHAYA_SITE_URL", "https://finchaya.com").rstrip("/")
    now = now or datetime.now(timezone.utc)
    today = today or date.today()
    out = {"checked": 0, "skipped": 0, "new": 0, "emailed": 0, "failed": 0, "errors": []}
    alerts = query("SELECT alert_id, user_id, settings, settings_key, sentence, last_stamp FROM ipo_trend_alert WHERE active ORDER BY alert_id")
    if not alerts:
        if not dry_run:
            _heartbeat(execute, out)
        return out
    users = {r["user_id"]: r for r in query("SELECT user_id, email, is_active FROM mf_user WHERE user_id = ANY(%(u)s)", {"u": sorted({a["user_id"] for a in alerts})})}
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    sent_today = {r["user_id"]: r["n"] for r in query(
        "SELECT a.user_id, COUNT(DISTINCT (s.alert_id, s.emailed_at)) AS n FROM ipo_trend_alert_seen s JOIN ipo_trend_alert a USING (alert_id) "
        "WHERE s.emailed AND s.emailed_at >= %(d)s GROUP BY a.user_id", {"d": day_start})}
    stamp = ("%s|%s" % (market_date, today)) if market_date else None
    pending_ids = {r["alert_id"] for r in query("SELECT DISTINCT alert_id FROM ipo_trend_alert_seen WHERE NOT emailed AND email_attempts < %(m)s", {"m": MAX_EMAIL_ATTEMPTS})}
    scans = {}                                                  # settings_key -> rows (or None when the screen could not be run)

    for a in alerts:
        try:
            settings = _j(a["settings"])
            if stamp is not None and a["last_stamp"] == stamp and a["alert_id"] not in pending_ids:
                out["skipped"] += 1
                continue
            new_rows = []
            if stamp is None or a["last_stamp"] != stamp:
                if a["settings_key"] not in scans:
                    try:
                        scans[a["settings_key"]] = scan(settings)
                    except Exception as e:                       # noqa: BLE001  one screen failing must not stop the others
                        scans[a["settings_key"]] = None
                        out["errors"].append("scan %s: %s: %s" % (a["settings_key"][:60], type(e).__name__, e))
                        log(out["errors"][-1])
                rows = scans[a["settings_key"]]
                if rows is None:
                    continue
                out["checked"] += 1
                seen = {(r["isin"], r["scenario"], str(r["signal_date"])) for r in query(
                    "SELECT isin, scenario, signal_date FROM ipo_trend_alert_seen WHERE alert_id = %(a)s", {"a": a["alert_id"]})}
                new_rows = [r for r in rows if usable(r) and event_key(r) not in seen]
                out["new"] += len(new_rows)
                for r in new_rows:
                    log("alert %s: new match %s %s (%s)" % (a["alert_id"], r.get("symbol"), r["scenario"], r["alert_date"]))
                    if not dry_run:
                        # saved BEFORE the email is tried, so a mail failure loses nothing
                        execute("INSERT INTO ipo_trend_alert_seen (alert_id, isin, scenario, signal_date, symbol, company_name, reading) "
                                "VALUES (%(a)s, %(i)s, %(s)s, %(d)s, %(sym)s, %(n)s, %(r)s::jsonb) ON CONFLICT DO NOTHING",
                                {"a": a["alert_id"], "i": r["isin"], "s": r["scenario"], "d": r["alert_date"], "sym": r.get("symbol"), "n": r.get("name"),
                                 "r": json.dumps(snapshot(r), default=str)})
            # everything saved and not yet emailed (this run's new matches, and earlier ones whose email failed)
            if dry_run:
                waiting = [dict(r, _id=None) for r in new_rows]
            else:
                waiting = [dict(_j(r["reading"]), _id=r["seen_id"], _attempts=r["email_attempts"]) for r in query(
                    "SELECT seen_id, reading, email_attempts FROM ipo_trend_alert_seen WHERE alert_id = %(a)s AND NOT emailed AND email_attempts < %(m)s "
                    "ORDER BY signal_date DESC, symbol", {"a": a["alert_id"], "m": MAX_EMAIL_ATTEMPTS})]
            if waiting:
                _email(a, waiting, users, sent_today, send, site, execute, now, dry_run, log, out)
            if not dry_run and (stamp is None or a["last_stamp"] != stamp):
                execute("UPDATE ipo_trend_alert SET last_checked_at = %(at)s, last_stamp = %(s)s WHERE alert_id = %(id)s", {"at": _utc(now), "s": stamp, "id": a["alert_id"]})
        except Exception as e:                                   # noqa: BLE001
            out["errors"].append("alert %s: %s: %s" % (a["alert_id"], type(e).__name__, e))
            log(out["errors"][-1])
    if not dry_run:
        _heartbeat(execute, out)
    return out


def _email(alert, waiting, users, sent_today, send, site, execute, now, dry_run, log, out):
    u = users.get(alert["user_id"])
    ids = [w["_id"] for w in waiting if w.get("_id") is not None]
    if not u or not u.get("is_active", True) or not u.get("email"):
        if ids and not dry_run:                                  # nobody to tell: do not retry for ever
            execute("UPDATE ipo_trend_alert_seen SET email_attempts = %(m)s, email_error = 'no active account with an email address' WHERE seen_id = ANY(%(ids)s)",
                    {"m": MAX_EMAIL_ATTEMPTS, "ids": ids})
        log("alert %s: nobody to email" % alert["alert_id"])
        return
    if sent_today.get(alert["user_id"], 0) >= MAX_EMAILS_PER_DAY:
        return                                                   # over today's share: it waits, untouched, for tomorrow
    subject, text, html = build_email(alert, waiting, site)
    if dry_run:
        log("  [dry run] would email %s: %s" % (u["email"], subject))
        return
    ok, detail = send(u["email"], subject, text, html)
    if ok:
        execute("UPDATE ipo_trend_alert_seen SET emailed = true, email_attempts = email_attempts + 1, email_error = NULL, emailed_at = %(at)s WHERE seen_id = ANY(%(ids)s)",
                {"at": _utc(now), "ids": ids})
        sent_today[alert["user_id"]] = sent_today.get(alert["user_id"], 0) + 1
        out["emailed"] += 1
        log("alert %s: emailed %s (%d stock%s)" % (alert["alert_id"], u["email"], len(waiting), "" if len(waiting) == 1 else "s"))
    else:
        execute("UPDATE ipo_trend_alert_seen SET email_attempts = email_attempts + 1, email_error = %(e)s WHERE seen_id = ANY(%(ids)s)", {"e": str(detail)[:300], "ids": ids})
        out["failed"] += 1
        log("alert %s: email FAILED (%s)" % (alert["alert_id"], detail))


def _heartbeat(execute, out):
    """The row the page reads to say alerts are really being looked at."""
    execute("INSERT INTO ipo_trend_alert_run (checked, skipped, new_matches, emailed, failed, error) VALUES (%(c)s, %(s)s, %(n)s, %(e)s, %(f)s, %(err)s) RETURNING run_id",
            {"c": out["checked"], "s": out["skipped"], "n": out["new"], "e": out["emailed"], "f": out["failed"], "err": ("; ".join(out["errors"])[:500] or None)}, returning=True)


def checker_status(query, now=None):
    """Whether alerts are being checked: when the checker last ran, and whether that is too long ago. A failed read is 'unknown', never 'healthy'."""
    now = now or datetime.now(timezone.utc)
    try:
        row = query("SELECT started_at, checked, new_matches, emailed, failed, error FROM ipo_trend_alert_run ORDER BY run_id DESC LIMIT 1", None, one=True)
    except Exception:                                            # noqa: BLE001
        return {"known": False, "never_run": None, "last_run_at": None, "stale": None}
    if not row:
        return {"known": True, "never_run": True, "last_run_at": None, "stale": True}
    at = row["started_at"]
    if isinstance(at, str):
        at = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return {"known": True, "never_run": False, "last_run_at": at, "stale": (_utc(now) - _utc(at)) > timedelta(hours=STALE_CHECK_HOURS), "emailed": row["emailed"], "failed": row["failed"],
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


def make_scan(conn):
    """scan(settings) -> the IPO trend screen's rows for these settings, run on this connection (the site's own screen, with the same rules)."""
    import ipo_trend
    import screener_api as S

    def scan(settings):
        raw = dict(settings, recent=LOOKBACK[settings["timeframe"]])
        with conn.cursor() as cur:
            res = ipo_trend.run_screen(cur, "ipo_trend", raw, None, ipo_trend.ALL_ROWS, S._meta, lambda c, rows: rows)
        conn.rollback()
        if res.get("unavailable"):
            raise RuntimeError(res["unavailable"])
        return res["rows"]
    return scan


def main(argv):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    load_dotenv("/opt/mfapi/.env")

    if "--sample" in argv:                                    # one real email, built the way an alert's is, from made-up figures: proves delivery without waiting for a match
        i = argv.index("--sample")
        to = argv[i + 1] if i + 1 < len(argv) else ""
        import mailer
        if not mailer.valid_address(to):
            sys.exit("Usage: ipo_alerts.py --sample you@example.com")
        row = {"isin": "INE000A01001", "symbol": "SAMPLE", "name": "Sample Company Limited", "scenario": "HOLD", "scenario_label": "Pullback held, new high", "timeframe": "DAILY",
               "alert_date": date.today().isoformat(), "trigger": 113.0, "level": 112.0, "level_date": date.today().isoformat(), "higher_low": 105.0,
               "higher_low_date": date.today().isoformat(), "base": 100.0, "base_date": date.today().isoformat(), "listing_date": date.today().isoformat(), "issue_price": 100.0}
        subject, text, html = build_email({"alert_id": 0, "sentence": sentence(clean_settings({}))}, [row], mailer.site_url())
        ok, detail = mailer.send_email(to, "[SAMPLE, made-up figures] " + subject, "THIS IS A SAMPLE. The stock and the figures are made up.\n\n" + text, html=html)
        print("sample email to %s: %s (%s)" % (to, "SENT" if ok else "FAILED", detail))
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
        import mailer
        print("\n".join(mailer.describe()))
        st = checker_status(query)
        print("checker: %s" % ("never run" if st.get("never_run") else "last ran %s%s" % (st.get("last_run_at"), "  (TOO LONG AGO)" if st.get("stale") else "")))
        for r in query("SELECT active, COUNT(*) AS n FROM ipo_trend_alert GROUP BY 1 ORDER BY 1"):
            print("  alerts %-8s %d" % ("active" if r["active"] else "stopped", r["n"]))
        for r in query("SELECT emailed, baseline, COUNT(*) AS n FROM ipo_trend_alert_seen GROUP BY 1, 2 ORDER BY 1, 2"):
            print("  matches %-10s %-9s %d" % ("emailed" if r["emailed"] else "waiting", "(baseline)" if r["baseline"] else "", r["n"]))
        return

    dry = "--dry-run" in argv
    got = conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (LOCK_KEY + (1 if dry else 0),)).fetchone()["got"]
    if not got:
        print("another IPO-alert checker is already running; nothing done")
        return
    import price_bars
    with conn.cursor() as cur:
        market = price_bars.latest_market_date(cur)
    conn.rollback()
    out = run_once(query, execute, make_scan(conn), market_date=market, dry_run=dry)
    print("%schecked %d, unchanged %d, new matches %d, emailed %d, email failed %d%s"
          % ("[dry run] " if dry else "", out["checked"], out["skipped"], out["new"], out["emailed"], out["failed"], ("; ERRORS: " + " | ".join(out["errors"])) if out["errors"] else ""))
    sys.exit(1 if out["errors"] else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
