"""
feature_requests.py -- "ask us to add an indicator": the shared part of the request box (back-test page; the trade plan keeps its own copy of the same
rules in tradeplan_api.py and writes to the same table, so the owner reads ONE list of what people want).
---------------------------------------------------------------------------
Rows go into indicator_request, one row per person per thing. The same request spelled differently ("Ichimoku Cloud", "ichimoku-cloud") has
one name_key and counts once. A request for something the tool already has is answered with "it is already there" and not stored. An
administrator reads the list counted by how many people asked (admin_list).

Only INDICATOR requests are taken here. (The trade plan also takes "data" requests, for a time frame or an instrument we hold no candles for;
the back-test page does not offer that for now.)

Pure helpers: they take `query` / `execute` from the caller and raise ValueError(message) for a request that cannot be taken, which the caller
turns into its own HTTP 400. They never decide who may ask; the caller's gate does that.
"""

import re
from datetime import datetime, timedelta, timezone

MAX_REQUESTS_PER_DAY = 20       # per person
MAX_NAME = 80
MAX_DETAILS = 500


def name_key(text):
    """'Ichimoku Cloud', 'ichimoku-cloud' and 'ICHIMOKU  cloud' are one request."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _text(v, lo, hi, what):
    if not isinstance(v, str):
        raise ValueError("%s must be text." % (what[:1].upper() + what[1:]))
    v = re.sub(r"\s+", " ", v).strip()
    if len(v) < lo:
        raise ValueError("Type %s." % what)
    if len(v) > hi:
        raise ValueError("%s can be up to %d characters." % (what[:1].upper() + what[1:], hi))
    return v


def make(user_id, body, query, execute, known=None, tag=""):
    """Take an indicator request. `known(name)` returns the label of an indicator the tool already has (or None).
    Returns {"ok": True, "message": ..., ["exists": True] | ["already": True]}. Raises ValueError (400) or OverflowError (429)."""
    if not isinstance(body, dict):
        raise ValueError("The request is not in a form I can read.")
    if body.get("kind") not in (None, "", "indicator"):
        raise ValueError("Only requests for an indicator are taken here for now.")
    name = _text(body.get("name"), 2, MAX_NAME, "the name of the indicator you need")
    details = body.get("details")
    details = "" if details in (None, "") else _text(details, 0, MAX_DETAILS, "your details")
    nk = name_key(name)
    if not nk:
        raise ValueError("Type the name of the indicator you need.")
    have = known(name) if known else None
    if have:
        return {"ok": True, "exists": True, "message": "%s is already in the list, so there is nothing to request." % have}
    if query("SELECT 1 AS x FROM indicator_request WHERE user_id = %(u)s AND kind = 'indicator' AND name_key = %(nk)s",
             {"u": user_id, "nk": nk}, one=True):
        return {"ok": True, "already": True, "message": "You have already asked for this. We have it on the list."}
    since = datetime.now(timezone.utc) - timedelta(days=1)
    n = (query("SELECT COUNT(*) AS n FROM indicator_request WHERE user_id = %(u)s AND created_at >= %(since)s",
               {"u": user_id, "since": since}, one=True) or {}).get("n", 0)
    if n >= MAX_REQUESTS_PER_DAY:
        raise OverflowError("That is a lot of requests for one day. Try again tomorrow.")
    tagged = ("[%s] %s" % (tag, details)).strip() if tag else details
    execute("INSERT INTO indicator_request (user_id, kind, name, name_key, details) VALUES (%(u)s, 'indicator', %(name)s, %(nk)s, %(d)s) "
            "RETURNING request_id", {"u": user_id, "name": name, "nk": nk, "d": tagged or None}, returning=True)
    return {"ok": True, "message": "Thank you. We have noted it, and the more people ask for the same thing the sooner it moves up."}


def mine(query, user_id):
    return query("SELECT kind, name, details, status, created_at FROM indicator_request WHERE user_id = %(u)s "
                 "ORDER BY created_at DESC LIMIT 50", {"u": user_id})


def admin_list(query):
    """Everyone's requests, counted by how many people asked, the most-asked first."""
    rows = query("SELECT request_id, user_id, kind, name, name_key, details, timeframe, instrument, status, created_at "
                 "FROM indicator_request ORDER BY created_at DESC, request_id DESC LIMIT 2000")
    by = {}
    for r in rows:
        g = by.setdefault((r["kind"], r["name_key"]), {"kind": r["kind"], "name": r["name"], "people": set(), "latest": r["created_at"],
                                                       "first": r["created_at"], "status": r["status"], "details": []})
        g["people"].add(r["user_id"])
        g["first"] = r["created_at"]
        g["name"] = r["name"]
        if r["details"] and len(g["details"]) < 5:
            g["details"].append(r["details"])
    out = [dict(g, people=len(g["people"])) for g in by.values()]
    out.sort(key=lambda g: str(g["latest"]), reverse=True)
    out.sort(key=lambda g: -g["people"])
    return out
