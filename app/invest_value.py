"""
invest_value.py -- what a holding is worth, priced from NAV history.
---------------------------------------------------------------------------
Imported by portfolio_api.py and saved_portfolio_api.py. Runs no queries
of its own beyond the two below, and writes nothing.

WHY THIS EXISTS
    A holding used to carry one number typed by hand: what it is worth
    today. That number is the foundation of the whole look-through --
    every rupee figure on the report is a share of it -- and it was the
    one thing nobody could check.

    It was also ambiguous in the worst way. A SIP row has two amounts on
    it, what goes in each month and what the holding is worth, and
    typing the first into the second turns a healthy portfolio into a
    76% loss on the report. That is not a mistake to guard against with
    a warning. It is a number that should never have been asked for.

    With a start date, a mode and an instalment, the value is not a
    matter of opinion: price each instalment at the NAV of its own day,
    add up the units, and multiply by today's NAV. We have every NAV.

WHAT IT RETURNS PER HOLDING
    units          bought across every instalment
    value          units x the latest NAV on file
    paid           what actually went in
    nav / nav_date the closing NAV used, and its date
    instalments    how many were priced
    unpriced       instalments with no NAV on or before them -- a start
                   date before the fund existed, which is reported
                   rather than quietly dropped

WHICH NAV SERIES
    Not the fund's own scheme code. v_returns_source maps a scheme to
    the series that actually carries its NAV history, because an IDCW
    plan's history can live on its Growth sibling -- and reading the
    wrong one reports a fund's payouts as losses. Same COALESCE the
    scoring scripts use, so a value here agrees with a return there.

NAV ON OR BEFORE, NEVER NEAREST
    A SIP dated the 5th lands on a Sunday four times a year, and there
    is no NAV for a Sunday. The instalment is priced at the last NAV on
    or before it, which is what the AMC does. Taking the NEAREST NAV
    would sometimes reach forward into a price the investor could not
    have transacted at.

ONE QUERY FOR EVERY FUND, NOT ONE PER INSTALMENT
    A five-year SIP is sixty dates. Sixty lateral lookups per fund, for
    a dozen funds, is a page that takes a second to draw for no reason.
    The whole NAV series for every fund involved comes back in one
    query and the matching happens here.
"""

from bisect import bisect_right
from calendar import monthrange
from datetime import date, timedelta

# How far BEFORE the first instalment to start reading NAVs.
#
# The lookup is "the last NAV on or before this date", so the series has
# to begin earlier than the first instalment or that instalment has
# nothing to reach back to. A SIP dated the 6th starting on a Saturday
# needs Friday's NAV, which is outside a window that begins on the 6th
# -- and the instalment is then silently unpriced. Forty-five days
# covers a weekend, a run of public holidays, and the gap around a
# fund's own launch.
NAV_LOOKBACK = timedelta(days=45)

MODES = ("lumpsum", "sip")

# The series that actually holds each fund's NAV history.
NAV_SOURCE = """
SELECT c.canonical_scheme_code AS scheme_code,
       COALESCE(v.nav_scheme_code, c.canonical_scheme_code) AS nav_scheme_code
FROM v_fund_canonical c
LEFT JOIN v_returns_source v ON v.scheme_code = c.canonical_scheme_code
WHERE c.canonical_scheme_code = ANY(%(codes)s)
"""
# NOTE: a Regular sibling is not itself canonical, so the lookup above
# returns nothing for it and the COALESCE below falls through to the
# scheme's own code. That is the right answer -- v_returns_source is
# consulted separately, per scheme code, for the IDCW substitution.

NAV_SERIES = """
SELECT scheme_code, nav_date, nav
FROM mf_nav
WHERE scheme_code = ANY(%(codes)s) AND nav_date >= %(since)s AND nav > 0
ORDER BY scheme_code, nav_date
"""

