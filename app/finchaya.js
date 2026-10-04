/* ===================================================================
   finchaya.js -- the header, on every page.
   ===================================================================
   Seven pages and no way between them: someone on the plan page could
   not reach their portfolios without typing a URL.

   WHAT SITS WHERE, AND WHY
       Across the top: only what everyone uses -- funds, stocks, the
       planner. Everything else lives behind a menu.

       That is not tidiness. A retail investor shown a "Clients" tab is
       being told the product is for somebody else, and any signed-in
       visitor who found that page could start filing client records
       simply because it was advertised to them.

   WHAT THIS IS NOT
       A permission system. Every endpoint checks entitlement itself.
       Hiding a link protects nothing -- it just stops the wrong door
       being pointed at. The menu asks the server who is looking and
       fails CLOSED: no answer means the extra items stay hidden.

   SIGNING IN
       The dialogue used to exist only in index.html, so every other page
       could tell you to sign in and give you no way to do it -- /plan
       refused a review, and the header offered nothing. It lives here now
       because the header is the one thing on all of them.

       index.html keeps its own: render() stands down when a page already
       has navigation, so the two never both appear.

   The script is deferred and inserts itself at the top of <body>, so
   content renders first. A navigation bar is not worth a blank screen.
   A page with its own navigation is left alone; two headers is worse
   than none.
   =================================================================== */
