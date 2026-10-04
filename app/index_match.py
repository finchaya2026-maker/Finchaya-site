"""
index_match.py -- pair an index with a fund that actually tracks it
----------------------------------------------------------------------
Imported by check_index_weights.py and by the builder that follows it.
Importing it runs a self-test against real names from our own universe,
so a regression here is loud rather than silent.

WHY THIS IS ITS OWN FILE WITH ITS OWN TESTS
    The first matcher paired the Nifty 100 with a NASDAQ 100 ETF, the
    Nifty 50 with a "Nifty500 Multicap 50:25:25" fund, and put the US
    S&P 500 top of the list for the Nifty 500. Every one of those
    looked perfectly reasonable in the output -- a plausible fund name
    beside a plausible index name -- and every figure built on them
    would have been nonsense that nobody could have spotted on screen.

    Three mistakes caused it, and each is worth naming:

    1. "nifty" was treated as noise and stripped. It is not noise. It
       is the WHOLE of what distinguishes the Nifty 100 from the NASDAQ
       100, both of which hold about a hundred large companies. The
       provider has to match.

    2. Numbers were matched as substrings, and "50" is inside "150"
       and inside "50:25:25".

    3. Nothing checked the segment. "Nifty 100" and "Nifty Smallcap
       100" share a provider and a number and hold a hundred names
       each. Only the word "smallcap" tells them apart, and the
       matcher was throwing it away.

THE RULE NOW
    A tracker stands in for an index only when all four agree:
      provider   nifty vs nasdaq vs bse vs s&p
      segment    the exact set of {midcap, smallcap, multicap, ...}
      size       the index's headline number is one of the tracker's
      count      the tracker actually holds about that many stocks

    The last one is the only check made of DATA rather than of names,
    and it is the one that catches a fund whose name lies.
"""

import re

# The index provider or market. Never noise: it is the entire
# difference between the Nifty 100 and the NASDAQ 100.
PROVIDERS = {"nifty", "nasdaq", "bse", "sensex", "sp", "msci", "dow",
             "hangseng", "ftse"}

# What slice of the market. Two indices with the same provider and the
# same number are still different indices if these differ.
SEGMENTS = {"midcap", "smallcap", "largemidcap", "midsmallcap", "multicap",
            "largecap", "smallcap250", "next", "bank", "auto", "pharma",
            "healthcare", "it", "fmcg", "energy", "infra", "infrastructure",
            "psu", "metal", "media", "realty", "consumption", "commodities",
            "mnc", "services", "financial", "private", "oil", "gas",
            "digital", "defence", "manufacturing", "tourism", "housing",
            "capital", "microcap", "total", "market"}

# A strategy applied ON TOP of an index is a different index. These all
# carry the parent's name and hold deliberately different weights,
# which is the very thing being measured.
FLAVOURS = {"equal", "weight", "momentum", "quality", "value", "alpha",
            "volatility", "lowvol", "esg", "dividend", "enhanced", "growth",
            "vol", "beta", "sharia", "shariah"}

# Words that genuinely carry nothing.
NOISE = {"index", "fund", "etf", "tri", "the", "of", "ltd", "plan",
         "direct", "regular", "growth" if False else "", "idcw", "scheme",
         "exchange", "traded", "open", "ended", "series", "return",
         "returns", "tax", "saver", "elss", "nfo"}


def tokens(name):
    """Words and numbers, with letter/digit runs split apart.

    "Nifty500 Multicap 50:25:25" has to become
    ["nifty","500","multicap","50","25","25"] or the 500 hides inside a
    word and the 50s hide inside a punctuation run.
    """
    s = (name or "").lower()
    # S&P has to survive as one token. Splitting on punctuation first
    # turns it into "s" and "p", the provider check then sees no
    # provider at all, and the US S&P 500 sails through as a candidate
    # tracker for the Nifty 500 -- which is exactly what happened.
    s = s.replace("s&p", "sp").replace("s & p", "sp").replace("s and p", "sp")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"(?<=[a-z])(?=[0-9])", " ", s)
    s = re.sub(r"(?<=[0-9])(?=[a-z])", " ", s)
    return [t for t in s.split() if t]


def parts(name):
    ts = tokens(name)
    return {
        "provider": {t for t in ts if t in PROVIDERS},
        "segment": {t for t in ts if t in SEGMENTS},
        "flavour": {t for t in ts if t in FLAVOURS},
        "numbers": [int(t) for t in ts if t.isdigit()],
        "tokens": ts,
    }


def index_size(name):
    """The index's headline constituent count.

    The LARGEST number in the name. "Nifty500 Multicap 50:25:25" is a
    500-stock index whose name also contains 50, 25 and 25 -- those are
    the allocation split, not a size. The Nifty 50's largest is 50.
    """
    ns = parts(name)["numbers"]
    return max(ns) if ns else None


