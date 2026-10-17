"""
ipo_store.py -- the IPO tables: writing what the daily job found, and reading it back as the page shows it. SQL and plain dicts only.
---------------------------------------------------------------------------
    upsert_exchange_issue    an issue the NSE lists (upcoming, open, or already trading)
    upsert_sebi_filing       a draft offer document on SEBI's list
    set_exchange_detail      the NSE's page for one issue (lead managers, registrar, lot ...) and its prospectus link
    recompute_stages         where every issue stands today
    pick_documents           which documents to read next (so a run takes a bounded time)
    finish_document          what was read from one document
    build_card               the short summary of an issue (shown in lists and filtered by the scanner), from the document read for it

EVERYTHING HERE RECORDS FACTS. There is no score, rank, rating or verdict in any column, and build_card computes none: its numbers are the document's
numbers (sizes, shares, amounts, holdings) and plain arithmetic on them (a growth rate, a share of the total).
"""

import json
import re
from datetime import date, timedelta

import psycopg

import ipo_sources as S

EXTRACT_VERSION = 1
CARD_VERSION = 1
MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------------------------------------------------------------
def _slug_for(cur, name, symbol, company_key):
    base = S.slugify(name)
    cur.execute("SELECT company_key FROM ipo_issue WHERE slug = %s", (base,))
    row = cur.fetchone()
    if row is None or row["company_key"] == company_key:
        return base
    alt = S.slugify("%s %s" % (name, symbol or "")) if symbol else base + "-2"
    cur.execute("SELECT company_key FROM ipo_issue WHERE slug = %s", (alt,))
    row = cur.fetchone()
    if row is None or row["company_key"] == company_key:
        return alt
    n = 3
    while True:
        cand = "%s-%d" % (base, n)
        cur.execute("SELECT 1 FROM ipo_issue WHERE slug = %s", (cand,))
        if cur.fetchone() is None:
            return cand
        n += 1


def _find_issue(cur, company_key, symbol=None):
    """The row for this company: by symbol (the exchange's own identifier), else by name key, else a close spelling of a name already filed."""
    if symbol:
        cur.execute("SELECT * FROM ipo_issue WHERE symbol = %s", (symbol,))
        r = cur.fetchone()
        if r:
            return r
    cur.execute("SELECT * FROM ipo_issue WHERE company_key = %s", (company_key,))
    r = cur.fetchone()
    if r:
        return r
    if len(company_key) >= 8:                                   # one spelling inside the other ("pentacleconsultants" / "pentacleconsultantsindia")
        cur.execute("""SELECT * FROM ipo_issue WHERE symbol IS NULL AND length(company_key) >= 8
                       AND (company_key LIKE %s OR %s LIKE company_key || '%%') ORDER BY issue_id LIMIT 2""", (company_key + "%", company_key))
        rows = cur.fetchall()
        if len(rows) == 1:
            return rows[0]
    return None


def upsert_exchange_issue(cur, rec, today):
    """Insert or update the issue the NSE lists. The exchange's dates, band, price and lot win over what is stored; a field the exchange leaves empty
    keeps what was there. Returns the issue_id."""
    key = S.company_key(rec["name"])
    if not key:
        raise ValueError("an exchange record without a company name: %r" % (rec,))
    cur_row = _find_issue(cur, key, rec.get("symbol"))
    fields = {k: rec.get(k) for k in ("symbol", "board", "open_date", "close_date", "listing_date", "price_low", "price_high", "issue_price", "lot_size", "issue_size_shares")}
    if cur_row is None:
        slug = _slug_for(cur, rec["name"], rec.get("symbol"), key)
        stage = S.stage_for(today, fields["open_date"], fields["close_date"], fields["listing_date"], have_nse=True)
        cur.execute("""INSERT INTO ipo_issue (company_key, slug, name, symbol, board, stage, open_date, close_date, listing_date, price_low, price_high,
                                              issue_price, lot_size, issue_size_shares)
                       VALUES (%(key)s, %(slug)s, %(name)s, %(symbol)s, %(board)s, %(stage)s, %(open_date)s, %(close_date)s, %(listing_date)s, %(price_low)s,
                               %(price_high)s, %(issue_price)s, %(lot_size)s, %(issue_size_shares)s) RETURNING issue_id""",
                    dict(fields, key=key, slug=slug, name=rec["name"], stage=stage))
        return cur.fetchone()["issue_id"]
    merged = {k: (v if v is not None else cur_row[k]) for k, v in fields.items()}
    merged["board"] = fields["board"] or cur_row["board"]
    stage = S.stage_for(today, merged["open_date"], merged["close_date"], merged["listing_date"], have_nse=True)
    cur.execute("""UPDATE ipo_issue SET name = %(name)s, symbol = %(symbol)s, board = %(board)s, stage = %(stage)s, open_date = %(open_date)s, close_date = %(close_date)s,
                          listing_date = %(listing_date)s, price_low = %(price_low)s, price_high = %(price_high)s, issue_price = %(issue_price)s,
                          lot_size = %(lot_size)s, issue_size_shares = %(issue_size_shares)s, updated_at = now()
                   WHERE issue_id = %(id)s""", dict(merged, stage=stage, id=cur_row["issue_id"], name=rec["name"]))      # the exchange's spelling of the name wins
    return cur_row["issue_id"]


