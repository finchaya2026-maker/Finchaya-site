"""
request_notice.py -- tells the OWNER, by email, when a member asks for something ("add this indicator", "add this data").
---------------------------------------------------------------------------
    cd /opt/mfapi && venv/bin/python3 request_notice.py              # one pass, quietly (this is what cron runs)
    venv/bin/python3 request_notice.py --dry-run                     # print the email that WOULD go; send and remember nothing
    venv/bin/python3 request_notice.py --status                      # is mail set up, who it goes to, what is waiting
    venv/bin/python3 request_notice.py --sample                      # send ONE clearly labelled sample email to the owner, to see it arrive

WHERE REQUESTS COME FROM
    The back-test page, the trade plan and the screen builder each have an "ask us to add ..." box. All three write one row
    per person per thing into the table indicator_request, and nothing more. This job reads that table, so it does not matter
    which of those pages is live or which version of them is: whatever lands in the table is emailed.

HOW IT KEEPS FROM SENDING TWICE, OR NOT AT ALL
    * It remembers the newest request it has told you about in a small file next to this script (request_notice.state), and
      writes that file ONLY AFTER the email was accepted. If the email fails, nothing is remembered and the next run (10
      minutes later) tries again, so an outage loses nothing.
    * One email per run, however many requests arrived (the free email plan has a daily limit that alerts to members share).
      More than MAX_PER_EMAIL at once: the first MAX_PER_EMAIL go now and the rest in the next run.
    * A request is picked up once it is a minute old, so one that is still being saved is never skipped.
    * The first run (no file yet) tells you about the last 7 days, not about older ones.
    * An unreadable state file stops it with a message (it would otherwise re-send the last 7 days).
    * Two copies at once cannot both send: a database lock keeps it to one.

WHO GETS IT
    FINCHAYA_OWNER_EMAIL in /opt/mfapi/.env if set, else the reply-to address (MF_MAIL_REPLY_TO). The email is plain text.
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "request_notice.state")
MAX_AGE_DAYS = 7                # on the first run, requests older than this are not emailed
SETTLE_SECONDS = 60             # a request is picked up once it is this old
MAX_PER_EMAIL = 25
LOCK_KEY = 727431
IST = timezone(timedelta(hours=5, minutes=30))

_TAG = re.compile(r"^\[([^\]]*)\]\s*(.*)$", re.S)


class Problem(Exception):
    """Something that stops this run and is worth saying in words; nothing has been sent or remembered."""


# =====================================================================
# SMALL HELPERS
# =====================================================================
def site_url(env=None):
    return (env if env is not None else os.environ).get("FINCHAYA_SITE_URL", "https://mf.finchaya.com").rstrip("/")


def one_line(s, n=200):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def mask_email(a):
    a = str(a or "")
    if "@" not in a:
        return a or "(none)"
    local, _, dom = a.partition("@")
    return "%s***@%s" % (local[:1], dom)


def owner_address(env=None):
    """FINCHAYA_OWNER_EMAIL, else the reply-to address, else None. 'Name <a@b.c>' is reduced to 'a@b.c'."""
    import mailer
    env = os.environ if env is None else env
    for key in ("FINCHAYA_OWNER_EMAIL", "MF_MAIL_REPLY_TO"):
        v = mailer.bare_address(env.get(key) or "")
        if v and mailer.valid_address(v):
            return v
    return None


def read_state(path):
    """The newest request_id already told, or None when there is no file yet. Raises Problem if the file is unreadable."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        raise Problem("%s exists but cannot be read as a number. Nothing was sent. Delete the file to start over "
                      "(the last %d days of requests will then be emailed again)." % (path, MAX_AGE_DAYS))


def write_state(path, request_id):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("%d\n" % request_id)
    os.replace(tmp, path)


def where_from(details, timeframe, instrument):
    """(page, the person's own words). The back-test page and the screen builder tag what they store; the trade plan does not."""
    m = _TAG.match(details or "")
    if m:
        tag, words = m.group(1).strip(), m.group(2).strip()
        low = tag.lower()
        if low.startswith("back-test"):
            return "Back-test page", words
        if low.startswith("screener builder") or low.startswith("screen builder"):
            rest = re.sub(r"^(a|an)\s+", "", tag.split(",", 1)[1].strip()) if "," in tag else ""      # "a fundamental figure" -> "fundamental figure"
            return "Screen builder" + (" (%s)" % rest if rest else ""), words
        return tag or "unknown", words
    return "Trade plan", (details or "").strip()


