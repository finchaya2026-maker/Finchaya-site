"""
brokers.py -- hand a finished plan over to wherever the person invests.
=======================================================================
Mounted in api.py:

    from brokers import router as brokers_router
    app.include_router(brokers_router)

WHAT THIS IS, AND THE THING IT DELIBERATELY IS NOT
    It is a set of OUTBOUND LINKS. Nothing here places an order, holds a
    credential, or talks to any platform's API.

    That is not timidity, it is the state of the market. As of September
    2026 no Indian platform accepts a basket of mutual funds pushed in
    from outside. smallcase solved exactly this for equities -- their
    Gateway pushes a basket of stocks into a user's existing broker
    account -- and for mutual funds they offer only holdings IMPORT, for
    analytics. Zerodha withdrew mutual fund order placement from Kite
    Connect in 2020 ("order placement needs payment from the user's bank
    account") and their docs still say it plainly. Groww, Kuvera, Paytm
    Money, Angel One and INDmoney have no third-party MF order API at
    all. MF Central's read API is being closed to third parties on AMFI's
    instruction.

    The only real "stage a basket, investor approves it" rail is MFU's
    TransactEezz, and it requires the investor to hold an MFU CAN and to
    be OUR client under our intermediary code -- which is the opposite of
    "use the platform you already have".

    So: links. They are worth building anyway, because the alternative is
    a person reading four fund names off a screen and typing them into a
    search box one at a time, getting one wrong, and buying the Regular
    plan of the third one.

NO REFERRAL CODES. NOT ONE. NOT EVER, WITHOUT ADVICE.
    Every URL built here is the plain public address of the fund's page.
    No affiliate parameters, no tracking ids, no partner tags, and
    FinChaya earns nothing when somebody follows one.

    This is a compliance boundary, not a design preference. SEBI's RIA
    FAQs (August 2025, FAQ 34) require an investment adviser's
    implementation services to be through direct plans only and with "no
    consideration including any commission or referral fees, whether
    embedded or indirect or otherwise, by whatever name called ...
    directly or indirectly" -- and the client must be under no obligation
    to use them. Groww's affiliate programme pays up to Rs 405 on a first
    mutual fund transaction. Taking that money is precisely the thing
    that closes the RIA door.

    If a referral parameter is ever added here, it should be because a
    compliance adviser said in writing that it may be, and the entity
    doing the earning holds the registration that permits it.

TWO KINDS OF LINK, AND THE PAGE MUST SAY WHICH
    EXACT   the URL addresses that one scheme, keyed on its ISIN, which
            AMFI publishes and we already store. Verified on Zerodha Coin
            and Paytm Money.
    SEARCH  the platform has no address we can construct reliably --
            Groww's URLs carry frozen pre-rename slugs (Parag Parikh
            Flexi Cap still lives at .../parag-parikh-long-term-value-
            fund-direct-growth), and Kuvera keys on an RTA scheme code.
            Constructing those needs a scraped mapping that would rot
            silently. So we send the person to the platform with the
            fund's name and ISIN to hand, and say so.

    Presenting a search link as though it were the fund's page is how
    somebody ends up on the wrong scheme. The distinction is returned on
    every row for the page to show.
"""

from typing import List, Optional

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from portfolio_api import DB, _q

router = APIRouter(prefix="/api/handoff", tags=["handoff"])

MAX_FUNDS = 20


# =====================================================================
# THE PLATFORMS
# =====================================================================
# `featured` decides the buttons; the rest sit behind "another platform".
# Ordered by how many Indian investors are likely to be on them, not by
# how good the link is -- a reader looking for their own platform scans
# for its name, and burying Groww because its link is weaker would be
# optimising for us rather than for them.
#
# `mode` is the honest half:
#   exact   -> `url` is the fund's own page, built from its ISIN
#   search  -> `home` is where to look, and the row carries the name and
#              ISIN to paste in
#   none    -> no public web route at all; listed so a reader on it knows
#              they have not been forgotten
PLATFORMS = [
    {
        "key": "coin", "name": "Zerodha Coin", "featured": True,
        "mode": "exact",
        # Verified live on two unrelated schemes. Uppercase ISIN.
        "url": "https://coin.zerodha.com/mf/fund/{ISIN}",
        "note": "Opens the fund's own page on Coin.",
    },
    {
        "key": "groww", "name": "Groww", "featured": True,
        "mode": "search",
        "home": "https://groww.in/mutual-funds",
        # The reason, stated, because "why can't you link straight to it"
        # is the obvious question and the answer is interesting.
        "note": "Groww's fund addresses use each scheme's OLD name, from "
                "before it was renamed, so they cannot be worked out from "
                "the name we hold. Search for it there with the name below.",
    },
    {
        "key": "paytm", "name": "Paytm Money", "featured": True,
        "mode": "exact",
        # The slug segment is ignored -- only the lowercase ISIN at the
        # end is read. Verified: a nonsense slug still resolves.
        "url": "https://www.paytmmoney.com/mutual-funds/scheme/fund/{isin}",
        "note": "Opens the fund's own page on Paytm Money.",
    },
    {
        "key": "kuvera", "name": "Kuvera", "featured": True,
        "mode": "search",
        "home": "https://kuvera.in/explore",
        "note": "Kuvera addresses funds by an RTA code rather than by "
                "ISIN, so the exact page cannot be constructed. Search "
                "for it there with the name below.",
    },
    {
        "key": "indmoney", "name": "INDmoney", "featured": False,
        "mode": "search", "home": "https://www.indmoney.com/mutual-funds",
        "note": "Search for the fund by name.",
    },
    {
        "key": "angelone", "name": "Angel One", "featured": False,
        "mode": "search", "home": "https://www.angelone.in/mutual-funds",
        "note": "Search for the fund by name.",
    },
    {
        "key": "etmoney", "name": "ET Money", "featured": False,
        "mode": "search", "home": "https://www.etmoney.com/mutual-funds",
        "note": "Search for the fund by name.",
    },
    {
        "key": "mfcentral", "name": "MF Central", "featured": False,
        "mode": "none",
        "home": "https://www.mfcentral.com/",
        "note": "MF Central is the registrars' own platform and has no "
                "public page per fund — sign in there and search for "
                "each one. Its ISIN below is what identifies it exactly.",
    },
    {
        "key": "amc", "name": "The fund house's own site", "featured": False,
        "mode": "none", "home": None,
        "note": "Every AMC sells its own direct plans. Search for the "
                "fund house by name; the ISIN below identifies the exact "
                "plan and option.",
    },
]

