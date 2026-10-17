"""
ownership_screens.py -- stock screens on WHO OWNS THE COMPANY, read from the quarterly shareholding pattern
(stock_shareholding, filled by fetch_shareholding.py from NSE's filings). Mounted by screener_api.py.
------------------------------------------------------------------------
WHAT A SCREEN HERE SAYS, AND WHAT IT DOES NOT
    Every company files, within 21 days of each quarter end, the share of its shares held by promoters,
    mutual funds, foreign institutions, individuals and so on. A screen compares one quarter's filing with
    the one before it. "FIIs are buying" therefore means: foreign institutions held a larger SLICE of the
    company at the end of this quarter than at the end of the last. That is the net of everything they
    bought and sold in between, seen once a quarter. It is not a daily flow, it can also move because the
    company issued new shares, and the filing is up to three months old by the time it is read. Each screen
    says so in its own words, and none of them says buy.

TWO RULES THAT KEEP IT TRUE
    1. A QUARTER IS COMPARED WITH THE QUARTER BEFORE IT, NEVER WITH "THE PREVIOUS ROW". A company that missed
       a filing would otherwise be compared with a year ago and its change would be four times too big.
       The chain of quarter-end dates is built from the calendar, and a company with no row for the earlier
       quarter is left out, not guessed at.
    2. A COMPANY THAT HAS NOT FILED YET HAS NOT CHANGED ITS HOLDERS. Filings arrive over three weeks after
       each quarter end, so the newest quarter is always partly filed. The quarter offered by default is the
       newest one that at least 60% of the companies have filed; a newer, partial one can be chosen and is
       labelled as such with how many have filed.

DATA THE SCREENS NEED
    The breakdown (foreign institutions, mutual funds, individuals, pledge) is read from each filing's XBRL
    by fetch_shareholding.py --detail, quarter by quarter. A quarter is only offered when it and the
    quarter before it (more, for the "in a row" screen) have the breakdown for at least MIN_FILINGS
    companies. The promoter holding comes with the list itself, so promoter screens work wherever filings
    exist. If the history is not loaded, a screen says so instead of showing an empty list that looks like
    a quiet market.

PERCENTAGE POINTS, NOT PER CENT
    Foreign institutions going from 8.0% to 8.5% of the company is 0.5 points, whatever the price did.
    Every threshold and every change on screen is in points.
"""

import calendar
from datetime import date, timedelta

MIN_FILINGS = 100            # a quarter needs this many usable filings to be offered at all
DEFAULT_SHARE = 0.6          # the default quarter is the newest one this fraction of the fullest has filed
QUARTERS_OFFERED = 6
QUIET_PRICE_BAND = 10.0      # "the price barely moved": within this many per cent over the quarter
PRICE_STALE_DAYS = 10        # a close further than this from the quarter end is not that quarter's price
HISTORY_DAYS = 1200
MAX_ROWS = 200


# =====================================================================
# THE CATALOGUE (read by the page, so the wording on screen and the rule here cannot drift apart)
# =====================================================================
def _points(default, label="Moved by at least (points)", lo=0.1, hi=10.0):
    return {"param": "points", "label": label, "kind": "number", "default": default, "min": lo, "max": hi, "step": 0.1}


_HOLDERS = [["fii", "Foreign institutions (FIIs)"], ["mf", "Mutual funds"], ["promoter", "Promoters"]]
_CAVEAT = ("A holding is the net of everything bought and sold in the quarter, seen once a quarter, and it can "
           "also move because the company issued new shares. ")