def ist_stamp(dt):
    if dt is None:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt.astimezone(IST)
    return "%d %s %d, %02d:%02d IST" % (d.day, d.strftime("%b"), d.year, d.hour, d.minute)


# =====================================================================
# WHAT IS NEW, AND THE EMAIL
# =====================================================================
_NEW = ("SELECT r.request_id, r.user_id, r.kind, r.name, r.name_key, r.details, r.timeframe, r.instrument, r.status, r.created_at, "
        "       u.email AS email, u.full_name AS full_name, "
        "       (SELECT COUNT(DISTINCT x.user_id) FROM indicator_request x WHERE x.kind = r.kind AND x.name_key = r.name_key) AS people "
        "FROM indicator_request r LEFT JOIN mf_user u ON u.user_id = r.user_id "
        "WHERE r.request_id > %(last)s AND r.created_at >= %(since)s AND r.created_at <= %(settled)s "
        "ORDER BY r.request_id")


def new_requests(query, last_id, now):
    return query(_NEW, {"last": last_id or 0, "since": now - timedelta(days=MAX_AGE_DAYS),
                        "settled": now - timedelta(seconds=SETTLE_SECONDS)})


def build_email(rows, env=None, sample=False):
    """(subject, text) for these requests, oldest first."""
    n = len(rows)
    names = [one_line(r["name"], 40) for r in rows]
    if n == 1:
        subject = "FinChaya request: %s" % names[0]
    else:
        subject = "FinChaya: %d new requests (%s)" % (n, ", ".join(names[:3]) + (", ..." if n > 3 else ""))
    if sample:
        subject = "[SAMPLE] " + subject
    out = []
    if sample:
        out += ["THIS IS A SAMPLE. It shows what a real request email looks like. Nobody asked for anything.", ""]
    out.append("%s asked for something FinChaya does not have yet:" % ("A member" if n == 1 else "Members have"))
    out.append("")
    for i, r in enumerate(rows, 1):
        page, words = where_from(r.get("details"), r.get("timeframe"), r.get("instrument"))
        kind = "data" if r.get("kind") == "data" else "indicator"
        out.append("%d. %s   [%s]" % (i, one_line(r["name"], 80), kind))
        out.append("   Asked on:   %s" % ist_stamp(r.get("created_at")))
        out.append("   Where:      %s" % page)
        who = r.get("email")
        if who:
            nm = one_line(r.get("full_name"), 60)
            out.append("   Asked by:   %s%s (user %s)" % (who, " - " + nm if nm else "", r.get("user_id")))
        else:
            out.append("   Asked by:   user %s (the account no longer exists)" % r.get("user_id"))
        if r.get("timeframe"):
            out.append("   Time frame: %s" % one_line(r["timeframe"], 40))
        if r.get("instrument"):
            out.append("   Instrument: %s" % one_line(r["instrument"], 80))
        if words:
            out.append("   Their words: %s" % one_line(words, 500))
        p = int(r.get("people") or 1)
        out.append("   %s" % ("Only this member has asked for it so far." if p <= 1
                              else "%d different members have now asked for this." % p))
        out.append("")
    out += ["-" * 60,
            "Everyone's requests, counted by how many people asked: sign in as an administrator and open",
            "%s/api/tradeplan/requests" % site_url(env),
            "This email goes only to you. To stop it, remove the request_notice line from the server's crontab."]
    return subject, "\n".join(out)


# =====================================================================
# ONE PASS
# =====================================================================
def run(query, send, state_path=None, now=None, dry_run=False, env=None, owner=None):
    """One pass. `send(to, subject, text)` returns (ok, detail). Returns a dict: waiting, sent, to, subject, text, last_id, problem.
    Nothing is remembered unless the email was accepted; a dry run sends and remembers nothing."""
    state_path = state_path or STATE_FILE
    now = now or datetime.now(timezone.utc)
    out = {"waiting": 0, "sent": 0, "to": None, "subject": None, "text": None, "last_id": None, "problem": None}
    try:
        last = read_state(state_path)
        out["last_id"] = last
        to = owner or owner_address(env)
        rows = new_requests(query, last, now)
        out["waiting"] = len(rows)
        if not rows:
            return out
        if not to:
            raise Problem("No address to send to: set FINCHAYA_OWNER_EMAIL (or MF_MAIL_REPLY_TO) in /opt/mfapi/.env. "
                          "%d request(s) are waiting; nothing was remembered." % len(rows))
        batch = rows[:MAX_PER_EMAIL]
        subject, text = build_email(batch, env)
        out.update(to=to, subject=subject, text=text)
        if dry_run:
            return out
        ok, detail = send(to, subject, text)
        if not ok:
            raise Problem("The email was not accepted (%s). Nothing was remembered, so the next run tries again." % one_line(detail, 200))
        write_state(state_path, max(r["request_id"] for r in batch))
        out["sent"] = len(batch)
        out["last_id"] = max(r["request_id"] for r in batch)
    except Problem as e:
        out["problem"] = str(e)
    return out


