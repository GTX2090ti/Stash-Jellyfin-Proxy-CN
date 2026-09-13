#!/usr/bin/env python3
"""Extract every user-visible string from the config-UI HTML template.

The HTML side is parsed with a real parser (``html.parser``), so the
result is exact: text nodes outside ``<script>``/``<style>``/``<code>``/
``<pre>``, plus the ``title``, ``placeholder`` and ``aria-label``
attributes of every element.

The JS side is deliberately *not* scraped here. A quote-pairing regex over
a 1.2k-line script produces garbage (it pairs an apostrophe in one literal
with a quote three functions later), and the result would be worse than no
list at all. Instead, dynamic strings are wrapped in ``t()`` calls in
``app.js`` and ``dev-tools/i18n_audit.py`` cross-checks the catalog against
those call sites — an exact check against real usage rather than a guess.

Usage::

    python dev-tools/i18n_extract.py            # pretty JSON
    python dev-tools/i18n_extract.py --count    # totals only
"""
from __future__ import annotations

import json
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HTML_PATH = REPO / "stash_jellyfin_proxy" / "ui" / "templates" / "index.html"

# Tags whose text content is markup/CSS sample, not prose.
_SKIP_TAGS = {"script", "style", "code", "pre"}
# Attributes the runtime engine rewrites.
ATTR_NAMES = ("title", "placeholder", "aria-label")


class _HTMLStrings(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.texts: list[str] = []
        self.attrs: list[str] = []

    def _collect_attrs(self, attrs):
        for name, value in attrs:
            if name in ATTR_NAMES and value and value.strip():
                self.attrs.append(value.strip())

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
        self._collect_attrs(attrs)

    def handle_startendtag(self, tag, attrs):
        self._collect_attrs(attrs)

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self.skip_depth:
            return
        # Collapse the template's own line wrapping so a phrase split
        # across source lines still matches the catalog key.
        text = " ".join(data.split())
        if text:
            self.texts.append(text)


def extract_html() -> tuple[list[str], list[str]]:
    parser = _HTMLStrings()
    parser.feed(HTML_PATH.read_text(encoding="utf-8"))
    # The catalog is keyed by source string, so one entry covers every
    # occurrence; dedupe while preserving first-seen order.
    return (list(dict.fromkeys(parser.texts)),
            list(dict.fromkeys(parser.attrs)))


def main() -> int:
    texts, attrs = extract_html()
    if "--count" in sys.argv:
        print(f"text: {len(texts)}")
        print(f"attr: {len(attrs)}")
        print(f"total: {len(texts) + len(attrs)}")
        return 0
    json.dump({"text": texts, "attr": attrs}, sys.stdout,
              ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