FEATURED = [p["key"] for p in PLATFORMS if p["featured"]]


# ISIN and name for a list of scheme codes.
#
# v_fund_canonical, not mf_scheme directly: the rest of the site addresses
# funds by canonical code and a plan's codes are canonical. Joining the
# other way round would miss every fund whose plan variants were merged.
FUND_ISINS = """
SELECT c.canonical_scheme_code AS code,
       c.scheme_name,
       c.amc_name,
       MAX(s.scheme_isin) AS scheme_isin
FROM v_fund_canonical c
JOIN mf_scheme s ON s.scheme_name = c.scheme_name
                AND s.amc_name IS NOT DISTINCT FROM c.amc_name
WHERE c.canonical_scheme_code = ANY(%(codes)s)
GROUP BY 1, 2, 3
"""


class HandoffFund(BaseModel):
    scheme_code: str
    pct: Optional[float] = None
    amount: Optional[float] = None


class HandoffRequest(BaseModel):
    funds: List[HandoffFund]


@router.get("/platforms")
def platforms():
    """The catalogue, so the page never hard-codes a platform's name."""
    return {
        "platforms": [{k: v for k, v in p.items() if k != "url"}
                      for p in PLATFORMS],
        "featured": FEATURED,
        # Said once, here, so every page that shows these carries it.
        "disclosure": "These are plain links to each platform's own pages. "
                      "FinChaya is paid nothing by any of them, earns no "
                      "commission or referral fee on anything bought "
                      "through them, and does not track whether you use "
                      "them. They are listed alphabetically by how widely "
                      "they are used, not by any arrangement.",
    }


@router.post("/links")
def links(body: HandoffRequest):
    """Per fund, per platform: where to go, and how exact it is.

    Open to anyone signed in or not. It exposes nothing that is not
    already public -- an ISIN is published by AMFI in a free file -- and
    a paywall on "here is where to buy the thing you have chosen" would
    be charging for the exit.
    """
    if not body.funds:
        raise HTTPException(400, "No funds to hand over.")
    if len(body.funds) > MAX_FUNDS:
        raise HTTPException(400, "That is more than %d funds." % MAX_FUNDS)

    codes = [str(f.scheme_code) for f in body.funds]
    want = {str(f.scheme_code): f for f in body.funds}

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        rows = {str(r["code"]): r for r in _q(cur, FUND_ISINS, {"codes": codes})}

    out = []
    for code in codes:
        r = rows.get(code)
        isin = (r or {}).get("scheme_isin")
        f = want[code]
        per = {}
        for p in PLATFORMS:
            if p["mode"] == "exact" and isin:
                per[p["key"]] = {
                    "mode": "exact",
                    "url": p["url"].replace("{ISIN}", isin.upper())
                                   .replace("{isin}", isin.lower()),
                }
            else:
                # An exact-mode platform with no ISIN on file falls back
                # to search rather than to a broken link. Better to say
                # "look it up" than to send somebody to a 404 and let
                # them conclude the fund does not exist there.
                per[p["key"]] = {"mode": "search" if p["mode"] != "none"
                                 else "none",
                                 "url": p.get("home")}
        out.append({
            "scheme_code": code,
            "name": (r or {}).get("scheme_name") or code,
            "amc": (r or {}).get("amc_name"),
            "isin": isin,
            "pct": f.pct,
            "amount": f.amount,
            "links": per,
        })

    missing = [o["name"] for o in out if not o["isin"]]
    return {
        "funds": out,
        # Named, not silently downgraded. A fund with no ISIN on file is
        # a gap in what has reached us, and the page should say which one
        # rather than quietly offering a weaker link for it.
        "no_isin": missing,
    }