def send_sample(send, env=None, owner=None):
    to = owner or owner_address(env)
    if not to:
        raise Problem("No address to send to: set FINCHAYA_OWNER_EMAIL (or MF_MAIL_REPLY_TO) in /opt/mfapi/.env.")
    now = datetime.now(timezone.utc)
    rows = [{"request_id": 0, "user_id": 0, "kind": "indicator", "name": "Ichimoku cloud", "details": "[Back-test] I use it on weekly charts.",
             "timeframe": None, "instrument": None, "created_at": now, "email": "member@example.com", "full_name": "A Member", "people": 3},
            {"request_id": 0, "user_id": 0, "kind": "data", "name": "Hourly candles", "details": None,
             "timeframe": "1h", "instrument": "Nifty 50", "created_at": now, "email": "member@example.com", "full_name": None, "people": 1}]
    subject, text = build_email(rows, env, sample=True)
    ok, detail = send(to, subject, text)
    return to, ok, detail


# =====================================================================
# COMMAND LINE
# =====================================================================
def _sender(env=None):
    import mailer
    cfg = mailer.mail_config(env)
    if not cfg["configured"]:
        raise Problem("Email is not set up on this server (%s)." % cfg["why"])

    def send(to, subject, text):
        return mailer.send_email(to, subject, text, env=env)
    return send


def main(argv):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
    load_dotenv("/opt/mfapi/.env")
    import mailer

    if "--sample" in argv:
        try:
            to, ok, detail = send_sample(_sender())
        except Problem as e:
            print(str(e))
            return 1
        print("sample email to %s: %s" % (mask_email(to), "SENT" if ok else "NOT SENT (%s)" % detail))
        return 0 if ok else 1

    import psycopg
    from psycopg.rows import dict_row
    dsn = os.environ.get("FINCHAYA_DB") or os.environ.get("DATABASE_URL") or os.environ.get("MF_DSN")
    if not dsn:
        print("FINCHAYA_DB is not set.")
        return 1
    conn = psycopg.connect(dsn, row_factory=dict_row)

    def query(sql, params=None, one=False):
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            rows = cur.fetchall()
        conn.commit()
        return (rows[0] if rows else None) if one else rows

    if "--status" in argv:
        print("\n".join(mailer.describe()))
        print("sends to    : %s" % mask_email(owner_address()))
        try:
            last = read_state(STATE_FILE)
        except Problem as e:
            print(str(e))
            return 1
        print("remembered  : %s" % ("nothing yet (the next run tells you about the last %d days)" % MAX_AGE_DAYS if last is None
                                    else "requests up to number %d have been told" % last))
        tot = query("SELECT COUNT(*) AS n, MAX(created_at) AS newest FROM indicator_request", None, one=True)
        print("requests    : %d in all, newest %s" % (tot["n"], ist_stamp(tot["newest"]) if tot["n"] else "-"))
        print("waiting     : %d to be emailed" % len(new_requests(query, last, datetime.now(timezone.utc))))
        return 0

    dry = "--dry-run" in argv
    got = conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (LOCK_KEY + (1 if dry else 0),)).fetchone()["got"]
    if not got:
        print("another request_notice is already running; nothing done")
        return 0
    try:
        send = _sender() if not dry else None
    except Problem as e:
        print(str(e))
        return 1
    out = run(query, send, dry_run=dry)
    if out["problem"]:
        print("PROBLEM: " + out["problem"])
        return 1
    if dry:
        print("[dry run] %d request(s) waiting" % out["waiting"])
        if out["text"]:
            print("would send to %s\nSubject: %s\n\n%s" % (mask_email(out["to"]), out["subject"], out["text"]))
        return 0
    if out["sent"]:
        print("emailed %d request(s) to %s" % (out["sent"], mask_email(out["to"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
