"""
ipo_refresh.py -- the daily job for the IPO section: find the issues, fetch their offer documents, read them, store what was found.
---------------------------------------------------------------------------
    python ipo_refresh.py --probe                     # can this machine reach NSE and SEBI, read a PDF, and write to the database? Changes nothing.
    python ipo_refresh.py --status                    # what is stored, and how the last runs went
    python ipo_refresh.py --dry-run                   # fetch and read, print what would be stored, store nothing
    python ipo_refresh.py                             # the daily run
    python ipo_refresh.py --first-run                 # the first load: look back further (SEBI list 400 days, NSE issues 800 days, documents 150 days)
    python ipo_refresh.py --limit-docs 5 --max-minutes 30    # bound a run (the default is 12 documents, 45 minutes)
    python ipo_refresh.py --symbols HDFIRE,RAVITA     # only these issues' documents
    python ipo_refresh.py --reextract                 # read again every document that was read by an older version of the reader

WHAT A RUN DOES, IN ORDER
    1. The NSE's lists: forthcoming issues, issues open now, and every issue since --past-days ago (with listing date and final price). Each is stored
       or updated by company (ipo_issue).
    2. The NSE's page for each issue that is coming, open, waiting to list or listed lately: lead managers, registrar, lot size, subscription so far,
       and the link to its prospectus (stored as a document to read).
    3. SEBI's list of draft offer documents filed since --sebi-days ago. A new company becomes an issue at the stage "filed".
    4. Where every issue stands today (filed / upcoming / open / closed / listed), from its dates.
    5. Documents, most useful first (the prospectus of an issue that is coming or open; one that listed lately; drafts filed lately; earlier failures,
       at most 3 tries): download, read with ipo_extract.py, store what was read with the page numbers, delete the file.
    6. The short summary of each issue (ipo_store.build_card) is rebuilt for every issue whose document or dates changed.
    One advisory lock stops two runs overlapping. A source that does not answer is reported and skipped; the rest of the run goes on.

NOTHING HERE FORMS A VIEW OF ANY ISSUE. The job copies what the issuers and exchanges publish.

NEEDS: pypdf, curl_cffi (NSE needs a Chrome-like handshake), requests, psycopg, python-dotenv.
CRON (IST; the NSE updates through the evening): 30 21 * * *  cd /opt/mfapi && /opt/mfapi/venv/bin/python ipo_refresh.py >> /var/log/ipo_refresh.log 2>&1
"""

import argparse
import os
import sys
import tempfile
import time
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

import ipo_extract as X
import ipo_sources as S
import ipo_store as ST

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
load_dotenv("/opt/mfapi/.env")

IST = timezone(timedelta(hours=5, minutes=30))
LOCK_KEY = 727451
RUN_TYPE = "IPO_REFRESH"
DETAIL_STAGES = ("upcoming", "open", "closed")


def db_dsn():
    dsn = os.environ.get("FINCHAYA_DB") or os.environ.get("DATABASE_URL") or os.environ.get("MF_DSN")
    if not dsn:
        sys.exit("No FINCHAYA_DB in the environment or .env.")
    return dsn


def today_ist():
    return datetime.now(IST).date()


def say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------------------------------------------------------------
# reading one document
# ---------------------------------------------------------------------------------------------------------------------------------
def fetch_pdf(doc, nse, sebi, tmpdir):
    """Download one document's PDF into tmpdir. Returns (path, bytes, pdf_url)."""
    path = os.path.join(tmpdir, "doc_%d.pdf" % doc["doc_id"])
    if doc["source"] == "SEBI":
        url = doc.get("pdf_url") or sebi.pdf_url(doc["source_ref"])
        n = S.download_pdf(url, path)
        return path, n, url
    url = doc["source_ref"]
    n = nse.download_zip_pdf(url, path)
    return path, n, url


def read_document(path):
    """(highlights, pages) from a PDF on disk; raises ValueError when there is nothing readable in it."""
    pages, layout = S.read_pdf_pages(path)
    chars = sum(len(p) for p in pages)
    if len(pages) < 40 or chars < 40000:
        raise ValueError("%d pages and %d characters of text: too little to be an offer document with a text layer" % (len(pages), chars))
    hl = X.extract(pages, layout=layout)
    hl["meta"] = {"pages": len(pages), "extract_version": ST.EXTRACT_VERSION}
    return hl, len(pages)


