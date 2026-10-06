/* Theme switching for the SJP dashboard.
 *
 * Two themes, both driven purely by CSS custom properties keyed off
 * <html data-theme="…">:
 *
 *   dark — the established palette (also the no-JS default, since the
 *          :root block in app.css holds these values)
 *   day  — light palette
 *
 * Pattern mirrors i18n.js: same chip-switcher wiring, same localStorage
 * persistence wrapped in try/catch for private mode, same boot strategy.
 * This file is loaded in <head>, so the attribute is applied as early as
 * possible to avoid a flash of the wrong theme.
 */
(function (global) {
  "use strict";

  var STORAGE_KEY = "sjp.theme";
  var THEMES = ["day", "dark"];
  var DEFAULT = "dark";
  var theme = DEFAULT;

  function isValid(name) {
    return THEMES.indexOf(name) !== -1;
  }

  function storedPreference() {
    try {
      var v = global.localStorage.getItem(STORAGE_KEY);
      return isValid(v) ? v : DEFAULT;
    } catch (_) {
      // Private mode / storage disabled — fall back to the default.
      return DEFAULT;
    }
  }

  function apply(name) {
    var doc = global.document;
    if (!doc || !doc.documentElement) return;
    doc.documentElement.setAttribute("data-theme", name);
    // Keep native UI (scrollbars, form controls, date pickers) in step so
    // the browser does not render dark widgets inside a light page.
    doc.documentElement.style.colorScheme = name === "day" ? "light" : "dark";
    theme = name;
  }

  function setTheme(name, opts) {
    var next = isValid(name) ? name : DEFAULT;
    var persist = !opts || opts.persist !== false;
    if (persist) {
      try {
        global.localStorage.setItem(STORAGE_KEY, next);
      } catch (_) { /* private mode — in-memory only */ }
    }
    apply(next);
    syncSwitcher();
  }

  function syncSwitcher() {
    var box = global.document.getElementById("theme-switch");
    if (!box) return;
    var buttons = box.querySelectorAll(".theme-opt");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].classList.toggle(
        "active", buttons[i].dataset.themeOpt === theme
      );
    }
  }

  function wireSwitcher() {
    var box = global.document.getElementById("theme-switch");
    if (!box) return;
    box.addEventListener("click", function (e) {
      var btn = e.target.closest(".theme-opt");
      if (!btn) return;
      setTheme(btn.dataset.themeOpt);
    });
    syncSwitcher();
  }

  var booted = false;
  function boot() {
    if (booted) return;
    booted = true;
    apply(storedPreference());
    wireSwitcher();
  }

  global.SJP_THEME = {
    setTheme: setTheme,
    getTheme: function () { return theme; },
    syncSwitcher: syncSwitcher,
    themes: THEMES.slice(),
    boot: boot,
  };

  // Apply before first paint so the stored theme never flashes.
  apply(storedPreference());
  if (global.document && global.document.readyState === "loading") {
    global.document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(window);