SCREENS = [
    {
        "key": "own_fii_buying", "group": "own", "name": "FIIs are buying",
        "what": "Companies where foreign institutions held a larger share of the company at the end of the "
                "latest quarter than at the end of the one before.",
        "why": _CAVEAT + "Read from the shareholding pattern each company files, so it is a quarter old at "
               "best. Foreign institutions here means foreign portfolio investors and other foreign institutions.",
        "controls": [_points(0.5)], "columns": ["fii", "mf", "promoter"],
    },
    {
        "key": "own_fii_leaving", "group": "own", "name": "FIIs are leaving",
        "what": "Companies where foreign institutions held a smaller share of the company at the end of the "
                "latest quarter than at the end of the one before.",
        "why": _CAVEAT + "A smaller share can mean they sold, or that the company created new shares that "
               "diluted every holder.",
        "controls": [_points(0.5)], "columns": ["fii", "mf", "promoter"],
    },
    {
        "key": "own_mf_buying", "group": "own", "name": "Mutual funds are buying",
        "what": "Companies where mutual funds together held a larger share of the company at the end of the "
                "latest quarter than at the end of the one before.",
        "why": _CAVEAT + "This is every mutual fund added up, once a quarter. For fund-by-fund share counts "
               "every month, see “Funds are buying” under Fund flows.",
        "controls": [_points(0.5)], "columns": ["mf", "fii", "promoter"],
    },
    {
        "key": "own_mf_leaving", "group": "own", "name": "Mutual funds are leaving",
        "what": "Companies where mutual funds together held a smaller share of the company at the end of the "
                "latest quarter than at the end of the one before.",
        "why": _CAVEAT + "For which funds sold, and by how many shares, see “Funds are leaving” under "
               "Fund flows.",
        "controls": [_points(0.5)], "columns": ["mf", "fii", "promoter"],
    },
    {
        "key": "own_both_buying", "group": "own", "name": "FIIs and mutual funds both buying",
        "what": "Companies where foreign institutions AND mutual funds each held a larger share at the end of "
                "the latest quarter than at the end of the one before.",
        "why": "Two very different kinds of investor moving the same way is rarer than either alone. " + _CAVEAT,
        "controls": [_points(0.25, "Each moved by at least (points)")], "columns": ["fii", "mf", "promoter"],
    },
    {
        "key": "own_both_leaving", "group": "own", "name": "FIIs and mutual funds both leaving",
        "what": "Companies where foreign institutions AND mutual funds each held a smaller share at the end of "
                "the latest quarter than at the end of the one before.",
        "why": "The other side of the same screen. " + _CAVEAT,
        "controls": [_points(0.25, "Each moved by at least (points)")], "columns": ["fii", "mf", "promoter"],
    },
    {
        "key": "own_quiet", "group": "own", "name": "Institutions bought, price stood still",
        "what": "Institutions (foreign and domestic together) held a larger share at the end of the latest "
                "quarter, while the share price moved no more than %d%% either way over the same quarter."
                % QUIET_PRICE_BAND,
        "why": "Buying that the chart has not reacted to. It needs both halves of the database: the filing says "
               "who bought, the candles say the price did not move. A company with a split or bonus in the "
               "quarter is left out because its stored prices cannot be compared. " + _CAVEAT,
        "controls": [_points(1.0, "Institutions up by at least (points)")], "columns": ["institutions", "fii", "mf"],
    },
    {
        "key": "own_steady", "group": "own", "name": "Buying quarter after quarter",
        "what": "Companies where the chosen holder's share rose in every one of the last few quarters, not "
                "just the latest.",
        "why": "One quarter's rise can be a one-off; a rise every quarter is a habit. It needs several quarters "
               "of filings with the breakdown loaded, and says so when they are not. " + _CAVEAT,
        "controls": [{"param": "holder", "label": "Who", "kind": "choice", "default": "fii", "options": _HOLDERS},
                     {"param": "streak", "label": "Rising in each of the last", "kind": "choice", "default": 3,
                      "options": [[2, "2 quarters"], [3, "3 quarters"], [4, "4 quarters"]]},
                     _points(0.1, "Rise each quarter of at least (points)", 0.05, 5.0)],
        "columns": ["holder"],
    },
    {
        "key": "own_promoter_buying", "group": "own", "name": "Promoters are buying",
        "what": "Companies where the promoter group held a larger share of the company at the end of the latest "
                "quarter than at the end of the one before.",
        "why": "The people who control the company increasing what they own. A promoter share can also rise "
               "when the company buys back its own shares, which shrinks the total. " + _CAVEAT,
        "controls": [_points(0.25)], "columns": ["promoter", "pledged", "institutions"],
    },
    {
        "key": "own_promoter_selling", "group": "own", "name": "Promoters are reducing",
        "what": "Companies where the promoter group held a smaller share of the company at the end of the latest "
                "quarter than at the end of the one before.",
        "why": "A smaller promoter share means a sale (a block deal, an offer for sale), or new shares issued to "
               "others that diluted them. The filing does not say which. " + _CAVEAT,
        "controls": [_points(0.5)], "columns": ["promoter", "pledged", "institutions"],
    },
    {
        "key": "own_pledge", "group": "own", "name": "Promoter shares pledged",
        "what": "Companies where the promoter group has pledged, or otherwise encumbered, at least this share of "
                "its own holding.",
        "why": "A pledge is a promoter borrowing against their shares. It matters when the price falls, because the "
               "lender can then sell them. The figure is as the filing totals it: pledges, non-disposal "
               "undertakings and other encumbrances. A pledge of 0 means the filing says none. It is the share of "
               "the promoters' own holding, not of the whole company.",
        "controls": [{"param": "pledge", "label": "Pledged at least (%)", "kind": "number", "default": 25,
                      "min": 1, "max": 100, "step": 1}],
        "columns": ["pledged", "promoter", "institutions"],
    },
    {
        "key": "own_inst_in", "group": "own", "name": "Institutions buying, individuals selling",
        "what": "Companies where institutions (foreign and domestic together) held a larger share and individual "
                "investors a smaller one, at the end of the latest quarter against the one before.",
        "why": "Shares moving from one kind of holder to another. The two always net out, so this shows who "
               "ended up with them. " + _CAVEAT,
        "controls": [_points(0.5, "Each moved by at least (points)")], "columns": ["institutions", "individuals", "promoter"],
    },
    {
        "key": "own_retail_in", "group": "own", "name": "Individuals buying, institutions selling",
        "what": "Companies where individual investors held a larger share and institutions (foreign and "
                "domestic together) a smaller one, at the end of the latest quarter against the one before.",
        "why": "The other side of the same screen. " + _CAVEAT,
        "controls": [_points(0.5, "Each moved by at least (points)")], "columns": ["individuals", "institutions", "promoter"],
    },
]
SCREEN_KEYS = {s["key"] for s in SCREENS}
_BY_KEY = {s["key"]: s for s in SCREENS}