(function () {
  // Everyone. Nothing here needs an account.
  const MAIN = [
    {href: "/",          label: "Funds",   match: p => p === "/"},
    {href: "/#/stocks",  label: "Stocks",  match: () => false},
    {href: "/plan",      label: "Plan",    match: p => p.startsWith("/plan")},
    // Across the top rather than behind the menu, because it is a
    // question people arrive with rather than one they build up to. An
    // investor who already holds four funds and suspects two of them are
    // the same thing has no reason to open a planner, and would not think
    // to look for the answer inside one.
    {href: "/overlap",   label: "Overlap", match: p => p.startsWith("/overlap")},
  ];

  // Behind the menu. `need` decides who is offered it.
  const MORE = [
    {href: "/portfolios", label: "My portfolios", need: "signed_in"},
    {href: "/portfolio",  label: "Look through a portfolio", need: null},
    // Same treatment as the look-through above: a paid deep-dive tool
    // somebody arrives at with a question already in mind ("which funds
    // clear a bar"), not a utility everyone opens every visit -- so it
    // sits behind the menu rather than across the top.
    {href: "/screener",   label: "Fund screener", need: null},
    {href: "/clients",    label: "Clients",       need: "distributor"},
    // Named for what it shows, not for what it is. "Admin" is a word
    // about permissions; the reason to open it is that it holds everyone
    // else's plans and portfolios, and the menu should say so -- the page
    // existed for days and was looked for in the portfolio section.
    {href: "/admin",      label: "All users’ data", need: "admin"},
    {href: "/allocation", label: "Allocation rules", need: "admin"},
  ];

  let AUTH = {signed_in: false, user: null, google_client_id: ""};
  let gsiReady = false, gsiLoad = null;
  const esc = t => String(t == null ? "" : t).replace(/[&<>"]/g,
    c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

  function render(who, auth) {
    if (document.querySelector(".fc-head") || document.querySelector("nav.nav"))
      return;
    const here = location.pathname.replace(/\/$/, "") || "/";

    const items = MORE.filter(l => !l.need || who[l.need]);
    const head = document.createElement("header");
    head.className = "fc-head";
    head.innerHTML =
      '<a class="fc-brand" href="/">Fin<span>Chaya</span></a>' +
      '<nav class="fc-nav">' +
        MAIN.map(l => '<a href="' + l.href + '"' +
          (l.match(here) ? ' aria-current="page"' : '') + '>' + l.label + '</a>'
        ).join("") +
      '</nav>' +
      '<div class="fc-right">' +
        (items.length ? '<div class="fc-more">' +
          '<button type="button" class="fc-moreb" aria-expanded="false">' +
            'More<svg viewBox="0 0 24 24" class="ico"><path d="M6 9l6 6 6-6"/></svg>' +
          '</button>' +
          '<div class="fc-menu" hidden>' +
            items.map(l => '<a href="' + l.href + '"' +
              (here.startsWith(l.href) ? ' aria-current="page"' : '') + '>' +
              l.label + '</a>').join("") +
          '</div></div>' : "") +
        '<div class="fc-auth" id="fcAuth"></div>' +
      '</div>';

    document.body.insertBefore(head, document.body.firstChild);
    paintAuth(auth);

    const btn = head.querySelector(".fc-moreb");
    if (!btn) return;
    const menu = head.querySelector(".fc-menu");

    // The inline display is deliberate belt-and-braces. `hidden` alone was
    // being overridden by .fc-menu{display:flex} -- the CSS now answers
    // that, but an inline style beats any stylesheet, so a cached copy of
    // the old CSS cannot put the menu back on screen at page load.
    const setOpen = on => {
      menu.hidden = !on;
      menu.style.display = on ? "flex" : "none";
      btn.setAttribute("aria-expanded", String(on));
    };
    setOpen(false);

    const shut = () => setOpen(false);
    btn.addEventListener("click", e => {
      e.stopPropagation();
      setOpen(btn.getAttribute("aria-expanded") !== "true");
    });
    document.addEventListener("click", shut);
    document.addEventListener("keydown", e => { if (e.key === "Escape") shut(); });
  }

  /* ---- the account -------------------------------------------------
     Google only, matching index.html. The OTP endpoints exist on the
     server but no page has ever offered them, and shipping a second way
     in that nobody has used would be its own risk. */
  function paintAuth(auth) {
    const slot = document.getElementById("fcAuth");
    if (!slot) return;
    if (auth.signed_in) {
      const who = (auth.user && (auth.user.email || auth.user.full_name)) || "";
      slot.innerHTML = '<span class="fc-who" title="' + esc(who) + '">' +
        esc(who) + '</span>' +
        '<button type="button" class="fc-authb quiet" id="fcAcctBtn">Subscription</button>' +
        '<button type="button" class="fc-authb quiet" id="fcSignOut">Sign out</button>';
      document.getElementById("fcSignOut").addEventListener("click", signOut);
      document.getElementById("fcAcctBtn").addEventListener("click",
        () => window.fcAccount());
    } else {
      slot.innerHTML =
        '<button type="button" class="fc-authb" id="fcSignIn">Sign in</button>';
      document.getElementById("fcSignIn").addEventListener("click", () => openAuth());
    }
  }

  function authBox() {
    let m = document.getElementById("fcAuthModal");
    if (m) return m;
    m = document.createElement("div");
    m.id = "fcAuthModal";
    m.className = "fc-modal";
    m.innerHTML =
      '<div class="fc-modalbox" role="dialog" aria-modal="true"' +
           ' aria-labelledby="fcAuthTitle">' +
        '<h3 id="fcAuthTitle">Sign in</h3>' +
        '<p class="fc-authsub">Use your Google account. Nothing to fill in,' +
          ' and no password to remember.</p>' +
        '<div class="fc-gslot" id="fcGoogleBtn"></div>' +
        '<p class="fc-authmsg" id="fcAuthMsg"></p>' +
        '<p class="fc-authfoot">' +
          '<button type="button" class="fc-authb quiet" id="fcAuthCancel">' +
          'Cancel</button></p>' +
      '</div>';
    document.body.appendChild(m);
    const shut = () => { m.hidden = true; m.style.display = "none"; };
    shut();
    document.getElementById("fcAuthCancel").addEventListener("click", shut);
    m.addEventListener("click", e => { if (e.target === m) shut(); });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && !m.hidden) shut();
    });
    return m;
  }

  // The Google script is fetched only when somebody asks to sign in. It
  // is a third party on every page otherwise, for a button most visitors
  // never press.
  function loadGsi() {
    if (window.google && window.google.accounts && window.google.accounts.id)
      return Promise.resolve(true);
    if (!gsiLoad) {
      gsiLoad = new Promise(resolve => {
        const sc = document.createElement("script");
        sc.src = "https://accounts.google.com/gsi/client";
        sc.async = true; sc.defer = true;
        sc.onload = () => resolve(true);
        sc.onerror = () => resolve(false);
        document.head.appendChild(sc);
      });
    }
    return gsiLoad;
  }

  async function openAuth() {
    const m = authBox();
    const msg = document.getElementById("fcAuthMsg");
    const say = (text, bad) => {
      msg.className = "fc-authmsg" + (bad ? " bad" : "");
      msg.textContent = text;
    };
    say("");
    m.hidden = false; m.style.display = "flex";

    if (!AUTH.google_client_id) {
      say("Google sign-in is not switched on for this server yet.", true);
      return;
    }
    if (!await loadGsi()) {
      say("Google sign-in could not load. Check your connection and try again.",
          true);
      return;
    }
    if (!gsiReady) {
      window.google.accounts.id.initialize({
        client_id: AUTH.google_client_id,
        callback: onCredential,
        cancel_on_tap_outside: true,
      });
      gsiReady = true;
    }
    const slot = document.getElementById("fcGoogleBtn");
    slot.innerHTML = "";
    window.google.accounts.id.renderButton(slot, {
      theme: "outline", size: "large", width: 280,
      text: "continue_with", shape: "pill",
    });
  }

  async function onCredential(resp) {
    const msg = document.getElementById("fcAuthMsg");
    msg.className = "fc-authmsg";
    msg.textContent = "Signing you in…";
    try {
      const r = await fetch("/api/auth/google", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({credential: resp.credential})});
      if (!r.ok) {
        let d = null;
        try { d = await r.json(); } catch { /* no body */ }
        throw new Error((d && d.detail) || ("server returned " + r.status));
      }
      // Reload rather than re-render. This script has no idea what the
      // page around it shows, and several of them gate their content on
      // the session -- a page left stale after signing in is the
      // confusing outcome, not the slow one.
      location.reload();
    } catch (e) {
      msg.className = "fc-authmsg bad";
      msg.textContent = e.message || "That sign-in did not work.";
    }
  }

  async function signOut() {
    try { await fetch("/api/auth/logout", {method: "POST"}); }
    catch { /* revoke locally anyway */ }
    // Without this Google silently re-selects the same account next time
    // and signing out looks as though it did nothing.
    try { window.google.accounts.id.disableAutoSelect(); }
    catch { /* not loaded on this page */ }
    location.reload();
  }

  // So a page that gets a 401 can offer the dialogue instead of printing
  // "sign in" with nothing to press.
  window.fcSignIn = openAuth;

  /* ---- the subscription notice -------------------------------------
     One wording, three pages. The options stay on screen and only the
     RESULT is replaced -- someone who cannot see the look-through can
     still set a goal and choose funds, so what they are being asked to
     pay for is visible rather than merely withheld.

         fcLocked(host, "the look-through")

     window.fcAuthState is filled by start() below, so a page can ask
     whether to render the real thing or this. It is a CONVENIENCE, not a
     control: every endpoint refuses on its own, and a page that forgot to
     check would get a 402 rather than data. */
  window.fcAuthState = {signed_in: false, score_access: false,
                        subscribed: false, scores_enabled: true, ready: false};

  /* ---- paying -------------------------------------------------------
     fcSubscribe(msgEl) opens Razorpay Checkout for the signed-in account.

     THE PAGE NEVER UNLOCKS ANYTHING. When Checkout reports success we post
     what it said to /api/pay/verify (which only records it) and then ASK
     /api/pay/status whether the webhook has switched the account on. Only
     that answer reloads the page. See payments.py for why. */
  function loadCheckout() {
    if (window.Razorpay) return Promise.resolve();
    return new Promise((ok, no) => {
      const s = document.createElement("script");
      s.src = "https://checkout.razorpay.com/v1/checkout.js";
      s.onload = ok;
      s.onerror = () => no(new Error("Could not load the payment window. "
        + "Check your connection and try again."));
      document.head.appendChild(s);
    });
  }

  async function waitForAccess(say) {
    say("Payment received. Confirming with the bank…");
    for (let i = 0; i < 30; i++) {            // about 90 seconds
      await new Promise(r => setTimeout(r, 3000));
      try {
        const s = await fetch("/api/pay/status").then(r => r.json());
        if (s.premium) { location.reload(); return; }
      } catch { /* keep waiting */ }
    }
    say("Your payment went through but the bank has not confirmed it yet. "
      + "Access switches on by itself when it does - reload in a few "
      + "minutes, or email finchaya2026@gmail.com if it does not.");
  }

  window.fcSubscribe = async function (msgEl, plan) {
    const say = (t, bad) => {
      if (!msgEl) return;
      msgEl.textContent = t;
      msgEl.style.color = bad ? "#b3261e" : "";
    };
    try {
      say("Opening the payment window…");
      await loadCheckout();
      const r = await fetch("/api/pay/subscription?plan="
        + encodeURIComponent(plan || "yearly"), {method: "POST"});
      if (r.status === 401) { say(""); window.fcSignIn && window.fcSignIn(); return; }
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "Could not start the payment.");

      const rz = new window.Razorpay({
        key: d.key_id,
        subscription_id: d.subscription_id,
        name: "FinChaya",
        description: "FinChaya subscription",
        prefill: {email: (AUTH && AUTH.email) || ""},
        theme: {color: "#0f7a56"},
        handler: async resp => {
          try {
            await fetch("/api/pay/verify", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify(resp),
            });
          } catch { /* the webhook is the real signal */ }
          waitForAccess(say);
        },
        modal: {ondismiss: () => say("")},
      });
      rz.on("payment.failed", e => say(
        (e && e.error && e.error.description) || "The payment failed.", true));
      rz.open();
      say("");
    } catch (e) {
      say(e.message || "Something went wrong. Nothing was charged.", true);
    }
  };

  // Is billing switched on, and what are the plans? One cheap call,
  // cached for the page's life. Fails closed: no answer means "off".
  let payCfg = null;
  async function billingCfg() {
    if (payCfg === null)
      payCfg = await fetch("/api/pay/config").then(r => r.json())
        .catch(() => ({enabled: false, plans: []}));
    return payCfg;
  }

  // Fills `host` with the plan choice (yearly pre-selected, because the
  // server lists it first and marks it default) and a Subscribe button.
  // Resolves false, drawing nothing, when billing is off.
  window.fcPayBox = async function (host) {
    const cfg = await billingCfg();
    if (!cfg.enabled || !cfg.plans || !cfg.plans.length || !host) return false;
    if (!document.getElementById("fcPlanCss")) {
      const st = document.createElement("style");
      st.id = "fcPlanCss";
      st.textContent = ".fc-plans{display:flex;flex-direction:column;gap:8px;"
        + "margin:10px 0;width:100%}.fc-plan{display:flex;gap:10px;"
        + "align-items:flex-start;padding:10px 12px;border:1.5px solid #cfd8d3;"
        + "border-radius:10px;cursor:pointer;text-align:left}"
        + ".fc-plan:has(input:checked){border-color:#0f7a56;background:#e3f3ec}"
        + ".fc-plan small{display:block;font-size:13px;opacity:.75}";
      document.head.appendChild(st);
    }
    host.innerHTML = '<div class="fc-plans">' + cfg.plans.map(p => `
      <label class="fc-plan">
        <input type="radio" name="fcPlan" value="${esc(p.plan)}"
               ${p.default ? "checked" : ""}>
        <span><b>${esc(p.price)}</b>
          <small>${esc(p.note || "")}</small></span>
      </label>`).join("") + `</div>
      <button type="button" class="fc-authb" id="fcPayGo">Subscribe</button>
      <span class="sub" id="fcPayMsg"></span>`;
    host.querySelector("#fcPayGo").addEventListener("click", () => {
      const pick = host.querySelector('input[name="fcPlan"]:checked');
      window.fcSubscribe(host.querySelector("#fcPayMsg"),
                         pick ? pick.value : "yearly");
    });
    return true;
  };

  /* ---- my subscription ----------------------------------------------
     fcAccount() opens a small dialog: what the account has, when it
     renews or ends, and a Cancel button. Cancelling never removes paid
     time -- the server cancels at the end of the period -- and the wording
     says so before the person confirms. Built here, styled here, so the
     header on every page can call it. */
  const fmtDay = iso => {
    if (!iso) return "";
    const d = new Date(iso + "T00:00:00");
    return isNaN(d) ? iso : d.toLocaleDateString("en-IN",
      {day: "numeric", month: "long", year: "numeric"});
  };

  window.fcAccount = async function () {
    if (!document.getElementById("fcAcctCss")) {
      const st = document.createElement("style");
      st.id = "fcAcctCss";
      st.textContent = ".fc-acct{position:fixed;inset:0;z-index:9999;"
        + "background:rgba(0,0,0,.45);display:flex;align-items:center;"
        + "justify-content:center;padding:16px}"
        + ".fc-acct-box{background:#fff;color:#1b2b24;border-radius:14px;"
        + "max-width:420px;width:100%;padding:22px;max-height:90vh;"
        + "overflow:auto;box-shadow:0 10px 40px rgba(0,0,0,.25)}"
        + ".fc-acct-box h3{margin:0 0 10px;font-size:19px}"
        + ".fc-acct-box p{margin:0 0 12px;font-size:15px;line-height:1.45}"
        + ".fc-acct-row{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}"
        + ".fc-acct-note{font-size:13px;opacity:.7}";
      document.head.appendChild(st);
    }
    const old = document.getElementById("fcAcct");
    if (old) old.remove();
    const ov = document.createElement("div");
    ov.id = "fcAcct";
    ov.className = "fc-acct";
    ov.innerHTML = '<div class="fc-acct-box" role="dialog" aria-modal="true" '
      + 'aria-labelledby="fcAcctT"><h3 id="fcAcctT">My subscription</h3>'
      + '<div id="fcAcctBody"><p>Loading…</p></div></div>';
    document.body.appendChild(ov);
    const shut = () => ov.remove();
    ov.addEventListener("click", e => { if (e.target === ov) shut(); });
    document.addEventListener("keydown", function k(e) {
      if (e.key === "Escape") { shut(); document.removeEventListener("keydown", k); }
    });
    const body = ov.querySelector("#fcAcctBody");
    const closeBtn = '<div class="fc-acct-row"><button type="button" '
      + 'class="fc-authb quiet" id="fcAcctClose">Close</button></div>';
    const wireClose = () => {
      const c = body.querySelector("#fcAcctClose");
      if (c) c.addEventListener("click", shut);
    };

    let s;
    try {
      const r = await fetch("/api/pay/status");
      if (r.status === 401) {
        body.innerHTML = "<p>Sign in to see your subscription.</p>" + closeBtn;
        wireClose();
        return;
      }
      s = await r.json();
    } catch {
      body.innerHTML = "<p>Could not load your subscription. Try again in "
        + "a moment.</p>" + closeBtn;
      wireClose();
      return;
    }

    const sub = s.subscription;
    const live = sub && ["authenticated", "active", "pending"]
      .includes(sub.status);
    const planName = sub && sub.plan ? sub.plan : "";
    const until = fmtDay((sub && sub.current_end) || s.premium_until);
    let html;

    if (live && !sub.cancel_requested) {
      html = "<p>Your <b>" + esc(planName || "FinChaya") + "</b> plan is "
        + "active" + (until ? " and renews on <b>" + esc(until) + "</b>"
        : "") + ".</p>"
        + '<div class="fc-acct-row">'
        + '<button type="button" class="fc-authb quiet" id="fcAcctCancel">'
        + "Cancel subscription</button>"
        + '<button type="button" class="fc-authb quiet" id="fcAcctClose">'
        + "Close</button></div>";
    } else if (sub && sub.cancel_requested && s.premium) {
      html = "<p>Your subscription is <b>cancelled</b>. You keep full "
        + "access until <b>" + esc(until || "the end of the period you paid "
        + "for") + "</b>, and you will not be charged again.</p>" + closeBtn;
    } else if (s.premium) {
      html = "<p>Your account has access"
        + (s.premium_until ? " until <b>" + esc(fmtDay(s.premium_until))
          + "</b>" : " with no end date") + ".</p>" + closeBtn;
    } else {
      html = "<p>You do not have an active subscription.</p>"
        + '<div id="fcAcctPay"></div>' + closeBtn;
    }
    body.innerHTML = html;
    wireClose();

    const pay = body.querySelector("#fcAcctPay");
    if (pay) window.fcPayBox(pay).then(drawn => {
      if (!drawn) pay.innerHTML = '<p class="fc-acct-note">Billing is not '
        + 'open yet. Email finchaya2026@gmail.com and we will switch it '
        + "on.</p>";
    });

    const cancel = body.querySelector("#fcAcctCancel");
    if (cancel) cancel.addEventListener("click", () => {
      body.innerHTML = "<p><b>Cancel your subscription?</b></p>"
        + "<p>You will keep full access until "
        + (until ? "<b>" + esc(until) + "</b>" : "the end of the period you "
          + "have paid for")
        + ". You will not be charged again, and there is no refund for the "
        + "current period.</p>"
        + '<p class="fc-acct-note" id="fcAcctMsg"></p>'
        + '<div class="fc-acct-row">'
        + '<button type="button" class="fc-authb" id="fcAcctYes">'
        + "Yes, cancel</button>"
        + '<button type="button" class="fc-authb quiet" id="fcAcctNo">'
        + "Keep my subscription</button></div>";
      body.querySelector("#fcAcctNo").addEventListener("click",
        () => window.fcAccount());
      body.querySelector("#fcAcctYes").addEventListener("click", async () => {
        const msg = body.querySelector("#fcAcctMsg");
        msg.textContent = "Cancelling…";
        try {
          const r = await fetch("/api/pay/cancel", {method: "POST"});
          const d = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(d.detail || "Could not cancel.");
          window.fcAccount();            // redraw with the new state
        } catch (e) {
          msg.textContent = e.message || "Could not cancel. Nothing changed.";
          msg.style.color = "#b3261e";
        }
      });
    });
  };

  window.fcLocked = function (host, what, detail) {
    if (!host) return;
    const signedIn = window.fcAuthState.signed_in;
    host.innerHTML = `
      <div class="fc-lock">
        <h3>${esc(what || "This")} is part of the subscription</h3>
        <p>${detail || "This reads through to the shares every fund holds "
                     + "underneath, rather than judging a fund by its name. "
                     + "That is the part that is paid for."}</p>
        <p class="fc-lock-act">
          ${signedIn
            ? '<span class="sub">Your account does not have a subscription '
              + 'yet. Email <a href="mailto:finchaya2026@gmail.com'
              + '?subject=FinChaya%20subscription">finchaya2026@gmail.com</a>'
              + ' and we will switch it on.</span>'
            : '<button type="button" class="fc-authb" id="fcLockIn">Sign in</button>'
              + '<span class="sub">Already subscribed? Sign in to see it.</span>'}
        </p>
      </div>`;
    const b = host.querySelector("#fcLockIn");
    if (b) b.addEventListener("click", () => window.fcSignIn && window.fcSignIn());

    // Signed in without a subscription: offer to pay if billing is on,
    // otherwise the email line above stays as the fallback.
    if (signedIn) {
      const act = host.querySelector(".fc-lock-act");
      if (act) window.fcPayBox(act);   // leaves the email line if billing is off
    }
  };

  /* ---- what changed since last month -------------------------------
     One component, two sources: a FUND's portfolio month on month, and one
     COMPANY's owners month on month. Same shape, same wording, so the two
     screens cannot describe the same event differently.

         fcChanges(host, {fund: scheme_code})
         fcChanges(host, {stock: isin})

     The design point is the second column. A holding moving from 5% to 6%
     might be a manager buying, or a manager doing nothing while the price
     rose -- and in weight alone those are identical. The share count is
     what separates them, so it is never omitted. */
  const CH_LABEL = {
    new: "New", bought: "Bought more", sold: "Sold some",
    exited: "Exited", held: "Left alone",
    "corporate action": "Bonus or split", unknown: "No share count",
  };
  const CH_TONE = {
    new: "buy", bought: "buy", sold: "sell", exited: "sell",
    held: "flat", "corporate action": "flat", unknown: "flat",
  };
  const CH_ORDER = ["new", "bought", "sold", "exited",
                    "corporate action", "held", "unknown"];

  const signed = n => (n > 0 ? "+" : "") + n.toFixed(2);

  window.fcChanges = async function (host, opts) {
    if (!host || !opts) return;
    const url = opts.fund
      ? "/api/portfolio/fund-changes/" + encodeURIComponent(opts.fund)
      : "/api/portfolio/stock-changes/" + encodeURIComponent(opts.stock);

    host.innerHTML = '<p class="fc-ch-sub">Comparing the last two months&hellip;</p>';
    let d, needsSub = false;
    try {
      const r = await fetch(url);
      const t = await r.text();
      d = t ? JSON.parse(t) : null;
      // 402 is the stock-side log, which names the funds that bought and
      // sold and is part of the subscription. Not an error to apologise
      // for: it gets the same prompt as every other paid screen.
      if (r.status === 402) { needsSub = true; throw new Error("subscription"); }
      if (!r.ok) throw new Error((d && d.detail) || ("server returned " + r.status));
    } catch (e) {
      if (needsSub) {
        host.hidden = false;
        window.fcLocked(host, "Which funds bought and sold this stock",
          "The funds that added to, trimmed or exited a company between the " +
          "last two month-ends are part of the subscription.");
        return;
      }
      host.innerHTML = '<p class="fc-ch-sub">' + esc(e.message) + "</p>";
      return;
    }

    host.className = (host.className + " fc-ch").trim();
    host.hidden = false;

    if (!d.comparable) {
      // Not an error. Every fund looked like this until a second month
      // of portfolios existed.
      host.innerHTML = '<p class="fc-ch-head">Nothing to compare yet</p>' +
        '<p class="fc-ch-sub">' + esc(d.reason || "") + "</p>";
      return;
    }

    const rows = d.changes || d.funds || [];
    const isFund = !!opts.fund;
    const c = d.counts || {};
    const groups = CH_ORDER.filter(k => (c[k] || 0) > 0);
    if (!groups.length) {
      host.innerHTML = '<p class="fc-ch-head">No change</p>' +
        '<p class="fc-ch-sub">Nothing was added, removed or traded between ' +
        esc(d.compared_with) + " and " + esc(d.as_of) + ".</p>";
      return;
    }
    let active = groups[0];

    const moved = ["new", "bought", "sold", "exited"]
      .reduce((a, k) => a + (c[k] || 0), 0);
    const headline = isFund
      ? (moved
          ? moved + " position" + (moved === 1 ? "" : "s") + " changed hands"
          : "No trading this month")
      : (moved + " fund" + (moved === 1 ? "" : "s") + " moved on this company");

    const quiet = c.held
      ? " " + c.held + " " + (isFund ? "holding" : "fund") +
        (c.held === 1 ? " was" : "s were") + " left untouched — any " +
        "weight change there is the market, not a decision."
      : "";

    host.innerHTML = `
      <p class="fc-ch-head">${esc(headline)}</p>
      <p class="fc-ch-sub">Between ${esc(d.compared_with)} and
        ${esc(d.as_of)}.${isFund && d.traded_pct != null
          ? " " + d.traded_pct + "% of the fund is in positions that were "
            + "opened, closed or resized." : ""}${quiet}</p>
      <div class="fc-ch-tabs" role="tablist">
        ${groups.map(k => `<button type="button" class="fc-ch-tab" role="tab"
          data-g="${k}" aria-selected="${k === active}">${
          esc(CH_LABEL[k] || k)} (${c[k]})</button>`).join("")}
      </div>
      <div class="fc-ch-list" id="fcChList"></div>
      <p class="fc-ch-foot" id="fcChFoot"></p>`;

    const list = host.querySelector("#fcChList");

    function paint() {
      const mine = rows.filter(r => r.kind === active);
      if (!mine.length) {
        list.innerHTML = '<p class="fc-ch-empty">Nothing here.</p>';
        return;
      }
      list.innerHTML = mine.map(r => {
        const nm = isFund ? r.name : r.scheme_name;
        const weight = r.pct_prev == null
          ? r.pct_now.toFixed(2) + "% (new)"
          : r.pct_now == null
            ? "was " + r.pct_prev.toFixed(2) + "%"
            : r.pct_prev.toFixed(2) + "% &rarr; " + r.pct_now.toFixed(2) + "%";
        const shares = r.qty_delta_pct == null ? "&ndash;"
          : (r.qty_delta_pct > 0 ? "+" : "") + r.qty_delta_pct + "% shares";
        return `<div class="fc-ch-row">
          <span class="fc-ch-nm">${esc(nm || "")}</span>
          <span class="fc-ch-w">${weight}</span>
          <span class="fc-ch-q ${CH_TONE[r.kind] || "flat"}">${shares}</span>
        </div>`;
      }).join("");
    }

    host.querySelectorAll(".fc-ch-tab").forEach(b =>
      b.addEventListener("click", () => {
        active = b.dataset.g;
        host.querySelectorAll(".fc-ch-tab").forEach(x =>
          x.setAttribute("aria-selected", String(x.dataset.g === active)));
        paint();
      }));

    const notes = ["<b>Weight and shares are different things.</b> A holding "
      + "can gain weight while the manager sells it, if the price rose faster "
      + "than they trimmed. The share column is the one that says whether "
      + "anyone acted."];
    if (c["corporate action"])
      notes.push("A bonus or split changes the share count for every holder "
        + "at once, so those rows are marked rather than read as buying.");
    if (!isFund && d.not_disclosed)
      notes.push("<b>" + d.not_disclosed + " fund(s) that held this company "
        + "have not published a " + esc(d.as_of) + " portfolio yet</b> and "
        + "are left out. They have not sold; they have not reported.");
    notes.push("AMCs disclose monthly, so both months are month-end "
      + "snapshots. Anything bought and sold inside a month is invisible here.");
    host.querySelector("#fcChFoot").innerHTML = notes.join(" ");

    paint();
  };

  /* ---- portfolio overlap -------------------------------------------
     Lives here, not in a page, because it is wanted in three: the fund
     page's compare tab, the portfolio look-through, and the planner. One
     component, three call sites -- the alternative is three copies that
     drift, and two screens of one product disagreeing about the same two
     funds is worse than neither showing it.

         fcOverlap(hostElement, codeA, codeB)

     Everything it needs comes from /api/portfolio/overlap. */

  // A picture that is merely suggestive of the number would be worse than
  // no picture, so the circles are placed so the LENS AREA is genuinely
  // the overlap share of each circle. Binary search on the standard
  // two-circle lens formula; forty steps is far past pixel resolution.
  function vennGap(p) {
    if (!(p > 0)) return 2;
    if (p >= 1) return 0;
    let lo = 0, hi = 2;
    for (let i = 0; i < 40; i++) {
      const d = (lo + hi) / 2;
      const lens = 2 * Math.acos(d / 2)
                 - (d / 2) * Math.sqrt(Math.max(0, 4 - d * d));
      if (lens / Math.PI > p) lo = d; else hi = d;
    }
    return (lo + hi) / 2;
  }

  const KIND = {
    FOREIGN: "overseas holdings", CASH: "cash", DEBT: "debt",
    TREPS: "TREPS and repo", REIT: "REITs and InvITs",
    EQUITY: "shares we could not identify", OTHER: "other instruments",
  };

  function vennSvg(pct) {
    const r = 58, d = vennGap(pct / 100) * r;
    const w = 2 * r + d + 8, h = 2 * r + 8;
    const cy = h / 2, ax = (w - d) / 2, bx = ax + d;
    return `<svg class="fc-ov-venn" viewBox="0 0 ${w} ${h}"
        width="${Math.round(w)}" height="${Math.round(h)}"
        role="img" aria-label="${pct}% of these two funds is the same companies">
      <defs><clipPath id="fcOvClip"><circle cx="${ax}" cy="${cy}" r="${r}"/></clipPath></defs>
      <circle data-region="only_a" cx="${ax}" cy="${cy}" r="${r}"
        fill="var(--ov-a)" fill-opacity=".78" stroke="var(--card)" stroke-width="2"/>
      <circle data-region="only_b" cx="${bx}" cy="${cy}" r="${r}"
        fill="var(--ov-b)" fill-opacity=".78" stroke="var(--card)" stroke-width="2"/>
      <circle data-region="both" cx="${bx}" cy="${cy}" r="${r}"
        clip-path="url(#fcOvClip)" fill="var(--ov-both)" fill-opacity=".92"
        stroke="var(--card)" stroke-width="2"/>
    </svg>`;
  }

  function missingLine(side) {
    if (!side.missing || !side.missing.length) return "";
    const parts = side.missing
      .filter(m => m.pct >= 0.05)
      .map(m => m.pct.toFixed(1) + "% " + (KIND[m.kind] || m.kind.toLowerCase()));
    if (!parts.length) return "";
    return "<b>" + esc(side.name) + "</b> is " + side.equity_pct.toFixed(1) +
      "% priced shares here. The rest -- " +
      parts.join(", ").replace(/, ([^,]*)$/, " and $1") +
      " -- is not in these lists, so it can be in neither column.";
  }

  /* The Venn on its own, for pages that already know the number and just
     need the picture -- the plan page's overlap finding, for one. Kept
     here rather than copied there: the circle spacing is solved so the
     LENS AREA is the real share, and a second implementation would drift
     from this one silently. */
  window.fcVenn = function (pct, labelA, labelB) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    return `<div class="fc-venn-wrap">
      ${vennSvg(p)}
      <div class="fc-venn-key">
        <span><i style="background:var(--ov-a)"></i>${esc(labelA || "First fund")}</span>
        <span><i style="background:var(--ov-both)"></i><b>${p.toFixed(0)}% shared</b></span>
        <span><i style="background:var(--ov-b)"></i>${esc(labelB || "Second fund")}</span>
      </div></div>`;
  };

  window.fcOverlap = async function (host, codeA, codeB) {
    if (!host) return;
    host.innerHTML = '<p class="fc-ov-sub">Reading both portfolios&hellip;</p>';

    let d;
    try {
      const r = await fetch("/api/portfolio/overlap", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({a: String(codeA), b: String(codeB)})});
      const t = await r.text();
      d = t ? JSON.parse(t) : null;
      if (!r.ok) throw new Error((d && d.detail)
                                 || ("server returned " + r.status));
    } catch (e) {
      host.innerHTML = '<div class="fc-ov-warn">' + esc(e.message) + '</div>';
      return;
    }

    const pct = d.overlap_pct;
    const TABS = [
      {key: "only_a", label: "Only in " + d.a.name, colour: "var(--ov-a)",
       rows: d.only_a},
      {key: "both", label: "In both funds", colour: "var(--ov-both)",
       rows: d.both},
      {key: "only_b", label: "Only in " + d.b.name, colour: "var(--ov-b)",
       rows: d.only_b},
    ];
    let active = "both";

    host.className = (host.className + " fc-ov").trim();
    host.innerHTML = `
      <div class="fc-ov-top">
        ${vennSvg(pct)}
        <div class="fc-ov-said">
          <p class="fc-ov-head">${pct.toFixed(1)}% of these two funds is the
            same companies.</p>
          <p class="fc-ov-sub">The smaller of the two weights in every shared
            company, added up. ${d.common_count} ${
              d.common_count === 1 ? "company is" : "companies are"} held by
            both; ${d.only_a.length} ${d.only_a.length === 1 ? "is" : "are"}
            only in ${esc(d.a.name)} and ${d.only_b.length} only in
            ${esc(d.b.name)}.</p>
          <p class="fc-ov-sub">Split a rupee evenly between them and about
            ${pct.toFixed(0)}p of every 100 buys a company you would already
            own through the other.</p>
        </div>
      </div>

      <div class="fc-ov-tabs" role="tablist">
        ${TABS.map(t => `<button type="button" class="fc-ov-tab" role="tab"
          data-tab="${t.key}" aria-selected="${t.key === active}">
          <span class="fc-ov-dot" style="background:${t.colour}"></span>
          ${esc(t.label)} (${t.rows.length})</button>`).join("")}
      </div>

      <div class="fc-ov-list" id="fcOvList"></div>

      <p class="fc-ov-foot" id="fcOvFoot"></p>`;

    const list = host.querySelector("#fcOvList");

    // Rendered rather than hidden. Toggling `hidden` on a class that
    // carries a display is how a "show more" on the planner shipped doing
    // nothing at all -- there is no reason to court it again here.
    function paint() {
      const t = TABS.find(x => x.key === active);
      if (!t.rows.length) {
        list.innerHTML = `<p class="fc-ov-empty">Nothing in this part
          &mdash; every company one of them holds, the other holds too.</p>`;
        return;
      }
      list.innerHTML = t.rows.map((row, i) => `<div class="fc-ov-row">
        <span class="fc-ov-n">${i + 1}.</span>
        <span class="fc-ov-nm">${esc(row.name)}</span>
        <span class="fc-ov-pc">${active === "both"
          ? row.pct_a.toFixed(2) + "% / " + row.pct_b.toFixed(2) + "%"
          : row.pct.toFixed(2) + "%"}</span>
      </div>`).join("");
    }

    function select(key) {
      if (!TABS.some(t => t.key === key)) return;
      active = key;
      host.querySelectorAll(".fc-ov-tab").forEach(b =>
        b.setAttribute("aria-selected", String(b.dataset.tab === key)));
      paint();
    }

    host.querySelectorAll(".fc-ov-tab").forEach(b =>
      b.addEventListener("click", () => select(b.dataset.tab)));
    host.querySelectorAll("[data-region]").forEach(el =>
      el.addEventListener("click", () => select(el.dataset.region)));

    const notes = [];
    notes.push(d.aligned
      ? "Both portfolios are as at " + esc(d.a.as_of) + "."
      : "<b>These are different months.</b> " + esc(d.a.name) + " is as at " +
        esc(d.a.as_of) + " and " + esc(d.b.name) + " as at " +
        esc(d.b.as_of) + ", so part of what reads as difference is a month "
        + "of trading rather than a difference in strategy.");
    [d.a, d.b].forEach(side => {
      const m = missingLine(side);
      if (m) notes.push(m);
    });
    notes.push("Weights are each fund's own percentage of net assets. " +
      "AMCs disclose monthly, so both funds have traded since.");
    host.querySelector("#fcOvFoot").innerHTML = notes.join(" ");

    paint();
  };

  async function start() {
    let who = {signed_in: false, admin: false, distributor: false};
    const [w, a] = await Promise.all([
      fetch("/api/distributor/me").then(r => r.ok ? r.json() : null)
        .catch(() => null),
      fetch("/api/auth/me").then(r => r.ok ? r.json() : null)
        .catch(() => null),
    ]);
    // Both fail closed: no answer means signed out and no extra items.
    if (w) who = w;
    if (a) AUTH = a;
    // Published so pages can decide what to draw. Fails CLOSED: no answer
    // leaves score_access false, which shows the notice rather than
    // briefly flashing paid content at someone who has not paid.
    window.fcAuthState = {
      signed_in: !!AUTH.signed_in,
      score_access: !!AUTH.score_access,
      // `subscribed` is the question for the PAID PRODUCTS (look-through,
      // screener, plan). score_access answers a different one -- may this
      // person see a score -- and is false for everybody while scores are
      // switched off. An older server that sends no `subscribed` falls
      // back to score_access, which is what it meant before the split.
      subscribed: AUTH.subscribed === undefined ? !!AUTH.score_access
                                                : !!AUTH.subscribed,
      scores_enabled: AUTH.scores_enabled !== false,
      distributor: !!who.distributor,
      ready: true,
    };
    document.dispatchEvent(new CustomEvent("fc-auth"));
    render(who, AUTH);
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", start);
  else start();
})();


