"""
billing_invoices.py -- the invoice for every successful charge, and the
email that tells the customer about it.
=====================================================================
Used by payments.py; nothing here talks to Razorpay.

WHAT IS WRITTEN, AND WHEN
    One row in billing_invoice per successful payment (payment_id is
    UNIQUE, so a replayed webhook cannot make a second invoice). It is
    created inside the SAME transaction as the access grant, so there is
    never a paid period without an invoice or an invoice without access.

    The customer's billing details (legal name, GSTIN, address, state)
    live in billing_customer, one row per account, filled in by the
    customer from "My subscription". They are COPIED onto the invoice at
    issue time: an invoice is a record of what was said on the day, and
    a later edit of the details must not rewrite old invoices.

GST
    Prices on the site are INCLUSIVE of GST. The invoice works the tax
    back out of the amount actually charged (so an offer or a changed
    plan price is still right):

        taxable = amount / (1 + rate)        rate from FINCHAYA_GST_RATE, 18%
        tax     = amount - taxable

    Same state as the supplier  -> CGST + SGST, half each
    Different state             -> IGST
    State unknown               -> the supplier's state (CGST + SGST),
                                   which is the rule for a consumer whose
                                   address is not on record.

    WHETHER IT IS A TAX INVOICE AT ALL depends on FINCHAYA_GSTIN in .env.
    Unset, FinChaya is not registered, no GST may be charged or shown, and
    the document is a plain RECEIPT with no tax split. Set it the day the
    registration comes through and every later charge gets a tax invoice.
    Nothing else changes.

INVOICE NUMBERS
    FC/2026-27/000001 -- a run per Indian financial year (April to March),
    no gaps, no reuse, which is what the GST rules ask for. The counter is
    the highest number already issued in that year, read under an advisory
    lock so two webhooks arriving together cannot pick the same one.

SETTINGS (all in /opt/mfapi/.env; the first four appear on the invoice)
    FINCHAYA_LEGAL_NAME     the name on the invoice      default "FinChaya"
    FINCHAYA_ADDRESS        one line, commas are fine
    FINCHAYA_STATE_CODE     two-digit GST state code, e.g. 29 for Karnataka
    FINCHAYA_GSTIN          15 characters; unset = receipts, not tax invoices
    FINCHAYA_GST_RATE       default 18
    FINCHAYA_SAC            service code on the invoice, default 998439
                            (other online content n.e.c.) -- confirm with
                            your CA; 998431 is the other common choice
    FINCHAYA_SITE_URL       default https://mf.finchaya.com (links in email)
    mail settings           the receipt goes out through mailer.py (Resend over HTTPS, or SMTP from MF_SMTP_* as before);
                            see mailer.py. Without mailer.py uploaded it falls back to the old SMTP-only sending.
"""

import html
import os
import re
import smtplib
import threading
from datetime import date, datetime, timezone
from email.message import EmailMessage
from typing import Optional

from psycopg.types.json import Json

LEGAL_NAME = os.environ.get("FINCHAYA_LEGAL_NAME", "").strip() or "FinChaya"
ADDRESS = os.environ.get("FINCHAYA_ADDRESS", "").strip()
STATE_CODE = os.environ.get("FINCHAYA_STATE_CODE", "").strip()
GSTIN = os.environ.get("FINCHAYA_GSTIN", "").strip().upper()
GST_RATE = float(os.environ.get("FINCHAYA_GST_RATE", "18") or 18)
SAC = os.environ.get("FINCHAYA_SAC", "").strip() or "998439"
SITE_URL = (os.environ.get("FINCHAYA_SITE_URL", "").strip()
            or "https://mf.finchaya.com").rstrip("/")
SUPPORT = "finchaya2026@gmail.com"

