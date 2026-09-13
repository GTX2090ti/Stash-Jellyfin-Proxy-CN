/**
 * Headless smoke test for ui/static/i18n.js.
 *
 * The audit script (i18n_audit.py) proves *coverage* — every string has an
 * entry, no entry is unreachable. It says nothing about whether the runtime
 * engine behaves. The two properties worth locking down are the ones that
 * were actually hard to get right:
 *
 *   1. Idempotency — apply() runs on every language change and on every
 *      app.js re-render, so it must never translate a translation
 *      ("代理" -> "代理运行中" -> must not become "代理运行中运行中").
 *   2. Non-destruction — apply() walks regions app.js has overwritten with
 *      fresher text via textContent/innerHTML. It must heal those into the
 *      new language rather than stomping them back to a stale original.
 *
 * i18n.js is an IIFE over `window`, so a small DOM stub is enough to load
 * and drive it under node with no browser.
 *
 *   node dev-tools/i18n_smoke.js
 * Exit code 0 = all assertions passed.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

/* ------------------------------------------------------------------ *
 * Minimal DOM
 * ------------------------------------------------------------------ */

function El(nodeName, attrs) {
  this.nodeType = 1;
  this.nodeName = nodeName;
  this.childNodes = [];
  this.parentNode = null;
  this._attrs = Object.assign({}, attrs || {});
}
El.prototype.appendChild = function (c) {
  c.parentNode = this;
  this.childNodes.push(c);
  return c;
};
El.prototype.setAttribute = function (n, v) {
  this._attrs[n] = String(v);
};
El.prototype.getAttribute = function (n) {
  return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null;
};
El.prototype.hasAttribute = function (n) {
  return Object.prototype.hasOwnProperty.call(this._attrs, n);
};
El.prototype.closest = function (sel) {
  const m = /^\[([^\]]+)\]$/.exec(sel);
  if (!m) return null;
  let n = this;
  while (n) {
    if (n.hasAttribute && n.hasAttribute(m[1])) return n;
    n = n.parentNode;
  }
  return null;
};
El.prototype.querySelectorAll = function (sel) {
  if (sel !== "*") return [];
  const out = [];
  (function walk(n) {
    for (const c of n.childNodes) {
      if (c.nodeType === 1) {
        out.push(c);
        walk(c);
      }
    }
  })(this);
  return out;
};

function Txt(v) {
  this.nodeType = 3;
  this.nodeName = "#text";
  this.nodeValue = v;
  this.parentNode = null;
}

