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

   THE TAB ICON. No page named one, so every tab was blank and every visit
   made a request for /favicon.ico that answered 404. Added here, once, for
   all pages, as a data: address (no extra request). A page that names its
   own icon is left alone.
   =================================================================== */
(function () {
  try {
    if (!document.querySelector('link[rel~="icon"]')) {
      const l = document.createElement("link");
      l.rel = "icon"; l.type = "image/svg+xml";
      l.href = "data:image/svg+xml," + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#0f7a56"/>' +
        '<text x="16" y="23.5" font-family="Georgia,serif" font-size="21" font-weight="700" text-anchor="middle" fill="#fff">F</text></svg>');
      document.head.appendChild(l);
    }
  } catch (e) { /* an icon is never worth an error */ }
})();

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
    // Not a page: opens the share dialog. `action` marks it, and render()
    // draws it as a link that the menu's click handler intercepts.
    {href: "#", label: "Refer a friend", need: "signed_in", action: "refer"},
    {href: "/portfolio",  label: "Look through a portfolio", need: null},
    // Same treatment as the look-through above: a paid deep-dive tool
    // somebody arrives at with a question already in mind ("which funds
    // clear a bar"), not a utility everyone opens every visit -- so it
    // sits behind the menu rather than across the top.
    {href: "/screener",   label: "Fund screener", need: null},
    // Company results as they are filed, this week and last, and who is due next. Part of the Stocks plan: the
    // door is shown to everyone (like the fund screener), and the page itself asks the server and shows a sign-in
    // or subscribe notice to anybody without the plan. /api/results refuses them either way.
    {href: "/results",    label: "Quarterly results", need: null},
    // Build your own screener (patterns, indicators, fund activity, ownership, ratios with AND / OR). Same audience and same treatment as the results.
    {href: "/screen-builder", label: "Build your own screener", need: null},
    // IPOs: upcoming, open, listed and drafts filed with SEBI, with what each offer document says. Information only, no verdict. Open to everyone
    // (MF_IPO_ACCESS defaults to everyone; if it is narrowed the page itself says so).
    {href: "/ipo", label: "IPOs", need: null},
    // Stock alerts, set from a stock page ("Set alert"); this is the list of them. Same audience as the results.
    {href: "/stock-alerts", label: "My stock alerts", need: "signed_in"},
    // Premium members and admins only (the server decides: /api/auth/me
    // returns `viewcheck`, and the endpoint refuses everyone else anyway).
    {href: "/viewcheck",  label: "Check your view", need: "viewcheck"},
    // Your own rules (profit, P/E, MACD, Dow theory ... joined with AND / OR), which Check your view then checks first. Shown to whoever the server lets in:
    // start() asks /api/tradeplan/access (administrators until MF_TRADEPLAN_ACCESS says otherwise); no answer means no item.
    {href: "/tradeplan",  label: "My trade plan", need: "tradeplan"},
    // Same audience as the item above; /api/auth/me returns `backtest`.
    {href: "/backtest",   label: "Back-test your rules", need: "backtest"},
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
    const moreLink = l => l.action
      ? '<a href="#" data-fc-action="' + l.action + '">' + l.label + '</a>'
      : '<a href="' + l.href + '"' + (here.startsWith(l.href) ? ' aria-current="page"' : '') + '>' + l.label + '</a>';
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
          '<div class="fc-menu" hidden>' + items.map(moreLink).join("") + '</div></div>' : "") +
        '<div class="fc-auth" id="fcAuth"></div>' +
        // On a phone the links and the "More" list become ONE menu, opened by this button (see the max-width:760px rules in finchaya.css)
        '<button type="button" class="fc-burger" aria-label="Open the menu" aria-expanded="false" aria-controls="fcDrawer">' +
          '<span class="bars" aria-hidden="true"><i></i><i></i><i></i></span><span class="lab">Menu</span></button>' +
      '</div>' +
      '<div class="fc-drawer" id="fcDrawer" hidden>' +
        MAIN.map(l => '<a href="' + l.href + '"' + (l.match(here) ? ' aria-current="page"' : '') + '>' + l.label + '</a>').join("") +
        (items.length ? '<div class="fc-dlabel">More</div>' + items.map(moreLink).join("") : "") +
        '<div class="fc-dacct" id="fcDrawerAcct"></div>' +
      '</div>';

    document.body.insertBefore(head, document.body.firstChild);
    paintAuth(auth);

    // The phone menu: opens from the button, shuts when a link is tapped, when the reader taps elsewhere, on Escape, and when the window grows back to desktop width.
    const burger = head.querySelector(".fc-burger"), drawer = head.querySelector(".fc-drawer");
    const setDrawer = on => {
      drawer.hidden = !on;
      head.classList.toggle("fc-open", on);
      burger.setAttribute("aria-expanded", String(on));
      burger.setAttribute("aria-label", on ? "Close the menu" : "Open the menu");
    };
    burger.addEventListener("click", e => { e.stopPropagation(); setDrawer(drawer.hidden); });
    drawer.addEventListener("click", e => {
      const a = e.target.closest("a, button");
      if (!a) return;
      const act = e.target.closest("[data-fc-action]");
      if (act) {
        e.preventDefault();
        if (act.dataset.fcAction === "refer" && window.fcRefer) window.fcRefer();
      }
      setDrawer(false);
    });
    document.addEventListener("click", e => { if (!e.target.closest(".fc-head")) setDrawer(false); });
    document.addEventListener("keydown", e => { if (e.key === "Escape") setDrawer(false); });
    window.addEventListener("resize", () => { if (window.innerWidth > 760) setDrawer(false); });

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
    menu.addEventListener("click", e => {
      const a = e.target.closest("[data-fc-action]");
      if (!a) return;
      e.preventDefault();
      shut();
      if (a.dataset.fcAction === "refer" && window.fcRefer) window.fcRefer();
    });
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
    // the same account entries, at the bottom of the phone menu
    const dacct = document.getElementById("fcDrawerAcct");
    if (dacct) {
      if (auth.signed_in) {
        const w = (auth.user && (auth.user.email || auth.user.full_name)) || "";
        dacct.innerHTML = (w ? '<div class="fc-dwho">Signed in as ' + esc(w) + '</div>' : "") +
          '<button type="button" id="fcDAcct">Subscription</button>' +
          '<button type="button" id="fcDSignOut">Sign out</button>';
        document.getElementById("fcDAcct").addEventListener("click", () => window.fcAccount());
        document.getElementById("fcDSignOut").addEventListener("click", signOut);
      } else {
        dacct.innerHTML = '<button type="button" id="fcDSignIn" class="fc-dprimary">Sign in</button>';
        document.getElementById("fcDSignIn").addEventListener("click", () => openAuth());
      }
    }
    if (auth.signed_in) {
      const who = (auth.user && (auth.user.email || auth.user.full_name)) || "";
      // somebody on the free trial sees how long is left, and it opens the plans
      const tri = window.fcAuthState && window.fcAuthState.trial;
      const pill = tri && tri.active
        ? '<button type="button" class="fc-authb fc-trialpill" id="fcTrialPill">Trial: ' + tri.active.days_left + ' day' + (tri.active.days_left === 1 ? "" : "s") + ' left</button>' : "";
      slot.innerHTML = '<span class="fc-who" title="' + esc(who) + '">' +
        esc(who) + '</span>' + pill +
        '<button type="button" class="fc-authb quiet" id="fcAcctBtn">Subscription</button>' +
        '<button type="button" class="fc-authb quiet" id="fcSignOut">Sign out</button>';
      document.getElementById("fcSignOut").addEventListener("click", signOut);
      document.getElementById("fcAcctBtn").addEventListener("click",
        () => window.fcAccount());
      const tp = document.getElementById("fcTrialPill");
      if (tp) tp.addEventListener("click", () => window.fcAccount("pay"));
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
                        subscribed: false, mf: false, stocks: false, scope: null,
                        scores_enabled: true, scores_on: false, ready: false,
                        trial: null};         // the free trial: {available, active:{scope,title,ends_on,days_left}, ended, used, days, reason} (see trial.py)

  // The three tiers, in the order they are offered. `area` on a feature row
  // below says which tier unlocks it; "all" always does.
  const FC_SCOPE_TITLE = {mf: "Mutual funds", stocks: "Stocks",
                          all: "Mutual funds + stocks"};
  const FC_PLAN_WORD = {mf: "the Mutual funds plan", stocks: "the Stocks plan"};

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

  async function waitForAccess(say, subId) {
    say("Payment received. Confirming with the bank…");
    for (let i = 0; i < 30; i++) {            // about 90 seconds
      await new Promise(r => setTimeout(r, 3000));
      try {
        const s = await fetch("/api/pay/status").then(r => r.json());
        // Wait for THIS subscription to be the live one, not for "premium"
        // in general: somebody changing plan is premium already, and the
        // page would otherwise reload before the new tier was switched on.
        const sub = s.subscription;
        const mine = !subId || (sub && sub.id === subId
          && ["active", "authenticated"].includes(sub.status));
        if (s.premium && mine) { location.reload(); return; }
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
        + encodeURIComponent(plan || "all_yearly"), {method: "POST"});
      if (r.status === 401) { say(""); window.fcSignIn && window.fcSignIn(); return; }
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "Could not start the payment.");

      const rz = new window.Razorpay({
        key: d.key_id,
        subscription_id: d.subscription_id,
        name: "FinChaya",
        description: "FinChaya " + (FC_SCOPE_TITLE[d.scope] || "subscription"),
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
          waitForAccess(say, d.subscription_id);
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
  let payBoxSeq = 0;
  async function billingCfg() {
    if (payCfg === null)
      payCfg = await fetch("/api/pay/config").then(r => r.json())
        .catch(() => ({enabled: false, plans: []}));
    return payCfg;
  }

  /* ---- what a subscription includes ----------------------------------
     ONE comparison table, Free against Subscriber, drawn above the price
     wherever the price is shown (the "My subscription" dialog, the locked
     panels, the "Subscription needed" dialog). It is never folded away:
     what a person gets is the reason to pay, so it is the first thing seen.

     A row with `free: false` is paid-only, and each one matches a gate the
     server really enforces -- portfolio_api._require_subscription,
     screener_api /run, selector /overlap and /review, api.py stock holders
     -- so adding a gate means adding a row here, and nothing is promised
     that the server does not withhold. A row with `free: true` is open to
     everyone and is there so the table is an honest comparison, not just a
     list of things behind the paywall.

     `scores: true` marks the rows about scores and the rising split. Those
     are withdrawn for most people (MF_SCORES_ENABLED off), and advertising
     a withdrawn product beside a price would be selling it, so they are
     drawn only for an account the server says has scores on. */
  /* `area` is the tier that unlocks the row: "mf", "stocks", "any" (either
     paid tier) or "free". Each matches a gate the server really enforces
     with that same area word -- portfolio_api._require_subscription(request,
     "mf"), screener_api /run ("stocks"), api.py stock holders ("stocks") --
     so nothing is promised that the server does not withhold. */
  const FC_FEATURES = [
    {t: "Portfolio look-through", area: "mf",
     d: "Every company you really own across all your funds, and where "
      + "funds repeat each other."},
    {t: "Fund screener", area: "mf",
     d: "Filter every fund on real return, risk and rolling-window history."},
    {t: "Plan builder", area: "mf",
     d: "Turn your goal into a shortlist of funds, with results."},
    {t: "Overlap and portfolio review", area: "mf",
     d: "How much a set of funds overlaps, and a review of the whole mix."},
    {t: "Stock screens", area: "stocks",
     d: "Funds buying or leaving, RSI, MACD and crossover signals, chart "
      + "patterns, ownership changes, upcoming results and corporate actions."},
    {t: "Which funds own a stock", area: "stocks",
     d: "The names of the funds holding a stock, and how big each position "
      + "is."},
    {t: "What changed", area: "stocks",
     d: "Which funds added to, trimmed or exited a stock since last month."},
    // Opened to the Stocks tier on 11 Oct 2026 (stock_access.py for the first two; backtest_api.py's default access is "premium").
    // Each row matches a gate the server really enforces.
    {t: "Quarterly results", area: "stocks",
     d: "Who has reported and who is due: sales, profit and EPS against the "
      + "quarter before and a year ago, with P/E and PEG."},
    {t: "Stock alerts", area: "stocks",
     d: "One email when a stock reaches a price, closes above a recent high, "
      + "comes down to a moving average, changes trend, triggers an indicator "
      + "or reports results."},
    {t: "Back-test your rules", area: "stocks",
     d: "Replay your own entry and exit rules over up to ten years of daily "
      + "prices and see what they would have done."},
    {t: "Build your own screener", area: "stocks",
     d: "Combine chart patterns, indicators, fund buying, ownership and "
      + "ratios with and / or, and save the screens you use."},
    // "Check your view" is deliberately NOT listed: it is still being
    // finished and stays admin-only until released.
    {t: "Fund and stock scores and ranks", area: "any", scores: true,
     d: "The score, the rank, and what drives them."},
    {t: "Rising, sideways or falling", area: "any", scores: true,
     d: "How much of a fund or portfolio is in stocks that are rising, "
      + "sideways or falling."},
    {t: "Fund and stock pages", area: "free",
     d: "Returns, holdings, risk numbers and category comparison."},
    {t: "Goal planner", area: "free",
     d: "Work out what your goal needs."},
    {t: "Saved portfolios", area: "free",
     d: "Free once you sign in."},
  ];
  // Does tier `scope` include a row with this area?
  const fcIncludes = (area, scope) =>
    area === "free" || scope === "all" || area === "any" || area === scope;

  // Drawn, not typed, so the ticks and crosses look the same on every device
  // and do not depend on a font having the glyph. The words are for screen
  // readers, which would otherwise read a column of unlabelled pictures.
  const FC_TICK = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d='
    + '"M4 10.5l4 4 8-9" fill="none" stroke="currentColor" stroke-width="3" '
    + 'stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const FC_CROSS = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d='
    + '"M5 5l10 10M15 5L5 15" fill="none" stroke="currentColor" '
    + 'stroke-width="3" stroke-linecap="round"/></svg>';

  /* The comparison is built from divs with table ROLES rather than a real
     <table>, on purpose. A real table needs fixed columns for the ticks, and
     in a narrow box (a phone, or a locked panel inside a page that is already
     padded) those columns left the feature name a few pixels wide, so it
     broke one letter per line. This layout starts STACKED -- the name and
     its description across the full width, a "Free" chip and a "Subscriber"
     chip underneath -- which works at any width, and only becomes side-by-side
     columns when the box itself is wide enough (a container query on the
     wrapper, not the screen, because the box can be narrow on a wide screen).
     A browser without container queries simply keeps the stacked version. */
  function featureTableHtml() {
    const showScores = !!(window.fcAuthState && window.fcAuthState.scores_on);
    const cell = (lbl, yes) => `
        <div class="fc-cmp-cell ${yes ? "yes" : "no"}" role="cell">
          ${yes ? FC_TICK : FC_CROSS}<span class="lbl">${lbl}</span>
          <span class="fc-vh">${yes ? "included" : "not included"}</span>
        </div>`;
    const rows = FC_FEATURES.filter(f => !f.scores || showScores).map((f, i) => `
      <div class="fc-cmp-row${i % 2 ? " alt" : ""}" role="row">
        <div class="fc-cmp-what" role="rowheader">
          <b>${esc(f.t)}</b><small>${esc(f.d)}</small>
        </div>
        <div class="fc-cmp-cells" role="presentation">
          ${cell("Free", f.area === "free")}${cell("Funds", fcIncludes(f.area, "mf"))}${cell("Stocks", fcIncludes(f.area, "stocks"))}${cell("Both", true)}
        </div>
      </div>`).join("");
    return `<div class="fc-cmp-wrap">
      <div class="fc-cmp" role="table" aria-label="What each plan includes">
        <div class="fc-cmp-title">What each plan includes</div>
        <div class="fc-cmp-head" role="row">
          <span role="columnheader" class="fc-vh">Feature</span>
          <span role="columnheader" class="h-free">Free</span>
          <span role="columnheader" class="h-mf">Funds</span>
          <span role="columnheader" class="h-stocks">Stocks</span>
          <span role="columnheader" class="h-sub">Both</span>
        </div>${rows}
      </div>
    </div>`;
  }

  // Styles for the comparison. Mobile-first: the stacked layout is the
  // default, and the columns arrive in the @container block. Everything is
  // scoped under .fc-cmp-wrap and sets its own font, case and spacing,
  // because the pages and the sign-in dialog style bare label/th/td/input and
  // would otherwise reach in.
  const FC_CMP_CSS = `
    .fc-cmp-wrap{container-type:inline-size;width:100%;max-width:680px;
      flex:1 1 100%;min-width:0;margin:12px 0 4px;text-align:left;color:#1b2b24;
      font-family:inherit;font-size:14px;letter-spacing:normal;
      text-transform:none;font-weight:400}
    .fc-cmp-wrap *{box-sizing:border-box}
    .fc-cmp-title{font-weight:700;font-size:16px;line-height:1.3;
      margin:0 0 8px}
    .fc-cmp-head{position:absolute;width:1px;height:1px;overflow:hidden;
      clip:rect(0 0 0 0)}
    .fc-cmp-row{padding:10px 8px 10px;line-height:1.3;min-width:0}
    .fc-cmp-row.alt{background:#f4f7f5}
    .fc-cmp-row:last-child{border-bottom:2px solid #0f7a56}
    .fc-cmp-what{min-width:0}
    .fc-cmp-what b{display:block;font-size:14px;font-weight:700;
      overflow-wrap:break-word}
    .fc-cmp-what small{display:block;margin-top:2px;font-size:12px;
      font-weight:400;opacity:.7;overflow-wrap:break-word}
    .fc-cmp-cells{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
    .fc-cmp-cell{display:inline-flex;align-items:center;gap:5px;
      padding:3px 10px 3px 7px;border-radius:999px;font-size:12px;
      font-weight:700}
    .fc-cmp-cell svg{width:13px;height:13px;flex:none}
    .fc-cmp-cell.yes{background:#e3f3ec;color:#1f7a45}
    .fc-cmp-cell.no{background:#fbe9e6;color:#c23a2b}
    .fc-vh{position:absolute;width:1px;height:1px;overflow:hidden;
      clip:rect(0 0 0 0);white-space:nowrap}

    /* Wide enough for columns: the Free / Funds / Stocks / Both tabs on
       top, one tick or cross per cell underneath, the labels left to the
       tabs. */
    @container (min-width:420px){
      .fc-cmp-head{position:static;width:auto;height:auto;overflow:visible;
        clip:auto;display:grid;
        grid-template-columns:minmax(0,1fr) 54px 62px 66px 60px;
        align-items:end;column-gap:4px}
      .fc-cmp-head .h-free,.fc-cmp-head .h-mf,.fc-cmp-head .h-stocks,
      .fc-cmp-head .h-sub{display:block;
        padding:9px 4px 8px;text-align:center;color:#fff;font-size:11px;
        font-weight:800;letter-spacing:.06em;text-transform:uppercase;
        border-radius:10px 10px 16px 16px;white-space:nowrap}
      .fc-cmp-head .h-free{background:#6b7c76;grid-column:2}
      .fc-cmp-head .h-mf{background:#2f7d9a;grid-column:3}
      .fc-cmp-head .h-stocks{background:#8a5a12;grid-column:4}
      .fc-cmp-head .h-sub{background:#0f7a56;grid-column:5}
      .fc-cmp-row{display:grid;
        grid-template-columns:minmax(0,1fr) 54px 62px 66px 60px;
        align-items:center;column-gap:4px}
      .fc-cmp-cells{display:contents}
      .fc-cmp-cell{justify-content:center;background:none !important;
        padding:6px 0;border-radius:0}
      .fc-cmp-cell .lbl{position:absolute;width:1px;height:1px;
        overflow:hidden;clip:rect(0 0 0 0)}
      .fc-cmp-cell svg{width:18px;height:18px}
      .fc-cmp-cell.yes{color:#2f9e4f}
      .fc-cmp-cell.no{color:#d64533}
    }`;

  // Fills `host` with the plan comparison, a Monthly / Yearly switch, one
  // card per tier (Mutual funds, Stocks, Both) and a Subscribe button.
  // `prefer` is the tier to pre-select -- the side of the site that was
  // locked -- else "all". An account that already has a plan is shown the
  // OTHER tiers, with a line on what changing plan does. Resolves false,
  // drawing nothing, when billing is off.
  window.fcPayBox = async function (host, prefer) {
    const cfg = await billingCfg();
    if (!host) return false;
    // The free trial (trial.py): offered above the plans to a signed-in account that may start one; a running or finished one is described there too.
    const tr = window.fcAuthState.trial;
    const trialOn = !!(tr && tr.available && window.fcAuthState.signed_in);
    if (!cfg.enabled && !trialOn) return false;
    // `tiers` from this server; rebuilt from the flat list for an older one.
    let tiers = cfg.enabled ? cfg.tiers : [];
    if (cfg.enabled && !tiers) {
      const by = {};
      (cfg.plans || []).forEach(p => {
        const sc = p.scope || "mf";
        (by[sc] = by[sc] || {scope: sc, title: FC_SCOPE_TITLE[sc], plans: []})
          .plans.push(Object.assign({cadence: p.cadence || p.plan}, p));
      });
      tiers = Object.values(by);
    }
    // the tier already paid for -- but a TRIAL is not paid for: its tier is still on offer, so the person can keep it
    const have = (tr && tr.active) ? null : window.fcAuthState.scope;
    tiers = tiers.filter(t => t.plans.length && t.scope !== have);
    if (!tiers.length && !trialOn) return false;

    if (!document.getElementById("fcPlanCss")) {
      const st = document.createElement("style");
      st.id = "fcPlanCss";
      st.textContent = ".fc-plans{display:flex;flex-direction:column;gap:8px;"
        + "margin:10px 0;width:100%;max-width:680px}"
        // `.fc-plans .fc-plan` (not bare `.fc-plan`) so it outranks the
        // sign-in dialog's own `.modal-card label` and `.modal-card input`,
        // which would otherwise stack the radio above the price in small
        // capitals and stretch the radio to the full width.
        + ".fc-plans .fc-plan{display:flex;gap:10px;margin:0;"
        + "align-items:flex-start;padding:10px 12px;border:1.5px solid #cfd8d3;"
        + "border-radius:10px;cursor:pointer;text-align:left;font-size:15px;"
        + "font-weight:400;letter-spacing:normal;text-transform:none;"
        + "color:#1b2b24}"
        + ".fc-plans .fc-plan input{width:auto;flex:none;margin:3px 0 0;"
        + "padding:0;border:0;background:none}"
        + ".fc-plans .fc-plan:has(input:checked){border-color:#0f7a56;"
        + "background:#e3f3ec}"
        + ".fc-plans .fc-plan small{display:block;font-size:13px;opacity:.75}"
        + ".fc-plans .fc-plan .fc-price{display:block;font-weight:700}"
        + ".fc-plans .fc-plan.off{opacity:.5;cursor:default}"
        + ".fc-cad{display:inline-flex;border:1.5px solid #cfd8d3;"
        + "border-radius:999px;overflow:hidden;margin:12px 0 0}"
        + ".fc-cad label{margin:0;padding:6px 16px;cursor:pointer;font-size:14px;"
        + "font-weight:600;letter-spacing:normal;text-transform:none;"
        + "color:#1b2b24;display:inline-flex;align-items:center}"
        + ".fc-cad label input{position:absolute;opacity:0;width:0;height:0;"
        + "margin:0}"
        + ".fc-cad label:has(input:checked){background:#0f7a56;color:#fff}"
        + ".fc-switch-note{font-size:13px;opacity:.8;margin:8px 0 0;"
        + "max-width:680px;text-align:left}"
        + ".fc-linkb{background:none;border:0;padding:0;color:#0f7a56;"
        + "text-decoration:underline;cursor:pointer;font:inherit}"
        // the free-trial block above the plans
        + ".fc-trial{border:1.5px solid #0f7a56;background:#e8f5ef;border-radius:12px;"
        + "padding:12px 14px;margin:10px 0;max-width:680px;text-align:left;color:#1b2b24}"
        + ".fc-trial b{font-size:16px}.fc-trial p{margin:6px 0 8px;font-size:14px;line-height:1.45}"
        + ".fc-trial-pick{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 10px}"
        + ".fc-trial-pick label{display:inline-flex;align-items:center;gap:6px;margin:0;"
        + "padding:6px 12px;border:1.5px solid #cfd8d3;border-radius:999px;background:#fff;"
        + "cursor:pointer;font-size:14px;font-weight:600;letter-spacing:normal;text-transform:none;color:#1b2b24}"
        + ".fc-trial-pick label input{width:auto;margin:0;padding:0;border:0;background:none}"
        + ".fc-trial-pick label:has(input:checked){border-color:#0f7a56;background:#0f7a56;color:#fff}"
        + FC_CMP_CSS;
      document.head.appendChild(st);
    }

    // Radio names unique to THIS box: a lock on the page and the account
    // dialog can both be open, and radios sharing a name across the
    // document form one group, so checking one would uncheck the other's.
    const uid = "fcPay" + (++payBoxSeq);
    const nTier = uid + "Tier", nCad = uid + "Cad", nTrial = uid + "Trial";
    const planFor = (t, cad) => t.plans.find(p => p.cadence === cad);
    const anyYearly = tiers.some(t => planFor(t, "yearly"));
    const anyMonthly = tiers.some(t => planFor(t, "monthly"));
    let cadence = anyYearly ? "yearly" : "monthly";
    // Pre-select: the side that was locked; after that, everything.
    let pick = (prefer && tiers.some(t => t.scope === prefer)) ? prefer
      : tiers.some(t => t.scope === "all") ? "all" : (tiers[0] ? tiers[0].scope : "all");
    let trialPick = ["mf", "stocks", "all"].includes(prefer) ? prefer : "all";

    // What the trial block says: an offer (no card, no payment), or where a running / finished one stands.
    const trialBlock = () => {
      if (tr && tr.active) {
        return `<div class="fc-trial"><b>You are on a free trial of ${esc(tr.active.title)}</b>
          <p>It runs to the end of <b>${esc(fmtDay(tr.active.ends_on))}</b> (${tr.active.days_left} day${tr.active.days_left === 1 ? "" : "s"} left).
          Subscribe below to keep it, or upgrade to a bigger plan; nothing is charged until you do.</p></div>`;
      }
      if (tr && tr.ended && !window.fcAuthState.subscribed) {
        return `<div class="fc-trial"><b>Your free trial ended on ${esc(fmtDay(tr.ended.ends_on))}</b>
          <p>You are welcome back at any point: subscribe whenever you like.</p></div>`;
      }
      if (!trialOn) return "";
      const days = tr.days || 7;
      return `<div class="fc-trial"><b>Try it free for ${days} days</b>
        <p>No card and no payment. Pick what to try; we email you on day ${days - 1} before it ends, and after that it is up to you.</p>
        <div class="fc-trial-pick" role="radiogroup" aria-label="What to try">${["mf", "stocks", "all"].map(sc => `
          <label><input type="radio" name="${nTrial}" value="${sc}" ${sc === trialPick ? "checked" : ""}>${esc(FC_SCOPE_TITLE[sc])}</label>`).join("")}</div>
        <button type="button" class="fc-authb" id="fcTrialGo">Start my ${days}-day free trial</button>
        <span class="sub" id="fcTrialMsg"></span></div>`;
    };
    const wireTrial = () => {
      host.querySelectorAll(`input[name="${nTrial}"]`).forEach(r => r.addEventListener("change", () => { trialPick = r.value; }));
      const go = host.querySelector("#fcTrialGo");
      if (!go) return;
      go.addEventListener("click", async () => {
        const msg = host.querySelector("#fcTrialMsg");
        const sel = host.querySelector(`input[name="${nTrial}"]:checked`);
        go.disabled = true;
        msg.style.color = "";
        msg.textContent = "Starting your trial…";
        try {
          const r = await fetch("/api/pay/trial", {method: "POST", headers: {"Content-Type": "application/json"},
                                                   body: JSON.stringify({scope: sel ? sel.value : trialPick})});
          const d = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(d.detail || "The trial could not be started.");
          msg.textContent = d.message || "Your free trial has started.";
          setTimeout(() => location.reload(), 1400);          // the page reloads with the plan switched on
        } catch (e) {
          go.disabled = false;
          msg.style.color = "#b3261e";
          msg.textContent = e.message || "The trial could not be started.";
        }
      });
    };

    const switchNote = have
      ? `<p class="fc-switch-note">You are on the <b>${esc(FC_SCOPE_TITLE[have]
          || have)}</b> plan. Choosing another plan starts it today; your
          current plan is cancelled as soon as the first payment of the new
          one goes through, and the days left on it are not refunded.</p>`
      : "";

    const draw = () => {
      if (!tiers.length) { host.innerHTML = trialBlock(); wireTrial(); return; }     // billing not open: the trial alone
      host.innerHTML = trialBlock() + featureTableHtml() + switchNote
        + ((anyYearly && anyMonthly) ? `
      <div class="fc-cad" role="radiogroup" aria-label="Billing period">
        <label><input type="radio" name="${nCad}" value="yearly"
               ${cadence === "yearly" ? "checked" : ""}>Yearly</label>
        <label><input type="radio" name="${nCad}" value="monthly"
               ${cadence === "monthly" ? "checked" : ""}>Monthly</label>
      </div>` : "")
        + '<div class="fc-plans">' + tiers.map(t => {
          const p = planFor(t, cadence);
          if (!p) return `<label class="fc-plan off">
            <input type="radio" name="${nTier}" value="${esc(t.scope)}" disabled>
            <span><b>${esc(t.title)}</b>
              <small>Not offered ${esc(cadence)}.</small></span></label>`;
          return `
      <label class="fc-plan">
        <input type="radio" name="${nTier}" value="${esc(t.scope)}"
               ${t.scope === pick ? "checked" : ""}>
        <span><b>${esc(t.title)}</b>
          <span class="fc-price">${esc(p.price)}</span>
          <small>${esc(t.blurb || "")}${t.blurb && p.note ? " · " : ""}${esc(p.note || "")}</small></span>
      </label>`;
        }).join("") + `</div>
      <button type="button" class="fc-authb" id="fcPayGo">${have ? "Change plan" : "Subscribe"}</button>
      <span class="sub" id="fcPayMsg"></span>
      <p class="fc-switch-note">Need the invoice in a business name, with a GSTIN?
        <button type="button" class="fc-linkb" id="fcBillOpen">Add your billing
        details</button> before paying; they go on every invoice from then on.</p>`;
      host.querySelector("#fcBillOpen").addEventListener("click", () =>
        window.fcAccount && window.fcAccount("billing"));
      host.querySelectorAll(`input[name="${nCad}"]`).forEach(r =>
        r.addEventListener("change", () => { cadence = r.value; draw(); }));
      host.querySelectorAll(`input[name="${nTier}"]`).forEach(r =>
        r.addEventListener("change", () => { pick = r.value; }));
      host.querySelector("#fcPayGo").addEventListener("click", () => {
        const sel = host.querySelector(`input[name="${nTier}"]:checked`);
        const t = tiers.find(x => x.scope === (sel ? sel.value : pick));
        const p = t && planFor(t, cadence);
        if (!p) return;
        window.fcSubscribe(host.querySelector("#fcPayMsg"), p.plan);
      });
      wireTrial();
    };
    draw();
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

  /* fcAccount(section): `section` = "billing" opens the dialog with the
     billing-details form expanded (the pay box links here). */
  window.fcAccount = async function (section, prefer) {
    if (!document.getElementById("fcAcctCss")) {
      const st = document.createElement("style");
      st.id = "fcAcctCss";
      st.textContent = ".fc-acct{position:fixed;inset:0;z-index:9999;"
        + "background:rgba(0,0,0,.45);display:flex;align-items:center;"
        + "justify-content:center;padding:16px}"
        + ".fc-acct-box{background:#fff;color:#1b2b24;border-radius:14px;"
        + "max-width:560px;width:100%;padding:22px;max-height:90vh;"
        + "overflow:auto;box-shadow:0 10px 40px rgba(0,0,0,.25)}"
        + ".fc-acct-box h3{margin:0 0 10px;font-size:19px}"
        + ".fc-acct-box p{margin:0 0 12px;font-size:15px;line-height:1.45}"
        + ".fc-acct-row{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}"
        + ".fc-acct-note{font-size:13px;opacity:.7}"
        + ".fc-acct-sec{margin-top:18px;border-top:1px solid #e3e8e5;padding-top:12px}"
        + ".fc-acct-sec summary{cursor:pointer;font-weight:700;font-size:15px;"
        + "list-style:none;display:flex;align-items:center;gap:8px}"
        + ".fc-acct-sec summary::before{content:'\\25B8';font-size:12px;opacity:.6}"
        + ".fc-acct-sec[open] summary::before{content:'\\25BE'}"
        + ".fc-acct-sec h4{margin:0 0 8px;font-size:15px}"
        + ".fc-bill{display:grid;gap:8px;margin-top:10px;max-width:460px}"
        + ".fc-bill label{display:block;font-size:13px;font-weight:600;"
        + "letter-spacing:normal;text-transform:none;color:#51615a}"
        + ".fc-bill input,.fc-bill select{width:100%;box-sizing:border-box;"
        + "font:inherit;font-size:15px;padding:8px 10px;border:1.5px solid #cfd8d3;"
        + "border-radius:8px;background:#fff;color:#1b2b24}"
        + ".fc-inv{width:100%;border-collapse:collapse;font-size:14px}"
        + ".fc-inv td{padding:6px 8px 6px 0;border-bottom:1px solid #eef1ef;"
        + "vertical-align:top}.fc-inv td.n{text-align:right;white-space:nowrap}"
        + ".fc-inv a{color:#0f7a56}";
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
    const planName = (sub && (sub.plan_label || sub.plan)) || "";
    const scopeWord = s.scope ? FC_SCOPE_TITLE[s.scope] || s.scope : "";
    const until = fmtDay((sub && sub.current_end) || s.premium_until);
    // The free trial, asked afresh: the account may have just started or finished one.
    const tri = await fetch("/api/pay/trial").then(r => r.ok ? r.json() : null).catch(() => null);
    if (tri) window.fcAuthState.trial = tri.enabled === false && !tri.active && !tri.ended ? null : tri;
    const onTrial = !!(tri && tri.active && !live);
    // Keep the state in step for the pay box, which hides the tier held (a trial's tier is not held: it can be subscribed to).
    if (s.premium && s.scope && !onTrial) window.fcAuthState.scope = s.scope;
    let html;

    if (live && !sub.cancel_requested) {
      html = "<p>Your <b>" + esc(planName || scopeWord || "FinChaya") + "</b> plan is "
        + "active" + (until ? " and renews on <b>" + esc(until) + "</b>"
        : "") + ".</p>"
        + '<div class="fc-acct-row">'
        + '<button type="button" class="fc-authb quiet" id="fcAcctChange">'
        + "Change plan</button>"
        + '<button type="button" class="fc-authb quiet" id="fcAcctCancel">'
        + "Cancel subscription</button>"
        + '<button type="button" class="fc-authb quiet" id="fcAcctClose">'
        + "Close</button></div>"
        + '<div id="fcAcctPay" hidden></div>';
    } else if (sub && sub.cancel_requested && s.premium) {
      html = "<p>Your <b>" + esc(planName || scopeWord) + "</b> subscription is "
        + "<b>cancelled</b>. You keep access until <b>"
        + esc(until || "the end of the period you paid for")
        + "</b>, and you will not be charged again.</p>" + closeBtn;
    } else if (onTrial) {
      // the pay box below says where the trial stands (its first block) and offers the plans
      html = '<div id="fcAcctPay"></div>' + closeBtn;
    } else if (s.premium) {
      html = "<p>Your account has <b>" + esc(scopeWord || "full") + "</b> access"
        + (s.premium_until ? " until <b>" + esc(fmtDay(s.premium_until))
          + "</b>" : " with no end date") + ".</p>" + closeBtn;
    } else {
      html = "<p>You do not have an active subscription.</p>"
        + '<div id="fcAcctPay"></div>' + closeBtn;
    }
    body.innerHTML = html;
    wireClose();

    // Billing details and invoices sit under the plan, above Close.
    const extra = document.createElement("div");
    extra.id = "fcAcctExtra";
    body.appendChild(extra);
    const closeRow = (body.querySelector("#fcAcctClose") || {}).parentElement;
    if (closeRow) body.appendChild(closeRow);
    accountExtras(extra, section === "billing");

    const pay = body.querySelector("#fcAcctPay");
    if (pay && !pay.hidden) window.fcPayBox(pay, prefer).then(drawn => {
      if (!drawn) pay.innerHTML = '<p class="fc-acct-note">Billing is not '
        + 'open yet. Email finchaya2026@gmail.com and we will switch it '
        + "on.</p>";
    });

    const change = body.querySelector("#fcAcctChange");
    if (change) change.addEventListener("click", () => {
      change.hidden = true;
      pay.hidden = false;
      window.fcPayBox(pay, "all").then(drawn => {
        if (!drawn) pay.innerHTML = '<p class="fc-acct-note">No other plan '
          + "is on offer right now.</p>";
      });
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

  /* ---- billing details + invoices, inside "My subscription" ----------
     The details (business name, GSTIN, address, state) are what the server
     prints on the NEXT invoice; the list is every invoice so far, each a
     link to its printable page. Both fail quietly: an older server without
     these endpoints simply shows nothing extra. */
  async function accountExtras(host, openBilling) {
    let det = null, inv = null;
    try {
      const [a, b] = await Promise.all([
        fetch("/api/pay/billing-details").then(r => r.ok ? r.json() : null),
        fetch("/api/pay/invoices").then(r => r.ok ? r.json() : null),
      ]);
      det = a; inv = b;
    } catch { /* leave both null */ }
    if (!det && !inv) return;

    let html = "";
    if (det) {
      const d = det.details || {};
      const opts = ['<option value="">Choose a state</option>']
        .concat((det.states || []).map(s => `<option value="${esc(s.code)}"`
          + `${s.code === d.state_code ? " selected" : ""}>${esc(s.name)}</option>`))
        .join("");
      html += `<details class="fc-acct-sec" id="fcBillSec"${openBilling ? " open" : ""}>
        <summary>Billing details for your invoice</summary>
        <p class="fc-acct-note">${det.tax_invoices
          ? "Every payment gets a GST tax invoice. Add a business name and GSTIN "
            + "to have it made out to the business. The state decides CGST+SGST "
            + "or IGST."
          : "Every payment gets a receipt. A business name and GSTIN are kept "
            + "here and printed on it; a GST break-up follows once FinChaya's "
            + "GST registration is in place."}</p>
        <div class="fc-bill">
          <div><label for="fcBillName">Name on the invoice</label>
            <input id="fcBillName" maxlength="160" value="${esc(d.legal_name || "")}"
                   placeholder="Business or your own name"></div>
          <div><label for="fcBillGstin">GSTIN (optional)</label>
            <input id="fcBillGstin" maxlength="15" value="${esc(d.gstin || "")}"
                   placeholder="15 characters, e.g. 29ABCDE1234F1Z5"
                   style="text-transform:uppercase"></div>
          <div><label for="fcBillAddr">Address (optional)</label>
            <input id="fcBillAddr" maxlength="400" value="${esc(d.address || "")}"
                   placeholder="One line is fine"></div>
          <div><label for="fcBillState">State</label>
            <select id="fcBillState">${opts}</select></div>
          <div class="fc-acct-row" style="margin-top:4px">
            <button type="button" class="fc-authb" id="fcBillSave">Save</button>
            <span class="sub" id="fcBillMsg"></span></div>
        </div></details>`;
    }
    if (inv) {
      const rows = (inv.invoices || []).map(i => `<tr>
          <td>${esc(fmtDay(i.date))}</td>
          <td>${esc(i.plan || "")}${i.period_start ? `<br><small style="opacity:.7">${esc(fmtDay(i.period_start))} – ${esc(fmtDay(i.period_end))}</small>` : ""}</td>
          <td class="n">${esc(i.amount)}</td>
          <td class="n"><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.kind)}<br><small>${esc(i.invoice_no)}</small></a></td>
        </tr>`).join("");
      html += `<div class="fc-acct-sec"><h4>Invoices</h4>${rows
        ? `<table class="fc-inv"><tbody>${rows}</tbody></table>
           <p class="fc-acct-note" style="margin-top:8px">Open one to print it or save it as a PDF.</p>`
        : '<p class="fc-acct-note">No invoices yet. One is made for every payment.</p>'}</div>`;
    }
    host.innerHTML = html;

    const save = host.querySelector("#fcBillSave");
    if (save) save.addEventListener("click", async () => {
      const msg = host.querySelector("#fcBillMsg");
      msg.textContent = "Saving…"; msg.style.color = "";
      try {
        const r = await fetch("/api/pay/billing-details", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            legal_name: host.querySelector("#fcBillName").value,
            gstin: host.querySelector("#fcBillGstin").value,
            address: host.querySelector("#fcBillAddr").value,
            state_code: host.querySelector("#fcBillState").value,
          }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || "Could not save.");
        msg.textContent = d.message || "Saved.";
      } catch (e) {
        msg.textContent = e.message || "Could not save."; msg.style.color = "#b3261e";
      }
    });
    if (openBilling) {
      const sec = host.querySelector("#fcBillSec");
      if (sec) sec.scrollIntoView({block: "start"});
    }
  }

  /* `area` is the side of the site that is locked: "mf" or "stocks". It
     names the plan in the heading, pre-selects that tier in the pay box,
     and -- for somebody who IS subscribed, just to the other tier -- says
     so instead of "you do not have a subscription". */
  window.fcLocked = function (host, what, detail, area) {
    if (!host) return;
    const st = window.fcAuthState;
    // The page can ask before the answer to "who is this?" has arrived (slow phone connection): drawing "Sign in" for somebody who IS signed in is wrong, so
    // wait for it and draw once it is here.
    if (!st.ready) {
      host.innerHTML = '<div class="fc-lock"><p class="sub">One moment&hellip;</p></div>';
      document.addEventListener("fc-auth", () => window.fcLocked(host, what, detail, area), {once: true});
      return;
    }
    const signedIn = st.signed_in;
    const planWord = FC_PLAN_WORD[area] || "the subscription";
    const otherPlan = signedIn && st.subscribed && st.scope && area
      && st.scope !== "all" && st.scope !== area;
    const onTrial = !!(st.trial && st.trial.active);
    host.innerHTML = `
      <div class="fc-lock">
        <h3>${esc(what || "This")} is part of ${esc(planWord)}</h3>
        <p>${detail || "This reads through to the shares every fund holds "
                     + "underneath, rather than judging a fund by its name. "
                     + "That is the part that is paid for."}</p>
        <p class="fc-lock-act">
          ${signedIn
            ? (otherPlan
              ? '<span class="sub">Your <b>' + esc(FC_SCOPE_TITLE[st.scope])
                + (onTrial ? "</b> free trial does not include this. The <b>Mutual funds + "
                           : "</b> plan does not include this. The <b>Mutual funds + ")
                + "stocks</b> plan does.</span>"
              : '<span class="sub">Your account does not have this plan '
                + 'yet. Email <a href="mailto:finchaya2026@gmail.com'
                + '?subject=FinChaya%20subscription">finchaya2026@gmail.com</a>'
                + ' and we will switch it on.</span>')
            : '<button type="button" class="fc-authb" id="fcLockIn">Sign in</button>'
              + '<span class="sub">Already subscribed? Sign in to see it.'
              + (st.trial && st.trial.days ? " New here? Sign in and start a " + st.trial.days + "-day free trial." : "") + "</span>"}
        </p>
      </div>`;
    const b = host.querySelector("#fcLockIn");
    if (b) b.addEventListener("click", () => window.fcSignIn && window.fcSignIn());

    // Signed in without this plan: offer to pay if billing is on,
    // otherwise the email line above stays as the fallback. Somebody on the
    // other tier is offered "both" first; a newcomer the tier that unlocks
    // this page.
    if (signedIn) {
      const act = host.querySelector(".fc-lock-act");
      if (act) {
        const box = document.createElement("div");
        act.appendChild(box);
        window.fcPayBox(box, otherPlan ? "all" : area).then(drawn => {
          // Billing on: the "email us" line is replaced by the plans. The
          // "your plan does not include this" line stays -- it is the
          // reason the plans are being shown.
          const line = act.querySelector(":scope > .sub");
          if (drawn && line && !otherPlan) line.remove();
        });
      }
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
          "last two month-ends are part of the Stocks plan.", "stocks");
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

  /* ---- refer a friend -----------------------------------------------
     Three parts, all here because the header is on every page:

       1. ARRIVING. A link like /plan?ref=K7M2QX9P is remembered (first one
          wins, for 30 days) and then taken out of the address bar. Taken
          out so that someone copying the address to send on does not pass
          the ORIGINAL member's code along as though it were their own.
       2. JOINING. When that person is signed in, the code is handed to the
          server once. The server decides whether it counts (a new account,
          not their own code, not already referred) and says so; whatever
          it says, the code is dropped, because a code that will never
          count should not be carried around forever. A network failure or
          "sign in first" keeps it for next time.
       3. SHARING. fcRefer() opens the dialog: the member's personal link, a
          message written for sharing (it comes from the server, so the
          website and a future app say the same thing), and the ways to send
          it. Nothing is sent by FinChaya. The member chooses who gets it.

     Stored: only the eight-character code, in this browser. */
  const REF_KEY = "fc_ref", REF_DAYS = 30;
  const REF_RE = /^[A-HJ-NP-Z2-9]{8}$/;          // the server's own rule

  function refSave(code) {
    try { localStorage.setItem(REF_KEY, JSON.stringify({code, at: Date.now()})); }
    catch (e) { /* private window: the credit is simply lost */ }
  }
  function refLoad() {
    try {
      const v = JSON.parse(localStorage.getItem(REF_KEY) || "null");
      if (!v || !REF_RE.test(v.code)) return null;
      if (Date.now() - v.at > REF_DAYS * 864e5) {
        localStorage.removeItem(REF_KEY);
        return null;
      }
      return v.code;
    } catch (e) { return null; }
  }
  function refClear() { try { localStorage.removeItem(REF_KEY); } catch (e) {} }

  function captureReferral() {
    let code = null;
    try { code = new URLSearchParams(location.search).get("ref"); }
    catch (e) { return; }
    if (code === null) return;
    code = code.trim().toUpperCase();
    if (REF_RE.test(code) && !refLoad()) refSave(code);   // first one wins
    try {
      const u = new URL(location.href);
      u.searchParams.delete("ref");
      history.replaceState(null, "", u.pathname + u.search + u.hash);
    } catch (e) { /* the address bar keeps the code; harmless */ }
  }
  captureReferral();

  async function claimReferral() {
    const code = refLoad();
    if (!code) return;
    let r;
    try {
      r = await fetch("/api/referral/claim", {
        method: "POST", credentials: "include",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({code}),
      });
    } catch (e) { return; }                       // offline: try next time
    // Not signed in yet, feature not deployed, or the server had a bad
    // moment: none of those says anything about the code, so keep it.
    if (r.status === 401 || r.status === 404 || r.status >= 500) return;
    refClear();
  }
  window.fcClaimReferral = claimReferral;

  // Copy text, with a fallback for browsers or pages where the clipboard
  // API is refused (an embedded view, an older phone browser).
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; }
    catch (e) { /* fall through */ }
    const t = document.createElement("textarea");
    t.value = text;
    t.setAttribute("readonly", "");
    t.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(t);
    t.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    t.remove();
    return ok;
  }

  const REF_ICON = {
    whatsapp: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" d="M12 3a9 9 0 0 0-7.8 13.5L3 21l4.7-1.2A9 9 0 1 0 12 3z"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M9 9.5c0 3 2.5 5.5 5.5 5.5"/></svg>',
    sms: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" d="M4 5h16v11H9l-5 4z"/></svg>',
    mail: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" d="M3 6h18v12H3zM3 7l9 7 9-7"/></svg>',
    copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" d="M8 8h12v12H8zM4 16V4h12"/></svg>',
    share: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 16V4m0 0L7 9m5-5l5 5M5 14v6h14v-6"/></svg>',
  };

  const REF_CSS = `
    .fc-ref{position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.5);
      display:flex;align-items:center;justify-content:center;padding:16px}
    .fc-ref-box{position:relative;background:#fff;color:#1b2b24;
      border-radius:14px;width:100%;max-width:560px;max-height:92vh;
      overflow:auto;padding:22px;box-shadow:0 10px 40px rgba(0,0,0,.28);
      font-family:inherit;font-size:15px;line-height:1.5;text-align:left}
    .fc-ref-box h3{margin:0 28px 6px 0;font-size:20px}
    .fc-ref-box p{margin:0 0 12px}
    .fc-ref-x{position:absolute;top:10px;right:10px;width:34px;height:34px;
      border:0;background:none;font-size:24px;line-height:1;cursor:pointer;
      color:#5a6d64;border-radius:8px}
    .fc-ref-x:hover{background:#eef3f0}
    .fc-ref-lead{color:#4d5f56}
    .fc-ref-msg{display:block;width:100%;box-sizing:border-box;min-height:210px;
      resize:vertical;padding:12px 14px;border:1.5px solid #cfd8d3;
      border-radius:10px;font:inherit;font-size:14.5px;line-height:1.5;
      color:#1b2b24;background:#fbfdfc}
    .fc-ref-msg:focus{outline:none;border-color:#0f7a56;background:#fff}
    .fc-ref-acts{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;
      margin:12px 0}
    @media (min-width:480px){.fc-ref-acts{grid-template-columns:repeat(4,1fr)}}
    .fc-ref-b{display:flex;align-items:center;justify-content:center;gap:7px;
      padding:10px 8px;border:1.5px solid #cfd8d3;border-radius:10px;
      background:#fff;color:#1b2b24;font:inherit;font-size:14px;
      font-weight:600;cursor:pointer;min-height:44px}
    .fc-ref-b:hover{border-color:#0f7a56;background:#f2f8f5}
    .fc-ref-b svg{width:18px;height:18px;flex:none}
    .fc-ref-b.wa{background:#1fa855;border-color:#1fa855;color:#fff}
    .fc-ref-b.wa:hover{background:#188a45;border-color:#188a45}
    .fc-ref-b.native{grid-column:1/-1}
    .fc-ref-link{display:flex;gap:8px;margin:0 0 12px}
    .fc-ref-link input{flex:1;min-width:0;padding:9px 11px;border:1.5px solid
      #cfd8d3;border-radius:10px;font:inherit;font-size:13.5px;color:#4d5f56;
      background:#f4f7f5}
    .fc-ref-link .fc-ref-b{flex:none;padding:9px 12px}
    .fc-ref-count{margin:6px 0 4px;font-size:15px}
    .fc-ref-count b{font-size:18px}
    .fc-ref-note{font-size:12.5px;color:#6a7d74;margin:6px 0 0}
    .fc-ref-box p.fc-ref-live{min-height:18px;font-size:13px;color:#0f7a56;
      margin:0 0 6px}
    .fc-ref-box:focus{outline:none}
    .fc-ref-err{color:#b3261e}`;

  window.fcRefer = async function () {
    if (!document.getElementById("fcReferCss")) {
      const st = document.createElement("style");
      st.id = "fcReferCss";
      st.textContent = REF_CSS;
      document.head.appendChild(st);
    }
    const old = document.getElementById("fcRefer");
    if (old) old.remove();

    const ov = document.createElement("div");
    ov.id = "fcRefer";
    ov.className = "fc-ref";
    ov.innerHTML = '<div class="fc-ref-box" role="dialog" aria-modal="true" ' +
      'aria-labelledby="fcReferT"><button type="button" class="fc-ref-x" ' +
      'aria-label="Close">&times;</button><h3 id="fcReferT">Refer a friend' +
      '</h3><div id="fcReferBody"><p>Getting your link&hellip;</p></div></div>';
    document.body.appendChild(ov);
    const shut = () => { ov.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = e => { if (e.key === "Escape") shut(); };
    document.addEventListener("keydown", onKey);
    ov.addEventListener("click", e => { if (e.target === ov) shut(); });
    ov.querySelector(".fc-ref-x").addEventListener("click", shut);
    const body = ov.querySelector("#fcReferBody");

    // Ask the server rather than trusting fcAuthState: on the home page a
    // person can sign in without the page reloading, and the flag is stale.
    let d;
    try {
      const r = await fetch("/api/referral/me", {credentials: "include"});
      if (r.status === 401) {
        body.innerHTML = '<p>Sign in to get your personal link to share.</p>' +
          '<p><button type="button" class="fc-ref-b" id="fcRefIn">Sign in' +
          '</button></p>';
        body.querySelector("#fcRefIn").addEventListener("click", () => {
          shut();
          const go = window.openAuth || window.fcSignIn;
          if (go) go();
        });
        return;
      }
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || "Could not load your link.");
      }
      d = await r.json();
    } catch (e) {
      // A TypeError is the browser failing to reach the server at all, and
      // its text ("Failed to fetch") means nothing to the person reading it.
      // Anything else is a sentence the server chose to send.
      const say = e instanceof TypeError
        ? "Could not reach FinChaya. Check your connection and try again."
        : (e.message || "Could not load your link. Try again in a moment.");
      body.innerHTML = '<p class="fc-ref-err">' + esc(say) + "</p>";
      return;
    }

    const link = d.url || (location.origin + d.path);
    const text0 = String(d.message || "{link}").split("{link}").join(link);
    body.innerHTML =
      '<p class="fc-ref-lead">Know someone planning for a goal? Send them ' +
      'FinChaya. Change the message if you like, then choose how to send it.</p>' +
      '<label for="fcRefMsg" style="position:absolute;left:-9999px">' +
      'Your message</label>' +
      '<textarea id="fcRefMsg" class="fc-ref-msg"></textarea>' +
      '<div class="fc-ref-acts">' +
        '<button type="button" class="fc-ref-b native" id="fcRefNative" ' +
          'hidden>' + REF_ICON.share + 'Share&hellip;</button>' +
        '<button type="button" class="fc-ref-b wa" id="fcRefWa">' +
          REF_ICON.whatsapp + 'WhatsApp</button>' +
        '<button type="button" class="fc-ref-b" id="fcRefSms">' +
          REF_ICON.sms + 'Messages</button>' +
        '<button type="button" class="fc-ref-b" id="fcRefMail">' +
          REF_ICON.mail + 'Email</button>' +
        '<button type="button" class="fc-ref-b" id="fcRefCopy">' +
          REF_ICON.copy + 'Copy message</button>' +
      '</div>' +
      '<p class="fc-ref-live" id="fcRefLive" role="status" ' +
        'aria-live="polite"></p>' +
      '<div class="fc-ref-link"><input id="fcRefLink" readonly ' +
        'aria-label="Your personal link" value="' + esc(link) + '">' +
        '<button type="button" class="fc-ref-b" id="fcRefCopyLink">' +
        REF_ICON.copy + 'Copy link</button></div>' +
      '<p class="fc-ref-count" id="fcRefCount"></p>' +
      '<p class="fc-ref-note">Your code is <b>' + esc(d.code) + '</b>. ' +
        'FinChaya sends nothing for you; you choose who gets this.</p>';

    const ta = body.querySelector("#fcRefMsg");
    const live = body.querySelector("#fcRefLive");
    ta.value = text0;
    const n = Number(d.referred) || 0;
    body.querySelector("#fcRefCount").innerHTML = n
      ? 'Friends who joined through your link: <b>' + n + '</b>'
      : 'No one has joined through your link yet.';

    // Whatever the member has edited, the link must still be in it: it is
    // how a friend who signs up is credited to them.
    const outgoing = () => {
      const t = ta.value.trim();
      return t.indexOf(link) === -1 ? (t ? t + "\n" : "") + link : t;
    };
    const say = (s, bad) => {
      live.textContent = s;
      live.className = "fc-ref-live" + (bad ? " fc-ref-err" : "");
      clearTimeout(say.t);
      say.t = setTimeout(() => { live.textContent = ""; }, 2600);
    };

    // sms: and mailto: are opened with a link click rather than by
    // assigning location.href, which in some browsers unloads the page
    // while the phone's app is starting.
    const openLink = url => {
      const a = document.createElement("a");
      a.href = url;
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      a.remove();
    };
    body.querySelector("#fcRefWa").addEventListener("click", () =>
      window.open("https://wa.me/?text=" + encodeURIComponent(outgoing()),
                  "_blank", "noopener"));
    body.querySelector("#fcRefSms").addEventListener("click", () =>
      openLink("sms:?&body=" + encodeURIComponent(outgoing())));
    body.querySelector("#fcRefMail").addEventListener("click", () =>
      openLink("mailto:?subject=" +
        encodeURIComponent(d.email_subject || "FinChaya") +
        "&body=" + encodeURIComponent(outgoing().replace(/\n/g, "\r\n"))));
    body.querySelector("#fcRefCopy").addEventListener("click", async () => {
      const ok = await copyText(outgoing());
      if (ok) say("Message copied.");
      else { ta.focus(); ta.select(); say("Press Ctrl+C (or hold and Copy) to copy.", true); }
    });
    body.querySelector("#fcRefCopyLink").addEventListener("click", async () => {
      const ok = await copyText(link);
      if (ok) say("Link copied.");
      else { const li = body.querySelector("#fcRefLink"); li.focus(); li.select();
             say("Press Ctrl+C (or hold and Copy) to copy.", true); }
    });

    // The phone's own share sheet, where there is one. It lists every app
    // the person actually has, which no fixed row of buttons can.
    if (navigator.share) {
      const nb = body.querySelector("#fcRefNative");
      nb.hidden = false;
      nb.addEventListener("click", async () => {
        try { await navigator.share({title: "FinChaya", text: outgoing()}); }
        catch (e) { /* closed without sharing: nothing to say */ }
      });
    }
    // Not the message box: focusing a text box on a phone raises the
    // keyboard over the very buttons the person came to press. Focus the
    // dialog itself, so keyboard and screen-reader users start inside it.
    const box = ov.querySelector(".fc-ref-box");
    box.setAttribute("tabindex", "-1");
    box.focus();
    ta.scrollTop = 0;
  };

  async function start() {
    let who = {signed_in: false, admin: false, distributor: false};
    const [w, a, tp, tri] = await Promise.all([
      fetch("/api/distributor/me").then(r => r.ok ? r.json() : null)
        .catch(() => null),
      fetch("/api/auth/me").then(r => r.ok ? r.json() : null)
        .catch(() => null),
      fetch("/api/tradeplan/access").then(r => r.ok ? r.json() : null)
        .catch(() => null),
      // the free trial: no answer (an older server) means no trial is offered
      fetch("/api/pay/trial").then(r => r.ok ? r.json() : null)
        .catch(() => null),
    ]);
    // Both fail closed: no answer means signed out and no extra items.
    if (w) who = w;
    if (a) AUTH = a;
    // Same fail-closed rule: no answer means no menu item.
    who.viewcheck = !!(a && a.viewcheck);
    who.backtest = !!(a && a.backtest);
    who.tradeplan = !!(tp && tp.allowed);
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
      // The tier: `mf` and `stocks` say which side of the site the plan
      // covers; `scope` is the plan's own name ("mf", "stocks", "all").
      // An older server that sends no tier means one plan covered all.
      mf: AUTH.subscribed_mf === undefined
        ? (AUTH.subscribed === undefined ? !!AUTH.score_access : !!AUTH.subscribed)
        : !!AUTH.subscribed_mf,
      stocks: AUTH.subscribed_stocks === undefined
        ? (AUTH.subscribed === undefined ? !!AUTH.score_access : !!AUTH.subscribed)
        : !!AUTH.subscribed_stocks,
      scope: AUTH.scope || null,
      scores_enabled: AUTH.scores_enabled !== false,
      // Stricter than scores_enabled: that one assumes "on" when the answer
      // is missing, which suits hiding a notice. This one decides whether to
      // ADVERTISE scores, so a missing answer must mean no.
      scores_on: AUTH.scores_enabled === true,
      distributor: !!who.distributor,
      viewcheck: !!AUTH.viewcheck,
      // "admin" while the tool is unreleased, "premium" once it is part of
      // the Stocks tier. Missing means unreleased.
      viewcheck_audience: AUTH.viewcheck_audience === "premium" ? "premium" : "admin",
      backtest: !!AUTH.backtest,
      tradeplan: !!who.tradeplan,
      trial: tri && tri.enabled !== false ? tri : null,
      ready: true,
    };
    document.dispatchEvent(new CustomEvent("fc-auth"));
    render(who, AUTH);
    // A friend who arrived by a referral link and has now signed in: tell
    // the server which code brought them. A no-op for everyone else.
    if (AUTH.signed_in) claimReferral();
    openSubscribeLink();
  }

  /* The link in the trial emails: <site>/?subscribe=<tier>. Signed in: open the Subscription window on the plans, the tier pre-selected, and take the
     word off the address. Not signed in: open the sign-in window; the page reloads signed in with the link still in the address, and comes back here. */
  function openSubscribeLink() {
    let sp;
    try { sp = new URLSearchParams(location.search); } catch { return; }
    if (!sp.has("subscribe")) return;
    const want = sp.get("subscribe");
    if (!AUTH.signed_in) { openAuth(); return; }
    sp.delete("subscribe");
    try { history.replaceState(null, "", location.pathname + (sp.toString() ? "?" + sp.toString() : "") + location.hash); } catch { /* fine */ }
    window.fcAccount("pay", ["mf", "stocks", "all"].includes(want) ? want : "all");
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