/* ===================================================================
   Cloudflare Web Analytics
   ===================================================================
   FROM HERE, NOT FROM EACH PAGE'S HTML.
       This file is the one thing loaded by every page on the app --
       index, plan, overlap, portfolio, portfolios, clients, admin,
       allocation, privacy, terms. Pasting a script tag into ten files
       means ten places to forget when an eleventh page is written, and
       the eleventh page is exactly the one whose traffic you will later
       wonder about.

   WHY IT WAS NEEDED AT ALL
       The marketing site at finchaya.com was reporting and the app at
       mf.finchaya.com was not. One Web Analytics token does cover every
       subdomain of its apex, so the token was never the problem -- the
       beacon simply was not on these pages. Automatic injection is a
       Cloudflare-side rewrite of proxied HTML, and it was reaching the
       marketing pages and not this origin's. Rather than diagnose why an
       edge rewrite is selective, the snippet is served deliberately.

   PRIVACY
       No cookies, no fingerprinting, no cross-site tracking -- which is
       the reason for choosing this over Google Analytics on a platform
       heading for SEBI registration. Nothing here identifies a person.

   TO SWITCH IT ON
       Put the token from Web Analytics -> finchaya.com -> Manage site
       into TOKEN below. Left empty, this block does nothing at all:
       an empty token would otherwise post beacons that Cloudflare
       discards, which is worse than silence because the network tab
       looks like it is working.
   =================================================================== */
