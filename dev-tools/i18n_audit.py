#!/usr/bin/env python3
"""Audit the zh-CN catalog against what the config UI actually displays.

Five independent checks, because a translation layer can fail in five
different ways and only some of them are visible when you load the page:

  A. COVERAGE   every prose string in index.html has a catalog entry
  B. CALL SITES every literal passed to t() in app.js exists in the catalog
  C. DEAD KEYS  every catalog entry is reachable (from HTML or a t() call)
  D. PLACEHOLDERS the {vars} in a key match the {vars} in its translation
  E. WIRING     the template/server contract that feeds the engine holds

Check C is what keeps the catalog from rotting: an entry whose English
string no longer exists in the UI is unreachable, and silently so.

Check E covers the parts of the feature that live *outside* the catalog.
Two failure modes are invisible to A-D and to the smoke test, because both
ship a page that merely looks slightly wrong:

  * a `{{PLACEHOLDER}}` the server never substitutes is served literally,
    so the browser shows `{{UI_LANG}}` as text and <html lang> is invalid;
  * the <script> order — `SJP_DEFAULT_LANG`, then i18n.js, then app.js —
    silently degrades to English if shuffled, since i18n.js reads the
    server default at load time and app.js renders before the engine is up.

Intentional non-translatables are listed in ALLOW_UNTRANSLATED below.
Anything else reported under A is a real gap.

Usage::

    python dev-tools/i18n_audit.py            # full report
    python dev-tools/i18n_audit.py --quiet     # failures only

Exit code is 0 when nothing is wrong, 1 otherwise — usable as a CI gate.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_extract import extract_html  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
I18N_JS = REPO / "stash_jellyfin_proxy" / "ui" / "static" / "i18n.js"
APP_JS = REPO / "stash_jellyfin_proxy" / "ui" / "static" / "app.js"
INDEX_HTML = REPO / "stash_jellyfin_proxy" / "ui" / "templates" / "index.html"
UI_API_PY = REPO / "stash_jellyfin_proxy" / "ui" / "api.py"

# Strings that must stay as-is in every language. Mostly identifiers,
# protocol tokens, and example values the operator types verbatim.
ALLOW_UNTRANSLATED = {
    # Placeholders filled server-side before the page is served.
    "{{SERVER_NAME}}", "{{ASSET_V}}",
    # Config keys and code identifiers shown inline.
    "SERVER_ID", "ACCESS_TOKEN", "SJP",
    # Protocol / API vocabulary.
    "Jellyfin API", "graphql", "/graphql", "AND", "OR",
    "DEBUG", "INFO", "WARNING", "ERROR",
    # Product name.
    "Stash",
    # Stash tag names and other operator data used as examples.
    "FAVORITE", "GENRE", "Series", "Playlists",
    "Tit Worship, JOI, Gooning", "The, A, An", "S02:E05 — Some Episode",
    # Sample URLs.
    "http://localhost:9999", "https://stash-sjs.example.com",
    "stash_jellyfin_proxy.conf",
    # Language names are always shown in their own language.
    "English", "AUTO", "EN",
}

# `"key": "value",` — the \\. alternative lets escaped quotes inside a
# value survive, and [^"\\] spans newlines so entries wrapped across two
# source lines still parse.
_ENTRY_RE = re.compile(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"')
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
# t("...") / t('...') with a literal first argument. The two quote styles
# are separate alternatives rather than one character class: a
# double-quoted key may legitimately contain an apostrophe
# ("...matching '{ua}'..."), and excluding both quote characters would
# truncate it at the first ' and report a false mismatch.
_T_CALL_RE = re.compile(
    r"""\bt\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')"""
)

