#!/usr/bin/env python3
"""End-to-end check of the config-UI language wiring, over real HTTP.

The audit script proves the *strings* are wired and the smoke test proves
the *engine* behaves. Neither touches the server. This one does: it imports
the real ``ui_app`` Starlette instance and drives it with Starlette's
TestClient, so what it asserts is the exact byte stream a browser receives.

Scope — three things only:

  1. ``UI_LANGUAGE`` reaches the markup for every accepted value, and a
     garbage value degrades to ``auto`` instead of leaking into the page.
  2. No ``{{PLACEHOLDER}}`` survives into the response. An un-substituted
     one is not an error at runtime — it renders as literal braces in the
     browser and is invisible to every other check.
  3. The load order that makes the feature work (server default, then
     i18n.js, then app.js) holds in the served HTML, not just the template.

Requires starlette + httpx. Run with the isolated venv::

    ../.venv/Scripts/python dev-tools/i18n_server_test.py

Exit code 0 = all assertions passed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from starlette.testclient import TestClient
except Exception as exc:  # pragma: no cover - dependency hint
    print(f"SKIP: starlette TestClient unavailable ({exc})")
    print("      pip install starlette httpx")
    raise SystemExit(0)

from stash_jellyfin_proxy import runtime  # noqa: E402
from stash_jellyfin_proxy.app import ui_app  # noqa: E402

passed = 0
failures: list[str] = []


def check(name: str, actual, expected) -> None:
    global passed
    if actual == expected:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name}\n         expected: {expected!r}\n         actual:   {actual!r}")


def check_true(name: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name}" + (f"\n         {detail}" if detail else ""))


client = TestClient(ui_app, raise_server_exceptions=False)

print("config-UI language: end-to-end HTTP test\n")

# --- 1. injection for every accepted value, plus a bogus one ----------
print("1. UI_LANGUAGE -> served markup")
CASES = [
    ("auto", "en", "auto"),
    ("en", "en", "en"),
    ("zh", "zh-CN", "zh"),
    ("klingon", "en", "auto"),   # unsupported -> clamped
    ("", "en", "auto"),
]
for value, want_html_lang, want_js_lang in CASES:
    runtime.UI_LANGUAGE = value
    r = client.get("/")
    label = f"UI_LANGUAGE={value!r}"
    check_true(f"{label}: HTTP 200", r.status_code == 200, f"got {r.status_code}")
    body = r.text
    check(f"{label}: <html lang>", (re.search(r'<html lang="([^"]*)"', body) or [None, None])[1], want_html_lang)
    check(
        f"{label}: SJP_DEFAULT_LANG",
        (re.search(r'window\.SJP_DEFAULT_LANG = "([^"]*)"', body) or [None, None])[1],
        want_js_lang,
    )
    check(f"{label}: no raw {{{{placeholder}}}}", "{{" in body or "}}" in body, False)

runtime.UI_LANGUAGE = "zh"
body = client.get("/").text

# --- 2. content-type and the switcher ---------------------------------
print("\n2. Served page contract")
check_true("content-type is text/html", "text/html" in client.get("/").headers.get("content-type", ""))
check_true("lang switcher present", 'id="lang-switch"' in body)
for lang in ("auto", "zh", "en"):
    check_true(f"switcher offers data-lang={lang!r}", f'data-lang="{lang}"' in body)
check_true("Interface Language select present", 'data-key="UI_LANGUAGE"' in body)
check_true("Interface card is live-saveable", 'data-save="interface"' in body)

# --- 3. load order in the served bytes ---------------------------------
print("\n3. Load order (server default -> i18n.js -> app.js)")
i18n_at = body.find("/static/i18n.js")
app_at = body.find("/static/app.js")
default_at = body.find("SJP_DEFAULT_LANG")
check_true("i18n.js is referenced", i18n_at != -1)
check_true("app.js is referenced", app_at != -1)
check_true("default is set before i18n.js", -1 < default_at < i18n_at,
           f"default_at={default_at} i18n_at={i18n_at}")
check_true("i18n.js precedes app.js", -1 < i18n_at < app_at, f"i18n_at={i18n_at} app_at={app_at}")

# --- 4. static assets really serve ------------------------------------
print("\n4. Static assets")
for asset, needle in (("/static/i18n.js", "SJP_I18N"), ("/static/app.js", "SJP"), ("/static/app.css", None)):
    r = client.get(asset)
    check_true(f"{asset}: HTTP 200", r.status_code == 200, f"got {r.status_code}")
    check_true(f"{asset}: non-empty body", len(r.content) > 1000, f"{len(r.content)} bytes")
    if needle:
        check_true(f"{asset}: contains {needle}", needle in r.text)
check_true("/static/i18n.js served as JS",
           "javascript" in client.get("/static/i18n.js").headers.get("content-type", ""))

# --- 5. status payload feeds the front end ----------------------------
print("\n5. /api/status payload")
r = client.get("/api/status")
if r.status_code == 200:
    payload = r.json()
    check("status.uiLanguage echoes runtime", payload.get("uiLanguage"), runtime.UI_LANGUAGE)
else:
    check_true("/api/status reachable", False, f"got {r.status_code} (Stash probe may be down)")

print(f"\n{passed} passed, {len(failures)} failed")
if failures:
    print("FAILED: " + "; ".join(failures))
    raise SystemExit(1)
print("ALL SERVER CHECKS PASSED")