function collectTextNodes(root, out) {
  for (const c of root.childNodes || []) {
    if (c.nodeType === 3) out.push(c);
    else if (c.childNodes) collectTextNodes(c, out);
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * Environment + load
 * ------------------------------------------------------------------ */

function makeEnv(opts) {
  const o = opts || {};
  const store = new Map();
  if (o.storedLang) store.set("sjp.ui.lang", o.storedLang);

  const documentElement = new El("HTML", { lang: "en" });
  const body = new El("BODY");
  documentElement.appendChild(body);

  const dispatched = [];
  const document = {
    // "loading" keeps i18n.js from booting itself, so the test drives
    // setLang() explicitly instead of racing a DOMContentLoaded event.
    readyState: "loading",
    documentElement: documentElement,
    body: body,
    createTreeWalker: function (root) {
      const nodes = collectTextNodes(root, []);
      let i = 0;
      return {
        currentNode: null,
        nextNode: function () {
          if (i >= nodes.length) return null;
          this.currentNode = nodes[i++];
          return this.currentNode;
        },
      };
    },
    getElementById: function () {
      return null;
    },
    addEventListener: function () {},
    dispatchEvent: function (ev) {
      dispatched.push(ev);
      return true;
    },
  };

  const env = {
    document: document,
    NodeFilter: { SHOW_TEXT: 4 },
    CustomEvent: function CustomEvent(type, init) {
      this.type = type;
      this.detail = (init || {}).detail;
    },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
    },
    navigator: { languages: o.navigatorLanguages || ["en-US"], language: o.navigatorLanguage || "en-US" },
    __dispatched: dispatched,
    __store: store,
  };
  Object.defineProperty(env, "window", { value: env, enumerable: true });
  return env;
}

function loadEngine(env) {
  const src = fs.readFileSync(
    path.join(__dirname, "..", "stash_jellyfin_proxy", "ui", "static", "i18n.js"),
    "utf8"
  );
  const ctx = vm.createContext(env);
  vm.runInContext(src, ctx, { filename: "i18n.js" });
  return env.SJP_I18N;
}

/* ------------------------------------------------------------------ *
 * Assertions
 * ------------------------------------------------------------------ */

let passed = 0;
const failures = [];

function check(name, actual, expected) {
  if (actual === expected) {
    passed++;
    console.log(`  ok   ${name}`);
  } else {
    failures.push(name);
    console.log(`  FAIL ${name}\n         expected: ${JSON.stringify(expected)}\n         actual:   ${JSON.stringify(actual)}`);
  }
}

/* ------------------------------------------------------------------ *
 * Tests
 * ------------------------------------------------------------------ */

console.log("i18n.js headless smoke test\n");

/* --- 1. t(): translation, interpolation, passthrough ---------------- */
{
  console.log("1. t()");
  const env = makeEnv();
  const i18n = loadEngine(env);
  i18n.setLang("zh");

  check("plain lookup", i18n.t("Dashboard"), "仪表盘");
  check("interpolation", i18n.t("Uptime: {v}", { v: "3m" }), "运行时长：3m");
  // Unit words are part of the translation, not appended by the caller —
  // app.js composes uptime via t("{d}d {h}h {m}m", ...) precisely so the
  // zh form can use 天/小时/分 without a concatenation helper.
  check("composed uptime key", i18n.t("{d}d {h}h {m}m", { d: 1, h: 2, m: 3 }), "1天 2小时 3分");
  check("unknown passes through", i18n.t("Not In Catalog 123"), "Not In Catalog 123");
  check("null is null", i18n.t(null), null);
  check("missing var is left literal", i18n.t("Uptime: {v}"), "运行时长：{v}");
}

/* --- 2. DOM walk: text nodes + attributes --------------------------- */
{
  console.log("\n2. DOM walk");
  const env = makeEnv();
  const body = env.document.body;

  const h1 = new El("H1");
  const h1text = new Txt(" Dashboard ");
  h1.appendChild(h1text);
  body.appendChild(h1);

  const input = new El("INPUT", { placeholder: "Search", title: "Dashboard" });
  body.appendChild(input);

  const pre = new El("PRE");
  const preText = new Txt("Dashboard");
  pre.appendChild(preText);
  body.appendChild(pre);

  const skip = new El("DIV", { "data-i18n-skip": "" });
  const skipText = new Txt("Dashboard");
  skip.appendChild(skipText);
  body.appendChild(skip);

  const i18n = loadEngine(env);
  // Re-run apply() on the tree now that the stub has children.
  i18n.setLang("zh");

  check("text node translated", h1text.nodeValue, " 仪表盘 ");
  check("surrounding whitespace preserved", h1text.nodeValue.trim(), "仪表盘");
  check("placeholder translated", input.getAttribute("placeholder"), "搜索");
  check("title translated", input.getAttribute("title"), "仪表盘");
  check("<pre> skipped", preText.nodeValue, "Dashboard");
  check("[data-i18n-skip] subtree skipped", skipText.nodeValue, "Dashboard");
  check("<html lang> set", env.document.documentElement.getAttribute("lang"), "zh-CN");
  check("langchange dispatched", env.__dispatched.filter((e) => e.type === "sjp:langchange").length >= 1, true);
  check("payload carries lang", env.__dispatched[env.__dispatched.length - 1].detail.lang, "zh");
}

/* --- 3. Idempotency ------------------------------------------------- */
{
  console.log("\n3. Idempotency (the double-translation bug)");
  const env = makeEnv();
  const t1 = new Txt("Proxy");
  env.document.body.appendChild(new El("SPAN")).appendChild(t1);
  const i18n = loadEngine(env);

  i18n.setLang("zh");
  const after1 = t1.nodeValue;
  i18n.apply(env.document.body);
  i18n.apply(env.document.body);
  const after3 = t1.nodeValue;

  check("first apply translates", after1, "代理");
  check("repeated apply is a no-op", after3, "代理");
}

/* --- 4. Non-destruction: app.js overwrote the node ------------------ */
{
  console.log("\n4. Coexistence with app.js re-renders");
  const env = makeEnv();
  const t1 = new Txt("Proxy");
  env.document.body.appendChild(new El("SPAN")).appendChild(t1);
  const i18n = loadEngine(env);
  i18n.setLang("zh");
  check("boot translated it", t1.nodeValue, "代理");

  // app.js pollStatus() writes a *fresh English* string into the node.
  t1.nodeValue = "Proxy Running";
  i18n.apply(env.document.body);
  check("fresh English is healed into zh", t1.nodeValue, "代理运行中");

  // A value that is neither the source nor ours must be left alone: this is
  // what keeps a concurrently-updating node from being stomped.
  t1.nodeValue = "代理运行中";
  i18n.apply(env.document.body);
  check("already-translated composite untouched", t1.nodeValue, "代理运行中");
}

/* --- 5. Switch back to English is lossless -------------------------- */
{
  console.log("\n5. Restore on switch back to English");
  const env = makeEnv();
  const el = new El("SPAN");
  // Tab + newline around the phrase, to prove lead/trail round-trip exactly
  // and that the phrase is matched after whitespace normalisation.
  const t1 = new Txt("\tLibraries\n");
  el.appendChild(t1);
  env.document.body.appendChild(el);
  const i18n = loadEngine(env);

  i18n.setLang("zh");
  check("translated, whitespace preserved", t1.nodeValue, "\t媒体库\n");
  i18n.setLang("en");
  check("restored byte-for-byte", t1.nodeValue, "\tLibraries\n");
  check("<html lang> restored", env.document.documentElement.getAttribute("lang"), "en");
  check("t() is identity in en", i18n.t("Dashboard"), "Dashboard");
  check("interpolation still works in en", i18n.t("Uptime: {v}", { v: "1m" }), "Uptime: 1m");
}

/* --- 6. Language precedence ---------------------------------------- */
{
  console.log("\n6. Precedence: stored > server > browser");

  // boot() is the entry point that consults localStorage; setLang() only
  // takes the preference it is handed. Exercising boot() is the point, so
  // every case below goes through it.
  function booted(localStorageLang, serverLang, browserLangs) {
    const env = makeEnv({
      storedLang: localStorageLang,
      navigatorLanguages: browserLangs,
    });
    env.SJP_DEFAULT_LANG = serverLang;
    const engine = loadEngine(env);
    engine.boot();
    return { engine, env };
  }

  let r = booted("zh", "en", ["en-US"]);
  check("stored 'zh' beats server 'en'", r.engine.getLang(), "zh");
  check("preference reported as 'zh'", r.engine.getPreference(), "zh");

  r = booted(undefined, "zh", ["en-US"]);
  check("server 'zh' beats browser 'en'", r.engine.getLang(), "zh");

  r = booted(undefined, "auto", ["zh-CN", "en-US"]);
  check("auto follows navigator.languages", r.engine.getLang(), "zh");

  r = booted(undefined, "auto", ["zh-Hans-CN", "en-US"]);
  check("region subtag is stripped", r.engine.getLang(), "zh");

  r = booted(undefined, "auto", ["fr-FR"]);
  check("unsupported browser -> en", r.engine.getLang(), "en");

  r = booted("en", "zh", ["zh-CN"]);
  check("stored 'en' beats everything", r.engine.getLang(), "en");

  r = booted(undefined, "zh-CN", ["en-US"]);
  check("server 'zh-CN' normalises to 'zh'", r.engine.getLang(), "zh");

  r = booted("klingon", "auto", ["en-US"]);
  check("bogus stored value -> auto", r.engine.getPreference(), "auto");
  r.engine.setLang("klingon");
  check("bogus setLang ignored", r.engine.getPreference(), "auto");

  // boot() must not write to storage: merely opening the page is not a
  // preference change, and writing would defeat "auto" for good.
  r = booted(undefined, "zh", ["en-US"]);
  check("boot() leaves storage untouched", r.env.__store.get("sjp.ui.lang"), undefined);

  // An explicit click, by contrast, does persist.
  r.engine.setLang("en");
  check("explicit switch persists", r.env.__store.get("sjp.ui.lang"), "en");
}

/* --- 7. Catalog hygiene -------------------------------------------- */
{
  console.log("\n7. Catalog hygiene");
  const env = makeEnv();
  const i18n = loadEngine(env);
  const zh = i18n.catalog;

  const empty = Object.keys(zh).filter((k) => !String(zh[k]).trim());
  check("no empty translations", empty.length, 0);

  // A handful of strings are deliberately identical in both languages:
  // brand name, protocol vocabulary, and Stash tag names the operator
  // types verbatim. Keeping them *in* the catalog (rather than relying on
  // unknown-string passthrough) documents the decision where a translator
  // will look for it. The exact set is pinned so a new accidental
  // `"Foo": "Foo"` entry — which would silently look translated — fails.
  const INTENTIONAL_IDENTITY = new Set([
    "Stash URL",
    "Stash {v}",
    "Series",
    "FAVORITE",
    "GENRE",
    "The, A, An",
    "Tit Worship, JOI, Gooning",
  ]);
  const ident = Object.keys(zh).filter((k) => zh[k] === k);
  const unexpected = ident.filter((k) => !INTENTIONAL_IDENTITY.has(k));
  const missing = [...INTENTIONAL_IDENTITY].filter((k) => zh[k] !== k);
  check("no unexpected identity entries", unexpected.slice(0, 3).join(" | ") || "ok", "ok");
  check("intentional identities all still identity", missing.join(" | ") || "ok", "ok");

  // Every placeholder in a key must appear in its translation, else a
  // runtime value silently vanishes from the rendered string.
  const vre = /\{(\w+)\}/g;
  const mismatched = [];
  for (const k of Object.keys(zh)) {
    const kv = (k.match(vre) || []).sort().join(",");
    vre.lastIndex = 0;
    const tv = (String(zh[k]).match(vre) || []).sort().join(",");
    vre.lastIndex = 0;
    if (kv !== tv) mismatched.push(`${k}  [${kv}] vs [${tv}]`);
  }
  check("placeholder parity key<->value", mismatched.slice(0, 3).join(" | ") || "ok", "ok");

  // The audit script (i18n_audit.py) is the coverage gate; this is a
  // cheap tripwire in case someone edits the catalog from here.
  check("catalog is substantial", Object.keys(zh).length >= 300, true);
}

/* ------------------------------------------------------------------ */

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  console.log("FAILED: " + failures.join("; "));
  process.exit(1);
}
console.log("ALL SMOKE CHECKS PASSED");