# GST state codes. The first two characters of a GSTIN are one of these.
STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def validate_gstin(gstin: str) -> Optional[str]:
    """None if fine, else the complaint. Format and state prefix only --
    the check digit is not verified here, the GST portal does that."""
    g = (gstin or "").strip().upper()
    if not g:
        return None
    if not GSTIN_RE.match(g):
        return ("A GSTIN is 15 characters: 2 digits, 5 letters, 4 digits, "
                "a letter, a digit or letter, Z, and a final digit or letter.")
    if g[:2] not in STATES:
        return "The first two digits of the GSTIN are not a state code."
    return None


def fy_label(d: date) -> str:
    """Indian financial year: 2026-04-01 .. 2027-03-31 -> '2026-27'."""
    y = d.year if d.month >= 4 else d.year - 1
    return "%d-%02d" % (y, (y + 1) % 100)


def split_tax(amount_paise: int, customer_state: Optional[str]):
    """Work GST back out of an inclusive amount. All figures in paise.

    Returns (taxable, cgst, sgst, igst, place_of_supply_state_code).
    Without a supplier GSTIN there is no tax: everything is 'taxable' and
    the splits are zero, which is what a receipt shows."""
    if not GSTIN:
        return amount_paise, 0, 0, 0, (customer_state or STATE_CODE or None)
    taxable = round(amount_paise / (1 + GST_RATE / 100))
    tax = amount_paise - taxable
    pos = customer_state or STATE_CODE or None
    if pos and STATE_CODE and pos != STATE_CODE:
        return taxable, 0, 0, tax, pos
    cgst = tax // 2
    return taxable, cgst, tax - cgst, 0, pos


def next_invoice_no(cur, on: date) -> str:
    """The next number in this financial year's run. Takes a transaction-
    scoped advisory lock so two charges in the same second cannot both
    read the same maximum."""
    fy = fy_label(on)
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('billing_invoice_no'))")
    cur.execute("""
        SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM billing_invoice
        WHERE fy = %(fy)s
    """, {"fy": fy})
    n = cur.fetchone()["n"]
    return "FC/%s/%06d" % (fy, n), fy, n


