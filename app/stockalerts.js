/* ===================================================================
   stockalerts.js -- the "Set alert" window on a stock page.

       fcStockAlerts.attach(buttonElement, {isin, symbol, name, price})   wire a button; it stays hidden unless the server
                                                                           has the feature (a 404 or "switched off" = no button)
       fcStockAlerts.open({isin, symbol, name, price})                    open the window directly (used by the My alerts page)

   WHAT THE PERSON DOES: picks WHAT to be alerted about (a price, a close above a recent high, a pullback to a moving
   average, a change of trend, an indicator, the next results), fills in the numbers, and reads one plain sentence that says
   exactly what will be watched. Every change asks the server (/api/stockalerts/describe) to turn the form into that
   sentence, so the page never invents wording the server would not stand behind, and "already true" or "cannot be read yet"
   is said BEFORE anything is saved.

   WHO: Stocks subscribers and administrators. The server decides on every call; this only avoids drawing a dead form.
   It asks /api/stockalerts/access itself, so it works however old the site-wide script is.

   Self-contained on purpose (own styles, prefix sa-): the stock page and the My alerts page use different style sheets.
   =================================================================== */
(function () {
  "use strict";
  if (window.fcStockAlerts) return;

  const API = "/api/stockalerts";
  const esc = t => String(t == null ? "" : t).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const dmy = iso => { const m = /^(\d{4})-(\d\d)-(\d\d)/.exec(iso || ""); return m ? (+m[3]) + " " + MONTHS[+m[2] - 1] + " " + m[1] : (iso || ""); };
  const rupees = v => "₹" + Number(v).toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2});

  /* ---------------------------------------------------------------- styles */
  const CSS = `
  .sa-ov{position:fixed;inset:0;z-index:9000;background:rgba(15,25,20,.55);display:flex;align-items:flex-start;justify-content:center;
    padding:5vh 14px 14px;overflow-y:auto;
    --sa-ink:var(--ink,#1b2b24);--sa-mute:var(--mute,var(--muted,#5f6f68));--sa-line:var(--line-firm,var(--rule,#d5ded9));
    --sa-brand:var(--brand-dark,var(--night,#0f4c3a));--sa-wash:var(--brand-wash,#eef5f1);--sa-note:var(--note,#8a5a00);
    --sa-notewash:var(--note-wash,#fff6e0);--sa-neg:var(--neg,var(--down,#b3261e));--sa-pos:var(--up,#1a7a45)}
  .sa-box{background:#fff;color:var(--sa-ink);width:min(600px,100%);border-radius:14px;box-shadow:0 18px 50px rgba(0,0,0,.28);
    padding:20px 20px 18px;font-family:inherit;font-size:15px;line-height:1.5}
  .sa-box *{box-sizing:border-box}
  .sa-top{display:flex;gap:12px;align-items:flex-start;justify-content:space-between}
  .sa-top h2{margin:0;font-size:1.2rem;line-height:1.25}
  .sa-top small{display:block;font-size:13px;color:var(--sa-mute);font-weight:400;margin-top:2px}
  .sa-x{background:none;border:0;font-size:26px;line-height:1;cursor:pointer;color:var(--sa-mute);padding:0 4px;border-radius:6px}
  .sa-x:hover{color:var(--sa-ink)}
  .sa-h{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--sa-mute);margin:16px 0 8px}
  .sa-kinds{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .sa-kind{display:block;text-align:left;font:inherit;background:#fff;border:1.5px solid var(--sa-line);border-radius:10px;padding:10px 12px;cursor:pointer;color:var(--sa-ink)}
  .sa-kind b{display:block;font-size:14.5px}
  .sa-kind span{display:block;font-size:12.5px;color:var(--sa-mute);margin-top:2px;line-height:1.4}
  .sa-kind:hover{border-color:var(--sa-brand)}
  .sa-kind[aria-pressed=true]{border-color:var(--sa-brand);background:var(--sa-wash);box-shadow:inset 0 0 0 1px var(--sa-brand)}
  .sa-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 12px}
  .sa-grid .sa-full{grid-column:1/-1}
  .sa-f label{display:block;font-size:12.5px;font-weight:600;color:var(--sa-mute);margin:0 0 4px}
  .sa-f select,.sa-f input{width:100%;font:inherit;font-size:15px;color:var(--sa-ink);background:#fff;border:1.5px solid var(--sa-line);border-radius:8px;padding:8px 10px;min-height:40px}
  .sa-f select:focus,.sa-f input:focus,.sa-kind:focus-visible,.sa-btn:focus-visible,.sa-ghost:focus-visible,.sa-x:focus-visible{outline:2px solid var(--sa-brand);outline-offset:1px}
  .sa-f[hidden],.sa-grid [hidden]{display:none}
  .sa-help{font-size:12.5px;color:var(--sa-mute);margin:4px 0 0}
  .sa-sum{margin-top:16px;border-radius:10px;padding:12px 14px;background:var(--sa-wash);border:1px solid var(--sa-line);min-height:60px}
  .sa-sum b{font-weight:700}
  .sa-sum.sa-bad{background:var(--sa-notewash);border-color:#e6c97a}
  .sa-sum.sa-idle{background:#f6f8f7;color:var(--sa-mute)}
  .sa-sum ul{margin:6px 0 0;padding-left:18px;font-size:13.5px;color:var(--sa-mute)}
  .sa-actions{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
  .sa-btn{font:inherit;font-weight:700;font-size:15px;background:var(--sa-brand);color:#fff;border:0;border-radius:9px;padding:10px 20px;cursor:pointer}
  .sa-btn:disabled{opacity:.45;cursor:default}
  .sa-ghost{font:inherit;font-weight:600;font-size:15px;background:#fff;color:var(--sa-ink);border:1.5px solid var(--sa-line);border-radius:9px;padding:10px 16px;cursor:pointer}
  .sa-ghost:hover{border-color:var(--sa-brand)}
  .sa-ok{background:#eaf6ee;border:1px solid #b9dcc6;border-radius:10px;padding:12px 14px;margin-top:16px}
  .sa-warn{background:var(--sa-notewash);border:1px solid #e6c97a;border-radius:10px;padding:10px 14px;margin-top:10px;font-size:14px}
  .sa-mine{margin-top:18px;border-top:1px solid var(--sa-line);padding-top:6px}
  .sa-row{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eef1ef;font-size:14px}
  .sa-row:last-child{border-bottom:0}
  .sa-chip{display:inline-block;font-size:11.5px;font-weight:700;border-radius:99px;padding:1px 8px;margin-right:6px;background:#eef1ef;color:var(--sa-mute);vertical-align:1px}
  .sa-chip.on{background:var(--sa-wash);color:var(--sa-brand)}
  .sa-chip.hit{background:#fff6e0;color:var(--sa-note)}
  .sa-del{background:none;border:0;color:var(--sa-neg);font:inherit;font-size:13px;font-weight:600;cursor:pointer;padding:2px 4px;white-space:nowrap}
  .sa-lock{padding:8px 0 4px}
  .sa-lock .fc-lock{box-shadow:none;padding:6px 0}
  .sa-foot{font-size:12px;color:var(--sa-mute);margin:14px 0 0}
  @media (max-width:520px){.sa-kinds,.sa-grid{grid-template-columns:1fr}.sa-ov{padding:0}.sa-box{border-radius:0;min-height:100vh}}
  `;
  function injectCss() {
    if (document.getElementById("sa-css")) return;
    const s = document.createElement("style"); s.id = "sa-css"; s.textContent = CSS; document.head.appendChild(s);
  }

  /* ---------------------------------------------------------------- server */
  let accessP = null, catP = null;
  const getAccess = (fresh) => (accessP && !fresh) ? accessP : (accessP = fetch(API + "/access", {headers: {Accept: "application/json"}})
    .then(r => r.ok ? r.json() : null).catch(() => null));
  const getCatalog = () => catP || (catP = fetch(API + "/catalog", {headers: {Accept: "application/json"}}).then(async r => {
    if (!r.ok) throw new Error("catalog " + r.status); return r.json(); }).catch(e => { catP = null; throw e; }));
  async function call(path, body, method) {
    const r = await fetch(API + path, {method: method || (body === undefined ? "GET" : "POST"), headers: {"Content-Type": "application/json", Accept: "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body)});
    let data = null; try { data = await r.json(); } catch (e) { /* not json */ }
    if (r.status === 401 || r.status === 403) { accessP = null; const err = new Error("access"); err.status = r.status; throw err; }
    if (!r.ok) { const err = new Error((data && data.detail) || "Something went wrong (" + r.status + "). Try again in a moment."); err.status = r.status; throw err; }
    return data;
  }

  /* ---------------------------------------------------------------- the button */
  function attach(btn, stock) {
    if (!btn) return;
    btn.hidden = true;
    getAccess().then(a => {
      if (!a || a.enabled === false) return;                 // not on this server, or switched off: no button
      btn.hidden = false;
      btn.onclick = () => open(stock);
    });
  }

  /* ---------------------------------------------------------------- the window */
  let current = null;                                          // the open window, so a second open() replaces it

  function field(id, label, control, extra) {
    return '<div class="sa-f' + (extra && extra.full ? " sa-full" : "") + '" id="' + id + 'F"' + (extra && extra.hidden ? " hidden" : "") + "><label for=\"" + id + '">' + label + "</label>" + control + "</div>";
  }
  const sel = (id, opts, val) => '<select id="' + id + '">' + opts.map(o => '<option value="' + esc(o[0]) + '"' + (String(o[0]) === String(val) ? " selected" : "") + ">" + esc(o[1]) + "</option>").join("") + "</select>";
  const num = (id, val, o) => '<input id="' + id + '" type="number" inputmode="decimal" value="' + esc(val) + '"' + (o && o.min != null ? ' min="' + o.min + '"' : "") + (o && o.max != null ? ' max="' + o.max + '"' : "") + (o && o.step ? ' step="' + o.step + '"' : ' step="any"') + (o && o.ph ? ' placeholder="' + esc(o.ph) + '"' : "") + ">";

  function fill(tpl, vals) {                                   // "is above the EMA ({period})" -> "... (20)"
    return String(tpl).replace(/\{(\w+)(?::g)?\}/g, (m, k) => vals[k] != null ? vals[k] : m);
  }

  async function open(stock) {
    injectCss();
    if (current) current.close();
    const ov = document.createElement("div");
    ov.className = "sa-ov";
    ov.innerHTML = '<div class="sa-box" role="dialog" aria-modal="true" aria-labelledby="saTitle" tabindex="-1">' +
      '<div class="sa-top"><h2 id="saTitle">Set an alert<small>' + esc(stock.name || stock.symbol || "") + (stock.symbol ? " &middot; " + esc(stock.symbol) : "") +
      (stock.price != null ? " &middot; last price " + esc(rupees(stock.price)) : "") + '</small></h2>' +
      '<button type="button" class="sa-x" id="saX" aria-label="Close">&times;</button></div>' +
      '<div id="saBody"><p class="sa-help">Loading&hellip;</p></div></div>';
    document.body.appendChild(ov);
    const prevOverflow = document.documentElement.style.overflow;
    document.documentElement.style.overflow = "hidden";
    const box = ov.firstChild, body = ov.querySelector("#saBody");
    const onKey = e => { if (e.key === "Escape") close(); };
    function close() {
      document.removeEventListener("keydown", onKey);
      document.documentElement.style.overflow = prevOverflow;
      ov.remove();
      if (current && current.ov === ov) current = null;
    }
    current = {close, ov};
    document.addEventListener("keydown", onKey);
    ov.addEventListener("mousedown", e => { if (e.target === ov) close(); });
    ov.querySelector("#saX").addEventListener("click", close);
    box.focus();

    const a = await getAccess(true);
    if (!a) { body.innerHTML = '<p class="sa-warn">Could not reach FinChaya. Check your connection and try again.</p>'; return; }
    if (!a.allowed) { lock(body, a); return; }
    let cat;
    try { cat = await getCatalog(); }
    catch (e) { if (e.message === "access") { lock(body, (await getAccess(true)) || {}); } else body.innerHTML = '<p class="sa-warn">Could not load the alert options just now. Try again in a moment.</p>'; return; }
    form(body, cat, stock, close);
  }

  function lock(host, a) {
    host.innerHTML = '<div class="sa-lock" id="saLock"></div>';
    const h = host.querySelector("#saLock");
    const intro = "Get one email when a stock does what you are watching for: a price level, a close above a recent high, a pullback to a moving average, a change of trend, an indicator, or the next results.";
    if (a.enabled === false) { h.innerHTML = '<p class="sa-help">Stock alerts are switched off for now.</p>'; return; }
    if (a.signed_in === false) {
      h.innerHTML = '<p>' + esc(intro) + '</p><p>Stock alerts are part of the Stocks plan. Sign in with an account on that plan to set one.</p><div class="sa-actions"><button type="button" class="sa-btn" id="saSign">Sign in</button></div>';
      h.querySelector("#saSign").addEventListener("click", () => { if (current) current.close(); if (window.fcSignIn) window.fcSignIn(); else location.assign("/"); });
    } else if (a.audience === "premium" && window.fcLocked) {
      window.fcLocked(h, "Stock alerts", esc(intro), "stocks");
    } else {
      h.innerHTML = '<p>' + esc(intro) + '</p><p class="sa-help">Stock alerts are available to ' + esc(a.who || "subscribers") + '.</p>';
    }
  }

  function form(host, cat, stock, close) {
    const S = {kind: null, token: 0, timer: null, ready: false, done: false};
    const kindBtns = cat.kinds.map(k => '<button type="button" class="sa-kind" data-kind="' + k.key + '" aria-pressed="false"><b>' + esc(k.label) + "</b><span>" + esc(k.blurb) + "</span></button>").join("");
    host.innerHTML = '<div class="sa-h">What do you want to be told about?</div><div class="sa-kinds" id="saKinds">' + kindBtns + "</div>" +
      '<div id="saParams"></div><div id="saSum" class="sa-sum sa-idle" aria-live="polite">Choose what to be alerted about.</div>' +
      '<div class="sa-actions"><button type="button" class="sa-btn" id="saGo" disabled>Create alert</button><button type="button" class="sa-ghost" id="saCancel">Close</button></div>' +
      '<div id="saMsg"></div><div class="sa-mine" id="saMine"></div>' +
      '<p class="sa-foot">' + esc(cat.honesty) + " Not investment advice; FinChaya is not a SEBI-registered Research Analyst or Investment Adviser.</p>";
    const $ = id => host.querySelector("#" + id);
    $("saCancel").addEventListener("click", close);
    $("saKinds").addEventListener("click", e => {
      const b = e.target.closest(".sa-kind"); if (!b) return;
      S.kind = b.dataset.kind; S.done = false; $("saMsg").innerHTML = "";
      host.querySelectorAll(".sa-kind").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
      drawParams(); changed();
    });
    host.addEventListener("input", e => { if (e.target.closest("#saParams")) changed(); });
    host.addEventListener("change", e => { if (e.target.closest("#saParams")) { onChange(e.target); changed(); } });

    /* ---- per-kind forms ---- */
    function drawParams() {
      const k = S.kind, P = $("saParams");
      const tfOpts = cat.timeframes.map(t => [t.key, t.label]);
      let html = '<div class="sa-h">The details</div><div class="sa-grid">';
      if (k === "price") {
        html += field("p_dir", "The close goes", sel("p_dir", [["above", "above"], ["below", "below"]], "above")) +
          field("p_src", "compared with", sel("p_src", cat.price.sources.map(s => [s.key, s.label]), "number")) +
          field("p_level", "Price (₹)", num("p_level", "", {min: 0, ph: stock.price != null ? "e.g. " + Math.round(stock.price * 1.05) : ""})) +
          field("p_days", "Number of sessions", num("p_days", cat.price.default_days, {min: 1, max: cat.price.max_days, step: 1}), {hidden: true}) +
          field("p_date", "Date", '<input id="p_date" type="date">', {hidden: true});
      } else if (k === "ema_support") {
        const c = cat.ema_support;
        html += field("e_tf", "On the chart", sel("e_tf", tfOpts, "D")) + field("e_period", "EMA length (candles)", num("e_period", c.period.default, {min: c.period.min, max: c.period.max, step: 1})) +
          field("e_mode", "The price should", sel("e_mode", c.modes.map(m => [m.key, m.label]), "holds"), {full: true}) +
          field("e_tol", "Distance from the EMA (%)", num("e_tol", c.tolerance.default, {min: c.tolerance.min, max: c.tolerance.max, step: 0.1}));
      } else if (k === "trend_change") {
        const c = cat.trend_change;
        html += field("t_tf", "On the chart", sel("t_tf", tfOpts, "D")) + field("t_dir", "Which way", sel("t_dir", c.directions.map(m => [m.key, m.label]), "up")) +
          field("t_meas", "How the turn is measured", sel("t_meas", c.measures.map(m => [m.key, m.label]), "MAJOR"), {full: true}) +
          field("t_chart", "Prices used", sel("t_chart", c.charts.map(m => [m.key, m.label]), "LINE")) +
          field("t_fall", "The earlier move was at least", sel("t_fall", c.falls.map(v => [v, v + "%"]), c.default_fall));
      } else if (k === "indicator") {
        html += field("i_tf", "On the chart", sel("i_tf", tfOpts, "D")) +
          field("i_ind", "Indicator", '<select id="i_ind">' + groupedIndicators(cat.indicators) + "</select>") +
          '<div class="sa-full sa-grid" id="i_params" style="padding:0"></div>' + field("i_test", "Alert me when it", '<select id="i_test"></select>', {full: true}) +
          '<div class="sa-full sa-grid" id="i_inputs" style="padding:0"></div>';
      } else if (k === "results") {
        const c = cat.results;
        html += field("r_event", "Tell me", sel("r_event", c.events.map(m => [m.key, m.label]), "filed"), {full: true}) +
          field("r_only", "", '<label style="display:flex;gap:8px;align-items:center;font-size:14px;color:var(--sa-ink,#1b2b24);font-weight:500"><input id="r_only" type="checkbox" style="width:auto;min-height:0"> Only if the results match something I choose</label>', {full: true}) +
          field("r_metric", "Measure", sel("r_metric", c.metrics.map(m => [m.key, m.label]), "profit"), {hidden: true}) +
          field("r_dir", "Compared with a year ago", sel("r_dir", [["up", "is up"], ["down", "is down"]], "up"), {hidden: true}) +
          field("r_pct", "By at least (%)", num("r_pct", 0, {min: 0, max: 10000, step: 1}), {hidden: true});
      }
      html += "</div>";
      P.innerHTML = html;
      if (k === "indicator") { drawIndicator(); }
      onChange(null);
    }

    function groupedIndicators(inds) {
      const groups = {};
      inds.forEach(i => { (groups[i.group] = groups[i.group] || []).push(i); });
      return Object.keys(groups).map(g => '<optgroup label="' + esc(g) + '">' + groups[g].map(i => '<option value="' + esc(i.key) + '">' + esc(i.label) + "</option>").join("") + "</optgroup>").join("");
    }
    const indOf = key => cat.indicators.find(i => i.key === key);

    function drawIndicator() {
      const ind = indOf($("i_ind").value);
      const pf = ind.params.map(p => field("ip_" + p.name, esc(p.label), num("ip_" + p.name, p.default, {min: p.min, max: p.max, step: p.type === "int" ? 1 : "any"}))).join("");
      $("i_params").innerHTML = pf;
      drawTests(null);
    }
    function paramVals() {
      const ind = indOf($("i_ind").value), v = {};
      ind.params.forEach(p => { const el = $("ip_" + p.name); v[p.name] = el && el.value !== "" ? Number(el.value) : p.default; });
      return v;
    }
    // The words in a test's label that stand for numbers typed BELOW it ("is above {level}") are shown as plain words here; the
    // real numbers appear in the sentence the server sends back.
    const INPUT_WORDS = {level: "your level", low: "your lower level", high: "your upper level", bars: "N", times: "X"};
    function drawTests(keep) {
      const ind = indOf($("i_ind").value), vals = paramVals();
      const tests = ind.tests;
      $("i_test").innerHTML = tests.map(t => '<option value="' + esc(t.key) + '">' + esc(fill(t.short, Object.assign({}, INPUT_WORDS, vals))) + "</option>").join("");
      const kept = keep && tests.some(t => t.key === keep);
      if (kept) $("i_test").value = keep;
      drawInputs(kept);
    }
    function drawInputs(preserve) {
      const ind = indOf($("i_ind").value), t = ind.tests.find(x => x.key === $("i_test").value);
      const had = {};                                           // numbers already typed for THIS test survive a change of period
      if (preserve) host.querySelectorAll("#i_inputs input").forEach(el => { had[el.id] = el.value; });
      $("i_inputs").innerHTML = t.inputs.map(p => field("ii_" + p.name, esc(p.label), num("ii_" + p.name, had["ii_" + p.name] != null ? had["ii_" + p.name] : p.default, {min: p.min, max: p.max, step: p.type === "int" ? 1 : "any"}))).join("");
    }

    function onChange(el) {                                       // fields that show or hide others
      const id = el && el.id, k = S.kind;
      if (k === "price" && (!id || id === "p_src")) {
        const s = $("p_src").value;
        $("p_levelF").hidden = s !== "number"; $("p_daysF").hidden = !/^last_n/.test(s); $("p_dateF").hidden = !/^date_/.test(s);
      }
      if (k === "trend_change" && (!id || id === "t_meas")) $("t_fallF").hidden = $("t_meas").value !== "MAJOR";
      if (k === "results" && (!id || id === "r_event" || id === "r_only")) {
        const filed = $("r_event").value === "filed";
        $("r_onlyF").hidden = !filed;
        const on = filed && $("r_only").checked;
        ["r_metricF", "r_dirF", "r_pctF"].forEach(x => { $(x).hidden = !on; });
      }
      if (k === "indicator") {
        if (id === "i_ind") drawIndicator();
        else if (id === "i_test") drawInputs(false);
        else if (id && /^ip_/.test(id)) { drawTests($("i_test").value); }
        else if (id === "i_tf") { /* nothing else depends on it */ }
      }
    }

    /* ---- reading the form into the payload ---- */
    function payload() {
      const k = S.kind, v = id => { const el = $(id); return el ? el.value : ""; };
      if (k === "price") {
        const src = v("p_src"), o = {kind: "price", direction: v("p_dir"), source: src};
        if (src === "number") { if (v("p_level") === "") return {wait: "Type the price you want to be told about."}; o.level = Number(v("p_level")); }
        else if (/^last_n/.test(src)) o.days = Number(v("p_days") || 0);
        else { if (!v("p_date")) return {wait: "Choose the date."}; o.date = v("p_date"); }
        return {alert: o};
      }
      if (k === "ema_support") return {alert: {kind: k, tf: v("e_tf"), period: Number(v("e_period")), mode: v("e_mode"), tolerance: Number(v("e_tol"))}};
      if (k === "trend_change") return {alert: {kind: k, tf: v("t_tf"), direction: v("t_dir"), measure: v("t_meas"), chart: v("t_chart"), fall: Number(v("t_fall"))}};
      if (k === "indicator") {
        const ind = indOf(v("i_ind")), t = ind.tests.find(x => x.key === v("i_test")), params = {}, inputs = {};
        ind.params.forEach(p => { params[p.name] = $("ip_" + p.name).value === "" ? null : Number($("ip_" + p.name).value); });
        t.inputs.forEach(p => { inputs[p.name] = $("ii_" + p.name).value === "" ? null : Number($("ii_" + p.name).value); });
        return {alert: {kind: k, tf: v("i_tf"), indicator: ind.key, params, test: t.key, inputs}};
      }
      if (k === "results") {
        const o = {kind: k, event: v("r_event")};
        if (o.event === "filed" && $("r_only").checked) o.filter = {metric: v("r_metric"), direction: v("r_dir"), min_pct: Number(v("r_pct") || 0)};
        return {alert: o};
      }
      return {wait: "Choose what to be alerted about."};
    }

    /* ---- the live sentence ---- */
    function changed() {
      S.ready = false; $("saGo").disabled = true;
      clearTimeout(S.timer);
      if (!S.kind) { return; }
      const p = payload();
      if (p.wait) { setSum("idle", esc(p.wait)); return; }
      setSum("idle", "Checking&hellip;");
      const mine = ++S.token;
      S.timer = setTimeout(async () => {
        try {
          const d = await call("/describe", {isin: stock.isin, alert: p.alert});
          if (mine !== S.token) return;
          if (!d.ok) { setSum("bad", esc(d.problem)); return; }
          setSum("ok", "You will get <b>one email</b> when <b>" + esc(stock.symbol || stock.name || "this stock") + " " + esc(d.sentence) + "</b>." +
            (d.notes && d.notes.length ? "<ul>" + d.notes.map(n => "<li>" + esc(n) + "</li>").join("") + "</ul>" : ""));
          S.ready = true; S.alert = p.alert; $("saGo").disabled = false;
        } catch (e) {
          if (mine !== S.token) return;
          if (e.message === "access") { lock(host, (await getAccess(true)) || {}); return; }
          setSum("bad", esc(e.message));
        }
      }, 350);
    }
    function setSum(kind, html) {
      const s = $("saSum"); s.className = "sa-sum" + (kind === "bad" ? " sa-bad" : kind === "idle" ? " sa-idle" : ""); s.innerHTML = html;
    }

    /* ---- create ---- */
    $("saGo").addEventListener("click", async () => {
      if (!S.ready) return;
      $("saGo").disabled = true; $("saMsg").innerHTML = "";
      try {
        const d = await call("", {isin: stock.isin, alert: S.alert});
        const to = await mailTo();
        let html = '<div class="sa-ok"><b>' + (d.already ? "You already have this alert." : "Alert set.") + "</b> We will email " +
          (to ? "<b>" + esc(to) + "</b>" : "you") + " once, when " + esc(stock.symbol || "the stock") + " " + esc(d.alert.sentence) + ".</div>";
        html += readiness(d);
        $("saMsg").innerHTML = html;
        S.done = true;
        window.dispatchEvent(new CustomEvent("sa-changed", {detail: {isin: stock.isin}}));
        loadMine();
      } catch (e) {
        if (e.message === "access") { lock(host, (await getAccess(true)) || {}); return; }
        $("saMsg").innerHTML = '<div class="sa-warn">The alert was NOT saved. ' + esc(e.message) + "</div>";
        $("saGo").disabled = false;
      }
    });
    let mailCache = null;
    async function mailTo() { if (mailCache) return mailCache; const a = await getAccess(); mailCache = a && a.email; return mailCache; }

    function readiness(d) {
      let h = "";
      if (d.email && !d.email.configured) {
        h += '<div class="sa-warn"><b>Heads up: email is not set up on this server yet</b>, so this alert is saved but cannot reach you until it is.' +
          (d.email.why ? " (" + esc(d.email.why) + ")" : "") + "</div>";
      }
      const c = d.checker;
      if (c && c.known !== false && (c.never_run || c.stale)) {
        h += '<div class="sa-warn"><b>Heads up: the alert checker ' + (c.never_run ? "has not run yet" : "has not run lately") + "</b>, so nothing is being watched until it does.</div>";
      }
      return h;
    }

    /* ---- the person's alerts on this stock ---- */
    async function loadMine() {
      try {
        const d = await call("?isin=" + encodeURIComponent(stock.isin));
        const list = d.alerts || [];
        $("saMine").innerHTML = list.length
          ? '<div class="sa-h">Your alerts on this stock</div>' + list.map(a => '<div class="sa-row" data-id="' + a.alert_id + '"><div>' + chip(a) + esc(a.sentence) +
              (a.status === "triggered" && a.trigger_reading ? '<div class="sa-help">' + esc(a.trigger_reading) + "</div>" : "") +
              (a.note ? '<div class="sa-help">' + esc(a.note) + "</div>" : "") + '</div><button type="button" class="sa-del" data-del="' + a.alert_id + '">Remove</button></div>').join("")
          : "";
        if (!S.done && !S.kind) {
          const r = readiness(d); if (r) $("saMsg").innerHTML = r;
        }
      } catch (e) { /* the list is a convenience */ }
    }
    function chip(a) {
      return a.status === "triggered" ? '<span class="sa-chip hit">Happened</span>' : a.status === "paused" ? '<span class="sa-chip">Paused</span>' : '<span class="sa-chip on">Watching</span>';
    }
    $("saMine").addEventListener("click", async e => {
      const b = e.target.closest("[data-del]"); if (!b) return;
      b.disabled = true;
      try { await call("/" + b.dataset.del + "/delete", {}); window.dispatchEvent(new CustomEvent("sa-changed", {detail: {isin: stock.isin}})); loadMine(); }
      catch (err) { b.disabled = false; $("saMsg").innerHTML = '<div class="sa-warn">' + esc(err.message === "access" ? "Please sign in again." : err.message) + "</div>"; }
    });
    loadMine();
  }

  window.fcStockAlerts = {attach, open};
})();