def upsert_sebi_filing(cur, row, today):
    """A DRHP on SEBI's list: the issue (new ones start as 'filed') and its document. Addenda and other notices are not stored. Returns (issue_id, doc_id)
    or None for a row that is not a draft offer document."""
    if row["kind"] != "DRHP":
        return None
    key = S.company_key(row["company"])
    if not key:
        return None
    cur_row = _find_issue(cur, key)
    if cur_row is None:
        slug = _slug_for(cur, row["company"], None, key)
        cur.execute("""INSERT INTO ipo_issue (company_key, slug, name, board, stage, sebi_filed_on, sebi_url)
                       VALUES (%s, %s, %s, 'mainboard', 'filed', %s, %s) RETURNING issue_id""", (key, slug, row["company"], row["date"], row["url"]))
        issue_id = cur.fetchone()["issue_id"]
    else:
        issue_id = cur_row["issue_id"]
        filed = cur_row["sebi_filed_on"]
        if filed is None or (row["date"] and row["date"] < filed):             # the first draft's date; a later one is a resubmission
            cur.execute("UPDATE ipo_issue SET sebi_filed_on = %s, sebi_url = %s, updated_at = now() WHERE issue_id = %s", (row["date"], row["url"], issue_id))
    cur.execute("""INSERT INTO ipo_document (issue_id, kind, source, source_ref, doc_date) VALUES (%s, 'DRHP', 'SEBI', %s, %s)
                   ON CONFLICT (issue_id, kind, source_ref) DO UPDATE SET doc_date = EXCLUDED.doc_date RETURNING doc_id""", (issue_id, row["url"], row["date"]))
    return issue_id, cur.fetchone()["doc_id"]


def set_exchange_detail(cur, issue_id, detail, today):
    """The NSE's page for one issue. Keeps it (jsonb), fills the lot and size the list did not give, and notes the prospectus link as a document to read."""
    cur.execute("SELECT lot_size FROM ipo_issue WHERE issue_id = %s", (issue_id,))
    lot = (cur.fetchone() or {}).get("lot_size")
    keep = {k: detail.get(k) for k in ("brlms", "registrar", "rhp_url", "issue_size_text", "face_value", "lot_size", "price_range", "issue_period", "issue_type",
                                         "subscription")}
    keep["fields"] = {k: v for k, v in (detail.get("fields") or {}).items() if k.lower() in S_KEEP_FIELDS or k.lower().startswith("book running")}
    cur.execute("UPDATE ipo_issue SET exchange_detail = %s::jsonb, lot_size = COALESCE(lot_size, %s), updated_at = now() WHERE issue_id = %s",
                (json.dumps(keep), detail.get("lot_size") if lot is None else lot, issue_id))
    if detail.get("rhp_url"):
        cur.execute("""INSERT INTO ipo_document (issue_id, kind, source, source_ref, doc_date, pdf_url) VALUES (%s, 'RHP', 'NSE', %s, %s, %s)
                       ON CONFLICT (issue_id, kind, source_ref) DO NOTHING""", (issue_id, detail["rhp_url"], today, detail["rhp_url"]))


S_KEEP_FIELDS = {"issue size", "issue type", "price range", "face value", "bid lot", "lot size", "minimum order quantity", "issue period", "tick size",
                 "name of the registrar", "ipo market timings", "discount", "maximum subscription amount for retail investor"}


