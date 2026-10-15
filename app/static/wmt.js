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
 */
(function () {
  if (window.wmt) return;
  var KEYS = ["gclid", "gbraid", "wbraid", "fbclid", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"];
  var STORE = "wmt_attr";
  var DAYS = 90;

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

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fillAll); else fillAll();
  // Forms that render late (popups, Elementor, React) and values typed before submit.
  document.addEventListener("focusin", function (e) { var f = e.target && e.target.form; if (f) fill(f); }, true);
  document.addEventListener("submit", function (e) { if (e.target && e.target.tagName === "FORM") fill(e.target); }, true);
  if (window.MutationObserver) {
    new MutationObserver(function () { fillAll(); }).observe(document.documentElement, { childList: true, subtree: true });
  }
  window.wmt = { attribution: attribution, fill: fill, fillAll: fillAll, version: "1.0" };
})();