MEASURE_LABEL = {
    "fii": "Foreign institutions", "mf": "Mutual funds", "promoter": "Promoters", "individuals": "Individuals",
    "institutions": "All institutions", "pledged": "Promoter shares pledged",
}
# What each measure needs. The promoter share comes with the list of filings; the rest needs the XBRL breakdown.
NEEDS_DETAIL = {"fii": True, "mf": True, "promoter": False, "individuals": True, "institutions": True, "pledged": True}

# The measures a screen's RULE reads. A company missing one of these is left out (and not counted as considered);
# the other columns on the row are context and may be blank.
RULE_READS = {
    "own_fii_buying": ["fii"], "own_fii_leaving": ["fii"], "own_mf_buying": ["mf"], "own_mf_leaving": ["mf"],
    "own_both_buying": ["fii", "mf"], "own_both_leaving": ["fii", "mf"], "own_quiet": ["institutions"],
    "own_promoter_buying": ["promoter"], "own_promoter_selling": ["promoter"], "own_pledge": ["pledged"],
    "own_inst_in": ["institutions", "individuals"], "own_retail_in": ["institutions", "individuals"],
}


# =====================================================================
# DATES
# =====================================================================
def is_quarter_end(d):
    return d.month in (3, 6, 9, 12) and d.day == calendar.monthrange(d.year, d.month)[1]


def prev_quarter_end(d):
    y, m = d.year, d.month - 3
    if m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, calendar.monthrange(y, m)[1])


def quarter_chain(q, back):
    """[q, the quarter before it, ...] with `back` earlier quarters."""
    out = [q]
    for _ in range(back):
        out.append(prev_quarter_end(out[-1]))
    return out


def quarter_label(d):
    fq = {6: 1, 9: 2, 12: 3, 3: 4}[d.month]
    fy = (d.year + 1 if d.month in (6, 9, 12) else d.year) % 100
    return "Q%d FY%02d" % (fq, fy)


# =====================================================================
# WHICH QUARTERS CAN BE COMPARED
# =====================================================================
QUARTERS_SQL = """
SELECT quarter_end, count(*) AS filed, count(*) FILTER (WHERE detail_loaded) AS detailed
FROM stock_shareholding
WHERE quarter_end >= %(since)s
GROUP BY quarter_end
"""


