"""
ipo_extract.py -- the key facts of an IPO, read out of its offer document (DRHP / RHP / prospectus). PURE: pages of text in, a dictionary out. No network, no database.
---------------------------------------------------------------------------------------------------------------------------
WHAT IT READS (every Indian offer document follows the SEBI ICDR layout, so the same headings recur)
    THE OFFER / THE ISSUE         fresh issue and offer-for-sale sizes in shares (and rupees once the price band is known), shares before and after, the selling
                                   shareholders and how many shares each is selling
    OBJECTS OF THE OFFER / ISSUE  what the money raised will be used for: the table of objects with its amounts, the share of each (debt repayment, capital
                                   expenditure, working capital, acquisitions, general corporate purposes ...), or "the company will not receive any proceeds"
    OUR PROMOTERS (AND PROMOTER GROUP)  who the promoters are, how much they hold now, and each individual promoter's short profile as the document gives it
    OUR BUSINESS                   the company's own overview of what it does
    SUMMARY OF FINANCIAL INFORMATION   revenue, profit, total assets, equity and per-share earnings for the years shown, in rupees crore
    and the PAGE of the document where each of these, and the risk factors, litigation, related-party transactions and basis for the offer price, begin.

THE RULES THIS FOLLOWS
    * INFORMATION, NEVER A VERDICT. It copies what the document says (and, for sums, adds up what the document says). It does not rate, score, rank or recommend.
    * EVIDENCE FIRST. Every fact carries the page of the document it came from (`page`, 1-based PDF page, so a link `...pdf#page=N` opens it), and text that is
      quoted is the document's own, lightly tidied (line breaks and footnote marks removed).
    * UNSURE MEANS ABSENT. A figure that cannot be read with confidence is None / missing, and `notes` says what was not found. Nothing is guessed.
    * Section starts are found from the headings themselves (a heading line on its own near the top of a page, after the table of contents), not from page numbers
      printed in the document, so a document whose printed numbers are off by a few pages (they always are) is read the same way.

extract(pages, kind="auto") -> {"sections": {...}, "offer": {...}, "objects": {...}, "promoters": {...}, "business": {...}, "financials": {...}, "auditor": ..., "notes": [...]}
"""

import re

# ---------------------------------------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------------------------------------
_PLACEHOLDER = "[●]"
UNIT_TO_CRORE = {"million": 0.1, "millions": 0.1, "mn": 0.1, "lakh": 0.01, "lakhs": 0.01, "lac": 0.01, "lacs": 0.01, "crore": 1.0, "crores": 1.0, "cr": 1.0,
                 "thousand": 0.0001, "thousands": 0.0001}


