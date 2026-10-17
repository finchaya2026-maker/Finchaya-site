"""
fetch_stock_financials.py -- quarterly and yearly Revenue/Profit/EPS/Debt,
parsed from the XBRL behind NSE's and BSE's own financial-results filings.
------------------------------------------------------------------------
WHY THIS TRUSTS CONTEXT-ID CONVENTION, NOT THE DATES INSIDE THE XBRL
    Each filing's XBRL tags every number against a "context": a period a
    fact applies to. The obvious approach -- read each context's own
    <startDate>/<endDate> to tell "this quarter" apart from "full year" --
    turned out to be unreliable: confirmed live (Schaeffler India's FY24
    annual filing) that the context named "FourD" carries the full-year
    figure (~4x the "OneD" figure) while both contexts declare the SAME
    3-month date range in their own XML. The dates are evidently a
    copy-paste artifact of whatever tool generated the filing, not
    trustworthy.

    What IS reliable, confirmed across multiple filers AND across both
    the old and new taxonomy (see below): the context ID naming
    convention itself -- "OneD" is always the single-period (standalone
    quarter) column, "FourD" is always the cumulative (year-to-date /
    full-year) column, "OneI" is the balance-sheet instant. This script
    reads by that convention, and the listing API (NSE's or BSE's, never
    the XBRL itself) is trusted for what TYPE a filing is (quarterly vs
    annual) and what period it covers -- never inferred from XBRL dates.

    Residual risk: a filer whose software doesn't follow this convention
    would be silently skipped (no "OneD"/"FourD" context found), not
    silently wrong -- see skip counts in the summary line.

WHY TWO XBRL FORMATS ARE PARSED, NOT ONE
    NSE's archive (fetch_xbrl) serves the older format: plain elements
    like <in-bse-fin:RevenueFromOperations contextRef="OneD">596...
    </in-bse-fin:RevenueFromOperations>. BSE's own filings -- confirmed
    live, needed to reach anything more recent than NSE's archive, which
    lags to around October 2025 -- turned out to use SEBI's newer
    "in-capmkt" taxonomy (dated 2026-01-31) in Inline XBRL form instead:
    <ix:nonFraction name='in-capmkt:RevenueFromOperations'
    contextRef='OneD' scale='7'>59,553.00</ix:nonFraction> -- comma-
    formatted, with the real magnitude given by the scale attribute
    (value * 10^scale) and negatives marked by a separate sign='-'
    attribute rather than a leading minus sign. parse_facts() reads
    both shapes into the same {context: {tag: value}} structure so
    everything downstream is agnostic to which source a filing came
    from.

WHY BSE'S XBRL IS FETCHED FROM AN "IFIndasDuplicateUploadDocument" HTML
FILE, NOT THE ".xml" THE LISTING API ALSO OFFERS
    BSE's Corp_FinanceResult_ng_new listing gives two filenames per
    filing: one under FourOneUploadDocument/...xml (the raw XBRL) and
    one under IFIndasDuplicateUploadDocument/..._IFIndAs.html (a
    self-contained Inline XBRL viewer page -- XBRL facts embedded in an
    HTML rendering of the result). Confirmed live: the .xml path 404s
    under every base URL tried, while the exact same filename with
    .html at https://www.bseindia.com/XBRLFILES/<name> serves fine.
    Whatever's blocking the raw .xml (likely a WAF rule against serving
    raw XML directly) doesn't touch the HTML viewer, so that's what
    this script fetches and parses instead.

WHY DEBT ONLY EXISTS FOR YEARLY, NOT QUARTERLY
    A quarterly result XBRL (Reg 33) only contains P&L facts. A balance
    sheet -- and with it BorrowingsCurrent/BorrowingsNoncurrent, this
    script's "debt" -- only shows up in an Annual filing's XBRL, at the
    "OneI" instant context.

WHY CONSOLIDATED IS PREFERRED OVER NON-CONSOLIDATED
    A company can file both for the same period; Consolidated (parent +
    subsidiaries) is the more representative figure for an investor and
    is what every mainstream stock app defaults to. Standalone is only
    used as a fallback when no Consolidated filing exists.

WHY THE BSE PASS RUNS SECOND AND CAN OVERWRITE NSE'S NUMBERS
    Both sources report the same underlying regulatory filing, so there
    is no correctness difference between them for a period they both
    have -- BSE running second and winning ties is simply preferring
    the pass more likely to be current. What UPSERT actually guards
    against is a FAILED BSE parse (fetch/parse error, or a filer that
    doesn't fit the context convention) blanking out a real NSE number
    with nulls -- see its COALESCE.

STILL NOT covered: segment-level detail, cash flow statement lines
(both formats have them, this script doesn't read them), or anything
for companies whose result doesn't use the Ind-AS taxonomy at all
(older/smaller filers occasionally use a different schema).

SAFE TO RE-RUN. Upserts on (isin, period_type, period_end).

USAGE
    python fetch_stock_financials.py --symbols RELIANCE,TCS   # spot check
    python fetch_stock_financials.py --limit 50 --dry-run     # scale test
    python fetch_stock_financials.py                          # full universe
"""