def offered_quarters(rows, need_detail, back):
    """(list of quarters that can be screened, the default one).
    A quarter qualifies when it AND the `back` quarters before it each have at least MIN_FILINGS usable
    filings. The default is the newest qualifying quarter that at least DEFAULT_SHARE of the fullest
    qualifying quarter has filed: the newest quarter is always partly filed, and a screen over half the
    market would look like a finding about those stocks."""
    have = {r["quarter_end"]: int(r["detailed"] if need_detail else r["filed"]) for r in rows if is_quarter_end(r["quarter_end"])}
    ok = [q for q in sorted(have, reverse=True) if all(have.get(c, 0) >= MIN_FILINGS for c in quarter_chain(q, back))]
    if not ok:
        return [], None
    # "full" is measured against the fullest quarter of all, not just the qualifying ones: when the only quarter
    # that qualifies is the newest, partly filed one, it must still be labelled partial.
    fullest = max(have.values())
    default = next((q for q in ok if have[q] >= DEFAULT_SHARE * fullest), ok[0])
    return [{"quarter_end": q, "label": quarter_label(q), "filed": have[q], "default": q == default,
             "partial": have[q] < DEFAULT_SHARE * fullest} for q in ok[:QUARTERS_OFFERED]], default


# =====================================================================
# THE MEASURES
# =====================================================================
def _f(v):
    return None if v is None else float(v)


def measure(row, key):
    """One holder's share of the company (0-100) in one filing, or None when the filing does not give it."""
    if row is None:
        return None
    if key == "promoter":
        return _f(row["promoter_pct"])
    if not row["detail_loaded"]:
        return None
    if key == "fii":
        return _f(row["fii_pct"])
    if key == "mf":
        return _f(row["mf_pct"])
    if key == "individuals":
        return _f(row["retail_pct"])
    if key == "pledged":
        return _f(row["promoter_pledged_pct"])
    if key == "institutions":
        parts = [_f(row[c]) for c in ("fii_pct", "mf_pct", "insurance_pct", "bank_pct", "other_dii_pct")]
        return None if any(p is None for p in parts) else sum(parts)
    raise KeyError(key)


def _move(now, before):
    return None if now is None or before is None else round(now - before, 4)


ROWS_SQL = """
SELECT isin, quarter_end, promoter_pct, mf_pct, insurance_pct, bank_pct, other_dii_pct, fii_pct, retail_pct,
       bodies_corporate_pct, nri_pct, other_public_pct, employee_trust_pct,
       promoter_pledged_pct, detail_loaded, revised
FROM stock_shareholding
WHERE quarter_end = ANY(%(quarters)s)
"""

# ---------------------------------------------------------------------------------------------------------
# RE-CATEGORISATION: THE ONE WAY THESE NUMBERS LIE
#   Companies decide which box a holder goes in, and they do not always keep to it. Checked on real filings
#   (9 Oct 2026): ICICI Bank's ADR holders sat under "other non-institutions" (18.2%) in the March filing and in
#   foreign institutions in June, so foreign institutions "rose 15 points"; RESPONIND, CLEANMAX and PPLPHARMA
#   each moved a foreign company between "foreign institution" and "foreign company"; Sammaan Capital's
#   Gulf investor moved from foreign institution to promoter. Nobody traded. Shown raw, these head every
#   "FIIs are buying" and "FIIs are leaving" list.
#   The signature is that ONE other box moves the opposite way by about the same amount, whereas real buying
#   and selling is spread over several holders. Two cases are set aside, for a move of RECLASS_MIN points or more:
#     * the opposite move is in a CATCH-ALL box ("others", foreign companies, bodies corporate, NRIs, trusts) and
#       matches it in size, between RECLASS_SHARE and RECLASS_SHARE_MAX times it: holders do not trade into a
#       box called "other". The upper bound matters: when the other box moved far MORE than the measure (Lenskart,
#       Pine Labs and Groww, new listings whose pre-IPO holders were in "others") several boxes gained, which is
#       an IPO and not a re-labelling;
#     * it is a PURE SWAP between two named boxes: the opposite box moved by at least RECLASS_SWAP of it and
#       everything else together moved by no more than RECLASS_REST of it (Sammaan Capital: foreign
#       institution 46.2% -> 19.6%, promoter +28.3, nothing else changed).
#   A first version also set aside any move matched by the PROMOTER box, and that removed real events: the
#   Pine Labs, Lenskart and Gujarat Energy IPOs (promoters sold, mutual funds and foreign institutions bought,
#   several boxes moved) and Emirates NBD's takeover of RBL Bank. Those are spread over many holders and stay.
#   The screen says how many it set aside, so nothing disappears silently.
RECLASS_MIN = 2.0
RECLASS_SHARE = 0.8
RECLASS_SHARE_MAX = 1.25
RECLASS_SWAP = 0.95
RECLASS_REST = 0.15
CATCH_ALL_COLS = ("other_public_pct", "bodies_corporate_pct", "nri_pct", "employee_trust_pct")
BUCKET_COLS = ("promoter_pct", "mf_pct", "insurance_pct", "bank_pct", "other_dii_pct", "fii_pct", "retail_pct",
               "bodies_corporate_pct", "nri_pct", "other_public_pct", "employee_trust_pct")