# ---------------------------------------------------------------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------------------------------------------------------------
def run(conn, args, nse, sebi, today, fetch=fetch_pdf, reader=read_document):
    """One refresh. `nse`, `sebi`, `fetch`, `reader` are passed in so a test can replace the network and the PDF reader. Returns a Counter of what happened
    and a list of error strings."""
    cur = conn.cursor()
    n, errors = Counter(), []
    started = time.monotonic()

    def step(name, fn):
        try:
            fn()
            conn.commit()
        except S.SourceError as e:
            conn.rollback()
            errors.append("%s: %s" % (name, e))
            say("  ! %s: %s" % (name, e))
        except Exception as e:                                                    # noqa: BLE001
            conn.rollback()
            errors.append("%s: %s: %s" % (name, type(e).__name__, str(e)[:160]))
            say("  ! %s failed:\n%s" % (name, traceback.format_exc()))

    touched = set()

    def safe(label, fn):
        """One record in its own savepoint: a bad record is reported and skipped, the rest of the step goes on. Returns fn's result, or None on failure."""
        try:
            with conn.transaction():
                return fn()
        except Exception as e:                                                    # noqa: BLE001
            errors.append("%s: %s: %s" % (label, type(e).__name__, str(e)[:160]))
            say("  ! %s" % errors[-1])
            return None

    # 1. the exchange's lists
    def lists():
        recs = []
        for label, get in (("forthcoming", nse.upcoming), ("open now", nse.current), ("past", lambda: nse.past(today - timedelta(days=args.past_days)))):
            try:
                got = get()
            except S.SourceError as e:
                errors.append("NSE %s list: %s" % (label, e))
                say("  ! NSE %s list: %s" % (label, e))
                continue
            say("  NSE %-12s %d issue(s)" % (label, len(got)))
            recs.extend(got)
        for rec in recs:
            if not rec.get("name"):
                continue
            iid = safe("issue %s" % rec.get("name"), lambda rec=rec: ST.upsert_exchange_issue(cur, rec, today))
            if iid is not None:
                touched.add(iid)
                n["issues_seen"] += 1
    step("NSE lists", lists)

    # 2. the exchange's page for each issue that is coming, open, waiting to list or listed lately
    def details():
        cur.execute("""SELECT issue_id, symbol, board, stage, listing_date, exchange_detail IS NULL AS no_detail FROM ipo_issue
                       WHERE symbol IS NOT NULL AND (stage IN ('upcoming', 'open', 'closed') OR listing_date >= %s)
                       ORDER BY open_date DESC NULLS LAST""", (today - timedelta(days=args.listed_days),))
        for r in cur.fetchall():
            if args.symbols and r["symbol"] not in args.symbols:
                continue
            if not (r["stage"] in ("upcoming", "open") or r["no_detail"]):
                continue                                                           # settled issues are read once
            if n["details"] >= args.max_details:
                break
            try:
                d = nse.detail(r["symbol"], r["board"])
            except S.SourceError as e:
                errors.append("NSE page for %s: %s" % (r["symbol"], e))
                say("  ! %s" % errors[-1])
                continue
            if safe("NSE page of %s" % r["symbol"], lambda r=r, d=d: ST.set_exchange_detail(cur, r["issue_id"], d, today) or True):
                touched.add(r["issue_id"])
                n["details"] += 1
                if n["details"] % 10 == 0:
                    say("  NSE issue pages read: %d" % n["details"])
        say("  NSE issue pages read: %d" % n["details"])
    if not args.no_nse:
        say("  reading the NSE page of each issue that is coming, open or listed lately (up to %d, about a second each) ..." % args.max_details)
        step("NSE issue pages", details)

    # 3. SEBI's draft offer documents
    def filings():
        say("  reading SEBI's list of draft documents back to %s (25 a page; a long look-back takes a few minutes) ..." % (today - timedelta(days=args.sebi_days)))
        rows = sebi.filings_since(today - timedelta(days=args.sebi_days))
        say("  SEBI list since %s: %d row(s), %d draft(s)" % (today - timedelta(days=args.sebi_days), len(rows), sum(1 for r in rows if r["kind"] == "DRHP")))
        for row in rows:
            got = safe("SEBI row %s" % row.get("company"), lambda row=row: ST.upsert_sebi_filing(cur, row, today))
            if got:
                touched.add(got[0])
                n["drafts_seen"] += 1
    if not args.no_sebi:
        step("SEBI list", filings)

    # 4. stages
    def stages():
        n["stage_changes"] = ST.recompute_stages(cur, today)
    step("stages", stages)

    # 5. documents
    def documents():
        todo = ST.pick_documents(cur, args.limit_docs, today, drhp_days=args.doc_days, listed_days=args.listed_days, reextract=args.reextract)
        if args.symbols:
            todo = [d for d in todo if d["symbol"] in args.symbols]
        say("  %d document(s) to read" % len(todo))
        for doc in todo:
            if (time.monotonic() - started) / 60.0 > args.max_minutes:
                say("  time budget of %d minutes used; the rest waits for the next run" % args.max_minutes)
                break
            label = "%s %s (%s)" % (doc["kind"], doc["name"], doc["source"])
            t0 = time.monotonic()
            conn.commit()
            ST.start_document(cur, doc["doc_id"])
            conn.commit()
            with tempfile.TemporaryDirectory(prefix="ipo_") as tmp:
                try:
                    path, nbytes, url = fetch(doc, nse, sebi, tmp)
                    hl, pages = reader(path)
                except (S.SourceError, ValueError) as e:
                    ST.fail_document(cur, doc["doc_id"], e)
                    conn.commit()
                    n["docs_failed"] += 1
                    say("  ✗ %s: %s" % (label, e))
                    continue
                except Exception as e:                                             # noqa: BLE001
                    ST.fail_document(cur, doc["doc_id"], "%s: %s" % (type(e).__name__, str(e)[:200]))
                    conn.commit()
                    n["docs_failed"] += 1
                    say("  ✗ %s: %s\n%s" % (label, e, traceback.format_exc()))
                    continue
            ST.finish_document(cur, doc["doc_id"], hl, pages, nbytes, pdf_url=url)
            conn.commit()
            touched.add(doc["issue_id"])
            n["docs_read"] += 1
            say("  ✓ %s: %d pages, %.0f s, %d note(s)" % (label, pages, time.monotonic() - t0, len(hl.get("notes") or [])))
    if not args.no_docs:
        step("documents", documents)

    # 6. the summary of each issue that changed (and any that has none yet)
    def cards():
        cur.execute("SELECT issue_id FROM ipo_issue WHERE card IS NULL OR (card->>'v')::int IS DISTINCT FROM %s", (ST.CARD_VERSION,))
        ids = touched | {r["issue_id"] for r in cur.fetchall()}
        for iid in ids:
            if safe("summary of issue %s" % iid, lambda iid=iid: ST.refresh_card(cur, iid) and True):
                n["cards"] += 1
    step("summaries", cards)
    return n, errors