(function analytics() {
  // BARE TOKEN ONLY. This previously held the whole embed snippet Cloudflare
  // shows on its dashboard (the <script data-cf-beacon='{"token": "..."}'>
  // tag itself, pasted whole) instead of the token inside it, so every
  // beacon was posting that entire string as its "token" value and being
  // silently discarded -- the network tab looked fine while nothing was
  // actually being counted.
  const TOKEN = "bb2e2535f0874546bc7fb37d4547e065";
  if (!TOKEN) return;

  // Never twice. If Cloudflare's automatic injection starts working on
  // this hostname later, two beacons would double every page view and
  // the numbers would be quietly wrong rather than obviously broken.
  if (document.querySelector('script[src*="cloudflareinsights.com"]')) return;

  const add = () => {
    const s = document.createElement("script");
    s.type = "module";
    s.defer = true;
    s.src = "https://static.cloudflareinsights.com/beacon.min.js";
    // spa is left at its default (true). The funds page is a hash router
    // -- #/all, #/stocks, #/stock/INE... -- so with SPA tracking off
    // every session would report exactly one page view, whatever the
    // person actually looked at.
    s.setAttribute("data-cf-beacon", JSON.stringify({token: TOKEN}));
    document.head.appendChild(s);
  };

  // After load, not during it. Measurement must never be the thing that
  // slows the page it is measuring.
  if (document.readyState === "complete") add();
  else window.addEventListener("load", add, {once: true});
})();