MEASURE_COLS = {"promoter": ("promoter_pct",), "fii": ("fii_pct",), "mf": ("mf_pct",), "individuals": ("retail_pct",),
                "institutions": ("fii_pct", "mf_pct", "insurance_pct", "bank_pct", "other_dii_pct")}


def looks_reclassified(now_row, prev_row, key):
    """True when the move of measure `key` between two filings looks like holders being re-categorised rather
    than trading. Needs the full breakdown in both filings; when it is missing nothing can be said, so False."""
    if now_row is None or prev_row is None or key not in MEASURE_COLS:
        return False
    if not (now_row["detail_loaded"] and prev_row["detail_loaded"]):
        return False
    own_cols = MEASURE_COLS[key]
    try:
        delta = sum(float(now_row[c]) - float(prev_row[c]) for c in own_cols)
    except (TypeError, ValueError, KeyError):
        return False
    if abs(delta) < RECLASS_MIN:
        return False
    moves = {}
    for c in BUCKET_COLS:
        if c in own_cols or now_row.get(c) is None or prev_row.get(c) is None:
            continue
        moves[c] = float(now_row[c]) - float(prev_row[c])
    for c, d in moves.items():
        if d * delta >= 0:
            continue
        if c in CATCH_ALL_COLS and RECLASS_SHARE * abs(delta) <= abs(d) <= RECLASS_SHARE_MAX * abs(delta):
            return True
        rest = sum(abs(x) for k, x in moves.items() if k != c)
        if RECLASS_SWAP * abs(delta) <= abs(d) <= RECLASS_SHARE_MAX * abs(delta) and rest <= RECLASS_REST * abs(delta):
            return True
    return False


def _q(cur, sql, params=None):
    cur.execute(sql, params or {})
    return cur.fetchall()


# =====================================================================
# PRICE OVER THE QUARTER (for the screen that needs it, and shown beside every row)
# =====================================================================
PRICE_SQL = """
SELECT i.isin,
       (SELECT close FROM stock_ohlc_daily a WHERE a.isin = i.isin AND a.as_of_date <= %(now)s
        ORDER BY a.as_of_date DESC LIMIT 1) AS close_now,
       (SELECT as_of_date FROM stock_ohlc_daily a WHERE a.isin = i.isin AND a.as_of_date <= %(now)s
        ORDER BY a.as_of_date DESC LIMIT 1) AS date_now,
       (SELECT close FROM stock_ohlc_daily b WHERE b.isin = i.isin AND b.as_of_date <= %(prev)s
        ORDER BY b.as_of_date DESC LIMIT 1) AS close_prev,
       (SELECT as_of_date FROM stock_ohlc_daily b WHERE b.isin = i.isin AND b.as_of_date <= %(prev)s
        ORDER BY b.as_of_date DESC LIMIT 1) AS date_prev
FROM unnest(%(isins)s::text[]) AS i(isin)
"""

CORP_ACTION_SQL = """
SELECT DISTINCT isin FROM stock_event
WHERE isin = ANY(%(isins)s) AND kind IN ('split', 'bonus')
  AND event_date > %(prev)s AND event_date <= %(now)s
"""