def why_not(index_name, tracker_name, tracker_holdings=None):
    """None if the tracker may stand in for the index; else the reason.

    Returns the reason rather than a bare False so the report can say
    WHY a plausible-looking pairing was refused -- which is the only way
    anybody will trust the ones it accepts.
    """
    i, t = parts(index_name), parts(tracker_name)

    # A tracker naming no provider is assumed to be the index's own --
    # "Edelweiss Smallcap 250 Index Fund" with no "Nifty" in it is not
    # pretending to be a NASDAQ product.
    ip = i["provider"] or {"nifty"}
    tp = t["provider"] or ip
    if ip != tp:
        return "different index provider (%s vs %s)" % (
            "/".join(sorted(ip)), "/".join(sorted(tp)))

    if i["segment"] != t["segment"]:
        return "different slice of the market (%s vs %s)" % (
            "/".join(sorted(i["segment"])) or "broad",
            "/".join(sorted(t["segment"])) or "broad")

    # A flavour on the tracker that the index does not have is a
    # different index with the same name.
    extra = t["flavour"] - i["flavour"]
    if extra:
        return "it is a '%s' variant, not the index" % "/".join(sorted(extra))

    size = index_size(index_name)
    if size is None:
        return "the index name carries no size to check against"
    if size not in t["numbers"]:
        return "does not carry the index's size (%d)" % size

    # THE ONLY CHECK MADE OF DATA RATHER THAN OF WORDS, and so the only
    # one that catches a fund whose name is misleading. A fund calling
    # itself a Nifty 500 tracker while holding 80 stocks is not one.
    if tracker_holdings is not None:
        lo, hi = size * 0.9, size * 1.1
        if not (lo - 5 <= tracker_holdings <= hi + 5):
            return "holds %d stocks, but the index has %d" % (
                tracker_holdings, size)
    return None


def _selftest():
    """Real names from our own universe, including every one the first
    version got wrong. Runs on import: a silent matcher is the whole
    problem this file exists to fix."""
    cases = [
        # (index, tracker, holdings, should it match?)
        ("Nifty 100 TRI", "Motilal Oswal Nasdaq 100 ETF", 103, False),
        ("Nifty 100 TRI", "ICICI Prudential NASDAQ 100 Index Fund", 102, False),
        ("Nifty 100 TRI", "Zerodha Nifty Smallcap 100 ETF", 100, False),
        ("Nifty 100 TRI", "ICICI Prudential Nifty 100 ETF", 100, True),
        ("Nifty 50 TRI", "HDFC NIFTY500 Multicap 50:25:25 Index Fund", 500, False),
        ("Nifty 50 TRI", "Mirae Asset Nifty500 Multicap 50:25:25 ETF", 500, False),
        ("Nifty 50 TRI", "Zerodha Nifty MidSmallcap400 50:50 Index Fund", 400, False),
        ("Nifty 50 TRI", "HDFC Nifty 50 Index Fund", 50, True),
        ("Nifty 500 TRI", "Motilal Oswal S&P 500 Index Fund", 502, False),
        ("Nifty 500 TRI", "DSP Nifty 500 Index Fund", 500, True),
        ("Nifty 500 TRI", "Motilal Oswal Nifty 500 ETF", 500, True),
        ("Nifty 500 TRI", "Nippon India Nifty 500 Equal Weight Index Fund", 499, False),
        ("Nifty 500 TRI", "HDFC NIFTY500 Multicap 50:25:25 Index Fund", 500, False),
        ("Nifty 500 TRI", "Mirae Asset Nifty Total Market Index Fund", 750, False),
        ("Nifty Midcap 150 TRI", "SBI Nifty Midcap 150 Index Fund", 150, True),
        ("Nifty Midcap 150 TRI", "DSP Nifty Midcap 150 Quality 50 ETF", 50, False),
        ("Nifty Midcap 150 TRI", "Kotak Nifty Midcap 150 Momentum 50 Index Fund", 50, False),
        ("Nifty Smallcap 250 TRI", "Edelweiss Nifty Smallcap 250 Index Fund", 250, True),
        ("Nifty Smallcap 250 TRI", "SBI Nifty Smallcap 250 ETF", 250, True),
        ("Nifty LargeMidcap 250 TRI", "HDFC NIFTY LargeMidcap 250 Index Fund", 250, True),
        ("Nifty LargeMidcap 250 TRI", "Edelweiss Nifty LargeMidcap 250 ETF", 250, True),
        ("Nifty500 Multicap 50:25:25 TRI",
         "HDFC NIFTY500 Multicap 50:25:25 Index Fund", 500, True),
        ("Nifty500 Multicap 50:25:25 TRI", "DSP Nifty 500 Index Fund", 500, False),
        # A name that lies: caught only by the holdings count.
        ("Nifty 500 TRI", "Some Nifty 500 Index Fund", 80, False),
    ]
    bad = []
    for idx, trk, n, want in cases:
        got = why_not(idx, trk, n) is None
        if got != want:
            bad.append("%s <- %s : expected %s, got %s (%s)"
                       % (idx, trk, "match" if want else "no match",
                          "match" if got else "no match",
                          why_not(idx, trk, n)))
    if bad:
        raise AssertionError("index_match self-test failed:\n  "
                             + "\n  ".join(bad))


_selftest()