# The same fund on a different plan.
#
# Siblings share a name and an AMC and differ by plan and option -- the
# join score_returns.py uses, so a pair found here is a pair the scoring
# would have found. Growth first, because an IDCW NAV has been reduced
# by every payout it ever made and is not comparable with a Growth one.
SIBLING = """
WITH canon AS (
    SELECT c.canonical_scheme_code AS code, c.scheme_name, c.amc_name
    FROM v_fund_canonical c
    WHERE c.canonical_scheme_code = ANY(%(codes)s)
)
SELECT k.code,
       (SELECT s.scheme_code FROM mf_scheme s
         WHERE s.scheme_name = k.scheme_name
           AND s.amc_name IS NOT DISTINCT FROM k.amc_name
           AND upper(s.plan_type) = %(plan)s
           -- GROWTH ONLY, and this is a correctness rule not a
           -- preference. An IDCW NAV has been reduced by every payout
           -- the fund ever made, so pairing a Direct Growth with a
           -- Regular IDCW compares a total-return series against a
           -- price-return one. On real data that produced "Regular beat
           -- Direct by 9 points", which cannot happen and is the
           -- signature of exactly this mistake. A fund whose only
           -- Regular plan is IDCW falls back to Direct and is marked,
           -- which is worse coverage and a right answer.
           AND upper(s.option_type) = 'GROWTH'
         ORDER BY s.scheme_code
         LIMIT 1) AS sibling
FROM canon k
"""


def plan_siblings(cur, codes, plan):
    """{canonical code: the same fund's code on `plan`}.

    Missing from the result means that fund has no such plan -- which
    is common enough (a few funds are Direct-only) that the caller must
    handle it, and must SAY so on the report rather than quietly
    reporting one fund on a different basis from its neighbours.
    """
    if not codes:
        return {}
    cur.execute(SIBLING, {"codes": sorted(set(codes)),
                          "plan": (plan or "").upper()})
    out = {}
    for r in cur.fetchall():
        code = r["code"] if isinstance(r, dict) else r[0]
        sib = r["sibling"] if isinstance(r, dict) else r[1]
        if sib:
            out[str(code)] = str(sib)
    return out


def instalment_dates(start, mode, today):
    """Every date money went in.

    Same day each month. A SIP dated the 31st lands on the 28th in
    February and on the 30th in April, which is what the AMC does --
    not the 1st or 3rd of the following month, which is what naive
    date arithmetic produces.
    """
    if mode == "lumpsum":
        return [start] if start <= today else []
    out, dom = [], start.day
    y, m = start.year, start.month
    while True:
        d = date(y, m, min(dom, monthrange(y, m)[1]))
        if d > today:
            break
        out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if len(out) > 1200:                 # a century of monthly SIPs
            break
    return out