def _ts_date(ts) -> Optional[date]:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date() if ts else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def create_invoice(cur, *, user, subscription_id, payment, subscription,
                   plan_name, plan_label, scope, event_id=None) -> Optional[dict]:
    """Write the invoice for one captured payment.

    Returns (row, created): the new row and True; the EXISTING row and
    False when this payment already has one (Razorpay re-sent the charge
    under a new delivery id), so the caller emails once, not per delivery;
    (None, False) when the payment carries no amount (nothing to invoice)."""
    pay_id = payment.get("id")
    amount = payment.get("amount")
    if not pay_id or not amount:
        return None, False
    cur.execute("SELECT * FROM billing_invoice WHERE payment_id = %(p)s", {"p": pay_id})
    have = cur.fetchone()
    if have:
        return have, False

    cur.execute("SELECT * FROM billing_customer WHERE user_id = %(u)s", {"u": user["user_id"]})
    cust = cur.fetchone() or {}
    customer = {
        "name": cust.get("legal_name") or user.get("full_name") or user.get("email") or "",
        "email": user.get("email") or payment.get("email") or "",
        "gstin": cust.get("gstin") or "",
        "address": cust.get("address") or "",
        "state_code": cust.get("state_code") or "",
        "state": STATES.get(cust.get("state_code") or "", ""),
    }
    supplier = {"name": LEGAL_NAME, "address": ADDRESS, "state_code": STATE_CODE,
                "state": STATES.get(STATE_CODE, ""), "gstin": GSTIN, "sac": SAC,
                "support": SUPPORT}

    issued = _ts_date(payment.get("created_at")) or date.today()
    taxable, cgst, sgst, igst, pos = split_tax(int(amount), customer["state_code"] or None)
    no, fy, seq = next_invoice_no(cur, issued)
    row = {
        "invoice_no": no, "fy": fy, "seq": seq, "user_id": user["user_id"],
        "subscription_id": subscription_id, "payment_id": pay_id, "event_id": event_id,
        "issued_on": issued, "tax_invoice": bool(GSTIN),
        "plan_name": plan_name, "plan_label": plan_label, "scope": scope,
        "period_start": _ts_date(subscription.get("current_start")),
        "period_end": _ts_date(subscription.get("current_end")),
        "currency": payment.get("currency") or "INR",
        "gross_paise": int(amount), "taxable_paise": taxable,
        "cgst_paise": cgst, "sgst_paise": sgst, "igst_paise": igst,
        "gst_rate": GST_RATE if GSTIN else 0,
        "place_of_supply": pos or "", "method": payment.get("method") or "",
        "supplier": Json(supplier), "customer": Json(customer),
    }
    cur.execute("""
        INSERT INTO billing_invoice
            (invoice_no, fy, seq, user_id, subscription_id, payment_id, event_id,
             issued_on, tax_invoice, plan_name, plan_label, scope,
             period_start, period_end, currency, gross_paise, taxable_paise,
             cgst_paise, sgst_paise, igst_paise, gst_rate, place_of_supply,
             method, supplier, customer)
        VALUES
            (%(invoice_no)s, %(fy)s, %(seq)s, %(user_id)s, %(subscription_id)s,
             %(payment_id)s, %(event_id)s, %(issued_on)s, %(tax_invoice)s,
             %(plan_name)s, %(plan_label)s, %(scope)s, %(period_start)s,
             %(period_end)s, %(currency)s, %(gross_paise)s, %(taxable_paise)s,
             %(cgst_paise)s, %(sgst_paise)s, %(igst_paise)s, %(gst_rate)s,
             %(place_of_supply)s, %(method)s, %(supplier)s, %(customer)s)
    """, row)
    cur.execute("SELECT * FROM billing_invoice WHERE payment_id = %(p)s", {"p": pay_id})
    return cur.fetchone(), True


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------
def rupees(paise) -> str:
    """12345 -> '₹123.45', Indian grouping for the rupee part."""
    paise = int(paise or 0)
    r, p = divmod(abs(paise), 100)
    s = str(r)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if paise < 0 else "") + "₹" + s + ".%02d" % p


def fmt_day(d) -> str:
    if not d:
        return ""
    if isinstance(d, str):
        return d
    return d.strftime("%d %b %Y")


def invoice_html(inv: dict) -> str:
    """A printable page for one invoice. Server-rendered, no script, so it
    is the same whether it is read in the browser or saved as PDF with
    the browser's own print."""
    e = html.escape
    sup, cust = inv["supplier"] or {}, inv["customer"] or {}
    tax_invoice = bool(inv["tax_invoice"])
    title = "TAX INVOICE" if tax_invoice else "RECEIPT"
    period = ""
    if inv.get("period_start") or inv.get("period_end"):
        period = " (%s to %s)" % (fmt_day(inv.get("period_start")), fmt_day(inv.get("period_end")))
    desc = "FinChaya subscription – %s%s" % (inv.get("plan_label") or inv.get("plan_name") or "", period)

    tax_rows = ""
    if tax_invoice:
        if inv["igst_paise"]:
            tax_rows = "<tr><td>IGST @ %g%%</td><td class=n>%s</td></tr>" % (
                float(inv["gst_rate"]), e(rupees(inv["igst_paise"])))
        else:
            half = float(inv["gst_rate"]) / 2
            tax_rows = ("<tr><td>CGST @ %g%%</td><td class=n>%s</td></tr>"
                        "<tr><td>SGST @ %g%%</td><td class=n>%s</td></tr>") % (
                half, e(rupees(inv["cgst_paise"])), half, e(rupees(inv["sgst_paise"])))
        tax_note = ("Prices are inclusive of GST. Place of supply: %s. SAC %s."
                    % (e(STATES.get(inv.get("place_of_supply") or "", inv.get("place_of_supply") or "—")),
                       e(sup.get("sac") or SAC)))
    else:
        tax_note = ("GST not charged: %s is not registered under GST. "
                    "This receipt is not a tax invoice." % e(sup.get("name") or LEGAL_NAME))

    cust_lines = [cust.get("name")]
    if cust.get("address"):
        cust_lines.append(cust["address"])
    if cust.get("state"):
        cust_lines.append("%s (%s)" % (cust["state"], cust.get("state_code")))
    if cust.get("gstin"):
        cust_lines.append("GSTIN: " + cust["gstin"])
    if cust.get("email"):
        cust_lines.append(cust["email"])
    sup_lines = [sup.get("name") or LEGAL_NAME]
    if sup.get("address"):
        sup_lines.append(sup["address"])
    if sup.get("state"):
        sup_lines.append("%s (%s)" % (sup["state"], sup.get("state_code")))
    if sup.get("gstin"):
        sup_lines.append("GSTIN: " + sup["gstin"])
    sup_lines.append(sup.get("support") or SUPPORT)

    lines = lambda xs: "<br>".join(e(x) for x in xs if x)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title.title())} {e(inv['invoice_no'])} – FinChaya</title>
