"""
ipo_universe.py -- which stocks are IPOs, and on what date each listed. One small file so every screen that narrows to IPOs means the same thing.
---------------------------------------------------------------------------
A stock is an IPO here when the NSE listed it as an issue (ipo_issue, filled by ipo_refresh.py) and it is on FinChaya as a stock (stock_master, by symbol).
The LISTING DATE is the exchange's: not the first price FinChaya happens to hold, which for the 230 BSE-to-NSE batch debuts of 2026 and for any stock whose history
starts where our data starts would be wrong.

    listings(cur)                     {isin: {"listing_date", "symbol", "name", "slug", "issue_price", "board"}} for every listed issue on the site, kept 5 minutes
    select(listings, within=30)       the ones that listed in the last 30 days
    select(listings, listed_from=date(2026, 9, 1), listed_to=date(2026, 9, 30))     ... or between two dates
    window_from_params(raw)           the window a reader asked for, from query-string style parameters (listed_within, listed_from, listed_to)

Where the IPO tables are not set up on a server (create_ipo_tables.py not run) listings() raises NotSetUp, so a screen can say so rather than show an empty list.
"""

import threading
import time
from datetime import date, timedelta

TTL_SECONDS = 300
MAX_WITHIN = 3660                    # ten years: more than any history we hold

SQL = """
SELECT m.isin, i.symbol, i.name, i.slug, i.board, i.listing_date, i.issue_price
FROM ipo_issue i
JOIN stock_master m ON m.symbol = i.symbol AND m.is_active
WHERE i.stage = 'listed' AND i.listing_date IS NOT NULL
ORDER BY i.listing_date
"""

_CACHE = {"at": 0.0, "day": None, "value": None}
_LOCK = threading.Lock()


class NotSetUp(Exception):
    """The IPO tables do not exist on this server."""


def clear():
    with _LOCK:
        _CACHE.update(at=0.0, day=None, value=None)


def listings(cur, today=None):
    """{isin: info} for every issue that has listed and whose stock is on the site. A symbol listed twice (an old ticker reused) keeps its latest listing."""
    today = today or date.today()
    with _LOCK:
        if _CACHE["value"] is not None and _CACHE["day"] == today and time.monotonic() - _CACHE["at"] < TTL_SECONDS:
            return _CACHE["value"]
    import psycopg
    try:
        cur.execute(SQL)
        rows = cur.fetchall()
    except psycopg.errors.UndefinedTable:
        cur.connection.rollback()
        raise NotSetUp("The IPO list is not set up on this server yet (create_ipo_tables.py has not been run).")
    out = {}
    for r in rows:
        r = r if isinstance(r, dict) else dict(zip(("isin", "symbol", "name", "slug", "board", "listing_date", "issue_price"), r))
        if r["listing_date"] > today:
            continue
        out[r["isin"]] = {"listing_date": r["listing_date"], "symbol": r["symbol"], "name": r["name"], "slug": r["slug"], "board": r["board"],
                          "issue_price": float(r["issue_price"]) if r["issue_price"] is not None else None}
    with _LOCK:
        _CACHE.update(at=time.monotonic(), day=today, value=out)
    return out


def select(all_listings, within=None, listed_from=None, listed_to=None, today=None):
    """The listings inside a window: listed in the last `within` days (today counts), or on or after `listed_from`, or on or before `listed_to` (the last two
    may be combined). With no window at all, every listing."""
    today = today or date.today()
    if within is not None:
        listed_from = max(listed_from, today - timedelta(days=int(within))) if listed_from else today - timedelta(days=int(within))
    return {isin: info for isin, info in all_listings.items()
            if (listed_from is None or info["listing_date"] >= listed_from) and (listed_to is None or info["listing_date"] <= listed_to)}


def _date(v, what):
    try:
        return date.fromisoformat(str(v).strip())
    except ValueError:
        raise ValueError("%s must be a date like 2026-09-30." % what)


def window_from_params(raw):
    """(within, listed_from, listed_to) from query-string style parameters. Blank or missing means no limit. Raises ValueError, in words, for a bad value."""
    raw = raw or {}
    within = raw.get("listed_within")
    if within in (None, "", "0", 0):
        within = None
    else:
        try:
            within = int(float(within))
        except (TypeError, ValueError):
            raise ValueError("'listed_within' must be a number of days.")
        if not 1 <= within <= MAX_WITHIN:
            raise ValueError("'listed_within' must be between 1 and %d days." % MAX_WITHIN)
    lf = _date(raw["listed_from"], "'listed_from'") if raw.get("listed_from") else None
    lt = _date(raw["listed_to"], "'listed_to'") if raw.get("listed_to") else None
    if lf and lt and lf > lt:
        raise ValueError("'listed_from' is after 'listed_to'.")
    return within, lf, lt


def describe(within=None, listed_from=None, listed_to=None):
    """The window in words, for a screen's footnote."""
    bits = []
    if within:
        bits.append("listed in the last %d days" % within)
    if listed_from:
        bits.append("listed on or after %s" % listed_from.isoformat())
    if listed_to:
        bits.append("listed on or before %s" % listed_to.isoformat())
    return " and ".join(bits) if bits else "any IPO on record"