def price_moves(cur, isins, now, prev):
    """{isin: per cent move of the close from the quarter before to this quarter end}. Left out when a price
    is missing or further than PRICE_STALE_DAYS from the quarter end, and when the company had a split or
    bonus in the quarter: stored prices are a mix of adjusted and unadjusted, so that move cannot be trusted."""
    isins = list(isins)
    if not isins:
        return {}
    skip = {r["isin"] for r in _q(cur, CORP_ACTION_SQL, {"isins": isins, "prev": prev, "now": now})}
    out = {}
    for r in _q(cur, PRICE_SQL, {"isins": isins, "now": now, "prev": prev}):
        if r["isin"] in skip or not r["close_now"] or not r["close_prev"]:
            continue
        if (now - r["date_now"]).days > PRICE_STALE_DAYS or (prev - r["date_prev"]).days > PRICE_STALE_DAYS:
            continue
        out[r["isin"]] = round((float(r["close_now"]) / float(r["close_prev"]) - 1) * 100, 1)
    return out


# =====================================================================
# THE RULES
# =====================================================================
def evaluate(key, params, m, trail=None):
    """Does one company pass screen `key`, and how strongly?
    m: {measure: (now, before)} for what this screen reads; trail: the oldest-first chain for the steady screen.
    Returns (passes, rank), larger rank = stronger. Pure: no database, so every rule can be tested alone."""
    pts = params.get("points")
    eps = 1e-9

    def mv(k):
        now, before = m[k]
        return _move(now, before)

    if key in ("own_fii_buying", "own_fii_leaving", "own_mf_buying", "own_mf_leaving",
               "own_promoter_buying", "own_promoter_selling"):
        k = key.split("_")[1]                                  # fii | mf | promoter
        sign = 1 if key.endswith("buying") else -1
        c = mv(k)
        return (c is not None and sign * c >= pts - eps), (sign * c if c is not None else 0)
    if key in ("own_both_buying", "own_both_leaving"):
        sign = 1 if key.endswith("buying") else -1
        a, b = mv("fii"), mv("mf")
        if a is None or b is None:
            return False, 0
        return (sign * a >= pts - eps and sign * b >= pts - eps), sign * (a + b)
    if key == "own_quiet":
        c = mv("institutions")
        return (c is not None and c >= pts - eps), (c or 0)
    if key in ("own_inst_in", "own_retail_in"):
        inst, ind = mv("institutions"), mv("individuals")
        if inst is None or ind is None:
            return False, 0
        s = 1 if key == "own_inst_in" else -1
        return (s * inst >= pts - eps and -s * ind >= pts - eps), s * inst - s * ind
    if key == "own_pledge":
        now = m["pledged"][0]
        return (now is not None and now >= params["pledge"] - eps), (now or 0)
    if key == "own_steady":
        vals = trail or []
        if len(vals) < 2 or any(v is None for v in vals):
            return False, 0
        steps = [round(vals[i + 1] - vals[i], 4) for i in range(len(vals) - 1)]
        return all(s >= pts - eps for s in steps), round(vals[-1] - vals[0], 4)
    raise KeyError(key)


def clean_params(spec, raw):
    """The reader's controls, clamped to what the screen allows; anything unusable becomes the default."""
    out = {}
    for c in spec["controls"]:
        v = raw.get(c["param"])
        if c["kind"] == "number":
            try:
                v = float(v)
                if v != v:                                     # NaN
                    raise ValueError
            except (TypeError, ValueError):
                v = c["default"]
            out[c["param"]] = max(c["min"], min(c["max"], v))
        else:
            allowed = [o[0] for o in c["options"]]
            try:
                v = type(c["default"])(v)
            except (TypeError, ValueError):
                v = c["default"]
            out[c["param"]] = v if v in allowed else c["default"]
    return out