EVIDENCE_SQL = """
SELECT i.issue_id, i.close_date,
       min(o.as_of_date) AS first_any,
       min(o.as_of_date) FILTER (WHERE o.as_of_date > i.close_date) AS first_after
FROM ipo_issue i
JOIN stock_master m ON m.symbol = i.symbol AND m.is_active
JOIN stock_ohlc_daily o ON o.isin = m.isin AND o.close IS NOT NULL AND o.close > 0
WHERE i.issue_id = ANY(%(ids)s) AND i.close_date IS NOT NULL
GROUP BY i.issue_id, i.close_date
"""


def listing_evidence(run, rows):
    """{issue_id: first trading day} for issues whose bidding closed, that the exchange's list gives NO listing date for, and that FinChaya's own prices show trading.
    run(sql, params) -> list of dict rows. The first price after the close counts as the listing day only when it is within LISTING_EVIDENCE_DAYS of the close and the
    symbol has NO price on or before the close (a ticker used before by another company would put the old company's prices here)."""
    ids = [r["issue_id"] for r in rows if r.get("symbol") and r.get("close_date") and r.get("listing_date") is None]
    if not ids:
        return {}
    out = {}
    for e in run(EVIDENCE_SQL, {"ids": ids}):
        after, any_ = e["first_after"], e["first_any"]
        if after is not None and any_ is not None and any_ > e["close_date"] and (after - e["close_date"]).days <= S.LISTING_EVIDENCE_DAYS:
            out[e["issue_id"]] = after
    return out


def recompute_stages(cur, today):
    """Where every issue stands today, from its dates and from what has traded. An issue that is only a filing (no exchange symbol) stays 'filed'. An issue whose
    bidding closed and that has traded since, though the exchange's list gave no listing date, becomes 'listed' with its first trading day as the listing date.
    (An issue that was withdrawn, postponed or never listed stays 'closed' in the table: the pages show it as ended, see ipo_api.)"""
    cur.execute("SELECT issue_id, symbol, stage, open_date, close_date, listing_date FROM ipo_issue")
    rows = cur.fetchall()

    def run(sql, params):
        cur.execute(sql, params)
        return cur.fetchall()
    cur.execute("SAVEPOINT listing_evidence")
    try:
        first = listing_evidence(run, rows)
        cur.execute("RELEASE SAVEPOINT listing_evidence")
    except psycopg.errors.UndefinedTable:                    # a server without the stock tables: the dates alone decide, as before
        cur.execute("ROLLBACK TO SAVEPOINT listing_evidence")
        first = {}
    changed = 0
    for r in rows:
        new = S.stage_for(today, r["open_date"], r["close_date"], r["listing_date"], have_nse=r["symbol"] is not None)
        if r["issue_id"] in first and new == "closed":
            cur.execute("UPDATE ipo_issue SET stage = 'listed', listing_date = %s, updated_at = now() WHERE issue_id = %s", (first[r["issue_id"]], r["issue_id"]))
            changed += 1
        elif new != r["stage"]:
            cur.execute("UPDATE ipo_issue SET stage = %s, updated_at = now() WHERE issue_id = %s", (new, r["issue_id"]))
            changed += 1
    return changed


def pick_documents(cur, limit, today, drhp_days=120, listed_days=90, reextract=False):
    """The documents to read next, most useful first: the prospectus of an issue that is coming or open, then one that listed lately, then draft
    documents filed lately (newest first), then earlier failures. A draft is skipped once its issue has a prospectus."""
    cur.execute("""
        SELECT d.doc_id, d.issue_id, d.kind, d.source, d.source_ref, d.pdf_url, d.attempts, d.status, d.doc_date, i.name, i.symbol, i.stage, i.board,
               CASE WHEN d.kind = 'RHP' AND i.stage IN ('upcoming', 'open', 'closed') THEN 1
                    WHEN d.kind = 'RHP' THEN 2
                    ELSE 3 END AS prio
        FROM ipo_document d JOIN ipo_issue i USING (issue_id)
        WHERE (d.status = 'new'
               OR (d.status = 'failed' AND d.attempts < %(max)s AND (d.fetched_at IS NULL OR d.fetched_at < now() - interval '20 hours'))
               OR (%(re)s AND d.status = 'done' AND d.extract_version IS DISTINCT FROM %(v)s))
          AND ((d.kind = 'RHP' AND (i.stage IN ('upcoming', 'open', 'closed') OR i.listing_date >= %(listed)s))
               OR (d.kind = 'DRHP' AND i.stage = 'filed' AND d.doc_date >= %(drhp)s
                   AND NOT EXISTS (SELECT 1 FROM ipo_document r WHERE r.issue_id = d.issue_id AND r.kind = 'RHP')))
        ORDER BY prio, (d.status = 'failed'), d.doc_date DESC NULLS LAST, d.doc_id
        LIMIT %(limit)s""",
                {"max": MAX_ATTEMPTS, "re": reextract, "v": EXTRACT_VERSION, "listed": today - timedelta(days=listed_days),
                 "drhp": today - timedelta(days=drhp_days), "limit": limit})
    return cur.fetchall()


