"""
mailer.py -- the ONE place this site sends email from.
---------------------------------------------------------------------------
    cd /opt/mfapi
    venv/bin/python3 mailer.py --status                       # what is set up (reads settings only)
    venv/bin/python3 mailer.py --diagnose                     # ... and whether the server can actually reach the mail service
    venv/bin/python3 mailer.py --test you@example.com         # send ONE real email and say what happened, in words

WHO USES IT
    stock alerts (stockalerts_checker.py), trade-plan alerts (tradeplan_alerts.py) and payment receipts
    (billing_invoices.py). Each used to carry its own copy of the sending code, and the receipts one could only speak
    SMTP -- the way that does not work from the server (see below). They now all call send_email / send_message here.

TWO WAYS TO SEND, CHOSEN BY SETTINGS (put them in /opt/mfapi/.env, then restart the service)
    MF_MAIL_PROVIDER=resend    an HTTPS API (port 443). Needs RESEND_API_KEY, and MF_MAIL_FROM on an address of a domain
                               that is verified at Resend (for example  FinChaya <alerts@finchaya.com>  once the DNS
                               records Resend gives you are added). THIS IS THE ONE THAT WORKS FROM THE DROPLET.
    (the default)              SMTP, from the settings the site already has: MF_SMTP_USER, MF_SMTP_PASS, MF_SMTP_HOST
                               (smtp.gmail.com), MF_SMTP_PORT (587 STARTTLS, or 465 SSL), MF_MAIL_FROM.
                               Fine from a laptop. DigitalOcean BLOCKS outbound SMTP from droplets (ACCOUNTS_AND_AUTH.md):
                               the connection just hangs, which is why this reports "use resend" when it times out.
    MF_MAIL_REPLY_TO (optional)  where a person's reply goes, if not the sender.
  Resend beats SMTP when both are set. If only RESEND_API_KEY is set, Resend is chosen.

IT NEVER RAISES. A mail problem must not stop a checker run or a payment webhook, so every function returns
(ok, detail) and `detail` says why in words. The detail is what ends up in the log and on the alerts page.

WHAT IT DOES NOT DO: marketing, newsletters, tracking pixels or links that record a click. Every message is a plain
transactional note to one person about something they set up or paid for.
"""

import json
import os
import re
import smtplib
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

_RESEND_URL = "https://api.resend.com/emails"
_RESEND_DOMAINS_URL = "https://api.resend.com/domains"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ADDR_RE = re.compile(r"<([^<>]+)>")


def site_url(env=None):
    return (env or os.environ).get("FINCHAYA_SITE_URL", "https://mf.finchaya.com").rstrip("/")


def _clean_header(s):
    return re.sub(r"[\r\n\x00]+", " ", str(s or "")).strip()


def bare_address(s):
    """'FinChaya <alerts@finchaya.com>' -> 'alerts@finchaya.com'."""
    m = _ADDR_RE.search(str(s or ""))
    return (m.group(1) if m else str(s or "")).strip()


def valid_address(s):
    return bool(_EMAIL_RE.match((s or "").strip()))


# =====================================================================
# SETTINGS
# =====================================================================
def mail_config(env=None):
    """Which way mail would be sent from these settings, and whether it can be at all. Reads settings only: it cannot
    tell whether the provider will accept a message -- only --test can."""
    env = os.environ if env is None else env
    frm = (env.get("MF_MAIL_FROM") or env.get("MF_SMTP_USER") or "").strip()
    reply = (env.get("MF_MAIL_REPLY_TO") or "").strip()
    prov = (env.get("MF_MAIL_PROVIDER") or "").strip().lower()
    if prov == "resend" or (not prov and env.get("RESEND_API_KEY")):
        key = (env.get("RESEND_API_KEY") or "").strip()
        why = None if key and frm else "needs RESEND_API_KEY and MF_MAIL_FROM"
        return {"transport": "resend", "configured": not why, "from": frm, "reply_to": reply, "why": why}
    if env.get("MF_SMTP_USER") and env.get("MF_SMTP_PASS"):
        host, port = env.get("MF_SMTP_HOST", "smtp.gmail.com"), env.get("MF_SMTP_PORT", "587")
        user = env["MF_SMTP_USER"].strip()
        # Gmail signs in with the full address. A placeholder (a few dots) is what a copied-around .env often holds, and it
        # fails later with a baffling "connection unexpectedly closed"; say so here instead.
        if host == "smtp.gmail.com" and "@" not in user:
            return {"transport": "smtp", "configured": False, "from": frm, "reply_to": reply, "host": host, "port": port,
                    "why": "MF_SMTP_USER looks like a placeholder, not a full Gmail address (MF_SMTP_PASS must be a Gmail app password)"}
        return {"transport": "smtp", "configured": True, "from": frm, "reply_to": reply, "host": host, "port": port,
                "why": None}
    return {"transport": None, "configured": False, "from": frm, "reply_to": reply,
            "why": "no mail settings: set MF_MAIL_PROVIDER=resend with RESEND_API_KEY and MF_MAIL_FROM, "
                   "or MF_SMTP_USER and MF_SMTP_PASS"}