import argparse
import hashlib
import os
import re
import sys
import time
from calendar import monthrange
from datetime import date, datetime, timedelta

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.rows import dict_row

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv()

DB = os.getenv("FINCHAYA_DB")
if not DB:
    sys.exit("FINCHAYA_DB is not set.")

YEARS_OF_HISTORY = 3
FETCH_WINDOW_DAYS = int(YEARS_OF_HISTORY * 365 + 120)  # a bit of slack for filing lag
PAUSE_SEC = 0.3
BATCH_SIZE = 50
BATCH_PAUSE_SEC = 5

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

nse = requests.Session()
nse.headers.update({
    "User-Agent": UA, "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-financial-results",
})

bse = requests.Session()
bse.headers.update({
    "User-Agent": UA, "Accept": "application/json",
    "Referer": "https://www.bseindia.com/corporates/comp_resultsnew",
    "Origin": "https://www.bseindia.com",
})

# The filing pages (XBRLFILES/...html) are ordinary web pages, not API calls,
# so they get ordinary headers. Sent "Accept: application/json" (the header
# `bse` above carries for the JSON listing), BSE answers 404 "not found" for
# a file that exists. Measured 7 Oct 2026 on 15 untouched Jun-2026 filings:
# the JSON headers fetched 0 of 15, plain headers 15 of 15. This is why every
# BSE figure after Dec 2024 came back empty in the 29 Sep run.
bse_files = requests.Session()
bse_files.headers.update({"User-Agent": UA})