def start_document(cur, doc_id):
    cur.execute("UPDATE ipo_document SET attempts = attempts + 1, fetched_at = now() WHERE doc_id = %s", (doc_id,))


def finish_document(cur, doc_id, highlights, pages, nbytes, pdf_url=None):
    cur.execute("""UPDATE ipo_document SET status = 'done', error = NULL, highlights = %s::jsonb, pages = %s, bytes = %s, extract_version = %s,
                          extracted_at = now(), pdf_url = COALESCE(%s, pdf_url) WHERE doc_id = %s""",
                (json.dumps(highlights, default=str), pages, nbytes, EXTRACT_VERSION, pdf_url, doc_id))


def fail_document(cur, doc_id, error):
    cur.execute("UPDATE ipo_document SET status = 'failed', error = %s WHERE doc_id = %s", (str(error)[:400], doc_id))


def best_document(cur, issue_id):
    """The document the page is built from: the newest finished prospectus, else the newest finished draft. None if nothing has been read."""
    cur.execute("""SELECT doc_id, kind, source, source_ref, doc_date, pdf_url, pages, extracted_at, highlights FROM ipo_document
                   WHERE issue_id = %s AND status = 'done' AND highlights IS NOT NULL
                   ORDER BY (kind = 'RHP' OR kind = 'PROSPECTUS') DESC, doc_date DESC NULLS LAST, doc_id DESC LIMIT 1""", (issue_id,))
    return cur.fetchone()


def refresh_card(cur, issue_id):
    cur.execute("SELECT * FROM ipo_issue WHERE issue_id = %s", (issue_id,))
    issue = cur.fetchone()
    doc = best_document(cur, issue_id)
    card = build_card(issue, doc)
    cur.execute("UPDATE ipo_issue SET card = %s::jsonb WHERE issue_id = %s", (json.dumps(card, default=str), issue_id))
    return card


# ---------------------------------------------------------------------------------------------------------------------------------
# the short summary of an issue
# ---------------------------------------------------------------------------------------------------------------------------------
def _crore_from_mn(v):
    return None if v is None else round(v / 10.0, 2)


def _annual_columns(periods):
    """Indexes of the full-year columns (FY2026 / March 31, 2026 / March 2026), not a part-year stub such as June 30, 2026."""
    return [i for i, p in enumerate(periods) if re.match(r"^FY\d{4}$", p) or p.lower().startswith("march") or re.search(r"31-03-\d{4}", p)]


def _pct_change(new, old):
    if new is None or old is None or old <= 0:
        return None
    return round((new / old - 1) * 100, 1)