# =====================================================================
# SENDING
# =====================================================================
def _parts(msg):
    """(plain text, html or None) of a message, whether it is single-part or multipart/alternative."""
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    # EmailMessage adds one newline to every body it stores; the API payload should carry the text as written
    return (plain.get_content().rstrip("\n") if plain else ""), (html.get_content().rstrip("\n") if html else None)


def send_message(msg, env=None, smtp_module=None, urlopen=None):
    """Send a ready EmailMessage (it needs To and Subject; From and Reply-To are set from the settings). Returns
    (ok, detail). NEVER RAISES."""
    env = os.environ if env is None else env
    smtp_module = smtp_module or smtplib
    urlopen = urlopen or urllib.request.urlopen
    to = _clean_header(msg["To"])
    if not valid_address(bare_address(to)):
        return False, "no usable email address on the account"
    cfg = mail_config(env)
    if not cfg["configured"]:
        return False, "email is not set up on this server (%s)" % cfg["why"]
    subject, frm = _clean_header(msg["Subject"])[:200], _clean_header(cfg["from"])
    for h in ("Subject", "From", "To", "Reply-To"):
        del msg[h]
    msg["Subject"], msg["From"], msg["To"] = subject, frm, to
    if cfg.get("reply_to"):
        msg["Reply-To"] = _clean_header(cfg["reply_to"])
    try:
        if cfg["transport"] == "resend":
            plain, html = _parts(msg)
            payload = {"from": frm, "to": [bare_address(to)], "subject": subject, "text": plain}
            if html:
                payload["html"] = html
            if cfg.get("reply_to"):
                payload["reply_to"] = _clean_header(cfg["reply_to"])
            req = urllib.request.Request(
                _RESEND_URL, method="POST", data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": "Bearer " + env["RESEND_API_KEY"].strip(),
                         "Content-Type": "application/json", "User-Agent": "finchaya-mailer/1"})
            with urlopen(req, timeout=20) as r:
                code = getattr(r, "status", 200)
                if 200 <= code < 300:
                    return True, "sent through the mail API"
                return False, "the mail API answered %s" % code
        host, port = cfg["host"], int(cfg["port"])
        if port == 465:
            with smtp_module.SMTP_SSL(host, port, timeout=20) as s:
                s.login(env["MF_SMTP_USER"], env["MF_SMTP_PASS"])
                s.send_message(msg)
        else:
            with smtp_module.SMTP(host, port, timeout=20) as s:
                s.starttls()
                s.login(env["MF_SMTP_USER"], env["MF_SMTP_PASS"])
                s.send_message(msg)
        return True, "sent through SMTP"
    except urllib.error.HTTPError as e:
        try:
            said = e.read().decode("utf-8", "replace")[:240]
        except Exception:                                    # noqa: BLE001
            said = ""
        return False, "the mail API refused it (%s) %s%s" % (e.code, said, _resend_hint(e.code, said))
    except Exception as e:                                   # noqa: BLE001
        hint = ""
        # Only for a connection that never got going (timeout, refused, no route): that is what a blocked port looks
        # like. smtplib's own errors (a wrong password, a refused sender) are OSError subclasses too, but they mean the
        # server was reached, so telling the person the port is blocked would send them after the wrong problem.
        if cfg["transport"] == "smtp" and isinstance(e, OSError) and not isinstance(e, smtplib.SMTPException):
            hint = " -- DigitalOcean blocks outbound SMTP; use MF_MAIL_PROVIDER=resend instead"
        return False, "%s: %s%s" % (type(e).__name__, str(e)[:160], hint)


def _resend_hint(code, said):
    s = (said or "").lower()
    if code in (401, 403) and ("api key" in s or "restricted" in s or "invalid" in s):
        return " -- the RESEND_API_KEY is wrong or not allowed to send"
    if code in (403, 422) and ("domain" in s or "verif" in s):
        return " -- the sender's domain is not verified at Resend yet (resend.com/domains)"
    if code == 429:
        return " -- Resend says slow down; it will be tried again on the next run"
    return ""


def send_email(to, subject, body, html=None, env=None, smtp_module=None, urlopen=None):
    """Send one email: plain text, plus an html version if given. Returns (ok, detail). NEVER RAISES."""
    msg = EmailMessage()
    msg["Subject"], msg["To"] = _clean_header(subject), _clean_header(to)
    msg.set_content(body or "")
    if html:
        msg.add_alternative(html, subtype="html")
    return send_message(msg, env=env, smtp_module=smtp_module, urlopen=urlopen)