# Keys reached indirectly, through a lookup table instead of a literal
# t("...") call site. Both tables map something to an English catalog key
# that is resolved at render time; without these the dead-key check would
# flag every label they hold.
_SORT_OPTIONS_BLOCK_RE = re.compile(r"const SORT_OPTIONS\s*=\s*\[(.*?)\n\];", re.S)
_SORT_OPTIONS_LABEL_RE = re.compile(
    r"""\[\s*["'][^"']*["']\s*,\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*\]"""
)
_NOTES_BLOCK_RE = re.compile(r"const GENRE_MODE_NOTES\s*=\s*\{(.*?)\n\};", re.S)
_OBJECT_VALUE_RE = re.compile(
    r"""[A-Za-z_]\w*\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
)


def _unescape(js_string: str) -> str:
    """Resolve the escapes that can appear in a catalog literal."""
    out = js_string.replace('\\"', '"').replace("\\'", "'")
    return out.replace("\\n", "\n").replace("\\\\", "\\")


def _strip_quotes(literal: str) -> str:
    return _unescape(literal[1:-1])


def parse_catalog() -> dict[str, str]:
    src = I18N_JS.read_text(encoding="utf-8")
    start = src.index("const ZH = {")
    # Brace-match to the end of the object so nothing outside it is parsed.
    depth = 0
    end = start
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    block = src[src.index("{", start): end + 1]
    return {_unescape(k): _unescape(v) for k, v in _ENTRY_RE.findall(block)}


def t_call_keys() -> list[str]:
    """English keys passed straight to t() as a literal."""
    src = APP_JS.read_text(encoding="utf-8")
    keys = []
    for m in _T_CALL_RE.finditer(src):
        lit = m.group(1) or m.group(2)
        keys.append(_unescape(lit))
    return list(dict.fromkeys(keys))


def indirect_keys() -> list[str]:
    """English keys reached through a lookup table rather than a call site."""
    src = APP_JS.read_text(encoding="utf-8")
    found: list[str] = []

    block = _SORT_OPTIONS_BLOCK_RE.search(src)
    if block:
        found += [_strip_quotes(m.group(1))
                  for m in _SORT_OPTIONS_LABEL_RE.finditer(block.group(1))]

    block = _NOTES_BLOCK_RE.search(src)
    if block:
        found += [_strip_quotes(m.group(1))
                  for m in _OBJECT_VALUE_RE.finditer(block.group(1))]

    return list(dict.fromkeys(found))


# --- E. template <-> server wiring ------------------------------------
_PLACEHOLDER_IN_HTML_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
_PLACEHOLDER_REPLACED_RE = re.compile(
    r"\.replace\(\s*[\"']\{\{([A-Z0-9_]+)\}\}[\"']"
)
_SCRIPT_SRC_RE = re.compile(r"<script[^>]*\bsrc=[\"']([^\"']+)[\"']")
_LANG_OPTION_RE = re.compile(r"<button[^>]*\bdata-lang=[\"']([^\"']+)[\"']")
_PRECISIONS_RE = re.compile(r"const PRECISIONS\s*=\s*\[([^\]]*)\]")


def wiring_issues() -> list[str]:
    """Verify the template/server contract that delivers the engine."""
    problems: list[str] = []
    html = INDEX_HTML.read_text(encoding="utf-8")
    api = UI_API_PY.read_text(encoding="utf-8")
    i18n = I18N_JS.read_text(encoding="utf-8")

    # E1/E2 — every placeholder must be substituted, and vice versa.
    in_template = set(_PLACEHOLDER_IN_HTML_RE.findall(html))
    substituted = set(_PLACEHOLDER_REPLACED_RE.findall(api))
    for name in sorted(in_template - substituted):
        problems.append(f"index.html uses {{{{{name}}}}} but ui/api.py never replaces it")
    for name in sorted(substituted - in_template):
        problems.append(f"ui/api.py replaces {{{{{name}}}}} but index.html no longer uses it")

    # E3 — load order: server default -> i18n.js -> app.js.
    srcs = _SCRIPT_SRC_RE.findall(html)
    order = {s.split("?")[0]: i for i, s in enumerate(srcs)}
    i18n_at = order.get("/static/i18n.js")
    app_at = order.get("/static/app.js")
    if i18n_at is None:
        problems.append("index.html does not load /static/i18n.js")
    elif app_at is None:
        problems.append("index.html does not load /static/app.js")
    elif i18n_at > app_at:
        problems.append("i18n.js must load before app.js, else the first render is untranslated")

    default_at = html.find("SJP_DEFAULT_LANG")
    if default_at == -1:
        problems.append("index.html never sets window.SJP_DEFAULT_LANG")
    elif i18n_at is not None:
        js_pos = html.find("/static/i18n.js")
        if js_pos != -1 and default_at > js_pos:
            problems.append("window.SJP_DEFAULT_LANG is set after i18n.js loads, so it is ignored")

    # E4 — the switcher's options must match what the engine will accept.
    in_dom = set(_LANG_OPTION_RE.findall(html))
    m = _PRECISIONS_RE.search(i18n)
    declared = set(re.findall(r'["\']([^"\']+)["\']', m.group(1))) if m else set()
    if not in_dom:
        problems.append("index.html has no #lang-switch buttons carrying data-lang")
    for v in sorted(in_dom - declared):
        problems.append(f"switcher offers data-lang={v!r}, which PRECISIONS rejects -> falls back to auto")
    for v in sorted(declared - in_dom):
        problems.append(f"PRECISIONS declares {v!r} but the switcher has no button for it")

    # E5 — the server-side key must reach the runtime, or UI_LANGUAGE does nothing.
    for needle, where in (("UI_LANGUAGE", "ui/api.py (_P5B_KEYS)"),):
        if f'"{needle}"' not in api:
            problems.append(f"{needle} is not exposed through {where}")

    return problems


def main() -> int:
    quiet = "--quiet" in sys.argv
    catalog = parse_catalog()
    html_text, html_attr = extract_html()
    html_strings = list(dict.fromkeys(html_text + html_attr))
    call_keys = t_call_keys()
    table_keys = indirect_keys()

    failures: list[str] = []

    # --- A. HTML coverage -------------------------------------------------
    missing_html = []
    for s in html_strings:
        if s in catalog or s in ALLOW_UNTRANSLATED:
            continue
        if not re.search(r"[A-Za-z]", s):
            continue                      # punctuation / digits only
        if re.fullmatch(r"https?://\S+", s):
            continue                      # a URL example
        missing_html.append(s)

    # --- B. t() call sites ------------------------------------------------
    missing_calls = [k for k in (call_keys + table_keys) if k not in catalog]

    # --- C. dead keys -----------------------------------------------------
    used = set(html_strings) | set(call_keys) | set(table_keys)
    dead = [k for k in catalog if k not in used]

    # --- D. placeholder parity -------------------------------------------
    bad_ph = []
    for k, v in catalog.items():
        if set(_PLACEHOLDER_RE.findall(k)) != set(_PLACEHOLDER_RE.findall(v)):
            bad_ph.append((k, v))

    # --- E. template <-> server wiring -----------------------------------
    wiring = wiring_issues()

    def section(title, rows, fmt=lambda r: f"    {r}"):
        if rows:
            failures.append(title)
            print(f"\n[{title}] {len(rows)}")
            for r in rows:
                print(fmt(r))

    if not quiet:
        print("Catalog entries            :", len(catalog))
        print("Strings in index.html      :", len(html_strings))
        print("Literal keys in t() calls  :", len(call_keys))
        print("Keys via lookup tables     :", len(table_keys))
        print("Unreachable catalog entries:", len(dead))
        print("Wiring problems            :", len(wiring))

    section("A. UNTRANSLATED HTML STRINGS", missing_html)
    section("B. t() KEYS MISSING FROM CATALOG", missing_calls)
    section("C. DEAD CATALOG ENTRIES", dead)
    section("D. PLACEHOLDER MISMATCH",
            bad_ph, lambda r: f"    key={r[0]!r}\n        val={r[1]!r}")
    section("E. TEMPLATE/SERVER WIRING", wiring)

    if not failures:
        print("\nALL CHECKS PASSED")
        return 0
    print(f"\n{len(failures)} CHECK(S) FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