def value_holdings(cur, holdings, today=None):
    """Price a list of holdings. Returns a LIST, aligned with the input.

    Aligned by POSITION, not keyed by scheme code. Keying by code looks
    tidier until two entries name the same fund -- then the second one
    silently inherits the first one's valuation, including when the
    second has no dates at all and should have been priced at nothing.
    A list cannot do that: entry i is about holding i, and holdings
    that could not be priced are None.

    holdings: objects or dicts carrying scheme_code, invested_on,
    invest_mode and invest_amount. Anything without all four is skipped
    silently -- an incomplete holding is not an error here, it is a
    holding whose owner has not filled the dates in.
    """
    today = today or date.today()

    def get(h, k):
        return h.get(k) if isinstance(h, dict) else getattr(h, k, None)

    wanted = []                      # (index, code, on, mode, amount, plan)
    for i, h in enumerate(holdings):
        code = get(h, "scheme_code") or get(h, "code")
        on, mode, amt = (get(h, "invested_on"), get(h, "invest_mode"),
                         get(h, "invest_amount"))
        plan = (get(h, "plan") or get(h, "plan_type") or "").upper() or None
        if not (code and on and mode in MODES and amt and float(amt) > 0):
            continue
        if isinstance(on, str):
            try:
                on = date.fromisoformat(on[:10])
            except ValueError:
                continue
        if on > today:
            continue
        wanted.append((i, str(code), on, mode, float(amt), plan))

    out = [None] * len(holdings)
    if not wanted:
        return out

    codes = sorted({w[1] for w in wanted})
    earliest = min(w[2] for w in wanted)

    # ---- which SCHEME, before which NAV series ---------------------
    #
    # Two hops, in this order, and the order matters.
    #
    #   1. the plan. A client holds REGULAR, and Regular carries the
    #      distributor commission inside its NAV. Pricing their money at
    #      Direct overstates it by roughly a point a year.
    #   2. the NAV series for THAT scheme. v_returns_source substitutes
    #      a Growth sibling for an IDCW plan, and it is keyed per scheme
    #      code -- so a Regular IDCW fund reads its REGULAR Growth
    #      sibling, not the Direct one. Doing the hops the other way
    #      round lands on the Direct series and undoes step 1.
    want_plan = {}
    for _, code, _, _, _, plan in wanted:
        if plan:
            want_plan.setdefault(plan, set()).add(code)
    sibling = {}
    for plan, plan_codes in want_plan.items():
        for c, sib in plan_siblings(cur, plan_codes, plan).items():
            sibling[(c, plan)] = sib

    # The scheme each holding is actually priced on, and whether that is
    # the plan it asked for.
    scheme_for = {}
    for i, code, _, _, _, plan in wanted:
        scheme_for[i] = sibling.get((code, plan), code) if plan else code

    all_schemes = sorted(set(scheme_for.values()))
    cur.execute(NAV_SOURCE, {"codes": all_schemes})
    nav_code = {r["scheme_code"] if isinstance(r, dict) else r[0]:
                r["nav_scheme_code"] if isinstance(r, dict) else r[1]
                for r in cur.fetchall()}

    series_codes = sorted({nav_code.get(c, c) for c in all_schemes})
    cur.execute(NAV_SERIES, {"codes": series_codes,
                             "since": earliest - NAV_LOOKBACK})

    # Two parallel lists per fund -- dates and navs -- so a date can be
    # found with a binary search instead of a scan. A sixty-instalment
    # SIP against a ten-year series is sixty lookups either way; one of
    # them is sixty comparisons and the other is a hundred and fifty
    # thousand.
    dates, navs = {}, {}
    for r in cur.fetchall():
        c = r["scheme_code"] if isinstance(r, dict) else r[0]
        d = r["nav_date"] if isinstance(r, dict) else r[1]
        v = r["nav"] if isinstance(r, dict) else r[2]
        dates.setdefault(c, []).append(d)
        navs.setdefault(c, []).append(float(v))

    for i, code, on, mode, amt, plan in wanted:
        scheme = scheme_for[i]
        # Asked for a plan and did not get it. Recorded per holding so
        # the report can mark THAT fund rather than carry a blanket
        # caveat nobody reads -- one fund measured on a different basis
        # from its neighbours is a fact about that fund.
        fell_back = bool(plan) and (code, plan) not in sibling
        src = nav_code.get(scheme, scheme)
        ds, ns = dates.get(src), navs.get(src)
        note = {"scheme_code": scheme, "plan_asked": plan,
                "plan_used": None if not plan
                             else ("DIRECT" if fell_back else plan),
                "plan_fallback": fell_back}
        if not ds:
            out[i] = dict(note, priced=False,
                          why="no NAV history for this fund")
            continue

        units = 0.0
        paid = 0.0
        priced = 0
        unpriced = 0
        first_priced = None
        for d in instalment_dates(on, mode, today):
            # `j`, not `i`. `i` is this holding's position in the result
            # list, and reusing it here overwrote that position with a
            # NAV row number -- which wrote the answer into the wrong
            # slot, or off the end of the list entirely.
            j = bisect_right(ds, d) - 1
            if j < 0:                       # before this fund had a NAV
                unpriced += 1
                continue
            units += amt / ns[j]
            paid += amt
            priced += 1
            if first_priced is None:
                first_priced = ds[j]

        if not priced:
            out[i] = dict(note, priced=False,
                          why="that date is before this fund's NAV history")
            continue

        out[i] = dict(note, **{
            "priced": True,
            "units": round(units, 4),
            "value": round(units * ns[-1], 2),
            "paid": round(paid, 2),
            "nav": round(ns[-1], 4),
            "nav_date": str(ds[-1]),
            "instalments": priced,
            # Not an error, and not hidden either: a SIP that started
            # before the fund did has instalments nobody could have
            # made, and the reader should know the figure covers fewer
            # months than they asked for.
            "unpriced": unpriced,
            "first_priced": str(first_priced) if first_priced else None,
        })
    return out