# =====================================================================
# COMMAND LINE
# =====================================================================
DISCLAIMER = ("Educational content only, not investment advice. FinChaya is not currently registered with SEBI as an "
              "Investment Adviser or Research Analyst.")


def describe(env=None):
    """Lines saying what is set up -- no secrets."""
    cfg = mail_config(env)
    lines = ["mail transport : %s%s" % (cfg["transport"] or "none",
                                         "" if cfg["configured"] else "   (NOT configured: %s)" % cfg["why"]),
             "sending from  : %s" % (cfg["from"] or "(unset)")]
    if cfg["transport"] == "smtp":
        lines.append("smtp server   : %s:%s" % (cfg["host"], cfg["port"]))
    if cfg.get("reply_to"):
        lines.append("replies go to : %s" % cfg["reply_to"])
    return lines


def diagnose(env=None, urlopen=None, connect=None):
    """What the server can actually reach, in words. Makes a connection, sends nothing."""
    env = os.environ if env is None else env
    urlopen = urlopen or urllib.request.urlopen
    connect = connect or (lambda h, p: socket.create_connection((h, p), timeout=8).close())
    cfg = mail_config(env)
    out = []
    if cfg["transport"] == "resend":
        out.append("Resend (HTTPS, port 443): the way that works from a DigitalOcean droplet.")
        if cfg["configured"]:
            try:
                req = urllib.request.Request(_RESEND_DOMAINS_URL, headers={
                    "Authorization": "Bearer " + env["RESEND_API_KEY"].strip(), "User-Agent": "finchaya-mailer/1"})
                with urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8"))
                doms = data.get("data") or []
                want = bare_address(cfg["from"]).split("@")[-1].lower()
                found = [d for d in doms if (d.get("name") or "").lower() == want]
                if found:
                    out.append("domain %s at Resend: %s" % (want, found[0].get("status", "unknown")))
                    if found[0].get("status") != "verified":
                        out.append("-> add the DNS records Resend lists for %s, then press Verify there." % want)
                else:
                    out.append("domain %s is NOT in your Resend account (it lists: %s)"
                               % (want, ", ".join(d.get("name", "?") for d in doms) or "none"))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    out.append("reached Resend; this key cannot list domains (a send-only key cannot). "
                               "That is fine -- run --test to see whether it can send.")
                else:
                    out.append("Resend answered %s" % e.code)
            except Exception as e:                           # noqa: BLE001
                out.append("could not reach Resend: %s: %s" % (type(e).__name__, str(e)[:120]))
    elif cfg["transport"] == "smtp":
        host, port = cfg["host"], int(cfg["port"])
        try:
            connect(host, port)
            out.append("can open a connection to %s:%d (the port is not blocked from here)" % (host, port))
        except Exception as e:                               # noqa: BLE001
            out.append("CANNOT open a connection to %s:%d (%s: %s)" % (host, port, type(e).__name__, str(e)[:100]))
            out.append("-> on a DigitalOcean droplet this is the blocked SMTP port. Use MF_MAIL_PROVIDER=resend.")
    else:
        out.append("Nothing is configured, so there is nothing to reach.")
    return out


def main(argv):
    try:
        from dotenv import load_dotenv
        here = os.path.dirname(os.path.abspath(__file__))
        load_dotenv(os.path.join(here, ".env"))
        load_dotenv("/opt/mfapi/.env")
    except Exception:                                        # noqa: BLE001
        pass

    if "--status" in argv or "--diagnose" in argv:
        print("\n".join(describe()))
        if "--diagnose" in argv:
            print("\n".join(diagnose()))
        return 0 if mail_config()["configured"] else 1

    if "--test" in argv:
        i = argv.index("--test")
        to = argv[i + 1] if i + 1 < len(argv) else ""
        print("\n".join(describe()))
        now = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        body = ("Hello,\n\nIf you can read this, FinChaya can email you. It was sent at %s through %s.\n\n"
                "Sign-in codes, payment receipts and your stock alerts all go out the same way.\n\n%s\n"
                % (now, mail_config()["transport"] or "no transport", DISCLAIMER))
        html = ("<p>Hello,</p><p>If you can read this, <b>FinChaya can email you</b>. It was sent at %s through %s.</p>"
                "<p>Sign-in codes, payment receipts and your stock alerts all go out the same way.</p>"
                "<p style='color:#6b7c76;font-size:13px'>%s</p>"
                % (now, mail_config()["transport"] or "no transport", DISCLAIMER))
        t0 = time.monotonic()
        ok, detail = send_email(to, "FinChaya test email", body, html=html)
        print("%s: %s (%.1fs)" % ("SENT" if ok else "NOT SENT", detail, time.monotonic() - t0))
        return 0 if ok else 1

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
