/*! wmt.js — attribution for lead forms. Web Marketing Toronto.
 *
 * Drop this on every page (GTM Custom HTML tag or a <script src>). It remembers how the visitor first
 * arrived (gclid, gbraid, wbraid, fbclid, utm_*, landing page, referrer) for 90 days, keeps the latest
 * visit too, and fills those values into every form on the page so the CRM can attribute the lead:
 *
 *   - if the form already has a field named gclid / utm_source / ... (or Elementor's form_fields[gclid],
 *     WPForms' wpforms[fields][gclid], Gravity's input_gclid), that field is filled;
 *   - otherwise a hidden input with that name is added, which plugins that post "all fields" pick up.
 *
 * Also exposes window.wmt.attribution() for custom integrations (Shopify Flow, Wix Velo, fetch()).
 *
 * Beacon (needs ?c=<client>.<public key> on the script URL, as the client page shows): when a form that
 * has an email or phone field is submitted — including platform forms that post to their own backend
 * (RealtyNinja, Wix, Squarespace) — the attribution plus that email/phone is sent to the CRM, which joins
 * it to the lead when the lead arrives by any route (emailed notification, Zapier, CallRail…).
 */
(function () {
  if (window.wmt) return;
  var KEYS = ["gclid", "gbraid", "wbraid", "fbclid", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "campaign_id", "adset_id", "ad_id"];
  var STORE = "wmt_attr";
  var DAYS = 90;

  // Where to send beacons: from ?c=slug.key on this script's URL, or data-wmt="slug.key" on the tag.
  var BEACON = null;
  try {
    var me = document.currentScript, src = me && me.src ? me.src : "";
    if (!src) { var sc = document.getElementsByTagName("script"); for (var si = 0; si < sc.length; si++) if (/wmt\.js/.test(sc[si].src || "")) { me = sc[si]; src = me.src; } }
    var conf = (me && me.getAttribute("data-wmt")) || ((src.match(/[?&]c=([^&#]+)/) || [])[1] || "");
    var mm = decodeURIComponent(conf).match(/^([a-z0-9-]+)\.([0-9a-f]{8,})$/i);
    if (mm && src) BEACON = src.split("/static/")[0] + "/v1/beacon/" + mm[1] + "/" + mm[2];
  } catch (e) {}

  function params() {
    var out = {}, q = window.location.search.replace(/^\?/, "").split("&");
    for (var i = 0; i < q.length; i++) {
      if (!q[i]) continue;
      var kv = q[i].split("="), k = decodeURIComponent(kv[0]).toLowerCase(), v = kv.length > 1 ? decodeURIComponent(kv.slice(1).join("=").replace(/\+/g, " ")) : "";
      if (KEYS.indexOf(k) >= 0 && v) out[k] = v;
    }
    return out;
  }
  function cookie(name) {
    var m = document.cookie.match(new RegExp("(?:^|; )" + name.replace(/[.$?*|{}()\[\]\\\/+^]/g, "\\$&") + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }
  function read() {
    try { var raw = localStorage.getItem(STORE); if (raw) return JSON.parse(raw); } catch (e) {}
    try { var c = cookie(STORE); if (c) return JSON.parse(c); } catch (e) {}
    return null;
  }
  function write(data) {
    var s = JSON.stringify(data);
    try { localStorage.setItem(STORE, s); } catch (e) {}
    try {
      var host = location.hostname.replace(/^www\./, "");
      document.cookie = STORE + "=" + encodeURIComponent(s) + "; max-age=" + (DAYS * 86400) + "; path=/; domain=." + host + "; SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : "");
    } catch (e) {}
  }
  function fresh(ts) { return ts && (Date.now() - ts) < DAYS * 86400000; }

  // First touch is kept for 90 days; last touch updates whenever a new click id or UTM shows up.
  var now = Date.now(), p = params(), saved = read() || {};
  var hasClick = false; for (var k in p) { if (p.hasOwnProperty(k)) hasClick = true; }
  var external = document.referrer && document.referrer.indexOf(location.hostname) < 0;
  if (!fresh(saved.first_ts) || !saved.first) saved = {};
  if (!saved.first) {
    saved.first = { landing_page: location.href.split("#")[0], referrer: document.referrer || "" };
    for (k in p) if (p.hasOwnProperty(k)) saved.first[k] = p[k];
    saved.first_ts = now;
  }
  if (hasClick || external || !saved.last) {
    saved.last = { landing_page: location.href.split("#")[0], referrer: document.referrer || "" };
    for (k in p) if (p.hasOwnProperty(k)) saved.last[k] = p[k];
    saved.last_ts = now;
  }
  write(saved);

  function attribution() {
    // First touch wins for the click id and campaign (that is what the ad platforms match on);
    // the landing page is the first one too. fbc/fbp come from Meta's pixel cookies if present.
    var a = {}, f = saved.first || {}, l = saved.last || {};
    for (var i = 0; i < KEYS.length; i++) { var key = KEYS[i]; a[key] = f[key] || l[key] || ""; }
    a.landing_page = f.landing_page || l.landing_page || location.href;
    a.referrer = f.referrer || l.referrer || "";
    a.fbc = cookie("_fbc"); a.fbp = cookie("_fbp");
    a.page_url = location.href.split("#")[0];
    return a;
  }

  function fieldFor(form, key) {
    var sel = '[name="' + key + '"],[name$="[' + key + ']"],[name="input_' + key + '"],[name$="_' + key + '"],[data-wmt="' + key + '"]';
    try { return form.querySelector(sel); } catch (e) { return null; }
  }
  function fill(form) {
    if (!form || form.getAttribute("data-wmt-done") === "1") return;
    if (/\/wp-login|\/wp-admin|\/cart|\/checkout|search/i.test(form.action || "") && !form.querySelector('input[type="tel"],input[type="email"],textarea')) return;
    var a = attribution();
    for (var key in a) {
      if (!a.hasOwnProperty(key) || !a[key]) continue;
      var el = fieldFor(form, key);
      if (el) { if (!el.value) el.value = a[key]; continue; }
      var input = document.createElement("input");
      input.type = "hidden"; input.name = key; input.value = a[key]; input.setAttribute("data-wmt", key);
      form.appendChild(input);
    }
    form.setAttribute("data-wmt-done", "1");
  }
  function fillAll() { var forms = document.forms; for (var i = 0; i < forms.length; i++) fill(forms[i]); }

  // ---- Beacon: who just submitted, plus how they arrived ----
  var lastForm = null, lastSent = "", lastSentAt = 0;
  function looksLike(el, re) {
    var hay = [el.name, el.id, el.placeholder, el.getAttribute("aria-label"), el.getAttribute("autocomplete")].join(" ").toLowerCase();
    return re.test(hay);
  }
  function labelText(el) {
    try { var l = el.id && document.querySelector('label[for="' + el.id + '"]'); return l ? l.textContent.toLowerCase() : ""; } catch (e) { return ""; }
  }
  function personFrom(form) {
    var out = { email: "", phone: "", name: "" }, first = "", last = "";
    var els = form ? form.querySelectorAll("input, textarea") : [];
    for (var i = 0; i < els.length; i++) {
      var el = els[i], t = (el.type || "").toLowerCase(), v = (el.value || "").trim();
      if (!v || t === "hidden" || t === "password" || t === "checkbox" || t === "radio" || t === "submit") continue;
      var lbl = labelText(el);
      if (!out.email && (t === "email" || looksLike(el, /e-?mail/) || /e-?mail/.test(lbl)) && /@/.test(v)) { out.email = v; continue; }
      if (!out.phone && (t === "tel" || looksLike(el, /phone|tel|mobile|cell/) || /phone|mobile|cell/.test(lbl)) && /\d{3}/.test(v)) { out.phone = v; continue; }
      if (!first && (looksLike(el, /first/) || /first/.test(lbl))) { first = v; continue; }
      if (!last && (looksLike(el, /last|surname/) || /last|surname/.test(lbl))) { last = v; continue; }
      if (!out.name && (looksLike(el, /(^|[^a-z])name/) || /name/.test(lbl)) && !/@/.test(v) && v.length < 120) { out.name = v; }
    }
    if (!out.name && (first || last)) out.name = (first + " " + last).trim();
    return out;
  }
  function beacon(form) {
    if (!BEACON) return;
    var who = personFrom(form);
    if (!who.email && !who.phone) return;
    var data = attribution();
    data.email = who.email; data.phone = who.phone; data.name = who.name;
    data.form_name = (form && (form.id || form.name || form.getAttribute("data-form-name") || form.action)) || "";
    var body = JSON.stringify(data), sig = who.email + "|" + who.phone;
    if (sig === lastSent && Date.now() - lastSentAt < 10000) return;
    lastSent = sig; lastSentAt = Date.now();
    try {
      if (navigator.sendBeacon && navigator.sendBeacon(BEACON, new Blob([body], { type: "text/plain" }))) return;
    } catch (e) {}
    try { fetch(BEACON, { method: "POST", body: body, keepalive: true, mode: "no-cors", headers: { "Content-Type": "text/plain" } }); } catch (e) {}
  }
  function formOf(el) { while (el && el.tagName !== "FORM") el = el.parentNode; return el && el.tagName === "FORM" ? el : null; }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fillAll); else fillAll();
  // Forms that render late (popups, Elementor, React) and values typed before submit.
  document.addEventListener("focusin", function (e) { var f = e.target && e.target.form; if (f) { fill(f); lastForm = f; } }, true);
  document.addEventListener("submit", function (e) { if (e.target && e.target.tagName === "FORM") { fill(e.target); beacon(e.target); } }, true);
  // Platform forms that submit over XHR and never fire a submit event: catch the button press.
  document.addEventListener("click", function (e) {
    var el = e.target; while (el && el !== document && !/^(BUTTON|INPUT)$/.test(el.tagName)) el = el.parentNode;
    if (!el || el === document) return;
    var t = (el.type || "").toLowerCase(), f = formOf(el);
    if (f && (t === "submit" || t === "image" || (el.tagName === "BUTTON" && t !== "button" && t !== "reset"))) beacon(f);
  }, true);
  // Sites that announce leads to the dataLayer (RealtyNinja: lead_form; GA4: generate_lead).
  try {
    window.dataLayer = window.dataLayer || [];
    var origPush = window.dataLayer.push;
    window.dataLayer.push = function () {
      try { for (var i = 0; i < arguments.length; i++) { var ev = arguments[i] && (arguments[i].event || ""); if (/lead_form|generate_lead|form_submit|lead/i.test(ev)) beacon(lastForm || document.activeElement && document.activeElement.form); } } catch (e) {}
      return origPush.apply(window.dataLayer, arguments);
    };
  } catch (e) {}
  if (window.MutationObserver) {
    new MutationObserver(function () { fillAll(); }).observe(document.documentElement, { childList: true, subtree: true });
  }
  window.wmt = { attribution: attribution, fill: fill, fillAll: fillAll, beacon: beacon, version: "1.2" };
})();