def tidy(text):
    """One line of plain text: line breaks and runs of spaces collapsed, footnote marks like (1)(2) after a word dropped, the stray space pypdf puts inside words
    kept as it is (it cannot be told from a real one)."""
    t = text.replace(" ", " ").replace("ﬁ", "fi").replace("ﬂ", "fl")
    t = re.sub(r"\s*\n\s*", " ", t)
    t = re.sub(r"(?<=[A-Za-z\]\.])\s?\(\d{1,2}\)(?:\(\d{1,2}\))*", "", t)
    # the spacing pypdf puts around punctuation: "24 ,050,000", "March 2 4, 1980", "Whole -time", "Limited , for", "110. 38%"
    t = re.sub(r"(\d)\s+,(?=\d)", r"\1,", t)
    t = re.sub(r"\b(January|February|March|April|May|June|July|August|September|October|November|December) (\d) (\d)\b", r"\1 \2\3", t)
    t = re.sub(r"(?<=[A-Za-z]) -(?=[A-Za-z])", "-", t)
    t = re.sub(r"(\d{2,3})\.\s(\d{2}%)", r"\1.\2", t)
    t = re.sub(r"\s+([,;])", r"\1", t)
    t = re.sub(r"(?<=[a-z])\s+\.(?=\s|$)", ".", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def number(s):
    """'1,400.00' -> 1400.0; '(31.99)' -> -31.99; '-' or '[●]' or 'Nil' -> None."""
    if s is None:
        return None
    s = s.strip().replace("₹", "").replace("`", "")
    if s in ("", "-", "–", "—", _PLACEHOLDER, "[●]#", "Nil", "NIL", "N.A.", "NA", "N/A"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").strip()
    if s.endswith("%"):
        s = s[:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def to_int(s):
    v = number(s)
    return None if v is None else int(round(v))


# ---------------------------------------------------------------------------------------------------------------------------
# finding the sections
# ---------------------------------------------------------------------------------------------------------------------------
# key -> (regex the heading line must match in full, minimum span in pages to read at most, label)
SECTION_HEADINGS = [
    ("risk", r"SECTIONS?\s+[IVX]+\s*[:\-–—]?\s*RISK FACTORS", "Risk factors"),
    ("offer", r"THE (?:OFFER|ISSUE)", "The offer"),
    ("summary_fin", r"SUMMARY (?:OF )?(?:OUR |THE )?(?:RESTATED |PRO ?FORMA )?(?:CONSOLIDATED )?(?:AND STANDALONE )?(?:STANDALONE )?(?:AND CONSOLIDATED )?"
                    r"(?:FINANCIAL (?:INFORMATION|STATEMENTS?|RESULTS|DATA)|FINANCIALS)(?: OF THE COMPANY)?", "Summary of financial information"),
    ("contingent", r"SUMMARY OF CONTINGENT LIABILIT(?:Y|IES)(?: AND COMMITMENTS)?", "Contingent liabilities"),
    ("related_party", r"SUMMARY OF RELATED PARTY TRANSACTIONS", "Related party transactions"),
    ("general_info", r"GENERAL INFORMATION", "General information"),
    ("capital", r"CAPITAL STRUCTURE", "Capital structure"),
    ("objects", r"OBJECTS OF THE (?:OFFER|ISSUE)", "Objects of the offer"),
    ("basis", r"BASIS (?:FOR|OF) (?:THE )?(?:OFFER|ISSUE) PRICE", "Basis for the offer price"),
    ("industry", r"INDUSTRY OVERVIEW|OVERVIEW OF (?:THE )?(?:INDIAN )?INDUSTRY|OUR INDUSTRY|INDUSTRY OVERVIEW", "Industry overview"),
    ("business", r"OUR BUSINESS|BUSINESS OVERVIEW", "Our business"),
    ("management", r"OUR MANAGEMENT", "Our management"),
    ("promoters", r"OUR PROMOTERS?(?: AND PROMOTER GROUP)?|OUR PROMOTER(?:S)? AND (?:THE )?PROMOTER GROUP", "Our promoters"),
    ("financial_statements", r"(?:RESTATED )?(?:CONSOLIDATED )?(?:STANDALONE )?FINANCIAL (?:INFORMATION|STATEMENTS)|RESTATED FINANCIAL INFORMATION", "Financial statements"),
    ("mdna", r"MANAGEMENT[’']?S DISCUSSION AND ANALYSIS.*", "Management's discussion and analysis"),
    ("litigation", r"OUTSTANDING LITIGATION(?: AND MATERIAL DEVELOPMENTS| AND OTHER MATERIAL DEVELOPMENTS)?", "Outstanding litigation"),
]
_HEAD_RES = [(k, re.compile(r"^(?:%s)$" % rx, re.I), label) for k, rx, label in SECTION_HEADINGS]
TOC_PAGES = 12                       # the table of contents is within the first pages; headings are looked for from after it
HEADING_LINES = 10                   # a heading is among the first lines of its page (the printed page number and a rule may precede it)


def _lines(page):
    return [re.sub(r"\s+", " ", ln).strip() for ln in page.split("\n") if ln.strip()]


def _is_toc(page):
    return len(re.findall(r"\.{5,}", page)) >= 3


def find_sections(pages):
    """{key: {"page": first PDF page (1-based), "end": last page of the section, "label"}} from the headings found after the table of contents. The first
    standalone heading after the table of contents wins (cross-references inside sentences are never a whole line)."""
    n = len(pages)
    start_at = 0
    for i in range(min(n, TOC_PAGES + 6)):
        if _is_toc(pages[i]):
            start_at = i + 1                                  # the page after the last table-of-contents page
    found, cands = {}, {}
    for i in range(start_at, n):
        if _is_toc(pages[i]):
            continue
        head = _lines(pages[i])[:HEADING_LINES]
        for ln in head:
            for key, rx, label in _HEAD_RES:
                if rx.match(ln.rstrip(" :.")):
                    if i + 1 not in cands.setdefault(key, []):
                        cands[key].append(i + 1)
                    if key not in found:
                        # "FINANCIAL INFORMATION" etc. must not be taken from the summary page; the order below keeps the first genuine one
                        found[key] = {"page": i + 1, "label": label}
    # a summary near the front can carry the same heading ("OUR BUSINESS") as the full section; the full one follows the section named here
    for key, prior in (("business", "industry"), ("industry", "basis")):
        if key in found and prior in found and found[key]["page"] < found[prior]["page"]:
            later = [p for p in cands.get(key, []) if p > found[prior]["page"]]
            if later:
                found[key]["page"] = later[0]
    # "GENERAL INFORMATION" also heads a part of the front matter; the real section is the last such heading before "CAPITAL STRUCTURE"
    if "general_info" in found and "capital" in found:
        before = [p for p in cands.get("general_info", []) if p < found["capital"]["page"]]
        if before:
            found["general_info"]["page"] = before[-1]
    order = sorted(found.items(), key=lambda kv: kv[1]["page"])
    for idx, (key, info) in enumerate(order):
        later = [v["page"] for k, v in order[idx + 1:] if v["page"] > info["page"]]
        info["end"] = (later[0] - 1) if later else min(n, info["page"] + 25)
        info["end"] = max(info["page"], min(info["end"], info["page"] + 60))
    return found


def section_text(pages, sec, max_pages=None):
    a, b = sec["page"], sec["end"]
    if max_pages:
        b = min(b, a + max_pages - 1)
    return "\n".join(pages[a - 1:b])


# ---------------------------------------------------------------------------------------------------------------------------
# THE OFFER
# ---------------------------------------------------------------------------------------------------------------------------
_SHARES = r"([\d,]{5,})"


def _first(rx, text, flags=re.I | re.S):
    m = re.search(rx, text, flags)
    return m


def read_selling_shareholders(text):
    """[{"name", "shares"}] from a table headed "Name of the (Promoter) Selling Shareholder", rows "n. Name  shares ...". Names are tidied; the same name once."""
    out, seen = [], set()
    for hm in re.finditer(r"Name\s+of\s+(?:the\s+)?(?:Promoter\s+|Investor\s+|Other\s+)?(?:Selling\s+)?Shareholders?", text, re.I):
        block = text[hm.start(): hm.start() + 3500]
        for name, shares in re.findall(r"^\s*\d{1,2}\.\s+(.+?)\s{1,}(?:Promoter(?:\s+Group)?|Investor|Other)?\s*([\d,]{5,})\b", block, re.M):
            name = re.sub(r"\s+(Promoter(?: Group)?|Investor|Other)(?: Selling)?(?: Shareholder)?$", "", tidy(name)).strip()
            if 3 <= len(name) <= 90 and re.match(r"[A-Za-z]", name) and name.lower() not in seen and to_int(shares):
                seen.add(name.lower())
                out.append({"name": name, "shares": to_int(shares)})
        if out:
            break
    return out


def read_selling_annexure(pages):
    """[{"name", "shares"}] from a table whose rows read 'n. Name  Up to N Equity Shares ...' (a draft's Annexure A). Found by its header; the next two pages carry the rest."""
    out, seen = [], set()
    for pi, page in enumerate(pages):
        if not re.search(r"Name\s+of\s+the\s+(?:Promoter\s+)?Selling\s+Shareholders?", page, re.I) or not re.search(r"Maximum\s+number\s+of\s+Equity\s+Shares", page, re.I):
            continue
        for chunk in pages[pi: pi + 3]:
            for name, shares in re.findall(r"^\s*\d{1,3}\.\s+([A-Z][A-Za-z.’' \-]{2,70}?)\s+Up\s*to\s*([\d,]{3,})\s+(?:Equity|equity)", chunk, re.M):
                name = tidy(name)
                if name.lower() not in seen and to_int(shares):
                    seen.add(name.lower())
                    out.append({"name": name, "shares": to_int(shares)})
        if out:
            break
    return out


_SELL_HEAD = re.compile(r"^.*\b(?:Date of consent letter|offered for sale|Equity Shares offered|consent letter|Aggregate amount|Offer for Sale\*?|Category of Selling Shareholder|Selling Shareholders?)\b\s*", re.I)


def read_selling_rows(text):
    """[{"name", "shares", "amount_mn"}] from the tables that list the sellers without numbering the rows (cover page, 'The Offer', 'Objects of the Offer'). Three layouts:
    '<name> <type> Selling Shareholder Up to N equity shares ...', '<name> <type> Selling Shareholder [●] 23.08' (amount in rupees million, shares left open) and
    '<name> Up to [●] Equity Shares of face value of ₹ 5 each aggregating up to ₹ 1,200.00 million <date>'. shares is None when the document leaves the count open."""
    flat = re.sub(r"\s+", " ", text)
    out, seen = [], set()

    def add(name, shares, amount):
        name = _SELL_HEAD.sub("", tidy(name))
        name = re.sub(r"\s+(?:Promoter Group|Promoter|Investor|Other|Individual)$", "", name).strip(" ,")
        if 3 <= len(name) <= 90 and len(name.split()) <= 9 and name.lower() not in seen and (shares or amount):
            seen.add(name.lower())
            out.append({"name": name, "shares": shares, "amount_mn": amount})

    typ = r"(?:Promoter Group|Promoter|Investor|Other|Individual)"
    for m in re.finditer(r"([A-Z0-9][A-Za-z0-9.,&'’\- ]{2,90}?)\s+(?:" + typ + r"\s+)?Selling\s+Shareholders?\s+Up\s*to\s*([\d,]{4,})\s+(?:equity|Equity)\s+shares", flat):
        add(m.group(1), to_int(m.group(2)), None)
    if not out:
        for m in re.finditer(r"([A-Z0-9][A-Za-z0-9.,&'’\- ]{2,90}?)\s+" + typ + r"\s+Selling\s+Shareholders?\s+(\[●\]|[\d,]{4,})\s+([\d,]+\.\d{2})\b", flat):
            add(m.group(1), to_int(m.group(2)) if m.group(2) != "[●]" else None, number(m.group(3)))
    if not out:
        for m in re.finditer(r"([A-Z][A-Za-z.,&'’\- ]{2,90}?)\s+Up\s*to\s*(\[●\]|[\d,]{4,})\s+Equity\s+Shares\s+of\s+face\s+value\s+of\s+₹\s*[\d.]+\s+each\s+aggregating\s+(?:up\s+)?to\s+₹\s*([\d,]+(?:\.\d+)?|\[●\])\s*million", flat):
            add(m.group(1), to_int(m.group(2)) if m.group(2) != "[●]" else None, number(m.group(3)))
    return out


def read_offer(pages, secs, notes):
    out = {"fresh_shares": None, "ofs_shares": None, "total_shares": None, "face_value": None, "shares_before": None, "shares_after": None,
           "fresh_amount_mn": None, "ofs_amount_mn": None, "total_amount_mn": None, "no_proceeds_to_company": False, "page": None, "selling_shareholders": [],
           "employee_reservation_text": None}
    sec = secs.get("offer")
    texts = []
    if sec:
        out["page"] = sec["page"]
        texts.append(section_text(pages, sec, 3))
    if secs.get("objects"):
        texts.append(section_text(pages, secs["objects"], 1))
    text = "\n".join(texts)
    flat = re.sub(r"\s+", " ", text)
    flat = re.sub(r"(?<=[A-Za-z])(?:\s?\(\d{1,2}\))+", "", flat)
    flat = re.sub(r"(\d)\s+,", r"\1,", flat)
    m = _first(r"Fresh Issue (?:of )?(?:up to |upto )?" + _SHARES + r" Equity Shares", flat) or _first(r"Fresh Issue\s*(?:up to |upto )?" + _SHARES + r"\s+Equity Shares", flat)
    if m:
        out["fresh_shares"] = to_int(m.group(1))
    m = (_first(r"Offer for Sale (?:of )?(?:an aggregate of )?(?:up to |upto )?" + _SHARES + r" Equity Shares", flat)
         or _first(r"Offer for Sale\s*(?:an aggregate of )?(?:up to |upto )?" + _SHARES + r"\s+Equity Shares", flat))
    if m:
        out["ofs_shares"] = to_int(m.group(1))
    m = _first(r"(?:The )?(?:Offer|Issue) (?:of Equity Shares )?(?:is|comprises|comprising)?\s*(?:of )?(?:an? )?(?:Initial Public (?:Offer|Issue) (?:of|comprising)? )?(?:up to |upto )" + _SHARES + r" Equity Shares", flat)
    if m:
        out["total_shares"] = to_int(m.group(1))
    elif out["fresh_shares"] and out["ofs_shares"]:
        out["total_shares"] = out["fresh_shares"] + out["ofs_shares"]
    elif out["fresh_shares"] and not out["ofs_shares"]:
        out["total_shares"] = out["fresh_shares"]
    if not (out["fresh_shares"] or out["ofs_shares"] or out["total_shares"]):          # an SME issue words it "Fresh Issue ... Upto 32,30,400* Equity Shares"
        m = _first(r"(?:Fresh )?(?:Issue|Offer)\b.{0,160}?(?:up\s*to|upto)\s*([\d,]{6,})\*?\s*Equity Shares", flat)
        if m:
            out["total_shares"] = to_int(m.group(1))
            if not re.search(r"Offer for Sale", flat, re.I):
                out["fresh_shares"] = out["total_shares"]
    m = _first(r"face value (?:of )?(?:₹|INR|Rs\.?)\s*([\d.]+)", flat)
    if m:
        out["face_value"] = m.group(1)
    big = r"(?<![\d,])(\d{1,3}(?:,\d{2,3}){2,})"                        # a share count of at least a million, in either comma style (1,234,567 or 12,34,567)
    m = _first(r"Equity Shares outstanding prior to the (?:Offer|Issue)(?:(?!outstanding after).){0,200}?" + big, flat)
    if m:
        out["shares_before"] = to_int(m.group(1))
    m = _first(r"Equity Shares outstanding after the (?:Offer|Issue)(?:(?!Use of).){0,260}?" + big, flat)
    if m:
        out["shares_after"] = to_int(m.group(1))

    def amount_after(label_rx):
        mm = _first(label_rx + r".{0,200}?aggregating (?:up )?to\s*(?:₹|INR|Rs\.?)\s*([\d,]+(?:\.\d+)?|\[●\])\s*(million|lakhs?|crores?)", flat)
        if not mm:
            return None
        v = number(mm.group(1))
        return None if v is None else round(v * UNIT_TO_CRORE[mm.group(2).lower()] * 10, 2)             # in rupees million (10 million = 1 crore)

    out["fresh_amount_mn"] = amount_after(r"Fresh Issue")
    out["ofs_amount_mn"] = amount_after(r"Offer for Sale")
    out["total_amount_mn"] = amount_after(r"(?:The )?(?:Offer|Issue) (?:of Equity Shares|is|comprises|comprising)")
    if out["fresh_amount_mn"] and out["ofs_amount_mn"] and out["total_amount_mn"] in (None, out["fresh_amount_mn"], out["ofs_amount_mn"]):
        out["total_amount_mn"] = round(out["fresh_amount_mn"] + out["ofs_amount_mn"], 2)             # "The Offer comprises a Fresh Issue of ₹ 5,500 million ..." names the first amount only
    # "will not receive any proceeds" is also said of the offer-for-sale PART of a mixed offer: it means the whole offer only when there is no fresh issue
    out["no_proceeds_to_company"] = (bool(re.search(r"(?:Our )?Company will not receive any (?:portion of the )?proceeds", flat, re.I)) and not out["fresh_shares"]
                                     and not out["fresh_amount_mn"] and not re.search(r"(?<!no )(?<!not a )Fresh Issue\b(?! component)", flat))
    # the employee reservation, as the document words it
    m = _first(r"Employee Reservation Portion[^.]{0,200}?(?:aggregating up to ₹\s*[\d,.\[●\]]+\s*million|[\d,]+ Equity Shares)", flat)
    if m:
        out["employee_reservation_text"] = tidy(m.group(0))[:300]
    # selling shareholders: a table of "n. Name  shares  ..." under a heading that names them
    objects_text = section_text(pages, secs["objects"], 3) if secs.get("objects") else ""
    out["selling_shareholders"] = (read_selling_shareholders(text) or read_selling_annexure(pages)
                                   or read_selling_rows(" ".join(pages[:8]) + " " + text + " " + objects_text))
    sell = out["selling_shareholders"]
    if sell and not (out["ofs_shares"] or out["ofs_amount_mn"]):
        out["selling_shareholders"] = []                                              # no offer for sale was found, so a "seller" read from a table is the company's own fresh issue or a misread
    elif sell and out["ofs_shares"] and all(s.get("shares") for s in sell):
        if abs(sum(s["shares"] for s in sell) - out["ofs_shares"]) > 0.01 * out["ofs_shares"]:
            notes.append("The sellers listed in the document could not be matched to the size of the offer for sale, so they are not shown; read the section on the offer.")
            out["selling_shareholders"] = []                                          # a list that does not add up to the offer is not shown as if it did
    if not (out["fresh_shares"] or out["ofs_shares"] or out["total_shares"]):
        notes.append("The size of the offer could not be read from the document.")
    return out


# ---------------------------------------------------------------------------------------------------------------------------
# OBJECTS OF THE OFFER
# ---------------------------------------------------------------------------------------------------------------------------
OBJECT_TAGS = [
    ("debt_repayment", r"repay|prepay|repayment|pre-payment|redemption of|borrowings"),
    ("working_capital", r"working capital"),
    ("capex", r"capital expenditure|capex|purchase of|setting up|set up|new (?:plant|facility|facilities|unit|store|showroom|branch)|expansion|construction|machiner|equipment|plant\b|civil work|"
             r"infrastructure|procurement of|installation"),
    ("acquisition", r"acqui|strategic investment|inorganic|consideration for"),
    ("subsidiary", r"investment in (?:our )?(?:wholly[- ]owned )?subsidiar|infusion in (?:our )?subsidiar|(?:investment|funding) (?:in|of|to) (?:our )?(?:material )?subsidiar"),
    ("brand_marketing", r"brand|marketing|advertis|promotion"),
    ("technology_rnd", r"technology|research and development|r&d|product development|software|digital|IT infrastructure"),
    ("general_corporate", r"general corporate"),
]
TAG_LABEL = {"debt_repayment": "Repaying borrowings", "working_capital": "Working capital", "capex": "Capital expenditure / expansion", "acquisition": "Acquisitions / strategic investments",
             "subsidiary": "Investment in a subsidiary", "brand_marketing": "Brand building / marketing", "technology_rnd": "Technology / product development",
             "general_corporate": "General corporate purposes", "other": "Other"}


def tag_object(text):
    tags = [k for k, rx in OBJECT_TAGS if re.search(rx, text, re.I)]
    return tags or ["other"]


_UNIT_PAREN = r"\(\s*(?:Amounts?\s*)?(?:in\s*)?(?:Rs\.?|₹|INR)?\s*(?:in\s*)?(?:Rs\.?|₹|INR)?\s*(?:million|millions|lakhs?|lacs?|crores?|mn)\s*\)"
_OBJ_HEADER = re.compile(r"(?:" + _UNIT_PAREN + r"\s*)?(?:Sr\.?|S\.)\s*No\.?[\s\S]{0,300}?(?:" + _UNIT_PAREN + r"|\(\s*%\s*\)|Amount(?=[ \t]*\n)|Proceeds(?=[ \t]*\n))"
                         r"(?:\s*Percentage[\s\S]{0,120}?\(\s*%\s*\))?", re.I)
_UNIT_IN_HEADER = re.compile(r"(?:in\s*)?(?:₹|Rs\.?|INR)?\s*(million|millions|lakhs?|lacs?|crores?|mn)\b", re.I)


def read_objects(pages, secs, notes):
    out = {"items": [], "total_mn": None, "unit": "₹ million", "no_proceeds_to_company": False, "page": None, "intro": None, "table_page": None}
    sec = secs.get("objects")
    if not sec:
        notes.append("The objects of the offer were not found.")
        return out
    out["page"] = sec["page"]
    span = [pages[i] for i in range(sec["page"] - 1, min(sec["end"], sec["page"] + 7))]
    text = "\n".join(span)
    flat0 = re.sub(r"\s+", " ", text)
    out["no_proceeds_to_company"] = (bool(re.search(r"(?:Our )?Company will not receive any (?:portion of the )?proceeds", flat0, re.I))
                                     and not re.search(r"(?<!no )(?<!not a )Fresh Issue\b(?! component)", flat0))
    m = re.search(r"OBJECTS OF THE (?:OFFER|ISSUE)\s*(.{200,1100}?)(?:Offer for Sale\s*\n|Fresh Issue\s*\n|Requirement of Funds|Utili[sz]ation of)", text, re.S | re.I)
    if not m:
        m = re.search(r"OBJECTS OF THE (?:OFFER|ISSUE)\s*(.{200,900}?\.)\s", text, re.S | re.I)
    out["intro"] = tidy(m.group(1))[:900] if m else None
    # the utilisation table, after "Utilization/Utilisation of (the) Net Proceeds"; its header may repeat after a page break, and rows may be numbered "1." or "1"
    text = "\n".join(re.sub(r"\A\s*\d{1,3}\s*\n", "", pg) for pg in span)                 # the printed page number opening each page
    unit, items = "million", []
    for mt in re.finditer(r"Utili[sz]ation of (?:the )?(?:Net )?(?:Offer )?(?:Issue )?Proceeds|Requirement of Funds", text, re.I):
        seg = text[mt.end(): mt.end() + 7000]
        hm = _OBJ_HEADER.search(seg)
        if not hm or hm.start() > 500:
            continue
        head_win = seg[hm.start(): hm.end() + 50]                                      # the header plus the words that may trail it ("(INR in Lakh)  % of Gross Proceeds")
        um = _UNIT_IN_HEADER.search(head_win)
        seg_unit = um.group(1).lower() if um else "million"
        has_pct = bool(re.search(r"Percentage|\(%\)|%\s*of\s+(?:Gross|Net|Total|the)", head_win, re.I))
        body = _OBJ_HEADER.sub(" ", seg[hm.start():])                                  # the header, and any repeat of it after a page break
        first = re.search(r"(?m)^[ \t]*1\.?[ \t]+(?=\S)", body)                         # the rows start at "1"; whatever trails the header before that is not a row
        if not first:
            continue
        body = body[first.start():]
        end = re.search(r"(?m)^\s*(?:Total|Net (?:Issue |Offer )?Proceeds|Gross (?:Issue |Offer )?Proceeds)\b", body[1:])
        body = body[:end.start() + 1] if end else body[:2500]
        body = re.sub(r"(?m)^[ \t]*[A-Z]\.[ \t]+[A-Z][^\n]*[A-Za-z,;:][ \t]*\n", "", body)       # a group heading between rows ("B. Funding for inorganic growth ...")
        marks, expect = [], 1
        for rm in re.finditer(r"(?m)^[ \t]*(\d{1,2})\.?[ \t]+(?=\S)", body):
            if int(rm.group(1)) == expect:
                marks.append(rm)
                expect += 1
        for i, rm in enumerate(marks):
            flat = re.sub(r"\s+", " ", body[rm.end(): marks[i + 1].start() if i + 1 < len(marks) else len(body)]).strip()
            flat = re.sub(r"(?<=[\d\]A-Za-z.])\s*\(\d\)(?!\w)", "", flat)                    # footnote marks: "800.00(1)"
            subs = re.findall(r"\(([a-z])\)\s+(.+?)\s+([\d,]+(?:\.\d+)?|\[●\])(?=\s+\([a-z]\)\s|\s*$)", flat)
            if len(subs) >= 2:                                                           # one object with lettered parts, each with its own amount
                parent = re.sub(r"[\s,;:–-]*(?:namely|being|comprising|including)?[\s,;:–-]*$", "", tidy(flat[:flat.index("(a)")]))
                for _letter, stext, samt in subs:
                    sl = re.sub(r"[\s;,.]+$", "", tidy(stext))
                    items.append({"text": "%s: %s" % (parent, sl), "amount_mn": number(samt), "tags": tag_object(parent + " " + sl)})
                continue
            tm = re.search(r"((?:\s+(?:\[●\][#^*†]*|[\d,]+(?:\.\d+)?%?|\(\d\)|-|–|Nil))+)\s*$", " " + flat)
            toks = [t for t in re.findall(r"\[●\]|[\d,]+(?:\.\d+)?%?|Nil|(?<=\s)[-–](?=\s|$)", tm.group(1))] if tm else []
            label_end = (tm.start() - 1) if tm else len(flat)
            amount = None
            if toks:
                numeric = [t for t in toks if t not in ("[●]", "-", "–", "Nil") and not t.endswith("%")]
                if has_pct and len(toks) >= 2:
                    tok = toks[-2]                                                   # "amount  %": the amount is the one before the percentage column
                    amount = None if tok.endswith("%") or tok in ("[●]", "-", "–", "Nil") else number(tok)
                else:
                    amount = number(numeric[0]) if numeric else None                  # the first figure: the total, when a deployment schedule follows it year by year
            label = re.sub(r"\s*(?:\(\d\)\s*)+$", "", re.sub(r"^\d{1,2}\.\s+(?=[A-Z])", "", tidy(flat[:max(label_end, 0)])))            # "1. 1. Funding ..." (the number printed twice)
            label = re.sub(r"\s*[;,*#^]+\s*$", "", label).strip()
            label = re.sub(r"\s*(?:and|&)\s*$", "", label)
            if 6 <= len(label) <= 400:
                items.append({"text": label, "amount_mn": amount, "tags": tag_object(label)})
        if items:
            unit = seg_unit
            pos = text[:mt.start()].count("\n")
            out["table_page"] = sec["page"] + next((k for k, pg in enumerate(span) if mt.group(0)[:12] in re.sub(r"\s+", " ", pg)), 0)
            break
    if not items:
        um = re.search(r"\(\s*(?:₹|Rs\.?|INR)?\s*in\s*(?:₹\s*)?(million|millions|lakhs?|lacs?|crores?|mn)\b", text, re.I)
        unit = um.group(1).lower() if um else "million"
    out["unit"] = "₹ " + {"mn": "million", "millions": "million", "lakh": "lakhs", "lac": "lakhs", "lacs": "lakhs", "crores": "crore"}.get(unit, unit)
    # the document's amounts are in its own unit: convert to rupees million for storage, the page shows crore
    factor = {"million": 1.0, "millions": 1.0, "mn": 1.0, "lakh": 0.1, "lakhs": 0.1, "lac": 0.1, "lacs": 0.1, "crore": 10.0, "crores": 10.0}.get(unit, 1.0)
    for it in items:
        if it["amount_mn"] is not None:
            it["amount_mn"] = round(it["amount_mn"] * factor, 2)
    out["items"] = items
    known = [it["amount_mn"] for it in items if it["amount_mn"] is not None]
    if known and all(it["amount_mn"] is not None for it in items if "general_corporate" not in it["tags"]):
        out["total_mn"] = round(sum(known), 2)
    if not items and not out["no_proceeds_to_company"]:
        notes.append("The table of how the money will be used was not found in the document; read the section itself.")
    return out


# ---------------------------------------------------------------------------------------------------------------------------
# PROMOTERS
# ---------------------------------------------------------------------------------------------------------------------------
def _clean_person(n):
    """A promoter's name without 'Mr.', a leading 'and', or the words of a sentence that slipped in; '' when it is not a name."""
    n = re.sub(r"^(?:and|&)\s+", "", n.strip(), flags=re.I)
    n = re.sub(r"\s*\((?:formerly|earlier|erstwhile)[^)]*\)?", "", n, flags=re.I)                    # "(formerly known as ...)"
    n = re.sub(r"^\((?:[ivx]+|[a-z]|\d+)\)\s*", "", n)                                               # "(i) Genius Bidco Holdings Pte. Ltd."
    n = re.sub(r"^(?:(?:Individual|Corporate)\s+)?Promoters?\s+(?=[A-Z])", "", n)                    # a sub-heading that ran into the name
    n = re.sub(r"^(?:Mr|Mrs|Ms|Dr|Smt|Shri|Sri)\.?\s+", "", n, flags=re.I).strip(" ,;:")
    n = n.rstrip(".") if not re.search(r"\b(?:Ltd|Inc|Pvt|Co)\.$", n) else n
    if not n or re.match(r"(?i)(?:our|as on|as of|the|promoters?|details)\b", n) or len(n.split()) > 7 or not re.search(r"[A-Z]", n) or len(n) > 90:
        return ""
    words = n.split()
    if sum(1 for w in words if w[:1].isupper()) < 0.7 * len(words):                      # a name is capitalised; a clause of the sentence is not
        return ""
    return n


def read_promoters(pages, secs, notes):
    out = {"names": [], "intro": None, "holding_shares": None, "holding_pct": None, "profiles": [], "page": None}
    sec = secs.get("promoters")
    if not sec:
        notes.append("The promoters section was not found.")
        return out
    out["page"] = sec["page"]
    text = section_text(pages, sec, 3)
    flat = re.sub(r"\s+", " ", text)
    flat = re.sub(r"(\d)\s+,", r"\1,", flat)
    # "The Promoters of our Company are A, B and C."  or  "A, B and C are the Promoters of our Company."
    cands = []                                                         # the sentence that names them: whichever form comes first on the page
    m1 = re.search(r"(?:The|our)\s+Promoters?\s+(?:of our Company\s+)?(?:is|are)\s+(.{3,600}?)\.\s+(?:As on|As of|As at|For details|Our|The|Details|All)", flat, re.I)
    if m1:
        cands.append((m1.start(), tidy(m1.group(1))))
    m2 = re.search(r"((?:[A-Z](?:[^.]|\bInc\.|\bLtd\.|\bPvt\.|\bCo\.){3,500}?))\s+(?:is|are)\s+the\s+Promoters?\s+of\s+our\s+Company", flat)
    if m2:
        w2 = tidy(m2.group(1))
        w2 = re.sub(r"^.*?\b(?:Prospectus|Offer Document|Information Memorandum)\s*,\s*", "", w2)           # "As on the date of this Red Herring Prospectus, A, B and C"
        w2 = re.sub(r"^(?:OUR PROMOTERS? AND PROMOTER GROUP\s*)?(?:Our Promoters?\s+)?(?:Promoters\s+)?", "", w2)    # the page heading that ran into the sentence
        cands.append((m2.start(), w2))
    m3 = re.search(r"(?:following (?:is|are) the Promoters?(?: of our Company)?|The Promoters? of our Company(?: as on the date of this [A-Za-z ]+?)? (?:is|are))\s*:\s*"
                   r"(.{3,900}?)\.\s+(?:Our|The|For|As|Details|Individual|Further)", flat)             # a numbered list: "1. A; 2. B; and 3. C."
    if m3:
        w3 = re.sub(r"(?<![A-Za-z])\d{1,2}\.\s+", "", tidy(m3.group(1)))
        cands.append((m3.start(), w3))
    who = None
    for _pos, cand in sorted(cands):                                   # the earliest sentence that yields real names
        parts = re.split(r";\s*(?:and\s+)?", cand) if ";" in cand else re.split(r",\s*|\s+and\s+|\s*&\s+", cand)
        got = [n for n in (_clean_person(p) for p in parts) if n]
        if got:
            who, out["names"] = cand, got
            break
    if who:
        out["intro"] = ("The Promoters of our Company are " if "," in who or " and " in who else "The Promoter of our Company is ") + who + "."
    num = r"([\d,]{7,}|Nil)"
    pct = r"([\d.]+|Nil)\s*%?"
    m = re.search(r"collectively hold\s+([\d,]{5,})\s+Equity Shares.{0,120}?(?:equivalent to|representing|aggregating to|constituting)\s+(?:approximately\s+)?([\d.]+)\s*%", flat, re.I)
    if not m:
        m = re.search(r"(?:hold|holds)\s+(?:in aggregate\s+)?([\d,]{5,})\s+Equity Shares.{0,120}?(?:equivalent to|representing|aggregating to|constituting)\s+(?:approximately\s+)?([\d.]+)\s*%", flat, re.I)
    if m:
        out["holding_shares"] = to_int(m.group(1))
        out["holding_pct"] = float(m.group(2))
    rows = []
    for rm in re.finditer(r"\b(\d{1,2})\.\s+([A-Z][A-Za-z.,’'& ]{3,80}?)\s+" + num + r"\s+" + pct, flat):           # the table: "1. Name  56,98,420  20.86%" ("Nil Nil" for a promoter with no shares)
        if rm.group(1) == "1" and rows:                                              # a second table (a corporate promoter's own shareholders) starts again at 1
            break
        rows.append(rm.groups()[1:])
    if rows:
        tnames = [n for n in (_clean_person(r[0]) for r in rows) if n]
        covers = all(any(n in t for t in tnames) for n in out["names"])             # every name the sentence gave is in the table (a table keeps "Air Products and Chemicals, Inc." whole)
        if tnames and (not out["names"] or covers or len(tnames) >= len(out["names"])):
            out["names"] = tnames                                                    # the table lists every promoter, in the document's own spelling
        if out["holding_shares"] is None and 1 <= len(rows) <= 12:
            tm = re.search(r"\bTotal\s+([\d,]{7,})\s+([\d.]+)\s*%?", flat[flat.find(rows[-1][0]):])           # the table's own "Total" row, after its last row
            if tm:
                out["holding_shares"], out["holding_pct"] = to_int(tm.group(1)), float(tm.group(2))
            else:                                                                  # no "Total" row: the sum of the rows
                out["holding_shares"] = sum(to_int(r[1]) or 0 for r in rows if r[1] != "Nil")
                out["holding_pct"] = round(sum(float(r[2]) for r in rows if r[2] != "Nil"), 2)
    # individual profiles as the document gives them: "Name, born on ..., aged N years, is the Promoter and also ..." / "Name, aged N years, is a Promoter ..."
    for pm in re.finditer(r"(?<![A-Za-z0-9])([A-Z][A-Za-z.’']+(?: [A-Z][A-Za-z.’']+){1,4})\s*,\s*(?:born on|aged|aged about)\s+([^.]{3,400}?(?:is|was)\s+(?:the |our |a |an )?[^.]{0,300}\.)", flat):
        name = re.sub(r"^(.+?)\s+\1$", r"\1", pm.group(1).strip())                    # the document prints the name twice (heading, then sentence)
        name = re.sub(r"^(?:(.+?) )\1\b", r"\1", name)
        name = _clean_person(name) or name
        for known in out["names"]:                                                     # the promoter list gives the proper spelling; the profile starts with it (maybe after a surname-first heading)
            if name.endswith(known) or set(known.split()) <= set(name.split()):
                name = known
                break
        if name in (p["name"] for p in out["profiles"]) or len(out["profiles"]) >= 8:
            continue
        txt = tidy(name + pm.group(0)[len(pm.group(1)):])
        txt = re.sub(r"\s*(?:He|She|They) resides at.*$", "", txt)
        if not re.search(r"Promoter", txt) and name not in out["names"]:               # a director or officer described near the promoters, not a promoter
            continue
        out["profiles"].append({"name": name, "text": txt[:420]})
    if not out["names"] and out["profiles"]:
        out["names"] = [p["name"] for p in out["profiles"]]
    if not out["names"] and not out["profiles"]:
        notes.append("The promoters could not be read from the document.")
    return out


# ---------------------------------------------------------------------------------------------------------------------------
# BUSINESS
# ---------------------------------------------------------------------------------------------------------------------------
def read_business(pages, secs, notes):
    out = {"overview": None, "page": None}
    sec = secs.get("business")
    if not sec:
        notes.append("The business section was not found.")
        return out
    out["page"] = sec["page"]
    text = section_text(pages, sec, 4)
    # the document's own "Overview" paragraph (after the boilerplate about forward-looking statements and industry reports)
    m = re.search(r"(?m)^\s*(?:OVERVIEW|Overview|Business Overview|OUR BUSINESS OVERVIEW)\s*$\n(.{300,3500})", text, re.S)
    if m:
        body = m.group(1)
    else:
        i = max(text.find("Overview"), text.find("OVERVIEW"))
        body = text[i + 8:] if i >= 0 else text
    body = re.sub(r"(?m)^\s*\d{1,3}\s*$", "", body)                                                   # printed page numbers
    paras = [tidy(p) for p in re.split(r"\n\s*\n", body) if len(tidy(p)) > 120]
    if not paras:
        paras = [tidy(body)]
    ov = " ".join(paras[:3])
    ov = re.sub(r"\s+", " ", ov)
    out["overview"] = ov[:1800] if len(ov) > 150 else None
    if not out["overview"]:
        notes.append("The company's own overview of its business was not found.")
    return out


# ---------------------------------------------------------------------------------------------------------------------------
# FINANCIALS
# ---------------------------------------------------------------------------------------------------------------------------
_NUM = r"\(?-?[\d,]+(?:\.\d+)?\)?|-|–"
_ROW = re.compile(r"^(?P<label>[A-Za-z(][^\n]*?[A-Za-z)\]\d])\s+(?P<nums>(?:(?:%s)(?:\s+|$)){2,6})$" % _NUM)
FIN_ROWS = [
    ("revenue", r"^(?:Total )?Revenue from operations?\b(?! growth)|^Revenue from operations\b|^Total revenue\b", "Revenue from operations"),
    ("total_income", r"^Total income\b|^Total revenue\s*\(|^Total revenue$", "Total income"),
    ("ebitda", r"^EBITDA\b(?! margin)", "EBITDA"),
    ("pbt", r"^Profit\s*/?\s*\(?loss\)? before (?:exceptional items and )?tax|^Profit before (?:exceptional items and )?tax|^Profit\s*/?\s*\(loss\)\s+before\s+tax|"
            r"^\(?Loss\)?\s*/?\s*\(?profit\)?\s+before\s+(?:exceptional items and )?tax|^Loss before (?:exceptional items and )?tax", "Profit before tax"),
    ("pat", r"^(?:Total )?Profit\s*/?\s*\(?loss\)?\s*(?:after tax|for the (?:year|period)(?!\s+attributable)|and total comprehensive income for the (?:year|period))|^Profit after tax|^Net profit(?: after tax)?|^(?:Total )?Profit for the (?:year|period)(?!\s+attributable)|^Profit\s*/\s*\(?loss\)?\s*\(?(?:IX|VII|X)\)?|^\(?Loss\)?\s*/?\s*\(?profit\)?\s*(?:after tax|for the (?:year|period))|^(?:Net )?Loss (?:after tax|for the (?:year|period))", "Profit after tax"),
    ("total_assets", r"^Total assets\b|^Total equity (?:and|&) liabilities\b|^Total$", "Total assets"),
    ("equity", r"^Total equity\b|^Net worth\b|^Shareholders.? funds\b|^Total shareholders.? funds\b", "Total equity / net worth"),
    ("borrowings", r"^Total (?:borrowings|debt)\b", "Total borrowings"),         # a bare "Borrowings" row is usually only the non-current part, so it is not used
    ("eps_basic", r"^\(?i\)?\s*Basic\b|^Basic(?: earnings per share| EPS)?\b|^Earnings per (?:equity )?share.{0,40}basic", "Earnings per share (basic, ₹)"),
]
_FIN_RES = [(k, re.compile(rx, re.I), label) for k, rx, label in FIN_ROWS]


def _period_clusters(block):
    """Every run of 2+ column labels in a page's text, in the order printed: a list of lists. Labels close together (a heading) form one run."""
    found = []
    for m in re.finditer(r"(\d{1,2})[-/.](\d{1,2})[-/.](20\d\d)|(?:March|Mar\.?|September|Sep\.?|June|December|Dec\.?)\s*\d{0,2},?\s*(20\d\d)|(?:Fiscal|FY|F\.Y\.?)\s*(20\d\d)", block, re.I):
        if m.group(3):
            d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            label = ("FY%d" % y) if mth == 3 else "%02d-%02d-%d" % (d, mth, y)
        elif m.group(4):
            label = re.sub(r"\s+", " ", m.group(0)).strip()
        else:
            label = "FY" + m.group(5)
        found.append((m.start(), m.end(), label))
    runs, cur = [], []
    for st, en, label in found:
        if cur and st - cur[-1][1] > 140:
            runs.append([c[2] for c in cur])
            cur = []
        cur.append((st, en, label))
    if cur:
        runs.append([c[2] for c in cur])
    return [r for r in runs if len(r) >= 2]


def _lead_strip(label):
    """'II Revenue from Operations' / 'a. Share Capital' / '(i) Basic' -> the words only."""
    label = re.sub(r"^(?:\(?[ivxIVX]{1,4}\)?[.)]?\s+(?=[A-Za-z])|\(?[a-z]\)\s+|[a-z]\.\s+(?=[A-Z])|\d{1,2}\.\s+(?=[A-Z]))", "", label)
    return re.sub(r"^Restated\s+(?=[A-Za-z])", "", label, flags=re.I)                    # "Restated profit before tax"


def read_financials(pages, secs, notes, layout=None):
    """Key figures from the 'Summary of financial information'. `layout(i)` (optional) gives page i's text in layout mode, which keeps row labels that the plain
    extraction sometimes drops; rows are read from both. Figures are converted to rupees crore; EPS stays in rupees. No unit found = no figures."""
    out = {"unit": None, "periods": [], "rows": {}, "page": None, "basis": None}
    sec = secs.get("summary_fin")
    if not sec:
        notes.append("The summary of financial information was not found.")
        return out
    out["page"] = sec["page"]
    span = list(range(sec["page"] - 1, min(sec["end"], sec["page"] + 6)))
    unit_found = None
    best = {}
    for pi in span:
        page = pages[pi]
        um = (re.search(r"(?:₹|Rs\.?|INR|Rupees)\s*(?:in\s*)?(million|millions|lakhs?|lacs?|crores?)\b", page, re.I)
              or re.search(r"\bin\s*(?:₹|Rs\.?|INR)\s*(million|millions|lakhs?|lacs?|crores?)\b", page, re.I)
              or re.search(r"\(\s*(?:amount\s*)?(?:in\s*)(million|millions|lakhs?|lacs?|crores?)\b", page, re.I))
        if um:
            unit_found = unit_found or um.group(1).lower()
        if re.search(r"PROFIT\s*(?:AND|&)\s*LOSS", page, re.I):
            out["basis"] = out["basis"] or ("consolidated" if re.search(r"CONSOLIDATED", page) else "standalone")
        runs = _period_clusters(page)
        texts = [page]
        if layout is not None:
            try:
                lt = layout(pi)
                if lt:
                    texts.insert(0, lt)
            except Exception:
                pass
        for text in texts:
            for ln in text.split("\n"):
                ln = re.sub(r"\s+", " ", ln).strip()
                ln = re.sub(r"(?<=\d)\.\s+(?=\d{2}(?:\s|$))", ".", ln)                    # a decimal point with a space after it: "37,657. 60"
                m = _ROW.match(ln)
                if not m:
                    continue
                label = _lead_strip(m.group("label"))
                nums = re.findall(_NUM, m.group("nums"))
                for key, rx, flabel in _FIN_RES:
                    if key in best or not rx.search(label):
                        continue
                    vals = [number(x) for x in nums]
                    if not any(v is not None for v in vals):
                        continue
                    per = next((r for r in runs if len(r) == len(vals)), None)
                    if per is None and len(vals) >= 3 and re.fullmatch(r"\d{1,2}", nums[0]):          # a 'Note No.' column in front of the figures
                        per = next((r for r in runs if len(r) == len(vals) - 1), None)
                        if per is not None:
                            vals = vals[1:]
                    if per is None and len(vals) in (2, 3, 4, 5):
                        per = ["Latest year", "Previous year", "Year before"][:len(vals)] if len(vals) <= 3 else ["Latest period"] + ["Year %d" % k for k in range(1, len(vals))]
                    if per is not None and len(per) == len(vals):
                        best[key] = {"label": flabel, "values": vals, "page": pi + 1, "periods": per}
    out["unit"] = unit_found
    if not unit_found:
        notes.append("The unit of the financial tables (million, lakh or crore) was not found, so no financial figures are shown; read the section itself.")
        out["rows"] = {}
        return out
    factor = UNIT_TO_CRORE.get(unit_found, 0.1)
    anchor = best.get("revenue") or best.get("pat") or next(iter(best.values()), None)
    if anchor and anchor["periods"][0] in ("Latest year", "Latest period"):
        # the figures were found but the column headings were not recognised: showing "Year 1, Year 2" would invite a wrong reading, so no figures are shown
        notes.append("The columns of the financial summary could not be matched to their periods, so no figures are shown; read the section itself.")
        return out
    if anchor:
        out["periods"] = anchor["periods"]
    n_periods = len(out["periods"])
    for key, row in best.items():
        vals = row["values"]
        if len(vals) != n_periods:
            continue                                        # a row whose columns do not line up with the periods is left out rather than guessed at
        if key == "eps_basic":
            out["rows"][key] = {"label": row["label"], "values": vals, "page": row["page"]}
        else:
            out["rows"][key] = {"label": row["label"], "values": [None if v is None else round(v * factor, 2) for v in vals], "page": row["page"]}
    if "revenue" not in out["rows"] and "pat" not in out["rows"]:
        notes.append("The financial summary tables could not be read into figures; read the section itself.")
    return out


# ---------------------------------------------------------------------------------------------------------------------------
# AUDITOR, ADDRESS
# ---------------------------------------------------------------------------------------------------------------------------
_FIRM = re.compile(r"((?:(?:[A-Z][\w.'’\-]*|&)\s+){0,7}?(?:LLP|&\s+Associates|and\s+Associates|Associates|&\s+Co\.?|and\s+Co\.?|&\s+Company|&\s+CO\.?)(?:\s+LLP)?)")


def read_auditor(pages, secs):
    """The statutory auditor's firm name (two, joined by 'and', when the document says the audit is joint), from the General Information section. None when
    not found."""
    sec = secs.get("general_info")
    if not sec:
        return None
    text = re.sub(r"\s+", " ", section_text(pages, sec, 6))
    for hm in re.finditer(r"(Joint )?Statutory Auditors?(?: to our Company| of our Company)?", text, re.I):
        window = text[hm.end(): hm.end() + 420]
        names = []
        for m in _FIRM.finditer(window):
            name = re.sub(r"^(?:Peer Review|Address|Statutory|namely|Name)\b[,:]?\s*", "", m.group(1).strip(" ,;:"))
            if name and name not in names and len(name) > 4:
                names.append(name)
            if len(names) >= (2 if hm.group(1) else 1):
                break
        if names:
            return " and ".join(names)[:160]
    return None


# ---------------------------------------------------------------------------------------------------------------------------
# RISK FACTORS: what the company's own list covers (the document's words, grouped by topic; no judgement of how serious any of them is)
# ---------------------------------------------------------------------------------------------------------------------------
RISK_TOPICS = [
    ("customers", "Dependence on customers", r"(?:customers?|clients?).{0,120}(?:concentrat|depend|significant portion|substantial portion|large portion|top (?:five|ten|10|5)|single|few|limited number)|"
                                         r"(?:concentrat|depend).{0,80}(?:customers?|clients?)|top\s*(?:\d+|three|five|ten)\s+(?:customers?|clients?)|"
                                         r"(?:significant|substantial|large|major)\s+portion.{0,60}(?:customers?|clients?)"),
    ("suppliers", "Dependence on suppliers or raw materials", r"(?:suppliers?|vendors?|raw materials?).{0,120}(?:concentrat|depend|single|limited number|few|significant portion)|"
                                                              r"(?:depend|rely|reliance).{0,80}(?:suppliers?|vendors?|raw materials?)|top\s*(?:\d+|three|five|ten)\s+(?:suppliers?|vendors?)"),
    ("litigation", "Pending litigation", r"litigation|legal proceedings|legal and regulatory proceedings|criminal proceedings"),
    ("tax", "Tax proceedings and demands", r"tax (?:proceedings|authorities|demands?|assessments?|disputes?)|income tax|GST|indirect tax"),
    ("related_party", "Related party transactions", r"related[- ]party"),
    ("pledge", "Shares pledged or encumbered", r"pledge|encumbr"),
    ("cash_flow", "Negative cash flows", r"negative (?:operating )?cash flows?|net cash (?:used|outflow)|cash flows? from operati(?:ng|ons?) (?:have|has|were|was) negative"),
    ("debt", "Borrowings and financing covenants", r"indebtedness|borrowings|debt|financing arrangements|financial covenants|restrictive covenants"),
    ("contingent", "Contingent liabilities", r"contingent liabilit"),
    ("compliance", "Past non-compliance or delays in filings", r"non-?compliance|not complied|delays? in (?:filing|payment|making)|regulatory filings|penalt(?:y|ies) (?:was|were|have been)|late filing"),
    ("auditor", "Auditors' observations or qualifications", r"auditors?.{0,80}(?:observations?|qualification|emphasis of matter|remarks?)|emphasis of matter|CARO"),
    ("objects", "Use of proceeds not independently appraised", r"not been appraised|utili[sz]ation of (?:the )?(?:net )?proceeds|objects of the (?:offer|issue).{0,60}(?:variation|vary)"),
    ("people", "Dependence on key people", r"key (?:managerial|management) personnel|senior management|key personnel|loss of (?:the )?services"),
    ("group", "Subsidiaries, group companies and joint ventures", r"subsidiar|group compan|joint venture|associate compan"),
    ("licences", "Approvals, licences and registrations", r"licen[cs]es?|permits?|registrations?|approvals?"),
    ("losses", "Past losses", r"incurred (?:net )?loss|history of (?:net )?loss|net loss(?:es)?|losses in (?:the )?(?:past|previous|recent)"),
    ("property", "Properties, leases and title", r"leased|lease agreements?|title (?:to|of)|immovable propert|premises"),
    ("ip", "Intellectual property", r"intellectual property|trademark|brand name|patents?"),
    ("promoters", "Promoters' control and conflicts", r"promoters? (?:will|continue|may|have).{0,60}(?:control|influence|interest)|conflicts? of interest|promoter group"),
]
_RISK_TOPIC_RES = [(k, label, re.compile(rx, re.I)) for k, label, rx in RISK_TOPICS]
_ABBREV = ("pvt", "ltd", "no", "nos", "rs", "mr", "mrs", "dr", "inc", "co", "m/s", "i.e", "e.g", "approx", "etc", "viz", "vs", "st", "ms", "sr", "jr")


def _first_sentence(text, cap=420):
    """The first sentence of a risk factor (its heading is written as one), not splitting on 'Pvt.', 'Ltd.', 'No.' and the like."""
    text = re.sub(r"\s+", " ", text).strip()
    pos = 0
    for m in re.finditer(r"(?<=[a-z0-9\)’'\]%])\.\s+(?=[A-Z“\"(])", text):
        before = text[:m.start()].rsplit(" ", 1)[-1].lower().strip(".")
        if before in _ABBREV or len(before) == 1:
            continue
        pos = m.start() + 1
        break
    s = text[:pos] if pos else text[:cap]
    return s[:cap].strip()


def read_risks(pages, secs):
    """{"count", "page", "topics": [{"key","label","count","items":[{"n","text","page"}]}], "headings": [{"n","text","page"}]} from the numbered risk factors."""
    sec = secs.get("risk")
    out = {"count": 0, "page": None, "topics": [], "headings": []}
    if not sec:
        return out
    out["page"] = sec["page"]
    last_page = sec["end"]
    nxt = None
    heads = []
    expect = 1
    for pi in range(sec["page"] - 1, min(last_page, sec["page"] + 70)):
        page = pages[pi]
        lines = page.split("\n")
        i = 0
        while i < len(lines):
            m = re.match(r"^\s*(\d{1,3})\.\s+(\S.*)$", lines[i])
            if m and int(m.group(1)) == expect and re.match(r"[A-Z0-9“\"(₹]", m.group(2).strip()):
                chunk = [m.group(2)]
                j = i + 1
                while j < len(lines) and len(" ".join(chunk)) < 700:
                    chunk.append(lines[j])
                    j += 1
                    if re.search(r"[a-z0-9\)’'\]%]\.\s*$", chunk[-1]):
                        break
                heads.append({"n": expect, "text": _first_sentence(" ".join(chunk)), "page": pi + 1})
                expect += 1
                i = j
                continue
            i += 1
    out["count"] = len(heads)
    out["headings"] = heads[:200]
    topics = []
    for key, label, rx in _RISK_TOPIC_RES:
        hits = [h for h in heads if rx.search(h["text"])]
        if hits:
            topics.append({"key": key, "label": label, "count": len(hits), "items": hits[:6]})
    topics.sort(key=lambda t: -t["count"])
    out["topics"] = topics
    return out


# ---------------------------------------------------------------------------------------------------------------------------
# THE WHOLE DOCUMENT
# ---------------------------------------------------------------------------------------------------------------------------
LINK_SECTIONS = ("risk", "litigation", "related_party", "contingent", "basis", "objects", "business", "promoters", "offer", "summary_fin", "mdna", "capital", "industry")


def extract(pages, layout=None):
    """The facts of one offer document. `pages` is a list of page texts (index 0 is page 1); `layout(i)` optionally returns page i's text in layout mode (used for tables)."""
    notes = []
    secs = find_sections(pages)
    if len(secs) < 3:
        notes.append("Few of the document's section headings were found, so little could be read automatically.")
    out = {
        "pages": len(pages),
        "sections": {k: {"page": v["page"], "label": v["label"]} for k, v in secs.items() if k in LINK_SECTIONS},
        "offer": read_offer(pages, secs, notes),
        "objects": read_objects(pages, secs, notes),
        "promoters": read_promoters(pages, secs, notes),
        "business": read_business(pages, secs, notes),
        "financials": read_financials(pages, secs, notes, layout),
        "auditor": read_auditor(pages, secs),
        "risks": read_risks(pages, secs),
    }
    out["notes"] = notes
    return out