<style>
 body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#1b2b24;margin:0;background:#f4f6f5}}
 .sheet{{max-width:760px;margin:24px auto;background:#fff;padding:36px 40px;border-radius:12px;box-shadow:0 2px 14px rgba(0,0,0,.08)}}
 h1{{font-size:22px;margin:0;letter-spacing:.04em}} .brand{{font-size:26px;font-weight:800;color:#0f7a56}}
 .top{{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;align-items:flex-start;margin-bottom:22px}}
 .meta{{font-size:14px;line-height:1.5;text-align:right}} .meta b{{display:inline-block;min-width:110px;text-align:left}}
 .parties{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin:18px 0 24px;font-size:14px;line-height:1.5}}
 .parties h3{{margin:0 0 4px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#6b7c76}}
 table{{width:100%;border-collapse:collapse;font-size:14px}} th,td{{padding:10px 8px;border-bottom:1px solid #e3e8e5;text-align:left;vertical-align:top}}
 th{{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#6b7c76}} td.n,th.n{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
 .total td{{font-weight:800;font-size:16px;border-top:2px solid #0f7a56;border-bottom:0}}
 .note{{font-size:13px;color:#51615a;margin-top:18px;line-height:1.5}} .print{{margin:0 auto 24px;max-width:760px;text-align:right}}
 .print button{{background:#0f7a56;color:#fff;border:0;border-radius:999px;padding:9px 18px;font-size:14px;cursor:pointer}}
 @media print{{body{{background:#fff}} .sheet{{box-shadow:none;margin:0;border-radius:0}} .print{{display:none}}}}
 @media (max-width:560px){{.parties{{grid-template-columns:1fr}} .meta{{text-align:left}} .sheet{{padding:22px 18px}}}}
</style></head><body>
<div class="print"><button type="button" onclick="window.print()">Print or save as PDF</button></div>
<div class="sheet">
 <div class="top">
  <div><div class="brand">FinChaya</div><h1>{e(title)}</h1></div>
  <div class="meta">
   <div><b>Invoice number</b> {e(inv['invoice_no'])}</div>
   <div><b>Date</b> {e(fmt_day(inv['issued_on']))}</div>
   <div><b>Payment reference</b> {e(inv['payment_id'])}</div>
   <div><b>Subscription</b> {e(inv['subscription_id'] or '')}</div>
   <div><b>Paid by</b> {e((inv.get('method') or '').upper() or '—')}</div>
  </div>
 </div>
 <div class="parties">
  <div><h3>From</h3>{lines(sup_lines)}</div>
  <div><h3>Billed to</h3>{lines(cust_lines)}</div>
 </div>
 <table>
  <thead><tr><th>Description</th><th class="n">Amount</th></tr></thead>
  <tbody>
   <tr><td>{e(desc)}<br><small style="color:#6b7c76">SAC {e(sup.get('sac') or SAC)}</small></td><td class="n">{e(rupees(inv['taxable_paise']))}</td></tr>
   {tax_rows}
   <tr class="total"><td>Total paid</td><td class="n">{e(rupees(inv['gross_paise']))}</td></tr>
  </tbody>
 </table>
 <p class="note">{tax_note}</p>
 <p class="note">This is a computer-generated document and needs no signature.
 Questions about this invoice: {e(SUPPORT)}. Manage your subscription at {e(SITE_URL)} (Subscription, in the header).</p>
</div></body></html>"""


def invoice_csv_rows(rows) -> str:
    """For the accountant: one line per invoice, amounts in rupees."""
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["invoice_no", "date", "type", "customer", "customer_gstin", "customer_state",
                "place_of_supply", "plan", "period_start", "period_end", "payment_id",
                "taxable", "cgst", "sgst", "igst", "total", "gst_rate", "sac"])
    for r in rows:
        c = r["customer"] or {}
        sup = r["supplier"] or {}
        w.writerow([r["invoice_no"], r["issued_on"], "tax invoice" if r["tax_invoice"] else "receipt",
                    c.get("name", ""), c.get("gstin", ""), c.get("state_code", ""),
                    r.get("place_of_supply") or "", r.get("plan_label") or r.get("plan_name") or "",
                    r.get("period_start") or "", r.get("period_end") or "", r["payment_id"],
                    "%.2f" % (r["taxable_paise"] / 100), "%.2f" % (r["cgst_paise"] / 100),
                    "%.2f" % (r["sgst_paise"] / 100), "%.2f" % (r["igst_paise"] / 100),
                    "%.2f" % (r["gross_paise"] / 100), r["gst_rate"], sup.get("sac", SAC)])
    return out.getvalue()


# ---------------------------------------------------------------------
# The email
# ---------------------------------------------------------------------
def receipt_email(to_addr: str, name: str, inv: dict, next_charge) -> EmailMessage:
    """What the customer gets from FinChaya after a successful charge.
    Razorpay send their own receipt; this one says what was bought, until
    when, and where the invoice is."""
    label = inv.get("plan_label") or inv.get("plan_name") or "FinChaya"
    total = rupees(inv["gross_paise"])
    link = "%s/api/pay/invoice/%s" % (SITE_URL, inv["invoice_no"])
    period = ""
    if inv.get("period_start") or inv.get("period_end"):
        period = "%s to %s" % (fmt_day(inv.get("period_start")), fmt_day(inv.get("period_end")))
    renew = fmt_day(next_charge) if next_charge else ""
    kind = "Tax invoice" if inv.get("tax_invoice") else "Receipt"
    gst_line = ("The amount is inclusive of GST; the invoice shows the break-up."
                if inv.get("tax_invoice") else "")

    msg = EmailMessage()
    msg["Subject"] = "Your FinChaya payment: %s – %s" % (total, label)
    msg["To"] = to_addr
    text = (
        f"Hello {name or 'there'},\n\n"
        f"Thank you. Your payment of {total} for the FinChaya {label} plan has gone through"
        f"{' and your access is on' if inv.get('scope') else ''}.\n\n"
        + (f"Period covered: {period}\n" if period else "")
        + (f"Next charge: {renew}\n" if renew else "")
        + f"Payment reference: {inv['payment_id']}\n"
        f"{kind} {inv['invoice_no']}: {link}\n"
        + (gst_line + "\n" if gst_line else "")
        + "\nTo change or cancel the plan, sign in at "
        f"{SITE_URL} and open Subscription in the header. Cancelling stops future charges; "
        "you keep access until the end of the period already paid for.\n\n"
        f"Need a GST invoice in your business name? Add your GSTIN under Subscription > "
        f"Billing details before the next charge.\n\n"
        f"FinChaya\n{SUPPORT}\n")
    msg.set_content(text)
    e = html.escape
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#6b7c76'>{e(k)}</td><td style='padding:4px 0'>{v}</td></tr>"
        for k, v in [("Plan", e(label)), ("Amount", e(total)),
                     ("Period", e(period)), ("Next charge", e(renew)),
                     ("Payment reference", e(inv["payment_id"])),
                     (kind, f"<a href='{e(link)}'>{e(inv['invoice_no'])}</a>")] if v)
    msg.add_alternative(f"""<html><body style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#1b2b24;font-size:15px;line-height:1.5">
<p>Hello {e(name or 'there')},</p>
<p>Thank you. Your payment of <b>{e(total)}</b> for the FinChaya <b>{e(label)}</b> plan has gone through{' and your access is on' if inv.get('scope') else ''}.</p>
<table style="border-collapse:collapse;font-size:15px">{rows}</table>
{('<p style="color:#51615a;font-size:14px">' + e(gst_line) + '</p>') if gst_line else ''}
<p>To change or cancel the plan, sign in at <a href="{e(SITE_URL)}">{e(SITE_URL.replace('https://', ''))}</a> and open <b>Subscription</b> in the header. Cancelling stops future charges; you keep access until the end of the period already paid for.</p>
<p style="color:#51615a;font-size:14px">Need a GST invoice in your business name? Add your GSTIN under Subscription &rsaquo; Billing details before the next charge.</p>
<p>FinChaya<br><a href="mailto:{e(SUPPORT)}">{e(SUPPORT)}</a></p>
</body></html>""", subtype="html")
    return msg


def send_in_background(msg: EmailMessage) -> bool:
    """Hand the message to a thread so the webhook answers Razorpay inside
    their five-second limit whatever the mail server is doing. Returns
    False (and logs) when mail is not configured; never raises."""
    # mailer.py is the one sender the whole site uses: it speaks Resend (HTTPS, works from the droplet) as well as SMTP
    # (which DigitalOcean blocks). Used when it has been uploaded; otherwise the SMTP-only code below, exactly as before.
    try:
        import mailer
    except ImportError:
        mailer = None
    if mailer is not None:
        cfg = mailer.mail_config()
        if not cfg["configured"]:
            print("mfapi: receipt email not sent to %s (%s)" % (msg["To"], cfg["why"]), flush=True)
            return False
        to_addr, subject = msg["To"], msg["Subject"]

        def send_via_mailer():
            ok, detail = mailer.send_message(msg)
            print("mfapi: receipt email %s to %s (%s): %s" % ("sent" if ok else "FAILED", to_addr, subject, detail), flush=True)

        threading.Thread(target=send_via_mailer, name="receipt-email", daemon=True).start()
        return True

    try:
        from api import MAIL_FROM, SMTP_HOST, SMTP_PASS, SMTP_PORT, SMTP_USER
    except Exception as ex:                                      # noqa: BLE001
        print("mfapi: receipt email not sent (api settings unavailable: %r)" % ex, flush=True)
        return False
    if not (SMTP_USER and SMTP_PASS):
        print("mfapi: receipt email not sent to %s (MF_SMTP_USER / MF_SMTP_PASS unset)"
              % msg["To"], flush=True)
        return False
    msg["From"] = MAIL_FROM

    def go():
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(SMTP_USER, SMTP_PASS)
                smtp.send_message(msg)
            print("mfapi: receipt email sent to %s (%s)" % (msg["To"], msg["Subject"]), flush=True)
        except Exception as ex:                                  # noqa: BLE001
            print("mfapi: receipt email to %s FAILED: %r" % (msg["To"], ex), flush=True)

    threading.Thread(target=go, name="receipt-email", daemon=True).start()
    return True