# =====================================================================
# RUNNING ONE
# =====================================================================
def run_screen(cur, key, raw_params, cap, quarter, limit, meta_fn, attach_fn, today=None):
    """The whole answer for one ownership screen. meta_fn(cur, isins, when) -> {isin: meta};
    attach_fn(cur, rows) adds each row's latest price. Both come from screener_api."""
    spec = _BY_KEY[key]
    params = clean_params(spec, raw_params)
    today = today or date.today()
    limit = max(1, min(int(limit), MAX_ROWS))

    steady = key == "own_steady"
    streak = int(params["streak"]) if steady else 1
    holder = params["holder"] if steady else None
    cols = [holder] if steady else list(spec["columns"])
    rule = [holder] if steady else RULE_READS[key]
    read = list(dict.fromkeys(cols + rule))
    columns = [{"key": c, "label": MEASURE_LABEL[c]} for c in cols]
    base = {"screen": key, "columns": columns, "params": params, "as_of": str(today)}

    quarters_rows = _q(cur, QUARTERS_SQL, {"since": today - timedelta(days=HISTORY_DAYS)})
    offered, default = offered_quarters(quarters_rows, any(NEEDS_DETAIL[k] for k in rule), streak)
    if not offered:
        why = ("The breakdown of who holds what has not been read for %d quarters in a row yet." % (streak + 1)
               if steady else "The breakdown of who holds what has not been read for two quarters in a row yet.")
        return {**base, "rows": [], "total": 0, "considered": 0, "quarter": None, "compared_with": None,
                "quarters": [], "unavailable": why}
    q = quarter if quarter in {o["quarter_end"] for o in offered} else default
    chain = quarter_chain(q, streak)
    now_q, prev_q = chain[0], chain[1]

    by = {}
    for r in _q(cur, ROWS_SQL, {"quarters": chain}):
        by.setdefault(r["isin"], {})[r["quarter_end"]] = r

    hits, considered, reclassified = [], 0, 0
    for isin, rows in by.items():
        now_row = rows.get(now_q)
        if now_row is None:
            continue
        if steady:
            trail = [measure(rows.get(c), holder) for c in reversed(chain)]            # oldest first
            if any(v is None for v in trail):
                continue
            considered += 1
            ok, rank = evaluate(key, params, {}, trail)
            if ok:
                # a re-categorisation in ANY step would fake a rise; one is enough to set the company aside
                if any(looks_reclassified(rows.get(chain[i]), rows.get(chain[i + 1]), holder) for i in range(streak)):
                    reclassified += 1
                    continue
                hits.append((isin, rank, {holder: (trail[-1], trail[-2])}, trail, now_row))
            continue
        m = {k: (measure(now_row, k), measure(rows.get(prev_q), k)) for k in read}
        # the rule needs its measures in both quarters (a pledge needs only the latest); the rest is context
        if any(m[k][0] is None or (key != "own_pledge" and m[k][1] is None) for k in rule):
            continue
        considered += 1
        ok, rank = evaluate(key, params, m)
        if ok:
            if key != "own_pledge" and any(looks_reclassified(now_row, rows.get(prev_q), k) for k in rule):
                reclassified += 1
                continue
            hits.append((isin, rank, m, None, now_row))

    if key == "own_quiet":
        moves = price_moves(cur, [h[0] for h in hits], now_q, prev_q)
        hits = [h for h in hits if h[0] in moves and abs(moves[h[0]]) <= QUIET_PRICE_BAND]

    meta = meta_fn(cur, [h[0] for h in hits], now_q)
    rows_out = []
    for isin, rank, m, trail, now_row in hits:
        mt = meta.get(isin)
        if not mt or not mt.get("company_name"):
            continue                                              # not an equity we know
        if cap and mt.get("cap_class") != cap:
            continue
        values = {k: {"now": None if v[0] is None else round(v[0], 2),
                      "before": None if v[1] is None else round(v[1], 2),
                      "change": None if _move(*v) is None else round(_move(*v), 2)}
                  for k, v in m.items() if k in cols}
        rows_out.append({"isin": isin, "symbol": mt.get("symbol"), "name": mt.get("company_name"),
                         "sector": mt.get("sector"), "cap": mt.get("cap_class"), "values": values, "rank": rank,
                         "trail": [round(v, 2) for v in trail] if trail else None,
                         "revised": bool(now_row["revised"]), "no_promoter": _f(now_row["promoter_pct"]) == 0})
    rows_out.sort(key=lambda r: (-r["rank"], r["name"] or ""))
    total = len(rows_out)
    rows_out = rows_out[:limit]
    # the price over the quarter beside every row, where it can be trusted
    shown = price_moves(cur, [r["isin"] for r in rows_out], now_q, prev_q)
    for r in rows_out:
        r["price_move"] = shown.get(r["isin"])
    attach_fn(cur, rows_out)
    chosen = next(o for o in offered if o["quarter_end"] == q)
    return {**base, "rows": rows_out, "total": total, "considered": considered, "reclassified": reclassified,
            "quarter": str(now_q), "quarter_label": quarter_label(now_q), "compared_with": str(prev_q),
            "compared_label": quarter_label(prev_q), "filed": chosen["filed"], "partial": chosen["partial"],
            "quarters": [{**o, "quarter_end": str(o["quarter_end"])} for o in offered]}