def build_card(issue, doc):
    """The numbers the list page shows and the scanner filters on. `issue` is an ipo_issue row, `doc` the best finished document (or None).
    Every key is a fact from the document or arithmetic on facts; a figure that could not be read is None, never guessed."""
    card = {"v": CARD_VERSION, "doc_kind": None, "doc_date": None, "read": False, "size_cr": None, "size_basis": None}
    px0 = issue.get("issue_price") or issue.get("price_high")
    if px0 and issue.get("issue_size_shares"):                       # the exchange's own list: shares offered x the price (or the top of the band)
        card["size_cr"] = round(float(px0) * issue["issue_size_shares"] / 1e7, 2)
        card["size_basis"] = "issue price" if issue.get("issue_price") else "top of the price band"
    if not doc or not doc.get("highlights"):
        return card
    h = doc["highlights"]
    # a draft's date is the day it was filed with SEBI; for a prospectus the stored date is only the day the job first saw it, which is not worth showing
    card.update(read=True, doc_kind=doc["kind"], doc_date=str(doc["doc_date"]) if doc.get("doc_date") and doc["kind"] == "DRHP" else None)
    o = h.get("offer") or {}
    fresh, ofs, total = o.get("fresh_shares"), o.get("ofs_shares"), o.get("total_shares")
    fresh_known = bool(fresh or o.get("fresh_amount_mn"))            # a draft may give the size of the fresh issue in rupees and leave the share count open
    if fresh_known and ofs:
        issue_type = "mix"
    elif fresh_known:
        issue_type = "fresh"
    elif ofs:
        issue_type = "ofs"
    else:
        issue_type = None
    card["issue_type"] = issue_type
    card["fresh_pct"] = round(100.0 * fresh / total, 1) if fresh and total and total >= fresh else None
    card["no_proceeds_to_company"] = bool(o.get("no_proceeds_to_company"))
    size = _crore_from_mn(o.get("total_amount_mn"))
    basis = "document" if size else None
    if size is None and o.get("fresh_amount_mn") and ofs is None:
        size, basis = _crore_from_mn(o["fresh_amount_mn"]), "document"
    px = issue.get("issue_price") or issue.get("price_high")
    shares = total or issue.get("issue_size_shares")
    if size is None and px and shares:
        size, basis = round(float(px) * shares / 1e7, 2), ("issue price" if issue.get("issue_price") else "top of the price band")
    card["size_cr"], card["size_basis"] = size, basis
    card["shares_total"], card["shares_fresh"], card["shares_ofs"] = total, fresh, ofs
    card["sellers"] = len(o.get("selling_shareholders") or [])
    # what the money is for
    ob = h.get("objects") or {}
    uses = {}
    for it in ob.get("items") or []:
        for t in it.get("tags") or []:
            uses[t] = uses.get(t, 0.0) + (it.get("amount_mn") or 0.0)
    card["uses"] = sorted(uses, key=lambda t: -uses[t])
    card["objects_total_cr"] = _crore_from_mn(ob.get("total_mn"))
    known = sum(it["amount_mn"] for it in ob.get("items") or [] if it.get("amount_mn"))
    card["use_pct"] = {t: round(100.0 * v / known, 1) for t, v in uses.items() if known and v} if known else {}
    # promoters
    p = h.get("promoters") or {}
    card["promoters"] = p.get("names") or []
    card["promoters_pct"] = p.get("holding_pct")
    sellers = {s["name"].lower() for s in (o.get("selling_shareholders") or [])}
    card["promoters_selling"] = bool(sellers and any(n.lower() in sellers for n in card["promoters"]))
    # financials (rupees crore)
    f = h.get("financials") or {}
    rows, per = f.get("rows") or {}, f.get("periods") or []
    ann = _annual_columns(per)
    rev, pat = rows.get("revenue"), rows.get("pat")
    card["fin_basis"] = f.get("basis")
    card["fin_period"] = per[ann[0]] if ann else (per[0] if per else None)
    if rev and ann:
        card["revenue_cr"] = rev["values"][ann[0]]
        if len(ann) > 1:
            card["revenue_growth"] = _pct_change(rev["values"][ann[0]], rev["values"][ann[1]])
    if pat and ann:
        card["pat_cr"] = pat["values"][ann[0]]
        if len(ann) > 1 and pat["values"][ann[1]] is not None and pat["values"][ann[0]] is not None:
            card["pat_growth"] = _pct_change(pat["values"][ann[0]], pat["values"][ann[1]]) if pat["values"][ann[1]] > 0 else None
    if rev and pat and ann and rev["values"][ann[0]] and pat["values"][ann[0]] is not None:
        card["pat_margin"] = round(100.0 * pat["values"][ann[0]] / rev["values"][ann[0]], 1)
    card["loss_making"] = bool(pat and ann and pat["values"][ann[0]] is not None and pat["values"][ann[0]] < 0)
    # the risk factors
    rk = h.get("risks") or {}
    card["risk_count"] = rk.get("count") or 0
    card["risk_topics"] = [t["key"] for t in (rk.get("topics") or [])]
    return card