def ref_hash(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def fetch_nse_financial_results(period, since, until):
    try:
        r = nse.get("https://www.nseindia.com/api/corporates-financial-results",
                     params={"index": "equities", "period": period,
                             "from_date": since.strftime("%d-%m-%Y"),
                             "to_date": until.strftime("%d-%m-%Y")},
                     timeout=30)
        if not r.ok:
            return []
        return r.json() or []
    except (requests.RequestException, ValueError):
        return []


def fetch_xbrl(url):
    try:
        r = nse.get(url, timeout=20)
        if not r.ok:
            return None
        return r.text
    except requests.RequestException:
        return None


BSE_XBRL_RETRIES = 3
BSE_XBRL_RETRY_PAUSE_SEC = 2


def fetch_bse_xbrl_html(xml_name):
    # Uses bse_files (plain headers), NOT the JSON-API session: see the note
    # beside bse_files. The 404s once blamed on an "edge-cache miss" were this.
    # A file BSE has not published yet also 404s, so retries stay.
    url = f"https://www.bseindia.com/XBRLFILES/{xml_name}"
    for attempt in range(BSE_XBRL_RETRIES):
        try:
            r = bse_files.get(url, timeout=60)
            if r.ok:
                return r.text
        except requests.RequestException:
            pass
        if attempt < BSE_XBRL_RETRIES - 1:
            time.sleep(BSE_XBRL_RETRY_PAUSE_SEC)
    return None


# Old taxonomy (NSE archive, and any pre-2026 BSE filing): plain elements
# under in-bse-fin, contextRef in a normal double-quoted attribute, value
# as plain text between the tags.
FACT_PAT = re.compile(
    r'<in-bse-fin:(\w+)\s+contextRef="([^"]+)"[^>]*>([^<]*)</in-bse-fin:\1>')

# New taxonomy (BSE's live filings, "in-capmkt", dated 2026-01-31): Inline
# XBRL. Facts are <ix:nonFraction> elements carrying the tag name, context
# and scale as attributes (single- or double-quoted, any order) rather
# than as the element name itself, and the value is comma-formatted text
# that still needs scale/sign applied.
IX_FACT_PAT = re.compile(r"(\(?)\s*<ix:nonfraction\s+([^>]+?)>([^<]*)</ix:nonfraction>", re.I)
# Spaces around "=" are allowed: some filings write  contextRef = 'OneD'  (seen on every loss-making
# company's EPS lines). Before 9 Oct 2026 this pattern demanded no spaces, so those facts were dropped
# silently and the EPS of every loss-making quarter was missing.
IX_ATTR_PAT = re.compile(r"(\w[\w:-]*)\s*=\s*['\"]([^'\"]*)['\"]")

# A LOSS is shown in brackets, (1.88), and some filings leave the sign attribute off the tag, so the
# bracket is the only mark of it. Applied only to profit and EPS lines: elsewhere a bracket can simply
# mean "less: an expense", which is a positive fact.
BRACKET_NEGATIVE_TAGS = re.compile(r"Profit|Loss|Earnings", re.I)


def parse_facts(text):
    facts = {}
    for tag, ctx, val in FACT_PAT.findall(text):
        facts.setdefault(ctx, {})[tag] = val

    for bracket, attrs_str, raw_val in IX_FACT_PAT.findall(text):
        attrs = dict(IX_ATTR_PAT.findall(attrs_str))
        name = attrs.get("name", "")
        tag = name.rsplit(":", 1)[-1] if name else ""
        ctx = attrs.get("contextRef", "")
        if not tag or not ctx or not raw_val.strip():
            continue
        try:
            num = float(raw_val.replace(",", "").strip())
        except ValueError:
            continue
        num *= 10 ** int(attrs.get("scale") or 0)
        if attrs.get("sign") == "-" or (bracket and BRACKET_NEGATIVE_TAGS.search(tag)):
            num = -abs(num)
        facts.setdefault(ctx, {})[tag] = num
    return facts


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


EPS_TAGS = (
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    # banks use these instead
    "DilutedEarningsPerShareAfterExtraordinaryItems",
    "BasicEarningsPerShareAfterExtraordinaryItems",
)

# Banks, life insurers and general insurers file on their own forms and do not
# use RevenueFromOperations / ProfitLossForPeriod. Checked 7 Oct 2026 on HDFC Bank,
# SBI Life and ICICI Lombard: the total income and the period profit sit under
# these names. They are tried ONLY when neither usual tag is present (see
# financial_figures), so an ordinary company with no sales is never given its
# "other income" as revenue.
ALT_REVENUE_TAGS = ("Income", "OperatingIncome")
ALT_PROFIT_TAGS = ("ProfitLossForThePeriod", "ProfitLossAfterTaxAndExtraordinaryItems", "ProfitLossAfterTax")


def eps_from(ctx_facts):
    """First non-zero EPS in the tag order; a genuine zero is kept if nothing else is there."""
    vals = [to_num(ctx_facts.get(t)) for t in EPS_TAGS]
    return next((v for v in vals if v), next((v for v in vals if v is not None), None))


def financial_figures(ctx_facts):
    """(revenue, profit) from one period's facts. Usual tags first; the bank/insurer
    names only when both usual tags are missing."""
    revenue = to_num(ctx_facts.get("RevenueFromOperations"))
    profit = to_num(ctx_facts.get("ProfitLossForPeriod"))
    if revenue is None and profit is None:
        revenue = next((v for v in (to_num(ctx_facts.get(t)) for t in ALT_REVENUE_TAGS) if v is not None), None)
        profit = next((v for v in (to_num(ctx_facts.get(t)) for t in ALT_PROFIT_TAGS) if v is not None), None)
    return revenue, profit


# ---- operating profit ------------------------------------------------------------------------------
# For the results page. Operating profit here is what a company earned from running its business before
# interest, depreciation and tax: revenue from operations minus the costs that are neither finance costs nor
# depreciation, i.e.  Revenue - (Expenses - FinanceCosts - Depreciation).  OTHER INCOME IS NOT INCLUDED (a
# bank-interest or treasury gain is not operating); some sites add it in, so their figure runs higher by
# exactly that line. Checked 11 Oct 2026 against Premier Polyfilm's Sep-2026 filing: revenue 115.06, expenses
# 103.07, finance costs 0.24, depreciation 1.18 -> 13.41 crore (14.53 with its 1.12 crore other income).
#
# Only for ordinary companies. A bank, a finance company or an insurer's biggest cost IS interest or claims, so
# "before interest" means nothing for them: operating profit is left None and the page shows profit before tax
# instead. Every one of the lines must be on the page; a missing one gives None, never a guess.
FINANCIAL_PAGE_FOLDERS = ("IFNBFC", "IFBanking", "IFLI", "IFGI")
FINANCIAL_ONLY_TAGS = ("InterestEarned", "ImpairmentOnFinancialInstruments", "FeesAndCommissionExpense")
PBT_TAGS = ("ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax", "ProfitBeforeTaxFromContinuingOperations")


def is_financial_filer(ctx_facts, page_name=None):
    """A bank, finance company or insurer. The form code sits in BSE's file name, either as the folder
    (IFNBFCDuplicateUploadDocument/...) or at the end of the name (ce_NBFC_511196_..._IFNBFC.html); where the
    name says nothing, the tags only these filers use decide."""
    name = page_name or ""
    if any(p in name for p in FINANCIAL_PAGE_FOLDERS):
        return True
    if "IFIndas" in name or "IFIndAs" in name:
        return False
    return any(t in ctx_facts for t in FINANCIAL_ONLY_TAGS)


def operating_figures(ctx_facts, page_name=None):
    """(operating_profit, profit_before_tax) of one period, in rupees; None for what the page does not give."""
    pbt = next((v for v in (to_num(ctx_facts.get(t)) for t in PBT_TAGS) if v is not None), None)
    if is_financial_filer(ctx_facts, page_name):
        return None, pbt
    revenue = to_num(ctx_facts.get("RevenueFromOperations"))
    expenses = to_num(ctx_facts.get("Expenses"))
    finance = to_num(ctx_facts.get("FinanceCosts"))
    depreciation = to_num(ctx_facts.get("DepreciationDepletionAndAmortisationExpense"))
    if None in (revenue, expenses, finance, depreciation):
        return None, pbt
    return revenue - (expenses - finance - depreciation), pbt


def fiscal_quarter_label(period_end):
    m = period_end.month
    if m in (4, 5, 6):
        q, fy_end_year = 1, period_end.year + 1
    elif m in (7, 8, 9):
        q, fy_end_year = 2, period_end.year + 1
    elif m in (10, 11, 12):
        q, fy_end_year = 3, period_end.year + 1
    else:
        q, fy_end_year = 4, period_end.year
    return f"Q{q} FY{str(fy_end_year)[-2:]}"


def fiscal_year_label(period_end):
    y = period_end.year
    if period_end.month == 3:
        return f"FY {y - 1}-{str(y)[-2:]}"
    return f"FY {y}"


def build_financial_row(isin, period_type, period_end, facts, consolidated, source, source_ref, page_name=None):
    """None if the expected context ("OneD" for a quarter, "FourD" for a
    year) isn't in the facts -- a filer that doesn't follow the context
    convention, or a parse that found nothing. Never returns a row with
    every metric null; that placeholder shape is built by the caller.
    page_name (the BSE file name) only helps tell a bank or finance company from an ordinary one."""
    if period_type == "quarterly":
        one = facts.get("OneD")
        if not one:
            return None
        revenue, profit = financial_figures(one)
        operating, pbt = operating_figures(one, page_name)
        return dict(
            isin=isin, period_type="quarterly", period_end=period_end,
            period_label=fiscal_quarter_label(period_end),
            revenue=revenue, profit=profit,
            eps=eps_from(one), debt=None,
            operating_profit=operating, profit_before_tax=pbt,
            consolidated=consolidated, source=source, source_ref=source_ref,
        )

    four = facts.get("FourD")
    if not four:
        return None
    one_i = facts.get("OneI") or {}
    borrow_cur = to_num(one_i.get("BorrowingsCurrent"))
    borrow_noncur = to_num(one_i.get("BorrowingsNoncurrent"))
    debt = (None if borrow_cur is None and borrow_noncur is None
            else (borrow_cur or 0) + (borrow_noncur or 0))
    revenue, profit = financial_figures(four)
    operating, pbt = operating_figures(four, page_name)
    return dict(
        isin=isin, period_type="yearly", period_end=period_end,
        period_label=fiscal_year_label(period_end),
        revenue=revenue, profit=profit,
        eps=eps_from(four), debt=debt,
        operating_profit=operating, profit_before_tax=pbt,
        consolidated=consolidated, source=source, source_ref=source_ref,
    )


# ---- balance sheets ---------------------------------------------------------------------------------
# For P/B, return on equity, debt to equity, book value per share and market cap. A balance sheet sits in
# the ANNUAL filing and in the SEPTEMBER one (the half-year); the other quarters carry none, and a page
# without one reads as None, never as zeros. Values are in rupees, as the filings give them.
#
# Four kinds of filer use four sets of names (checked 9 Oct 2026 on TCS, Reliance, KILITCH, HDFC Bank,
# Bajaj Finance, HDFC Life and ICICI Lombard):
#   corporate  Equity / EquityAttributableToOwnersOfParent; BorrowingsCurrent + BorrowingsNoncurrent
#   nbfc       the same equity names; debt is Borrowings + DebtSecurities + SubordinatedLiabilities + Deposits
#   bank       no Equity tag: Capital + ReservesAndSurplus; no debt-to-equity (deposits are the business)
#   insurer    ShareCapital + ReservesAndSurplusExcludingRevaluationReserve
# The folder of the page tells which (IFIndas, IFNBFC, IFBanking, IFLI/IFGI); where it does not, the tags do.
BS_FORMS = (("IFIndas", "corporate"), ("IFNBFC", "nbfc"), ("IFBanking", "bank"), ("IFLI", "insurer"), ("IFGI", "insurer"))
BS_COLUMNS = ("net_worth", "total_equity", "paid_up", "face_value", "shares", "debt", "total_assets",
              "profit_owners", "profit_before_tax", "finance_costs", "depreciation")


def _first(d, *tags):
    for t in tags:
        v = to_num(d.get(t))
        if v is not None:
            return v
    return None


def _sum_of(d, tags):
    vals = [v for v in (to_num(d.get(t)) for t in tags) if v is not None]
    return sum(vals) if vals else None


def bs_kind(page_name, one_i):
    first = (page_name or "").split("/")[0]
    for prefix, kind in BS_FORMS:
        if first.startswith(prefix):
            return kind
    if "ReservesAndSurplus" in one_i and ("Deposits" in one_i or "Advances" in one_i):
        return "bank"
    if "ReservesAndSurplusExcludingRevaluationReserve" in one_i:
        return "insurer"
    return "corporate"


def balance_sheet_figures(facts, page_name=None, annual=False):
    """The balance-sheet figures of one filing page, or None if it has no balance sheet (or no equity in it).
    `annual`: the page is a full-year filing, so its FourD column really is a year and the profit, tax,
    interest and depreciation lines are kept for return on equity; for any other page they are left None
    (FourD there is a half-year). Never guesses: a figure the page does not give is None."""
    one_i = facts.get("OneI") or {}
    if not one_i:
        return None
    year = facts.get("FourD") or {}
    quarter = facts.get("OneD") or {}
    kind = bs_kind(page_name, one_i)

    if kind == "bank":
        capital, reserves = _first(one_i, "Capital"), _first(one_i, "ReservesAndSurplus")
        net_worth = total_equity = capital + reserves if capital is not None and reserves is not None else None
        paid_cap = capital
        debt = None
    elif kind == "insurer":
        capital = _first(one_i, "ShareCapital", "PaidUpEquityShareCapital", "PaidUpEquityCapital")
        # In a September filing the reserves can sit in the period columns, not the balance-sheet instant
        # (HDFC Life, Sep 2025: the OneI "ReservesAndSurplus" there is a 6.8 crore line, the real figure is in FourD)
        reserves = _first(one_i, "ReservesAndSurplusExcludingRevaluationReserve")
        if reserves is None:
            reserves = _first(year, "ReservesAndSurplusExcludingRevaluationReserve") or _first(quarter, "ReservesAndSurplusExcludingRevaluationReserve")
        if reserves is None:
            reserves = _first(one_i, "ReservesAndSurplus")
        net_worth = total_equity = capital + reserves if capital is not None and reserves is not None else None
        paid_cap = capital
        debt = _first(one_i, "Borrowings")
    else:
        total_equity = _first(one_i, "Equity")
        if total_equity is None:
            total_equity = _sum_of(one_i, ("EquityShareCapital", "OtherEquity"))
        owners = _first(one_i, "EquityAttributableToOwnersOfParent")
        net_worth = owners if owners is not None else total_equity
        paid_cap = _first(one_i, "EquityShareCapital")
        if kind == "nbfc":
            debt = _sum_of(one_i, ("Borrowings", "BorrowingsCurrent", "BorrowingsNoncurrent", "DebtSecurities",
                                   "SubordinatedLiabilities", "Deposits"))
        else:
            debt = _sum_of(one_i, ("BorrowingsCurrent", "BorrowingsNoncurrent"))
    if net_worth is None:
        return None

    paid_up = _first(year, "PaidUpValueOfEquityShareCapital") or _first(quarter, "PaidUpValueOfEquityShareCapital") or paid_cap
    face = _first(year, "FaceValueOfEquityShareCapital") or _first(quarter, "FaceValueOfEquityShareCapital")
    shares = paid_up / face if paid_up and face and paid_up > 0 and face > 0 else None
    # Insurers' filings give no face value (checked on HDFC Life, SBI Life, ICICI Lombard, LIC, New India: no
    # face value and no EPS in the page), so for them shares stays None and the API works the count out from
    # paid_up and the face value held in stock_master.

    out = {"kind": kind, "net_worth": net_worth, "total_equity": total_equity, "paid_up": paid_up, "face_value": face,
           "shares": shares, "debt": debt, "total_assets": _first(one_i, "Assets"),
           "profit_owners": None, "profit_before_tax": None, "finance_costs": None, "depreciation": None}
    if annual and year:
        if kind == "bank":
            out["profit_owners"] = _first(year, "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates", "ProfitLossForThePeriod")
            out["profit_before_tax"] = _first(year, "ProfitLossFromOrdinaryActivitiesBeforeTax", "ProfitBeforeTax")
            out["finance_costs"] = _first(year, "InterestExpended")
        elif kind == "insurer":
            out["profit_owners"] = financial_figures(year)[1]
        else:
            out["profit_owners"] = _first(year, "ProfitOrLossAttributableToOwnersOfParent", "ProfitLossForPeriod")
            if out["profit_owners"] is None:
                out["profit_owners"] = financial_figures(year)[1]
            out["profit_before_tax"] = _first(year, "ProfitBeforeTax")
            out["finance_costs"] = _first(year, "FinanceCosts")
            out["depreciation"] = _first(year, "DepreciationDepletionAndAmortisationExpense")
    return out


def balance_sheet_row(isin, period_end, facts, consolidated, page_name, source_ref, filed_on=None, annual=False):
    """A stock_balance_sheet row for BS_UPSERT, or None if the page has no balance sheet."""
    figs = balance_sheet_figures(facts, page_name, annual)
    if figs is None:
        return None
    return dict(isin=isin, period_end=period_end, period_type="yearly" if annual else "interim",
                consolidated=consolidated, filed_on=filed_on, source_ref=source_ref, **figs)


def _bs_upsert_sql():
    """Written out from the column list so adding a column is one edit. Two filings can cover one balance-sheet
    date (the March quarter's page and the annual one; consolidated and standalone). The rules: the same basis
    fills in what the other left blank, never blanks a figure; a consolidated filing replaces a standalone
    one entirely, and a standalone one never replaces a consolidated one (figures of two bases must not mix)."""
    cols = ("kind",) + BS_COLUMNS
    names = "isin, period_end, period_type, consolidated, filed_on, source_ref, " + ", ".join(cols) + ", updated_at"
    values = ", ".join("%(" + c + ")s" for c in ("isin", "period_end", "period_type", "consolidated", "filed_on", "source_ref") + cols) + ", now()"
    def pick(c):
        return ("%s = CASE WHEN s.consolidated = EXCLUDED.consolidated THEN COALESCE(EXCLUDED.%s, s.%s) "
                "WHEN EXCLUDED.consolidated THEN EXCLUDED.%s ELSE s.%s END" % (c, c, c, c, c))
    sets = ",\n    ".join(pick(c) for c in cols + ("filed_on", "source_ref"))
    return ("INSERT INTO stock_balance_sheet AS s (" + names + ")\nVALUES (" + values + ")\n"
            "ON CONFLICT (isin, period_end) DO UPDATE SET\n    " + sets + ",\n"
            "    period_type = CASE WHEN s.period_type = 'yearly' OR EXCLUDED.period_type = 'yearly' THEN 'yearly' ELSE 'interim' END,\n"
            "    consolidated = s.consolidated OR EXCLUDED.consolidated,\n    updated_at = now()")


BS_UPSERT = _bs_upsert_sql()


def row_for_filing(filing, isin):
    xml_text = fetch_xbrl(filing["xbrl"])
    if not xml_text:
        return None, "download_failed"

    period_end = parse_date(filing["toDate"])
    if not period_end:
        return None, "bad_date"

    period_type = "quarterly" if filing["period"] == "Quarterly" else "yearly"
    facts = parse_facts(xml_text)
    row = build_financial_row(
        isin, period_type, period_end, facts,
        consolidated=(filing["consolidated"] == "Consolidated"),
        source="NSE", source_ref=filing["seqNumber"])
    if not row:
        return None, "no_OneD_context" if period_type == "quarterly" else "no_FourD_context"
    return row, None


def pick_best_filings(filings, known_isins):
    """One filing per (isin, period_type, toDate): prefer Consolidated,
    then the most recently broadcast."""
    best = {}
    for f in filings:
        isin = f.get("isin")
        if not isin or isin not in known_isins:
            continue
        if not f.get("xbrl") or f.get("xbrl").endswith("/-"):
            continue
        period_type = "quarterly" if f["period"] == "Quarterly" else "yearly"
        key = (isin, period_type, f.get("toDate"))
        rank = (1 if f.get("consolidated") == "Consolidated" else 0, f.get("broadCastDate") or "")
        if key not in best or rank > best[key][0]:
            best[key] = (rank, f)
    return [f for _, f in best.values()]


# BSE's own period code: a letter for which fiscal-year month the period
# ENDS in (M=March, J=June, S=September, D=December), then Q (single
# quarter) or C (cumulative -- year-to-date, which for the March one
# means the full year), then the fiscal year as "YYYY-YYYY". The ending
# calendar year is the range's second year for March, first year
# otherwise -- e.g. "JQ2026-2027" ends 30 Jun 2026, "MC2025-2026" ends
# 31 Mar 2026. H (half-yearly) codes are skipped: they fit neither this
# table's quarterly nor yearly bucket.
QUARTER_CODE = re.compile(r"^([MJSD])([QC])(\d{4})-(\d{4})$")
QUARTER_CODE_MONTH = {"M": 3, "J": 6, "S": 9, "D": 12}


def parse_quarter_code(code):
    m = QUARTER_CODE.match(code or "")
    if not m:
        return None
    month_letter, dur, y1, y2 = m.groups()
    month = QUARTER_CODE_MONTH[month_letter]
    year = int(y2) if month_letter == "M" else int(y1)
    period_end = date(year, month, monthrange(year, month)[1])
    period_type = "yearly" if dur == "C" else "quarterly"
    return period_end, period_type


def fetch_bse_recent_filings(scrip_code):
    try:
        r = bse.get("https://api.bseindia.com/BseIndiaAPI/api/Corp_FinanceResult_ng_new/w",
                     params={"SCRIP_CD": scrip_code, "FlagDur": "7", "HFQ": "",
                             "ISUBGROUP_CODE": "", "segment": "C"}, timeout=30)
        if not r.ok:
            return []
        return (r.json() or {}).get("Table") or []
    except (requests.RequestException, ValueError):
        return []


def bse_row_for_filing(isin, f, period_end, period_type):
    """None if neither filename BSE gives for this row is the fetchable
    HTML viewer (see module docstring) -- e.g. it's the raw-.xml row
    instance of a filing that also has an .html row instance elsewhere
    in the listing, which the caller will encounter separately."""
    consol_name = f.get("Consol_XMLName") or ""
    plain_name = f.get("XMLName") or ""
    if consol_name.endswith(".html"):
        html_name, consolidated = consol_name, True
    elif plain_name.endswith(".html"):
        html_name, consolidated = plain_name, (f.get("Fld_NatureOfReport") != "Standalone")
    else:
        return None

    text = fetch_bse_xbrl_html(html_name)
    if not text:
        return None
    facts = parse_facts(text)
    return build_financial_row(
        isin, period_type, period_end, facts, consolidated,
        source="BSE", source_ref=ref_hash(isin, period_type, period_end, html_name))


def bse_filing_rows(isin, scrip_code, cutoff):
    """One row per (period_type, period_end) BSE shows as filed within
    the window: real numbers where the HTML viewer fetched and parsed,
    a dates-only placeholder (every metric null) otherwise -- e.g. a
    filer whose XBRL doesn't follow the OneD/FourD convention, or a
    fetch that failed. FlagDur=7 ("beyond last year") returns a
    company's ENTIRE filing history -- TCS as far back as FY2004-05 --
    so cutoff enforces the same window the rest of this script
    promises."""
    best = {}  # (period_type, period_end) -> (rank, row)
    for f in fetch_bse_recent_filings(scrip_code):
        parsed = parse_quarter_code(f.get("quarter_code"))
        if not parsed:
            continue
        period_end, period_type = parsed
        if period_end < cutoff:
            continue

        row = bse_row_for_filing(isin, f, period_end, period_type)
        if row:
            rank = 2 if row["consolidated"] else 1
            time.sleep(PAUSE_SEC)
        else:
            row = dict(
                isin=isin, period_type=period_type, period_end=period_end,
                period_label=(fiscal_quarter_label(period_end) if period_type == "quarterly"
                              else fiscal_year_label(period_end)),
                revenue=None, profit=None, eps=None, debt=None,
                consolidated=bool(f.get("Consol_XMLName")),
                source="BSE", source_ref=ref_hash(isin, period_type, period_end, "bse-meta"),
            )
            rank = 0

        key = (period_type, period_end)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, row)

    return [row for _, row in best.values()]


