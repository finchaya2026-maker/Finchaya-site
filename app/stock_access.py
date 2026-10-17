"""
stock_access.py -- who may use the pages that are for STOCK SUBSCRIBERS (the quarterly-results page, stock alerts).
---------------------------------------------------------------------------
One small file so those pages cannot disagree about who gets in.

    gate = stock_access.Gate("MF_RESULTS_ACCESS", "MF_RESULTS_ENABLED")
    user = gate.require(request)        # 401 not signed in, 403 no stocks plan; returns the user
    gate.access(request)                # the dict the page asks first: enabled, signed_in, allowed, admin, audience, ...

WHO IS ALLOWED (MF_<NAME>_ACCESS decides; the DEFAULT IS `premium`)
    premium     a member whose subscription covers STOCKS ("stocks" or "all"; an account with no scope on record has
                everything, as api.scope_of reads it), not lapsed -- and administrators (the app_admin table)
    signed_in   any signed-in account
    admin       administrators only
    everyone    anybody, signed in or not
  Anything else is read as `premium`: a typo can only ever make the door narrower than meant, never wider.

THE PAYWALL SWITCH IS NOT CONSULTED, ON PURPOSE
    api.has_subscription() lets EVERYONE through while MF_PAYWALL_ENABLED is false, which is right for features that
    are being kept free until billing starts. These pages were asked for as "subscription users only", so they check
    for a real, live, paid subscription whatever the paywall switch says (the trade plan and the back-tester do the same).
    Administrators always get in, which is how the page can be looked at before anyone has paid.

THE CHECK IS MADE ON EVERY CALL. A hidden link protects nothing; the page only asks /access to avoid drawing a dead screen.
"""

import os
import time
from datetime import date

from fastapi import HTTPException

MODES = ("premium", "signed_in", "admin", "everyone")
WHO = {"premium": "subscribers to the Stocks plan", "signed_in": "signed-in members",
       "admin": "administrators", "everyone": "everyone"}

_ADMIN_TTL = 30
_admin_cache = {"ids": frozenset(), "at": None}


def admin_ids():
    """The administrators' user ids, kept for 30 seconds. An error leaves the list EMPTY (fail closed): it can only ever
    mean fewer people get in."""
    now = time.monotonic()
    at = _admin_cache["at"]
    if at is None or now - at > _ADMIN_TTL:
        try:
            from api import query
            ids = frozenset(r["user_id"] for r in query("SELECT user_id FROM app_admin"))
        except Exception:                                    # noqa: BLE001
            ids = frozenset()
        _admin_cache.update(ids=ids, at=now)
    return _admin_cache["ids"]


def is_admin(user):
    return bool(user) and user.get("user_id") in admin_ids()


def has_stocks_plan(user):
    """A live, paid subscription that covers stocks. Reads the account only; never the paywall switch."""
    if not user or not user.get("is_premium"):
        return False
    if (user.get("premium_scope") or "all") not in ("stocks", "all"):
        return False
    until = user.get("premium_until")
    if until is None:
        return True
    if hasattr(until, "date"):
        until = until.date()
    return until >= date.today()


class Gate:
    def __init__(self, access_env, enabled_env, default="premium"):
        self.access_env, self.enabled_env, self.default = access_env, enabled_env, default

    # read at call time, so a test (or an operator) can change the setting without re-importing
    def enabled(self):
        return os.getenv(self.enabled_env, "true").strip().lower() not in ("0", "false", "no", "off")

    def mode(self):
        m = os.getenv(self.access_env, self.default).strip().lower()
        return m if m in MODES else "premium"

    def allowed(self, user):
        if not self.enabled():
            return False
        m = self.mode()
        if m == "everyone":
            return True
        if not user:
            return False
        if m == "signed_in":
            return True
        if m == "premium" and has_stocks_plan(user):
            return True
        return is_admin(user)

    def require(self, request):
        from api import current_user
        user = current_user(request)
        if not self.enabled():
            raise HTTPException(404, "This is switched off at the moment.")
        if not user and self.mode() != "everyone":
            raise HTTPException(401, "Sign in to use this.")
        if not self.allowed(user):
            raise HTTPException(403, "This is available to %s." % WHO[self.mode()])
        return user

    def access(self, request):
        """Never an error: the page asks this before anything else, so a signed-out visitor, a member without the plan
        and an administrator each get the right screen."""
        from api import current_user
        user = current_user(request)
        ok = self.allowed(user)
        return {"enabled": self.enabled(), "signed_in": bool(user), "allowed": ok, "admin": is_admin(user),
                "audience": self.mode(), "who": WHO[self.mode()], "area": "stocks",
                "email": (user or {}).get("email") if ok else None}