# ---------------------------------------------------------------------------------------------------------------------------------
# probe and status
# ---------------------------------------------------------------------------------------------------------------------------------
def probe():
    ok = True

    def line(name, good, detail=""):
        nonlocal ok
        ok = ok and good
        say("  %-34s %s %s" % (name, "ok " if good else "FAIL", detail))

    say("\nProbe (changes nothing):")
    for mod in ("pypdf", "curl_cffi", "requests", "psycopg"):
        try:
            __import__(mod)
            line("python package " + mod, True)
        except ImportError as e:
            line("python package " + mod, False, "-> pip install %s (%s)" % (mod.replace("_", "-"), e))
    nse = S.NSE()
    try:
        up = nse.upcoming()
        line("NSE forthcoming issues", True, "%d issue(s)" % len(up))
    except Exception as e:                                                         # noqa: BLE001
        line("NSE forthcoming issues", False, "%s -- the NSE may be refusing this machine; the job can be run from another computer" % e)
        up = []
    try:
        past = nse.past(today_ist() - timedelta(days=30))
        line("NSE issues of the last 30 days", True, "%d issue(s)" % len(past))
        if past:
            d = nse.detail(past[0]["symbol"], past[0]["board"])
            line("NSE page of one issue", True, "%s; prospectus link %s" % (past[0]["symbol"], "yes" if d["rhp_url"] else "no"))
    except Exception as e:                                                         # noqa: BLE001
        line("NSE past issues / issue page", False, str(e))
    sebi = S.SEBI()
    try:
        rows = S.sebi_rows(sebi.first_page())
        line("SEBI list of draft documents", bool(rows), "%d row(s) on page 1" % len(rows))
        d = next((r for r in rows if r["kind"] == "DRHP"), None)
        if d:
            line("SEBI page of one draft", True, "PDF " + sebi.pdf_url(d["url"]))
        rows2 = S.sebi_rows(sebi.page(2))
        line("SEBI list page 2", bool(rows2), "%d row(s)" % len(rows2))
    except Exception as e:                                                         # noqa: BLE001
        line("SEBI list", False, str(e))
    try:
        with psycopg.connect(db_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('ipo_issue') IS NOT NULL AS a, to_regclass('ipo_document') IS NOT NULL AS b")
            r = cur.fetchone()
            line("tables ipo_issue / ipo_document", r["a"] and r["b"], "" if r["a"] and r["b"] else "-> python create_ipo_tables.py --admin doadmin")
            if r["a"]:
                cur.execute("""SELECT has_table_privilege(current_user, 'ipo_issue', 'INSERT') AND has_table_privilege(current_user, 'ipo_issue', 'UPDATE') AS w""")
                line("this login may write them", cur.fetchone()["w"])
    except Exception as e:                                                         # noqa: BLE001
        line("database", False, "%s: %s" % (type(e).__name__, str(e)[:120]))
    say("\n%s\n" % ("All good: the job can run here." if ok else "Something above failed; fix it before relying on the daily run."))
    return 0 if ok else 1


def status():
    with psycopg.connect(db_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT stage, count(*) AS n FROM ipo_issue GROUP BY stage ORDER BY 1")
        say("\nissues by stage: " + (", ".join("%s %d" % (r["stage"], r["n"]) for r in cur.fetchall()) or "none"))
        cur.execute("SELECT kind, status, count(*) AS n FROM ipo_document GROUP BY kind, status ORDER BY 1, 2")
        say("documents:      " + (", ".join("%s %s %d" % (r["kind"], r["status"], r["n"]) for r in cur.fetchall()) or "none"))
        cur.execute("SELECT i.name, d.kind, d.attempts, d.error FROM ipo_document d JOIN ipo_issue i USING (issue_id) WHERE d.status = 'failed' ORDER BY d.doc_id DESC LIMIT 8")
        for r in cur.fetchall():
            say("  failed: %s %s after %d try(ies): %s" % (r["kind"], r["name"], r["attempts"], (r["error"] or "")[:120]))
        cur.execute("""SELECT status, started_at AT TIME ZONE 'Asia/Kolkata' AS at, rows_read, rows_written, left(coalesce(error_message, ''), 160) AS err FROM batch_run_log
                       WHERE run_type = %s ORDER BY run_id DESC LIMIT 6""", (RUN_TYPE,))
        runs = cur.fetchall()
        say("last runs:" + ("" if runs else " none yet"))
        for r in runs:
            say("  %s  %-8s issues %s, documents %s  %s" % (r["at"].strftime("%d %b %H:%M") if r["at"] else "?", r["status"], r["rows_read"], r["rows_written"], r["err"]))
    say("")
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Daily refresh of the IPO section (information only).")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="fetch and read, store nothing (the database is not touched)")
    ap.add_argument("--first-run", action="store_true", help="look back further for the first load")
    ap.add_argument("--past-days", type=int, help="NSE issues that opened within this many days (default 400; --first-run 800)")
    ap.add_argument("--sebi-days", type=int, help="SEBI drafts filed within this many days (default 45; --first-run 400)")
    ap.add_argument("--doc-days", type=int, help="read drafts filed within this many days (default 120; --first-run 150)")
    ap.add_argument("--listed-days", type=int, default=90, help="read the prospectus of issues listed within this many days")
    ap.add_argument("--limit-docs", type=int, default=12, help="read at most this many documents in one run")
    ap.add_argument("--max-details", type=int, default=60, help="fetch at most this many NSE issue pages in one run")
    ap.add_argument("--max-minutes", type=int, default=45, help="start no new document after this many minutes")
    ap.add_argument("--symbols", help="comma-separated NSE symbols: only these issues' pages and documents")
    ap.add_argument("--reextract", action="store_true", help="read again documents read by an older version of the reader")
    ap.add_argument("--no-nse", action="store_true")
    ap.add_argument("--no-sebi", action="store_true")
    ap.add_argument("--no-docs", action="store_true")
    a = ap.parse_args(argv)
    a.past_days = a.past_days or (800 if a.first_run else 400)
    a.sebi_days = a.sebi_days or (400 if a.first_run else 45)
    a.doc_days = a.doc_days or 120
    a.symbols = {s.strip().upper() for s in a.symbols.split(",") if s.strip()} if a.symbols else None
    return a


def main(argv=None):
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):                                         # lines appear in a redirected log as they happen
        sys.stdout.reconfigure(line_buffering=True)
    if args.probe:
        return probe()
    if args.status:
        return status()
    today = today_ist()
    started = datetime.now(timezone.utc)
    say("IPO refresh %s%s" % (today, " (dry run: nothing is stored)" if args.dry_run else ""))
    if args.dry_run:
        return dry_run(args, today)
    with psycopg.connect(db_dsn(), row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s) AS got", (LOCK_KEY,))
        if not cur.fetchone()["got"]:
            say("Another IPO refresh is still running; skipping this one.")
            return 0
        conn.commit()
        try:
            n, errors = run(conn, args, S.NSE(), S.SEBI(), today)
        except Exception as e:                                                     # noqa: BLE001
            conn.rollback()
            cur.execute("INSERT INTO batch_run_log (run_type, business_date, status, started_at, finished_at, error_message) VALUES (%s, %s, 'FAILED', %s, now(), %s)",
                        (RUN_TYPE, today, started, ("%s: %s" % (type(e).__name__, e))[:500]))
            conn.commit()
            raise
        status_word = "SUCCESS" if not errors and not n["docs_failed"] else "PARTIAL"
        cur.execute("""INSERT INTO batch_run_log (run_type, business_date, status, started_at, finished_at, rows_read, rows_written, error_message)
                       VALUES (%s, %s, %s, %s, now(), %s, %s, %s)""",
                    (RUN_TYPE, today, status_word, started, n["issues_seen"] + n["drafts_seen"], n["docs_read"],
                     ("; ".join(errors) or ("%d document(s) failed" % n["docs_failed"] if n["docs_failed"] else None) or None)))
        conn.commit()
    say("done: %s" % ", ".join("%s %d" % kv for kv in sorted(n.items())))
    return 0 if status_word == "SUCCESS" else 1


def dry_run(args, today):
    nse, sebi = S.NSE(), S.SEBI()
    try:
        up = nse.upcoming() + nse.current()
        say("forthcoming/open on the NSE: %d" % len(up))
        for r in up:
            say("  %-10s %-9s %s  %s - %s  band %s-%s" % (r["symbol"], r["board"], r["name"], r["open_date"], r["close_date"], r["price_low"], r["price_high"]))
    except S.SourceError as e:
        say("NSE: %s" % e)
    try:
        rows = sebi.filings_since(today - timedelta(days=min(args.sebi_days, 10)))
        say("SEBI drafts in the last %d days: %d" % (min(args.sebi_days, 10), sum(1 for r in rows if r["kind"] == "DRHP")))
        for r in [r for r in rows if r["kind"] == "DRHP"][:10]:
            say("  %s  %s" % (r["date"], r["company"]))
    except S.SourceError as e:
        say("SEBI: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
