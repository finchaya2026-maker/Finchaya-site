/* ipo_view.js -- how one IPO is shown: the same sections on the IPO page (/ipo/<name>) and in the IPO tab of a listed company's stock page.
   window.fcIpoView.render(container, detail)   detail = GET /api/ipo/<name> or GET /api/ipo/for-stock/<isin>
   INFORMATION ONLY: no score, rank or verdict anywhere in what this draws; the notice from the server heads the page. Every figure carries the page of the offer
   document it was read from. The styles are injected once, scoped by the .ii-* class names, and use the site's own colour variables. */
(function () {
  "use strict";
  if (window.fcIpoView) return;
  const CSS = `
  .ii-root{color:var(--ink)}
  .ii-notice{margin:16px 0 0;padding:12px 16px;border-radius:var(--r);background:var(--note-wash);border:1px solid #f0d6a6;color:var(--ink-2);font-size:14px;line-height:1.55}
  .ii-notice b{color:var(--ink)}
  .ii-src{margin:10px 0 0;font-size:13.5px;color:var(--ink-2)}
  .ii-src a{color:var(--brand-dark);font-weight:600}
  .ii-sec{background:var(--card);border-radius:var(--r-card);box-shadow:var(--shadow);padding:20px 22px 18px;margin:18px 0 0}
  .ii-sec h2{margin:0;font-size:20px;letter-spacing:-.01em}
  .ii-sec > .ii-ask{margin:2px 0 12px;color:var(--muted);font-size:14px}
  .ii-sec h3{margin:18px 0 6px;font-size:15px;color:var(--ink-2)}
  .ii-facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px 20px;margin:12px 0 0}
  .ii-fact small{display:block;font-size:11.5px;font-weight:700;color:var(--muted);letter-spacing:.03em;text-transform:uppercase}
  .ii-fact b{display:block;font-size:17px;font-variant-numeric:tabular-nums;font-weight:700;margin-top:2px}
  .ii-fact span{display:block;font-size:12.5px;color:var(--muted);margin-top:1px;line-height:1.35}
  .ii-na{color:var(--muted);font-weight:500}
  .ii-up{color:var(--brand-dark)}
  .ii-down{color:var(--neg)}
  .ii-p{margin:8px 0 0;font-size:15px;line-height:1.6;color:var(--ink-2);max-width:92ch}
  .ii-p b{color:var(--ink)}
  .ii-quote{margin:10px 0 0;padding:10px 14px;border-left:3px solid var(--line-firm);background:var(--surface);color:var(--ink-2);font-size:14px;line-height:1.55;max-width:92ch}
  .ii-quote small{display:block;margin-top:6px;color:var(--muted);font-size:12px}
  .ii-bar{display:flex;height:22px;border-radius:6px;overflow:hidden;margin:10px 0 4px;background:var(--surface-2)}
  .ii-bar i{display:block;height:100%}
  .ii-bar .a{background:#5b8def}
  .ii-bar .b{background:#9aa5b8}
  .ii-legend{display:flex;gap:6px 20px;flex-wrap:wrap;font-size:13.5px;color:var(--ink-2)}
  .ii-legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:-1px}
  .ii-tbl{width:100%;border-collapse:collapse;margin:8px 0 0;font-variant-numeric:tabular-nums}
  .ii-tbl th,.ii-tbl td{padding:9px 10px;text-align:right;font-size:14.5px;border:0;vertical-align:top}
  .ii-tbl tbody tr{border-top:1px solid var(--line)}
  .ii-tbl thead th{font-size:12.5px;font-weight:700;color:var(--muted);letter-spacing:.02em;background:var(--surface)}
  .ii-tbl th:first-child,.ii-tbl td:first-child{text-align:left}
  .ii-tbl td.t{text-align:left;white-space:normal}
  .ii-tbl .pg{font-size:11.5px;color:var(--muted);font-weight:500;white-space:nowrap}
  .ii-wrap{overflow-x:auto}
  .ii-root a.pg{font-size:12px;color:var(--brand-dark);white-space:nowrap}
  .ii-use{display:inline-block;background:var(--surface-2);border-radius:999px;padding:0 9px;margin:3px 4px 0 0;font-weight:600;font-size:12px;color:var(--ink-2)}
  .ii-pbar{display:block;height:6px;border-radius:3px;background:#5b8def;margin-top:5px;min-width:2px}
  .ii-people{display:grid;gap:12px;margin:10px 0 0}
  .ii-person{padding:12px 14px;background:var(--surface);border-radius:var(--r);font-size:14.5px;line-height:1.55;color:var(--ink-2)}
  .ii-person b{color:var(--ink);display:block;margin-bottom:2px}
  .ii-topics{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0 0}
  .ii-topic{display:inline-block;padding:4px 12px;border-radius:999px;background:var(--surface-2);font-size:13.5px;font-weight:600;color:var(--ink-2)}
  .ii-topic b{color:var(--brand-dark);margin-left:5px}
  .ii-det{margin:12px 0 0;font-size:14.5px}
  .ii-det summary{cursor:pointer;color:var(--brand-dark);font-weight:600;max-width:100%;overflow-wrap:anywhere}
  .ii-det ol,.ii-det ul{margin:8px 0 0;padding:0 0 0 22px;color:var(--ink-2);line-height:1.55}
  .ii-det li{margin:0 0 5px}
  .ii-list{margin:8px 0 0;padding:0 0 0 20px;color:var(--ink-2);font-size:14.5px;line-height:1.7}
  .ii-perf{display:flex;gap:8px 28px;flex-wrap:wrap;margin:12px 0 0;padding:12px 16px;background:var(--surface);border-radius:var(--r);font-size:14.5px;color:var(--ink-2)}
  .ii-perf b{font-variant-numeric:tabular-nums}
  .ii-limits{margin:18px 0 0;padding:14px 18px;background:var(--surface);border-radius:var(--r);font-size:13.5px;color:var(--ink-2);line-height:1.6}
  .ii-limits ul{margin:6px 0 0;padding:0 0 0 20px}
  @media (max-width:700px){.ii-sec{padding:16px 14px 14px}.ii-facts{grid-template-columns:1fr 1fr}.ii-tbl th,.ii-tbl td{padding:8px 6px;font-size:14px}}
  `;
  function ensureStyle() {
    if (document.getElementById("ii-style")) return;
    const s = document.createElement("style");
    s.id = "ii-style";
    s.textContent = CSS;
    document.head.appendChild(s);
  }
  const esc = t => String(t == null ? "" : t).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const inr = (v, dp) => v == null ? "–" : Number(v).toLocaleString("en-IN", {minimumFractionDigits: dp, maximumFractionDigits: dp});
  const num = v => v == null ? "–" : Number(v).toLocaleString("en-IN");
  function crore(v) { if (v == null) return "–"; const a = Math.abs(v); return inr(v, a >= 1000 ? 0 : a >= 100 ? 1 : 2); }
  function ymd(s) { const p = String(s || "").split("-"); return p.length === 3 ? new Date(+p[0], +p[1] - 1, +p[2]) : null; }
  function dlong(s) { const d = ymd(String(s || "").slice(0, 10)); return d ? d.getDate() + " " + MONTHS[d.getMonth()] + " " + d.getFullYear() : ""; }
  const pctText = p => p == null ? "–" : (p > 0 ? "+" : p < 0 ? "−" : "") + Math.abs(p).toFixed(1) + "%";
  const pcls = p => p == null ? "ii-na" : p > 0 ? "ii-up" : p < 0 ? "ii-down" : "ii-na";
  let DOC_URL = null;
  function pg(n) {                                        // "p. 123", a link into the PDF when the document is a PDF we can point into
    if (!n) return "";
    if (DOC_URL && /\.pdf(#|$)/i.test(DOC_URL)) return '<a class="pg" target="_blank" rel="noopener" href="' + esc(DOC_URL) + "#page=" + n + '">p. ' + n + "</a>";
    return '<span class="pg">p. ' + n + "</span>";
  }
  const fact = (k, v, sub) => '<div class="ii-fact"><small>' + k + "</small><b>" + v + "</b>" + (sub ? "<span>" + sub + "</span>" : "") + "</div>";

  // ------------------------------------------------------------ the sections
  function facts(d) {
    const i = d.issue, o = d.offer, x = d.exchange;
    const out = [];
    if (i.stage === "filed") out.push(fact("Draft filed with SEBI", esc(dlong(i.filed_on)), "Dates and price not announced yet"));
    else out.push(fact("Bidding", esc(dlong(i.open_date)) + " – " + esc(dlong(i.close_date)),
      i.listing_date ? "Listing " + esc(dlong(i.listing_date)) : i.stage === "ended" ? "No listing found" : "Listing date not announced"));
    out.push(i.issue_price != null ? fact("Issue price", "₹" + inr(i.issue_price, 0), i.price_low != null ? "Band was ₹" + inr(i.price_low, 0) + " – " + inr(i.price_high, 0) : "")
      : i.price_high != null ? fact("Price band", "₹" + inr(i.price_low, 0) + (i.price_high !== i.price_low ? " – " + inr(i.price_high, 0) : ""), o.face_value ? "Face value ₹" + esc(o.face_value) : "")
      : fact("Price band", '<span class="ii-na">Not announced</span>', o.face_value ? "Face value ₹" + esc(o.face_value) : ""));
    out.push(i.lot_size ? fact("Lot", num(i.lot_size) + " shares", i.min_investment ? "About ₹" + inr(i.min_investment, 0) + " at the top of the band" : "") : fact("Lot", '<span class="ii-na">–</span>'));
    out.push(i.size_cr != null ? fact("Issue size", "₹" + crore(i.size_cr) + " Cr", i.size_basis && i.size_basis !== "document" ? "Worked out at the " + esc(i.size_basis) : "As the document states") : fact("Issue size", '<span class="ii-na">Not stated</span>', "The price is open in the document"));
    out.push(fact("Kind of issue", esc({fresh: "Fresh issue", ofs: "Offer for sale", mix: "Fresh issue + offer for sale"}[o.issue_type] || "–"),
      o.issue_type === "mix" && o.fresh_pct != null ? inr(o.fresh_pct, 1) + "% new shares" : o.issue_type === "ofs" ? "The company receives nothing" : o.issue_type === "fresh" ? "The company receives the money" : ""));
    if (o.shares_before) out.push(fact("Shares before the issue", num(o.shares_before)));
    if (x.lead_managers && x.lead_managers.length) out.push(fact("Lead managers", esc(x.lead_managers.join(", "))));
    if (x.registrar) out.push(fact("Registrar", esc(x.registrar)));
    return (i.stage_note ? '<div class="ii-notice"><b>' + esc(i.stage_label) + ".</b> " + esc(i.stage_note) + "</div>" : "") + '<div class="ii-facts">' + out.join("") + "</div>";
  }

  // A few plain sentences assembled from the figures below, in the order a reader asks. Each says where it came from; none says whether a number is good.
  function highlights(d) {
    if (!d.document) return "";
    const o = d.offer, m = d.money, p = d.promoters, f = d.financials, r = d.risks, b = [];
    const sellers = o.selling_shareholders || [];
    if (o.fresh_shares && o.ofs_shares) b.push("<b>The issue:</b> " + num(o.fresh_shares) + " new shares (the company receives that money) and " + num(o.ofs_shares) + " shares sold by " + (sellers.length ? sellers.length + " existing " + (sellers.length === 1 ? "shareholder" : "shareholders") : "existing shareholders") + " (the company does not). " + pg(o.page));
    else if (o.fresh_shares) b.push("<b>The issue:</b> a fresh issue of " + num(o.fresh_shares) + " new shares; the company receives the money. " + pg(o.page));
    else if (o.ofs_shares) b.push("<b>The issue:</b> an offer for sale of " + num(o.ofs_shares) + " shares by " + (sellers.length ? sellers.length + " existing " + (sellers.length === 1 ? "shareholder" : "shareholders") : "existing shareholders") + (o.no_proceeds_to_company ? "; the company receives none of the money." : ".") + " " + pg(o.page));
    else if (o.fresh_amount_cr) b.push("<b>The issue:</b> a fresh issue of up to ₹" + crore(o.fresh_amount_cr) + " Cr" + (o.ofs_shares ? " and an offer for sale" : "") + ". " + pg(o.page));
    const stated = (m.items || []).filter(x => x.amount_cr != null);
    if (stated.length) {
      const tag = x => x.tag_labels.filter(t => t !== "Investment in a subsidiary")[0] || x.tag_labels[0];
      b.push("<b>What the new money is for:</b> " + stated.slice(0, 5).map(x => esc(tag(x)).toLowerCase() + " ₹" + crore(x.amount_cr) + " Cr").join("; ") + (m.items.some(x => x.amount_cr == null) ? "; the rest (general corporate purposes) is set at pricing" : "") + ". " + pg(m.page));
    } else if (m.no_proceeds_to_company) b.push("<b>What the money is for:</b> nothing for the company: the objects are the sale by existing shareholders and the listing. " + pg(m.page));
    if (p.holding_pct != null) b.push("<b>Promoters:</b> " + esc((p.names || []).slice(0, 4).join(", ")) + ((p.names || []).length > 4 ? " and others" : "") + " hold " + inr(p.holding_pct, 2) + "% before the issue" +
      (p.selling === true ? " and are among the sellers" : "") + ". " + pg(p.page));
    else if (p.names && p.names.length) b.push("<b>Promoters:</b> " + esc(p.names.join(", ")) + ". " + pg(p.page));
    const rev = (f.rows || {}).revenue, pat = (f.rows || {}).pat;
    if (rev && rev.values[0] != null) {
      const col = f.periods.findIndex(x => /^FY\d{4}$|^march/i.test(x)); const k = col < 0 ? 0 : col;
      b.push("<b>Financials (" + esc(f.periods[k]) + "):</b> revenue ₹" + inr(rev.values[k], 2) + " Cr" + (f.revenue_growth != null && f.fin_period === f.periods[k] ? ", " + pctText(f.revenue_growth) + " on the year before" : "") +
        (pat && pat.values[k] != null ? "; profit after tax ₹" + inr(pat.values[k], 2) + " Cr" + (f.pat_margin != null && f.fin_period === f.periods[k] ? " (" + inr(f.pat_margin, 1) + "% of revenue)" : "") : "") + ". " + pg(rev.page));
    }
    if (r.count) b.push("<b>Risk factors:</b> the company lists " + r.count + " of them. " + pg(r.page));
    return '<section class="ii-sec"><h2>Key highlights</h2><p class="ii-ask">The main points of the offer document, in plain words. Each links to the page it came from.</p><ul class="ii-list">' + b.map(x => "<li>" + x + "</li>").join("") + "</ul></section>";
  }

  function why(d) {
    const o = d.offer, m = d.money, h = [];
    const fresh = o.fresh_shares, ofs = o.ofs_shares, tot = o.total_shares || ((fresh || 0) + (ofs || 0)) || null;
    if (!d.document) return '<p class="ii-p ii-na">The offer document has not been read yet.</p>';
    if (fresh && ofs && tot) {
      const a = 100 * fresh / tot, b = 100 * ofs / tot;
      h.push('<div class="ii-bar" role="img" aria-label="' + inr(a, 1) + ' per cent new shares, ' + inr(b, 1) + ' per cent existing holders selling"><i class="a" style="width:' + a + '%"></i><i class="b" style="width:' + b + '%"></i></div>' +
        '<div class="ii-legend"><span><i style="background:#5b8def"></i>New shares (fresh issue): ' + num(fresh) + " · " + inr(a, 1) + '%</span><span><i style="background:#9aa5b8"></i>Existing holders selling (offer for sale): ' + num(ofs) + " · " + inr(b, 1) + "%</span></div>");
    }
    const lines = [];
    if (fresh && !ofs) lines.push("This is a <b>fresh issue</b> of " + num(fresh) + " new shares: the company receives the money.");
    else if (ofs && !fresh) lines.push("This is an <b>offer for sale</b> of " + num(ofs) + " shares by existing holders" + (o.no_proceeds_to_company ? ": <b>the company does not receive any of the money</b>." : "."));
    else if (fresh && ofs) lines.push("The company receives the money from the " + num(fresh) + " new shares. It does not receive the money from the " + num(ofs) + " shares existing holders are selling.");
    else if (o.fresh_amount_cr) lines.push("The fresh issue is of up to ₹" + crore(o.fresh_amount_cr) + " Cr; the company receives that money.");
    if (o.page) lines.push("(The offer, " + pg(o.page) + ")");
    h.push('<p class="ii-p">' + lines.join(" ") + "</p>");

    if (m.items && m.items.length) {
      const known = m.items.reduce((s, x) => s + (x.amount_cr || 0), 0);
      h.push("<h3>How the new money will be used " + pg(m.page) + "</h3>");
      h.push('<div class="ii-wrap"><table class="ii-tbl"><thead><tr><th>Stated use</th><th>₹ crore</th><th>Share of the amounts stated</th></tr></thead><tbody>' +
        m.items.map(x => {
          const share = x.amount_cr != null && known ? 100 * x.amount_cr / known : null;
          return '<tr><td class="t">' + esc(x.text) + "<br>" + x.tag_labels.map(t => '<span class="ii-use">' + esc(t) + "</span>").join("") + "</td><td>" +
            (x.amount_cr != null ? crore(x.amount_cr) : '<span class="ii-na">Set at pricing</span>') + "</td><td>" +
            (share != null ? inr(share, 1) + '%<span class="ii-pbar" style="width:' + Math.max(2, share) + '%"></span>' : '<span class="ii-na">–</span>') + "</td></tr>";
        }).join("") + "</tbody></table></div>");
      const sums = Object.entries(m.use_pct || {}).filter(([k]) => k !== "Investment in a subsidiary").sort((a, b) => b[1] - a[1]);   // a subsidiary is a route for the money, not a purpose
      if (sums.length) h.push('<p class="ii-p">By purpose, of the amounts stated: ' + sums.map(([k, v]) => "<b>" + esc(k) + "</b> " + inr(v, 1) + "%").join(" · ") + ". An object can fall under more than one purpose, so these can add up to more than 100%.</p>");
    } else if (m.no_proceeds_to_company) {
      h.push('<p class="ii-p">The document lists no use of funds, because the company receives none of the money. The objects are the sale by existing holders and the listing of the shares.</p>');
    }
    if (m.intro) h.push('<div class="ii-quote">' + esc(m.intro) + "<small>The company's words, " + pg(m.page) + "</small></div>");

    const sell = o.selling_shareholders || [];
    if (sell.length) {
      const names = new Set((d.promoters.names || []).map(n => n.toLowerCase()));
      const byShares = sell.every(x => x.shares != null), byAmount = sell.every(x => x.amount_cr != null);
      const total = byShares ? sell.reduce((s, x) => s + x.shares, 0) : byAmount ? sell.reduce((s, x) => s + x.amount_cr, 0) : null;
      const anyAmt = sell.some(x => x.amount_cr != null);
      h.push("<h3>Who is selling (" + sell.length + ")</h3>");
      h.push('<div class="ii-wrap"><table class="ii-tbl"><thead><tr><th>Seller</th><th>Shares offered</th>' + (anyAmt ? "<th>₹ crore</th>" : "") + (total ? "<th>Share of the offer for sale</th>" : "") + "</tr></thead><tbody>" +
        sell.slice(0, 40).map(s => {
          const part = byShares ? s.shares : s.amount_cr;
          return "<tr><td class=\"t\">" + esc(s.name) + (names.has(s.name.toLowerCase()) ? ' <span class="ii-use">Promoter</span>' : "") + "</td><td>" + (s.shares != null ? num(s.shares) : '<span class="ii-na">Set at pricing</span>') + "</td>" +
            (anyAmt ? "<td>" + (s.amount_cr != null ? inr(s.amount_cr, 2) : '<span class="ii-na">–</span>') + "</td>" : "") + (total ? "<td>" + inr(100 * part / total, 1) + "%</td>" : "") + "</tr>";
        }).join("") + "</tbody></table></div>" + (sell.length > 40 ? '<p class="ii-p ii-na">The first 40 of ' + sell.length + " are shown; the document lists all of them.</p>" : ""));
    }
    return h.join("");
  }

  function people(d) {
    const p = d.promoters;
    if (!p.names || !p.names.length) return '<p class="ii-p ii-na">The promoters could not be read from the document.</p>';
    const h = ['<p class="ii-p">' + esc(p.intro || ("Promoters: " + p.names.join(", ") + ".")) + " " + pg(p.page) + "</p>"];
    if (p.holding_pct != null) h.push('<p class="ii-p">Together the promoters hold <b>' + inr(p.holding_pct, 2) + "%</b> of the company" + (p.holding_shares ? " (" + num(p.holding_shares) + " shares)" : "") + " before the issue." +
      (p.selling === true ? " At least one promoter is among the sellers in this issue." : p.selling === false && d.offer.selling_shareholders.length ? " No promoter is among the sellers named." : "") + "</p>");
    if (p.profiles && p.profiles.length) h.push('<div class="ii-people">' + p.profiles.map(x => '<div class="ii-person"><b>' + esc(x.name) + "</b>" + esc(x.text.replace(x.name, "").replace(/^[\s,]+/, "")) + "</div>").join("") + "</div>");
    h.push('<p class="ii-p ii-na">The document gives each person’s full profile under “Our Management”.</p>');
    return h.join("");
  }

  function business(d) {
    const b = d.business;
    if (!b.overview) return '<p class="ii-p ii-na">The company’s own overview could not be read from the document.</p>';
    return '<div class="ii-quote">' + esc(b.overview) + "<small>The company’s words, " + pg(b.page) + ". Read the whole section for the segments, customers and facilities.</small></div>";
  }

  function financials(d) {
    const f = d.financials, rows = f.rows || {};
    const order = [["revenue", "Revenue from operations"], ["total_income", "Total income"], ["pbt", "Profit before tax"], ["pat", "Profit after tax"], ["eps_basic", "EPS, basic (₹)"],
                   ["total_assets", "Total assets"], ["equity", "Equity / net worth"], ["borrowings", "Borrowings"]];
    const have = order.filter(([k]) => rows[k]);
    if (!have.length) return '<p class="ii-p ii-na">The financial tables could not be read into figures. The document’s summary is ' + (f.page ? "at " + pg(f.page) : "in the section on financial information") + ".</p>";
    const per = f.periods;
    const h = ['<div class="ii-wrap"><table class="ii-tbl"><thead><tr><th>₹ crore' + (f.basis ? " · " + esc(f.basis) : "") + "</th>" + per.map(p => "<th>" + esc(p) + "</th>").join("") + "<th>Page</th></tr></thead><tbody>"];
    for (const [k, label] of have) {
      const r = rows[k];
      h.push("<tr><td>" + label + "</td>" + r.values.map(v => "<td>" + (v == null ? '<span class="ii-na">–</span>' : inr(v, 2)) + "</td>").join("") + '<td class="pgc">' + pg(r.page) + "</td></tr>");
    }
    h.push("</tbody></table></div>");
    const bits = [];
    if (f.revenue_growth != null) bits.push("Revenue in " + esc(f.fin_period || "the latest full year") + " <b>" + pctText(f.revenue_growth) + '</b> on the year before.');
    if (f.pat_margin != null) bits.push("Profit after tax was <b>" + inr(f.pat_margin, 1) + "%</b> of revenue.");
    if (bits.length) h.push('<p class="ii-p">' + bits.join(" ") + "</p>");
    if (f.auditor) h.push('<p class="ii-p">Statutory auditor: <b>' + esc(f.auditor) + "</b>.</p>");
    h.push('<p class="ii-p ii-na">Converted to ₹ crore from the unit the document uses. Columns are as the document labels them; a column ending in June or September is a part year. Full statements are in the document.</p>');
    return h.join("");
  }

  function risks(d) {
    const r = d.risks;
    if (!r.count) return '<p class="ii-p ii-na">The risk factors could not be counted from the document.</p>';
    const h = ['<p class="ii-p">The company lists <b>' + r.count + "</b> risk factors " + pg(r.page) + ". Below are the subjects they cover, with how many of the company’s headings touch each. This counts mentions; it says nothing about how serious any of them is.</p>"];
    h.push('<div class="ii-topics">' + r.topics.map(t => '<span class="ii-topic">' + esc(t.label) + "<b>" + t.count + "</b></span>").join("") + "</div>");
    r.topics.slice(0, 8).forEach(t => {
      h.push('<details class="ii-det"><summary>' + esc(t.label) + " (" + t.count + ")</summary><ul>" + t.items.map(x => "<li>" + esc(x.text) + " " + pg(x.page) + "</li>").join("") + "</ul></details>");
    });
    h.push('<details class="ii-det"><summary>All ' + r.count + " risk factors, as the company numbered them</summary><ol>" + r.headings.map(x => "<li value=\"" + x.n + "\">" + esc(x.text) + " " + pg(x.page) + "</li>").join("") + "</ol></details>");
    return h.join("");
  }

  function elsewhere(d) {
    const h = [], x = d.exchange;
    if (x.subscription && x.subscription.length) h.push("<h3>Subscription so far</h3><ul class=\"ii-list\">" + x.subscription.map(s => "<li>" + esc(s.category) + ": " + inr(s.times, 2) + " times</li>").join("") + "</ul>");
    if (x.issue_period) h.push('<p class="ii-p">Issue period: ' + esc(x.issue_period) + ".</p>");
    if (d.where_to_read.length) h.push("<h3>Where to read more in the document</h3><ul class=\"ii-list\">" + d.where_to_read.map(s => "<li>" + esc(s.label) + " — " + pg(s.page) + "</li>").join("") + "</ul>");
    if (d.documents.length) h.push("<h3>Documents</h3><ul class=\"ii-list\">" + d.documents.map(x => "<li>" + esc({DRHP: "Draft red herring prospectus", RHP: "Red herring prospectus", PROSPECTUS: "Prospectus"}[x.kind] || x.kind) + " (" + esc(x.source) + (x.date ? ", " + esc(dlong(x.date)) : "") + ")" +
      (x.url ? ' — <a target="_blank" rel="noopener" href="' + esc(x.url) + '">open' + (/\.zip$/i.test(x.url) ? " (zip from the NSE)" : "") + "</a>" : "") + (x.status !== "done" ? ' <span class="ii-na">· ' + (x.status === "failed" ? "could not be read" : "not read yet") + "</span>" : "") + "</li>").join("") + "</ul>");
    return h.join("");
  }

  function perf(d) {
    const p = d.issue.perf;
    if (d.issue.stage !== "listed") return "";
    if (!p) return '<section class="ii-sec"><h2>How it has traded</h2><p class="ii-p ii-na">Trading figures are not on file for this issue.</p></section>';
    return '<section class="ii-sec"><h2>How it has traded</h2><p class="ii-ask">Prices against the issue price. Arithmetic on prices; it says nothing about what comes next.</p><div class="ii-perf"><span>Issue price <b>₹' + inr(d.issue.issue_price, 0) + "</b></span>" +
      "<span>Opened <b>₹" + inr(p.listing_open, 2) + '</b> <span class="' + pcls(p.listing_open_pct) + '">' + pctText(p.listing_open_pct) + "</span></span>" +
      "<span>Closed day one <b>₹" + inr(p.listing_close, 2) + '</b> <span class="' + pcls(p.listing_close_pct) + '">' + pctText(p.listing_close_pct) + "</span></span>" +
      "<span>Latest close <b>₹" + inr(p.latest, 2) + '</b> <span class="' + pcls(p.latest_pct) + '">' + pctText(p.latest_pct) + '</span> <span class="ii-na">(' + esc(dlong(p.latest_date)) + ")</span></span></div>" +
      (p.adjusted ? '<p class="ii-p ii-na">The latest close is adjusted for ' + esc(p.adjustment || "a split or bonus") + " after listing, so it is comparable with the issue price.</p>" : "") + "</section>";
  }

  // The whole body of one issue: the notice, where it was read from, and the sections.
  function body(d) {
    DOC_URL = d.document && d.document.url;
    const src = d.document ? "Source: the " + (d.document.kind === "DRHP" ? "draft red herring prospectus" : "red herring prospectus") + (d.document.date ? ", filed with SEBI on " + esc(dlong(d.document.date)) + "," : "") + " (" + esc(d.document.source) +
      ", " + (d.document.pages || "?") + ' pages), read automatically on ' + esc(dlong(d.document.read_on)) + '. <a target="_blank" rel="noopener" href="' + esc(d.document.url) + '">Open the document</a>.' : "No offer document has been read for this issue yet; the facts below come from the stock exchange.";
    const sec = (title, ask, html) => '<section class="ii-sec"><h2>' + title + "</h2>" + (ask ? '<p class="ii-ask">' + ask + "</p>" : "") + html + "</section>";
    return '<div class="ii-notice"><b>Information only. No verdict.</b> ' + esc(d.notice) + '</div><p class="ii-src">' + src + "</p>" +
      highlights(d) +
      '<section class="ii-sec"><h2>The issue</h2>' + facts(d) + "</section>" +
      perf(d) +
      sec("Why the company is raising money", "Is it new money for the business, existing holders selling, or repaying debt? The document says:", why(d)) +
      sec("Who is behind it", "The promoters and what they hold.", people(d)) +
      sec("What the company does", "", business(d)) +
      sec("The financials", "As summarised in the offer document.", financials(d)) +
      sec("The risk factors the company lists", "Subjects, not severity.", risks(d)) +
      sec("More to know", "", elsewhere(d)) +
      (d.notes && d.notes.length ? '<div class="ii-limits"><b>What could not be read from the document</b><ul>' + d.notes.map(n => "<li>" + esc(n) + "</li>").join("") + "</ul></div>" : "");
  }

  function render(container, d) {
    ensureStyle();
    container.classList.add("ii-root");
    container.innerHTML = body(d);
  }

  window.fcIpoView = {render: render, ensureStyle: ensureStyle};
})();