def upsert_sql(dated=False, operating=False):
    """The statement that stores one result. Built from a column list because the table has grown columns in
    steps (filed_on, then operating_profit and profit_before_tax) and the code must keep working whichever of
    the migrations has been run: a column the table does not have yet is simply left out of the statement.
    A failed parse (this pass or an earlier one) must never blank a real number some pass already found, so
    every figure is COALESCEd; a missing filing date never blanks a known one either."""
    figures = ["revenue", "profit", "eps", "debt"] + (["operating_profit", "profit_before_tax"] if operating else [])
    cols = ["isin", "period_type", "period_end", "period_label"] + figures + ["consolidated", "source", "source_ref"]
    if dated:
        cols.append("filed_on")
    sets = ["period_label = EXCLUDED.period_label"]
    sets += ["%s = COALESCE(EXCLUDED.%s, stock_financials.%s)" % (c, c, c) for c in figures]
    sets += ["consolidated = EXCLUDED.consolidated", "source = EXCLUDED.source", "source_ref = EXCLUDED.source_ref"]
    if dated:
        sets.append("filed_on = COALESCE(EXCLUDED.filed_on, stock_financials.filed_on)")
    sets.append("updated_at = now()")
    return ("INSERT INTO stock_financials\n    (" + ", ".join(cols) + ", updated_at)\nVALUES\n    ("
            + ", ".join("%(" + c + ")s" for c in cols) + ", now())\n"
            "ON CONFLICT (isin, period_type, period_end) DO UPDATE SET\n    " + ",\n    ".join(sets) + "\n")


UPSERT = upsert_sql()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma-separated NSE symbols, for a spot check")
    ap.add_argument("--limit", type=int, help="limit how many FILINGS to parse (not stocks)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    until = date.today()
    since = until - timedelta(days=FETCH_WINDOW_DAYS)
    cutoff = until - timedelta(days=YEARS_OF_HISTORY * 365)

    with psycopg.connect(DB, row_factory=dict_row) as conn, conn.cursor() as cur:
        where = "WHERE is_active"
        params = {}
        if args.symbols:
            syms = [s.strip().upper() for s in args.symbols.split(",")]
            where += " AND symbol = ANY(%(syms)s)"
            params["syms"] = syms
        cur.execute(f"SELECT isin, symbol, bse_scrip_code FROM stock_master {where}", params)
        stocks = cur.fetchall()
        known_isins = {r["isin"] for r in stocks}
        print(f"{len(known_isins)} stocks in scope.")

        raw = (fetch_nse_financial_results("Quarterly", since, until)
               + fetch_nse_financial_results("Annual", since, until))
        print(f"{len(raw)} raw filings fetched from NSE (last {YEARS_OF_HISTORY}+ years, both types).")

        filings = [f for f in pick_best_filings(raw, known_isins)
                   if parse_date(f["toDate"]) and parse_date(f["toDate"]) >= cutoff]
        filings.sort(key=lambda f: f.get("broadCastDate") or "", reverse=True)
        if args.limit:
            filings = filings[:args.limit]
        print(f"{len(filings)} filings in scope after filtering to known stocks, "
              f"last {YEARS_OF_HISTORY} years, and de-duping Consolidated/Non-Consolidated.")

        ok = skipped = 0
        skip_reasons = {}
        for i, f in enumerate(filings, 1):
            row, skip = row_for_filing(f, f["isin"])
            if skip:
                skipped += 1
                skip_reasons[skip] = skip_reasons.get(skip, 0) + 1
            else:
                ok += 1
                if not args.dry_run:
                    cur.execute(UPSERT, row)
            if i % 20 == 0:
                print(f"[{i}/{len(filings)}] {ok} parsed, {skipped} skipped")
            if not args.dry_run and i % BATCH_SIZE == 0:
                conn.commit()
                time.sleep(BATCH_PAUSE_SEC)
            time.sleep(PAUSE_SEC)

        if not args.dry_run:
            conn.commit()

        print(f"\nDone (NSE pass). {ok} filings parsed and written, {skipped} skipped "
              f"({skip_reasons}).")

        # --- BSE pass: current filings, real numbers where the HTML
        # viewer parses, dates-only placeholders where it doesn't ---
        with_scrip = [s for s in stocks if s["bse_scrip_code"]]
        print(f"\n{len(with_scrip)} of those have a resolved BSE scrip code -- "
              f"checking BSE for filings NSE hasn't caught up to yet.")
        bse_total = bse_with_numbers = 0
        for i, s in enumerate(with_scrip, 1):
            for row in bse_filing_rows(s["isin"], s["bse_scrip_code"], cutoff):
                bse_total += 1
                if row["revenue"] is not None or row["profit"] is not None:
                    bse_with_numbers += 1
                if not args.dry_run:
                    cur.execute(UPSERT, row)
            if i % 20 == 0:
                print(f"[{i}/{len(with_scrip)}] {bse_total} BSE period rows seen, "
                      f"{bse_with_numbers} with real numbers")
            if not args.dry_run and i % BATCH_SIZE == 0:
                conn.commit()
                time.sleep(BATCH_PAUSE_SEC)
            time.sleep(PAUSE_SEC)

        if not args.dry_run:
            conn.commit()
        print(f"\nDone (BSE pass). {bse_total} BSE period rows upserted, "
              f"{bse_with_numbers} with real numbers parsed "
              f"({bse_total - bse_with_numbers} dates-only placeholders).")
        if args.dry_run:
            print("--dry-run: nothing written.")


if __name__ == "__main__":
    main()
