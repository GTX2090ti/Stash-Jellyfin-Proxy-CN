"""Metadata scraping bridge — Jellyfin's "Identify"/refresh surface backed
by Stash's *own* scrapers.

Why this exists
---------------
Jellyfin clients drive metadata scraping through a small, well-known set of
endpoints:

    GET  /Items/{id}/MetadataEditor           list of providers for the dialog
    GET  /Items/{id}/RemoteSearch/{provider}  candidate matches for an item
    POST /Items/RemoteSearch/{type}           candidate matches (query body)
    POST /Items/RemoteSearch/Apply/{id}       apply the chosen match
    POST /Items/{id}/Refresh                  refresh one item (classic shape)
    POST /Items/{id}/RefreshMetadata          refresh one item
    GET  /Items/RemoteSearch/Image            proxy result artwork (ImageUrl=)
    POST /Library/Refresh                     refresh the whole library

Stash exposes the same *concept* as GraphQL primitives:

    listScrapers(types: [SCENE, PERFORMER])   -> the "providers"
    scrapeSingleScene / scrapeSinglePerformer -> search (by name or fragment)
    sceneUpdate / performerUpdate             -> persist the applied metadata
    metadataScan                              -> full library scan

This module maps one onto the other, so a user can tap "Identify" /
"识别" / "刷新元数据" on their phone and have the scrapers configured in
Stash (StashDB, ThePornDB, community scrapers, ...) do the matching.

Scope
-----
First-class support is **scenes and performers** — the two entity types
the Jellyfin UI exposes a scrape action for in practice. Studios, groups
and tags intentionally report an empty provider list: Stash's
`ScrapeContentType` enum has no STUDIO/TAG member, so there is no honest
way to enumerate a provider list for them. Their Apply calls degrade to a
logged no-op instead of pretending to work.

Write-back semantics
--------------------
Applying a scraped result overwrites the fields the scraper returned —
which is what "scrape this item" means everywhere else. Scalar metadata
(title/name, details, date, urls, cover image, performer attributes) is
always applied. Relationships (studio / performers / tags on a scene,
tags on a performer) are resolved by name through find-or-create and are
gated behind `SCRAPE_APPLY_RELATIONSHIPS` because they *replace* the
existing association list rather than merging into it.

Config knobs (plain conf keys — no loader change needed, since every key
in the conf file lands in `runtime.config`):

    ENABLE_SCRAPING             (bool, default true)
        Master switch. When false every endpoint here returns an empty
        provider list / 204, i.e. the proxy behaves exactly as before.

    SCRAPE_APPLY_RELATIONSHIPS  (bool, default true)
        Resolve and write studio / performers / tags from the scraped
        payload. Disable to keep your hand-curated associations.

    SCRAPE_APPLY_IMAGES         (bool, default true)
        Write the scraped cover image back to Stash.

    SCRAPE_RESULT_TTL_SECONDS   (int, default 1800)
        How long a search result stays applicable after the client
        fetched it (keyed by the token handed out in ProviderIds).

    SCRAPE_NUMERIC_SCRAPERS     (str, default "fantiajp,GetchuDL")
        Comma-separated scraper names/ids to try FIRST when the search
        term is a bare number (Fantia post ids, Getchu product codes).
        A pure-numeric term is an ID, not a title — the generic text
        scrapers can't do anything useful with it, while ID-oriented
        scrapers match it directly. Unknown names are ignored; if none
        of them is installed the default provider order is kept.

    SCRAPE_NUMERIC_URL_TEMPLATES (str, default see runtime.py)
        `<name>=<url template with {id}>` pairs. These ID scrapers are
        FRAGMENT/URL-only and refuse "by name" lookups, so the number has
        to be turned into a URL before they can read it.

    SCRAPE_ATTEMPT_TIMEOUT_SECONDS (int, default 20)
        Cap on a single scraper round-trip.

    SCRAPE_SEARCH_BUDGET_SECONDS (int, default 25)
        Overall budget for one search. Providers still untried when it
        runs out are skipped — the client's own request timeout comes
        first, and a partial answer beats a dead spinner.
"""
import asyncio
import base64
import json
import logging
import re
import time
import urllib.parse
import uuid

from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from starlette.responses import JSONResponse, Response

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.stash.client import stash_query
from stash_jellyfin_proxy.stash.tags import get_or_create_tag

logger = logging.getLogger("stash-jellyfin-proxy")

# Only these two entity types get a provider list. See module docstring.
_SUPPORTED_ENTITIES = ("scene", "performer")

# Cap on find-or-create fan-out per apply, so a hostile/silly scraper
# payload can't turn one tap into hundreds of Stash round-trips.
_MAX_RELATIONSHIPS = 30


# --------------------------------------------------------------------------
# Config helpers — conf-file keys win over the compiled-in defaults, which
# is what makes the knobs above hot-editable from the Web UI / conf file.
# --------------------------------------------------------------------------

def _flag(key: str, default: bool) -> bool:
    raw = runtime.config.get(key)
    if raw is None:
        raw = getattr(runtime, key, default)
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _int(key: str, default: int) -> int:
    raw = runtime.config.get(key)
    if raw is None:
        raw = getattr(runtime, key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _scraping_enabled() -> bool:
    return _flag("ENABLE_SCRAPING", True)


def _attempt_timeout() -> float:
    """Cap on one scraper round-trip. Stash talks to a third-party site per
    attempt, and a scraper that hangs must not hang the client's Identify."""
    raw = runtime.config.get("SCRAPE_ATTEMPT_TIMEOUT_SECONDS")
    if raw is None:
        raw = getattr(runtime, "SCRAPE_ATTEMPT_TIMEOUT_SECONDS", 20)
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 20.0


def _search_budget() -> float:
    """Overall wall-clock budget for one search/auto-match. Once it is spent
    the remaining providers are skipped and whatever was found is returned —
    clients time out long before a dozen serial scrapes finish."""
    raw = runtime.config.get("SCRAPE_SEARCH_BUDGET_SECONDS")
    if raw is None:
        raw = getattr(runtime, "SCRAPE_SEARCH_BUDGET_SECONDS", 25)
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 25.0


# Scrapers that timed out recently are pushed to the back of the attempt
# order. One hanging site (AdultTime, in practice) otherwise spends the whole
# per-search budget every single time, so the providers that *could* answer
# never get a turn.
_slow_until: Dict[str, float] = {}
_SLOW_COOLDOWN = 600.0


def _mark_slow(scraper_id: Optional[str]) -> None:
    if scraper_id:
        _slow_until[scraper_id] = time.monotonic() + _SLOW_COOLDOWN


def _is_slow(scraper_id: Optional[str]) -> bool:
    if not scraper_id:
        return False
    until = _slow_until.get(scraper_id, 0.0)
    if until and until <= time.monotonic():
        _slow_until.pop(scraper_id, None)
        return False
    return until > 0.0


async def _scrape_timed(entity: str, scraper_id: Optional[str],
                        scrape_input: Dict[str, Any]) -> List[Dict[str, Any]]:
    timeout = _attempt_timeout()
    try:
        return await asyncio.wait_for(_scrape(entity, scraper_id, scrape_input), timeout)
    except asyncio.TimeoutError:
        _mark_slow(scraper_id)
        logger.warning(f"scrape {entity} via {scraper_id or 'auto'} exceeded "
                       f"{timeout:.0f}s — skipping (deprioritised for "
                       f"{_SLOW_COOLDOWN:.0f}s)")
        return []


# --------------------------------------------------------------------------
# Entity classification — mirrors the id-prefix conventions the rest of the
# proxy already uses (see util/ids.py, endpoints/items.py).
# --------------------------------------------------------------------------

def _classify(item_id: str) -> Tuple[str, str]:
    """Return (entity, numeric_stash_id) for a Jellyfin item id."""
    if item_id.startswith("scene-"):
        rest = item_id[len("scene-"):]
        # Multi-file scenes append -f<fileId>; the scrape targets the scene.
        if "-f" in rest:
            rest = rest.split("-f", 1)[0]
        return "scene", rest
    if item_id.startswith("person-performer-"):
        return "performer", item_id[len("person-performer-"):]
    if item_id.startswith("performer-"):
        return "performer", item_id[len("performer-"):]
    if item_id.startswith("person-"):
        return "performer", item_id[len("person-"):]
    if item_id.startswith("studio-"):
        return "studio", item_id[len("studio-"):]
    if item_id.startswith("group-"):
        return "group", item_id[len("group-"):]
    if item_id.startswith("tag-"):
        return "tag", item_id[len("tag-"):]
    if item_id.startswith("genre-"):
        return "tag", item_id[len("genre-"):]
    return "", ""


# --------------------------------------------------------------------------
# Provider registry — Stash scrapers, re-read every few minutes.
# --------------------------------------------------------------------------

_PROVIDER_TTL = 300.0
_providers: Dict[str, Any] = {"at": 0.0, "items": []}

_LIST_SCRAPERS = """query ListScrapers($types: [ScrapeContentType!]!) {
    listScrapers(types: $types) {
        id
        name
        scene { supported_scrapes urls }
        performer { supported_scrapes urls }
    }
}"""


def _slug(value: str) -> str:
    """URL-safe provider key. The client puts this back into a path
    segment, so '/' and friends must not survive."""
    cleaned = re.sub(r"[^A-Za-z0-9._~-]+", "-", str(value)).strip("-")
    return cleaned or "scraper"


async def _load_providers() -> List[Dict[str, Any]]:
    """Fetch installed Stash scrapers, filtered to those that can actually
    answer a *text* search for scenes or performers."""
    now = time.monotonic()
    if _providers["items"] and now - _providers["at"] < _PROVIDER_TTL:
        return _providers["items"]

    res = await stash_query(_LIST_SCRAPERS, {"types": ["SCENE", "PERFORMER"]})
    raw = ((res or {}).get("data") or {}).get("listScrapers") or []

    items: List[Dict[str, Any]] = []
    used_keys = set()
    for scraper in raw:
        scraper_id = scraper.get("id")
        if not scraper_id:
            continue
        key = base = _slug(scraper_id)
        suffix = 2
        while key in used_keys:
            key = f"{base}-{suffix}"
            suffix += 1
        used_keys.add(key)

        entities = []
        entities_any = []
        patterns: Dict[str, List[str]] = {}
        for entity in _SUPPORTED_ENTITIES:
            spec = scraper.get(entity) or {}
            supported = spec.get("supported_scrapes") or []
            # NAME = "from text query". FRAGMENT-only scrapers can't answer
            # the search box, so they're not offered as providers — but they
            # are remembered, because a numeric query may still hit them.
            if supported:
                entities_any.append(entity)
            if "NAME" in supported:
                entities.append(entity)
            # URL prefixes this scraper claims to handle. Used to pick the one
            # scraper that can scrape an item from its OWN stored URL, instead
            # of fanning a name search out over every installed scraper.
            patterns[entity] = [u for u in (spec.get("urls") or []) if u]

        items.append({
            "key": key,
            "id": scraper_id,
            "name": scraper.get("name") or scraper_id,
            "entities": entities,
            "entities_any": entities_any,
            "patterns": patterns,
        })

    _providers["at"] = now
    _providers["items"] = items
    logger.info(
        "Stash scrapers available: "
        + (", ".join(f"{i['name']}[{'/'.join(i['entities']) or 'no-text-search'}]" for i in items) or "none")
    )
    return items


def _providers_for(entity: str) -> List[Dict[str, Any]]:
    return [p for p in _providers["items"] if entity in p["entities"]]


def _providers_any(entity: str) -> List[Dict[str, Any]]:
    """Every scraper that can handle this entity at all, including
    FRAGMENT/URL-only ones. Name search may not use these, but a scrape driven
    by the item's own URL can — that is the only way FantiaJp and friends ever
    answer."""
    return [p for p in _providers["items"] if entity in p.get("entities_any", [])]


def _normalize_url(value: str) -> str:
    """Lower-case, scheme-less, no leading `www.`, no trailing slash — so a
    scraper's declared prefix and a stored URL compare on equal terms."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = re.sub(r"^www\.", "", text)
    return text.rstrip("/")


def _patterns_match(patterns: Iterable[str], urls: Iterable[str]) -> bool:
    """Does any stored URL fall under any of the scraper's declared prefixes?

    Stash's own matcher is a prefix test (`fantia.jp/posts/` covers
    `https://fantia.jp/posts/3072923`); a leading `*.` is treated as a domain
    suffix, which is how the javbus mirrors are declared.
    """
    norm = [_normalize_url(u) for u in urls if u]
    norm = [u for u in norm if u]
    if not norm:
        return False
    for pattern in patterns or []:
        prefix = _normalize_url(pattern)
        if not prefix:
            continue
        if prefix.startswith("*."):
            suffix = prefix[2:]
            if any(u.split("/", 1)[0].endswith(suffix) for u in norm):
                return True
            continue
        if any(u.startswith(prefix) for u in norm):
            return True
    return False


def _resolve_provider(provider_key: str, entity: str) -> Optional[Dict[str, Any]]:
    """Accept the key we handed out, or the display name / raw id (some
    clients echo the name instead of the key).

    URL-only scrapers are included. They never appear in the Identify list,
    which only offers NAME-capable ones — but every result we return carries
    `SearchProviderName` set to the scraper's name, and a client may echo that
    straight back on a follow-up request. Resolving against the name-search
    pool alone answered "unknown provider: FantiaJp" and returned nothing.
    """
    if not provider_key:
        return None
    wanted = provider_key.strip()
    lowered = wanted.lower()
    # NAME-capable first, so a name search keeps behaving exactly as before.
    pools = [_providers_for(entity), _providers_any(entity)]
    for pool in pools:
        for p in pool:
            if p["key"] == wanted or p["id"] == wanted:
                return p
    for pool in pools:
        for p in pool:
            if p["name"].lower() == lowered:
                return p
    for pool in pools:
        for p in pool:
            if lowered in (p["name"].lower(), _slug(p["id"]).lower()):
                return p
    return None


# --------------------------------------------------------------------------
# Numeric-title routing
#
# A bare number as the search term is an ID, not a title — a Fantia post
# id, a Getchu product code. Generic text scrapers can't do anything with
# it; ID-oriented scrapers match it directly. When the term is pure
# digits, the configured scrapers (SCRAPE_NUMERIC_SCRAPERS) are tried
# first, in the order they're listed.
# --------------------------------------------------------------------------

_NUMERIC_QUERY = re.compile(r"\d{4,}")
_NUMERIC_SCRAPERS_DEFAULT = "fantiajp,GetchuDL"


def _numeric_query(term: str) -> bool:
    """True when the term is just a number (4+ digits)."""
    return bool(_NUMERIC_QUERY.fullmatch((term or "").strip()))


# A pasted link, with or without the scheme. Either a scheme-qualified URL, or
# a bare host that is followed by a path — so a filename (`movie.mp4`) or a
# catalogue number (`ABP-123`) is never mistaken for one.
_URL_LIKE = re.compile(
    r"^(?:https?://[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}(?:/\S*)?"
    r"|[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}/\S+)$", re.I)


def _looks_like_url(term: str) -> bool:
    return bool(_URL_LIKE.match((term or "").strip()))


def _numeric_scraper_names() -> List[str]:
    raw = runtime.config.get("SCRAPE_NUMERIC_SCRAPERS")
    if raw is None:
        raw = getattr(runtime, "SCRAPE_NUMERIC_SCRAPERS", _NUMERIC_SCRAPERS_DEFAULT)
    names: List[str] = []
    for part in str(raw).split(","):
        part = part.strip().lower()
        if part and part not in names:
            names.append(part)
    return names or _NUMERIC_SCRAPERS_DEFAULT.lower().split(",")


# Fantia's template carries two shapes separated by `|` on purpose: /posts/
# and /products/ are *different objects* that share the id, so a number that
# is not a post may still be a product (and the other way round). Both are
# tried and `_payload_relevance` then decides which describes the item.
_NUMERIC_URL_TEMPLATES_DEFAULT = ("fantiajp=https://fantia.jp/posts/{id}|"
                                  "https://fantia.jp/products/{id},"
                                  "getchudl=https://dl.getchu.com/i/item{id}")


def _numeric_url_templates() -> List[Tuple[str, str]]:
    """(scraper-name-substring, url-template) pairs from the conf. The template
    is how a bare number becomes something a FRAGMENT/URL-only scraper can
    actually read — FantiaJp and GetchuDL both refuse `by name`."""
    raw = runtime.config.get("SCRAPE_NUMERIC_URL_TEMPLATES")
    if raw is None:
        raw = getattr(runtime, "SCRAPE_NUMERIC_URL_TEMPLATES", _NUMERIC_URL_TEMPLATES_DEFAULT)
    pairs: List[Tuple[str, str]] = []
    for part in str(raw).split(","):
        if "=" not in part:
            continue
        name, template = part.split("=", 1)
        name, template = name.strip().lower(), template.strip()
        if name and template:
            pairs.append((name, template))
    return pairs


def _template_urls_for(provider: Dict[str, Any], term: str) -> List[str]:
    """Every URL a numeric term would live at for this scraper, in conf order.

    This returns a *list* because one source can serve several shapes of the
    same number. Fantia is the reason: `/posts/<id>` and `/products/<id>` are
    different objects that share the id — 1006291 is a fanart post under one
    and an unrelated adult product under the other, and both answer 200. A
    template may therefore carry several shapes separated by `|`.
    """
    key = f"{provider.get('name', '')} {provider.get('id', '')}".lower()
    out: List[str] = []
    for name, template in _numeric_url_templates():
        if name not in key:
            continue
        for shape in template.split("|"):
            url = shape.strip().replace("{id}", term)
            if url and url not in out:
                out.append(url)
    return out


# --------------------------------------------------------------------------
# Stash boxes — StashDB / PMV Stash / ThePornDB, queried as a name source
# --------------------------------------------------------------------------

# Filename pieces a stash box must never see: a bare number (`0541` — that is
# Cospuri's catalogue number and no box indexes those), a resolution marker
# (`2160p`), a container/hash fragment. What is left is the performer/title
# the box can actually answer — `LyaCutie` becomes `Lya Cutie`.
_TERM_SPLIT_RE = re.compile(r"[\s\-_.\[\]()]+")
_RES_TOKEN_RE = re.compile(r"\d{3,4}[pi]$", re.I)
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _name_query_from_term(term: str) -> str:
    """Turn a filename-shaped search term into words a stash box understands.

    `0541-LyaCutie-2160p` → `Lya Cutie`; `kv-139` → `kv 139` (scene numbers
    up to three digits ride along — half of JAV naming is exactly that);
    a bare number returns "" (it belongs to the URL-template route, and no
    box indexes bare numbers).
    """
    words: List[str] = []
    for raw in _TERM_SPLIT_RE.split((term or "").strip()):
        if not raw:
            continue
        if raw.isdigit():
            # catalogue ids and years are noise; short ones are scene numbers
            if len(raw) <= 3:
                words.append(raw)
            continue
        if _RES_TOKEN_RE.fullmatch(raw):
            continue
        words.extend(w for w in _CAMEL_RE.sub(" ", raw).split() if w)
    return " ".join(words)


_STASH_BOXES_QUERY = """query StashBoxes {
    configuration { general { stashBoxes { name } } }
}"""
_stash_boxes: Dict[str, Any] = {"at": 0.0, "items": []}


async def _load_stash_boxes() -> List[str]:
    """Names of the configured stash boxes, in configured order; the index in
    that list IS the `stash_box_index` the scrape API expects. Cached on the
    same TTL as the scraper list."""
    now = time.monotonic()
    if now - _stash_boxes["at"] < _PROVIDER_TTL:
        return _stash_boxes["items"]
    items: List[str] = []
    try:
        res = await stash_query(_STASH_BOXES_QUERY)
        boxes = ((((res or {}).get("data") or {}).get("configuration") or {})
                 .get("general") or {}).get("stashBoxes") or []
        items = [b.get("name") or f"StashBox{i}" for i, b in enumerate(boxes)]
    except Exception as e:
        logger.warning(f"stash box lookup failed: {e}")
        items = list(_stash_boxes["items"])
    _stash_boxes.update({"at": now, "items": items})
    return items


def _stashbox_enabled() -> bool:
    raw = runtime.config.get("SCRAPE_STASHBOX_ENABLED")
    if raw is None:
        return bool(getattr(runtime, "SCRAPE_STASHBOX_ENABLED", True))
    return str(raw).lower() != "false"


async def _match_stashbox(provider_key: str):
    """(index, name) when the dialog named one box (`StashDB`, `PMV Stash`),
    None otherwise. Comparison ignores case and inner spaces so either the
    client's spelling or the configured one matches."""
    key = re.sub(r"[\s_\-]+", "", (provider_key or "").strip().lower())
    if not key:
        return None
    for index, name in enumerate(await _load_stash_boxes()):
        norm = re.sub(r"[\s_\-]+", "", (name or "").lower())
        if norm and (norm == key or key in norm):
            return index, name
    return None


async def _stashbox_attempts(entity: str, term: str
                             ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """One `by name` attempt per configured stash box, against the normalised
    query. These ride in the ordinary fan-out: a box answers in the same
    shape any scraper does, and `_payload_relevance` decides where its hits
    land in the list."""
    if not _stashbox_enabled():
        return []
    query = _name_query_from_term(term)
    if not query:
        return []
    boxes = await _load_stash_boxes()
    if not boxes:
        return []

    def make(index: int, name: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        pseudo = {
            "key": f"stashbox{index}", "id": f"stashbox{index}", "name": name,
            "entities": [entity], "entities_any": [entity],
        }
        # `_BOX_MARKER` never reaches the wire (stripped in _scrape); the
        # `query` key marks this as a name search for _is_targeted.
        return pseudo, {_BOX_MARKER: index, "query": query}

    return [make(i, name) for i, name in enumerate(boxes)]


def _numeric_candidates(entity: str, term: str) -> List[Dict[str, Any]]:
    """The configured ID scrapers for this entity, in config order, each with
    the URLs a numeric term would live at ([] when no template matches)."""
    term = (term or "").strip()
    out: List[Dict[str, Any]] = []
    for want in _numeric_scraper_names():
        for p in _providers["items"]:
            if entity not in p.get("entities_any", []):
                continue
            if want not in f"{p.get('name', '')} {p.get('id', '')}".lower():
                continue
            out.append({
                "provider": p,
                "urls": _template_urls_for(p, term),
                "supports_name": entity in p.get("entities", []),
            })
    return out


def _scrape_attempts(entity: str, numeric_id: str, term: str,
                     targets: List[Dict[str, Any]]
                     ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Ordered `(provider, scrape_input)` attempts for one search or refresh.

    Three shapes, most specific first:

    * a **URL term** goes to the scrapers whose declared prefix claims it, via
      `scrapeURL`. Name-searching every scraper with a URL as the keyword is
      what turned a pasted link into 24 seconds of 404s and no results.
    * a **numeric term** is an ID, so the configured ID scrapers are given the
      URL it would live at (`{_URL_MARKER}`), because a FRAGMENT/URL-only
      scraper cannot answer a name lookup — FantiaJp replies "cannot load
      SCENE by name".
    * everything else is the ordinary name search, restricted to scrapers that
      advertise NAME support.

    Deliberately absent: a blind `{scene_id: …}` for the ID scrapers. Stash
    runs that as a *fragment* scrape when the item has no stored URL, and a
    URL-only scraper answers by echoing the fragment back — observed as a
    single "hit" whose title is the file basename. That fake hit used to win
    the fan-out and reach the client as the only result. When the item does
    have a matching stored URL, `_identity_attempts` already covers exactly
    this case — and it does so first, because it is merged ahead of this list.
    """
    id_key = "scene_id" if entity == "scene" else "performer_id"
    seen = set()
    attempts: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    numeric_prefix = 0

    stripped = (term or "").strip()
    if _looks_like_url(stripped):
        claiming = [
            p for p in _providers_any(entity)
            if _patterns_match((p.get("patterns") or {}).get(entity) or [], [stripped])
        ]
        if claiming:
            logger.info("⌕ URL query %r: %d scraper(s) claim it (%s)"
                        % (stripped, len(claiming),
                           ", ".join(p["name"] for p in claiming)))
        else:
            logger.info("⌕ URL query %r: no installed scraper declares it"
                        % stripped)
        return [(p, {_URL_MARKER: stripped}) for p in claiming]

    if _numeric_query(stripped):
        candidates = _numeric_candidates(entity, stripped)
        if candidates:
            logger.info("⌕ numeric query %r: ID scrapers first (%s)"
                        % (stripped,
                           ", ".join(c["provider"]["name"] for c in candidates)))
        else:
            logger.info("⌕ numeric query %r: none of %s is installed for %s "
                        "— falling back to the name search"
                        % (stripped, _numeric_scraper_names(), entity))
        for cand in candidates:
            provider = cand["provider"]
            seen.add(provider["id"])
            for url in cand["urls"]:
                attempts.append((provider, {_URL_MARKER: url}))
        numeric_prefix = len(attempts)

    for provider in targets or []:
        if provider["id"] in seen or entity not in provider.get("entities", []):
            continue
        seen.add(provider["id"])
        attempts.append((provider, {"query": term} if term else {id_key: numeric_id}))

    # Recently-timing-out scrapers go last, but only within the ordinary name
    # search — the configured numeric/ID scrapers keep their priority no
    # matter what. `sort` is stable, so declared order survives in each group.
    head, tail = attempts[:numeric_prefix], attempts[numeric_prefix:]
    tail.sort(key=lambda pair: _is_slow(pair[0]["id"]))
    return head + tail


# --------------------------------------------------------------------------
# Identity attempts — scrape the object from the URLs Stash already holds
# --------------------------------------------------------------------------

_STORED_URLS_QUERY = {
    "scene": """query StoredUrls($id: ID!) { findScene(id: $id) { id urls } }""",
    "performer": """query StoredUrls($id: ID!) { findPerformer(id: $id) { id urls } }""",
}


async def _fetch_stored_urls(entity: str, numeric_id: str) -> List[str]:
    """The URLs Stash already has for this item. A scene imported from Fantia
    carries its fantia.jp post URL; that URL is what Stash's own "Scrape with"
    uses, and it is the most accurate input we can give a scraper."""
    query = _STORED_URLS_QUERY.get(entity)
    if not query or not numeric_id:
        return []
    try:
        res = await stash_query(query, {"id": numeric_id})
    except Exception as e:
        logger.warning(f"stored-url lookup failed for {entity}-{numeric_id}: {e}")
        return []
    node_key = "findScene" if entity == "scene" else "findPerformer"
    node = ((res or {}).get("data") or {}).get(node_key) or {}
    return [u for u in (node.get("urls") or []) if u]


async def _identity_attempts(entity: str, numeric_id: str
                             ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Scrape the object itself, through ONLY the scrapers whose declared URL
    prefixes cover one of its stored URLs.

    Narrowing by URL pattern matters twice: Stash refuses a source with no
    scraper named (`scraper_id or stash_box_index must be set`), and a scene
    carrying `https://fantia.jp/posts/3072923` then resolves to a single
    FantiaJp call instead of a name search fanned out over a dozen scrapers.
    """
    urls = await _fetch_stored_urls(entity, numeric_id)
    if not urls:
        return []
    id_key = "scene_id" if entity == "scene" else "performer_id"
    out: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for provider in _providers_any(entity):
        patterns = (provider.get("patterns") or {}).get(entity) or []
        if _patterns_match(patterns, urls):
            out.append((provider, {id_key: numeric_id}))
    if out:
        logger.info(f"⌕ {entity}-{numeric_id}: stored URL {urls[0]!r} matches "
                    f"{len(out)} scraper(s) ({', '.join(p['name'] for p, _ in out)})")
    else:
        # Worth a line in the log: "the item has a URL but no installed scraper
        # claims it" is a very different problem from "the item has no URL",
        # and both used to be indistinguishable from "no match found".
        logger.info(f"⌕ {entity}-{numeric_id}: stored URL {urls[0]!r} matches no "
                    f"installed scraper "
                    f"({sum(1 for p in _providers_any(entity) if (p.get('patterns') or {}).get(entity))}"
                    f"/{len(_providers_any(entity))} declare a URL pattern)")
    return out


def _merge_attempts(*groups: List[Tuple[Dict[str, Any], Dict[str, Any]]]
                    ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Concatenate attempt lists, dropping duplicate (scraper, input) pairs.
    Earlier groups keep their priority; order within a group is preserved."""
    merged: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    seen = set()
    for group in groups:
        for provider, scrape_input in group:
            fingerprint = (provider["id"], json.dumps(scrape_input, sort_keys=True))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            merged.append((provider, scrape_input))
    return merged


# --------------------------------------------------------------------------
# Concurrent fan-out
# --------------------------------------------------------------------------

# Each scraper is an independent third-party site, so they are queried at the
# same time rather than one after another. Serially, a single hanging site
# (AdultTime, every time, at 20s) consumed the whole per-search budget and the
# scrapers that could actually answer never got a turn — which is what made
# the app's Identify come back empty after 25 seconds.
_FAN_OUT_CONCURRENCY = 6

# How many targeted hits may survive ranking. An id batch is small — a handful
# of URL shapes for one number — so this guards against a scraper that returns
# many rows rather than expressing a real limit.
_MAX_RANKED_RESULTS = 3


def _is_targeted(scrape_input: Dict[str, Any]) -> bool:
    """Identity/URL attempts name an exact source; a name search guesses.

    The distinction is what the winner is chosen by: a generic name search can
    answer in 50ms with something unrelated, and it must not be allowed to
    outrun the targeted scrape that is still in flight.
    """
    return "query" not in scrape_input


def _payload_urls(payload: Dict[str, Any]) -> List[str]:
    out = []
    single = payload.get("url")
    if isinstance(single, str) and single:
        out.append(single)
    many = payload.get("urls")
    if isinstance(many, list):
        out.extend(u for u in many if u)
    return out


def _is_empty_payload(payload: Dict[str, Any]) -> bool:
    """True when a payload carries nothing the client could display.

    `scrapeURL` answers with an empty *shell* — not null — when a scraper
    claims the URL but finds nothing at it (a post id that does not exist).
    Live evidence for `https://fantia.jp/posts/99999999`:

        title=None code=None urls=None studio=None performers=None tags=[]

    Left in, that becomes a candidate with no name at all in the Identify
    dialog, which reads as the search having produced garbage.
    """
    if payload.get("title") or payload.get("name") or payload.get("code"):
        return False
    if _payload_urls(payload):
        return False
    return not any(payload.get(k) for k in
                   ("studio", "performers", "tags", "images", "image"))


def _is_fragment_echo(scrape_input: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    """True when a payload is just the fragment we handed in, handed back.

    A URL-only scraper given a fragment cannot fetch anything, so what it
    returns carries no URL at all — observed as a single "hit" whose title is
    the file basename. Two narrower conditions keep this from ever firing on a
    real result: only fragment attempts are considered, and the title has to
    look like a filename.
    """
    if _URL_MARKER in scrape_input or "query" in scrape_input:
        return False
    if _payload_urls(payload):
        return False
    title = (payload.get("title") or payload.get("name") or "").strip()
    return bool(_MEDIA_EXT.search(title))


def _is_unusable(scrape_input: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    return _is_fragment_echo(scrape_input, payload) or _is_empty_payload(payload)


# --------------------------------------------------------------------------
# Relevance — telling apart two objects that share one id
# --------------------------------------------------------------------------

# Latin digits/letters plus kana and CJK: enough to compare a file's own
# words against a scraped title without pulling in a tokenizer.
_TOKEN_RE = re.compile(r"[0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff]+")


def _tokens(text: str) -> set:
    out = set()
    for t in _TOKEN_RE.findall(text or ""):
        out.add(t.lower())
        # A filename joins performer words (`LyaCutie`) where a stash box
        # keeps them apart (`Lya Cutie`); indexing the camelCase split too is
        # what lets the two sides of that comparison intersect at all.
        if any(c.isupper() for c in t[1:]):
            out.update(w.lower() for w in _CAMEL_RE.split(t) if w)
    return out


def _payload_relevance(payload: Dict[str, Any], seed_text: str) -> int:
    """How many of the *item's own* words also appear in a scraped payload.

    An id-based source can return a real, well-formed object that has nothing
    to do with this file, purely because the number matched. There is no
    error to catch in that case — Fantia answers 200 for both shapes of
    1006291 — so the only remaining signal is whether the answer looks like
    this item: the file says `buena-320` and the product's code says
    `buena-320`, while the unrelated post offers `FANTIA-1006291`.

    Used for *ranking only*, never for dropping: the caller keeps every hit
    and merely sorts by this score, so an unrankable result still reaches the
    client, just further down the list. A zero for everything therefore
    degrades to plain attempt order.
    """
    seed = _tokens(seed_text)
    if not seed:
        return 0
    parts: List[str] = []
    for key in ("title", "name", "code", "details"):
        value = payload.get(key)
        if isinstance(value, str):
            parts.append(value)
    for value in payload.get("urls") or []:
        if isinstance(value, str):
            parts.append(value)
    for key in ("performers", "tags"):
        for row in payload.get(key) or []:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                parts.append(row["name"])
    studio = payload.get("studio")
    if isinstance(studio, dict) and isinstance(studio.get("name"), str):
        parts.append(studio["name"])
    return len(seed & _tokens(" ".join(parts)))


async def _fan_out_within(entity: str,
                          attempts: List[Tuple[Dict[str, Any], Dict[str, Any]]],
                          deadline: float,
                          seed_text: str = ""
                          ) -> List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """Run every attempt concurrently; return the targeted hits, best first.

    Priority is honoured against *targeted* attempts only. As soon as any
    scraper answers, the response goes out — except while a targeted
    identity/URL attempt is still running, because that is the scrape known to
    be about this exact item. Letting a generic name search win that race is
    how a junk result became the only thing the dialog showed: FantiaJp needs
    ~3s for the real post, a name search answers in microseconds with
    something unrelated.

    Targeted hits are then ranked against `seed_text` (the item's own words)
    and returned most relevant first. That ordering matters because an id
    batch is genuinely several alternatives for one number and several of them
    may succeed: Fantia answers 200 for both `/posts/<id>` and
    `/products/<id>`, which are unrelated objects sharing the id. Whichever
    finished first used to win outright; ranking puts the one matching this
    file on top while the other stays visible below it.

    Hits that name the same source URL collapse to one — see the note at the
    ranking step. Distinct objects therefore stay distinct while the same
    object reached twice is listed once.

    A targeted attempt is itself bounded by `_attempt_timeout()`, so the wait
    cannot outlive the budget.
    """
    if not attempts:
        return []
    gate = asyncio.Semaphore(_FAN_OUT_CONCURRENCY)

    async def run(provider: Dict[str, Any], scrape_input: Dict[str, Any]):
        async with gate:
            return provider, await _scrape_timed(entity, provider["id"], scrape_input)

    # Keyed by task and carrying the attempt index: one scraper can appear
    # twice (its stored URL and a synthesized one), so the scraper object is
    # not a unique key. The attempt's input travels along for the win check.
    pending = {
        asyncio.create_task(run(provider, scrape_input)): (provider, index, scrape_input)
        for index, (provider, scrape_input) in enumerate(attempts)
    }
    winners: Dict[int, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}
    holding_logged = False
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.info(f"⌕ budget spent with {len(pending)} scraper(s) still "
                            "running — returning what we have")
                break
            done, _ = await asyncio.wait(set(pending), timeout=remaining,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for task in done:
                provider, index, scrape_input = pending.pop(task)
                if task.cancelled():
                    continue
                failure = task.exception()
                if failure is not None:
                    logger.warning(f"scrape {entity} via {provider['name']} "
                                   f"raised: {failure}")
                    continue
                _provider, payloads = task.result()
                kept = [p for p in payloads if not _is_unusable(scrape_input, p)]
                if len(kept) != len(payloads):
                    logger.info(f"⌕ {entity}: dropped {len(payloads) - len(kept)} "
                                f"empty/echoed result(s) from {provider['name']}")
                if kept:
                    winners[index] = (provider, kept)
            if not winners:
                continue
            best = min(winners)
            # Wait for the whole *targeted* set, not just the earlier attempts
            # in it: an id batch is a set of alternatives for one number and
            # more than one can succeed (Fantia /posts/ and /products/ both
            # answer 200). Only once they have all landed can relevance decide
            # which of them actually describes this item.
            if not any(_is_targeted(scrape_input)
                       for _p, _i, scrape_input in pending.values()):
                break
            if not holding_logged:
                holding_logged = True
                logger.info(f"⌕ {entity}: holding {winners[best][0]['name']} while a "
                            "targeted attempt is still running")
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if not winners:
        return []
    ranked = sorted(
        winners.items(),
        key=lambda kv: (-_payload_relevance((kv[1][1] or [{}])[0], seed_text),
                        kv[0]),
    )
    # Two attempts can legitimately open the *same* page: the item's stored URL
    # and a synthesized shape of the same number both resolve to one object for
    # that scraper (a scene carrying `fantia.jp/posts/1006291` gets scraped
    # twice — once by id, once by the built `/posts/` URL — and Fantia answers
    # with the same post both times). Ranking alone would then list that object
    # twice, so a hit whose payload names a source URL already claimed by a
    # better-ranked hit is dropped. Hits with no URL carry nothing to compare
    # and are always kept.
    ordered: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    claimed_urls = set()
    for index, (provider, payloads) in ranked:
        if not _is_targeted(attempts[index][1]):
            continue
        url = next(iter(_payload_urls((payloads or [{}])[0])), "")
        if url:
            if url in claimed_urls:
                continue
            claimed_urls.add(url)
        ordered.append((provider, payloads))
    if not ordered:
        # Nothing targeted landed — every hit is a name-search guess, so
        # rank them the same way and hand back the most plausible few. The
        # stash boxes matter here: an all-providers Identify against a
        # filename usually has no stored URL for any scraper, yet StashDB
        # can still name the performer and half a dozen of her scenes.
        ordered = [
            kv[1] for kv in sorted(
                winners.items(),
                key=lambda kv: (-_payload_relevance((kv[1][1] or [{}])[0], seed_text),
                                kv[0]),
            )
        ]
    ordered = ordered[:_MAX_RANKED_RESULTS]
    provider, payloads = ordered[0]
    extra = (f"; kept {len(ordered) - 1} more targeted hit(s) ranked below"
             if len(ordered) > 1 else "")
    logger.info(f"⌕ {entity}: {provider['name']} answered "
                f"({len(payloads)} result(s); {len(attempts)} scraper(s) fanned out{extra})")
    return ordered


# --------------------------------------------------------------------------
# Scrape primitives
# --------------------------------------------------------------------------

# Field lists are verified against the live schema — Stash rejects the WHOLE
# document (422) if one field is unknown, which silently turns every scrape
# into "no results". ScrapedScene has no production_date; ScrapedPerformer has
# no singular `image` (only `images`).
#
# They are shared between the two entry points on purpose: `scrapeSingleScene`
# and `scrapeURL` both hand back a ScrapedScene/ScrapedPerformer, and a field
# fixed in one document but forgotten in the other fails exactly the same
# silent way.
_SCENE_FIELDS = """
            title code details director date urls image
            studio { name }
            performers { name }
            tags { name }
        """

_PERFORMER_FIELDS = """
            name disambiguation gender birthdate death_date ethnicity country
            eye_color height weight measurements fake_tits penis_length circumcised
            career_start career_end tattoos piercings aliases details urls
            images remote_site_id
            tags { name }
        """

_SCRAPE_QUERY = {
    "scene": ("""query ScrapeSingleScene($source: ScraperSourceInput!, $input: ScrapeSingleSceneInput!) {
        scrapeSingleScene(source: $source, input: $input) {""" + _SCENE_FIELDS + """}
    }"""),
    "performer": ("""query ScrapeSinglePerformer($source: ScraperSourceInput!, $input: ScrapeSinglePerformerInput!) {
        scrapeSinglePerformer(source: $source, input: $input) {""" + _PERFORMER_FIELDS + """}
    }"""),
}

# The URL entry point. `scrapeSingleScene` is the wrong door for a
# FRAGMENT/URL-only scraper: a `scene_input` payload is run as a *fragment*
# scrape and comes back "scraper operation not supported", while `by name` is
# refused outright. `scrapeURL` takes the URL itself and lets Stash route it to
# whichever installed scraper declares a matching prefix — which is how the
# Stash UI's own "Scrape with → URL" works.
#
# `scrapeURL` returns the `ScrapedContent` UNION, so the concrete fields have
# to sit inside an inline fragment. Selecting them straight off the union is
# rejected by Stash with a bare 400 that carries no GraphQL error body, which
# makes it look like a transport fault rather than a query fault.
_URL_SCRAPE_QUERY = {
    "scene": ("""query ScrapeURL($url: String!) {
        scrapeURL(url: $url, ty: SCENE) {
            __typename
            ... on ScrapedScene {""" + _SCENE_FIELDS + """}
        }
    }"""),
    "performer": ("""query ScrapeURL($url: String!) {
        scrapeURL(url: $url, ty: PERFORMER) {
            __typename
            ... on ScrapedPerformer {""" + _PERFORMER_FIELDS + """}
        }
    }"""),
}

_URL_TYPENAME = {"scene": "ScrapedScene", "performer": "ScrapedPerformer"}

# Internal marker. An attempt carrying it is executed through `scrapeURL`
# instead of `scrapeSingleScene`; it never reaches the wire as a GraphQL input
# field. The leading underscores keep it from colliding with a real field name.
_URL_MARKER = "__url__"

# Internal marker for a stash-box source. An attempt carrying it runs through
# `scrapeSingleScene`/`scrapeSinglePerformer` with
# `source = {stash_box_index: N}` — the other half of `ScraperSourceInput`,
# next to `scraper_id`. Also stripped before the wire, like `_URL_MARKER`.
_BOX_MARKER = "__box__"


async def _scrape_by_url(entity: str, url: str) -> List[Dict[str, Any]]:
    """Scrape a bare URL, letting Stash route it to a matching scraper.

    This is the only entry point that can turn "the number 4064942" into
    metadata when nothing in the library points at that post yet:
    `scrapeSingleScene` with a `scene_input` payload is executed as a fragment
    scrape and rejected (`scraper operation not supported`), and `by name` is
    refused by URL-only scrapers outright. Stash returns null when no installed
    scraper claims the URL, so an unmatched URL is a cheap no-op rather than an
    error.
    """
    query = _URL_SCRAPE_QUERY.get(entity)
    if not query or not url:
        return []
    try:
        res = await stash_query(query, {"url": url})
    except Exception as e:
        logger.warning(f"url scrape {entity} failed ({url!r}): {e}")
        return []
    node = ((res or {}).get("data") or {}).get("scrapeURL")
    if not node or node.get("__typename") != _URL_TYPENAME.get(entity):
        errs = (res or {}).get("errors")
        if errs:
            logger.warning(f"url scrape {entity} returned errors: {errs}")
        return []
    return [node]


async def _scrape(entity: str, scraper_id: Optional[str], scrape_input: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run one scrape. `scraper_id=None` lets Stash pick (used for the
    fingerprint pass, which is the most accurate match when the scraper
    supports FRAGMENT)."""
    query = _SCRAPE_QUERY.get(entity)
    if not query:
        return []
    url = scrape_input.get(_URL_MARKER) if isinstance(scrape_input, dict) else None
    if url:
        # `scrapeURL` picks the scraper by URL prefix, so the id on the attempt
        # is only used for reporting which provider answered.
        return await _scrape_by_url(entity, url)
    box = scrape_input.get(_BOX_MARKER) if isinstance(scrape_input, dict) else None
    if box is not None:
        source = {"stash_box_index": int(box)}
    elif scraper_id:
        source = {"scraper_id": scraper_id}
    else:
        # `ScraperSourceInput` with every field unset is rejected outright —
        # "input error: scraper_id or stash_box_index must be set" — so there
        # is no such thing as "let Stash pick". Whatever called us has to name
        # the scraper (see _identity_attempts, which picks it by URL pattern).
        logger.warning(f"scrape {entity}: no scraper selected — skipped")
        return []
    # The internal markers travel on the attempt, never on the wire.
    wire_input = {k: v for k, v in scrape_input.items() if k != _BOX_MARKER}
    try:
        res = await stash_query(query, {"source": source, "input": wire_input})
    except Exception as e:  # stash_query swallows most errors, but be safe
        logger.warning(f"scrape {entity} failed (source={scraper_id!r}): {e}")
        return []
    data = ((res or {}).get("data") or {})
    results = data.get("scrapeSingleScene") if entity == "scene" else data.get("scrapeSinglePerformer")
    if not results:
        # Surface a real error message if Stash returned one, so a broken
        # scraper isn't silently indistinguishable from "no match".
        errs = (res or {}).get("errors")
        if errs:
            logger.warning(f"scrape {entity} returned errors: {errs}")
        return []
    return [r for r in results if r]


async def _auto_match(entity: str, numeric_id: str, seed_text: str,
                      rank_text: str = "") -> Optional[Dict[str, Any]]:
    """Best-effort auto-match: the item's own stored URL first, then the
    ordered attempt list (configured ID scrapers first when the seed is a
    number), then the name search.

    `seed_text` is the keyword to search with; `rank_text` is the item's own
    words to rank the answers against (see `_fetch_rank_text`). They are
    separate parameters because they are separate things — the keyword has to
    stay short and clean, the ranking seed wants every word the item carries.
    When no `rank_text` is supplied the keyword is used, which is exactly the
    old behaviour.
    """
    await _load_providers()

    attempts = _merge_attempts(
        await _identity_attempts(entity, numeric_id),
        _scrape_attempts(entity, numeric_id, seed_text, _providers_for(entity)),
        await _stashbox_attempts(entity, rank_text or seed_text),
    )
    deadline = time.monotonic() + _search_budget()
    for provider, payloads in await _fan_out_within(entity, attempts, deadline,
                                                    seed_text=rank_text or seed_text):
        if payloads:
            logger.info(f"⌕ matched {entity}-{numeric_id} via {provider['name']}")
            return payloads[0]
    return None


# --------------------------------------------------------------------------
# Search-result handoff
#
# The client fetches a result list, later POSTs the chosen entry back. We
# park the full scraped payload behind a short token carried in
# ProviderIds["Stash"], so Apply is exact instead of a re-scrape guess.
# --------------------------------------------------------------------------

_RESULT_MAX = 500
_results: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()


def _store_result(entity: str, provider: Dict[str, Any], payload: Dict[str, Any],
                  numeric_id: str = "") -> str:
    token = f"{entity[:2]}-{uuid.uuid4().hex[:10]}"
    ttl = _int("SCRAPE_RESULT_TTL_SECONDS", 1800)
    _results[token] = (time.monotonic() + ttl, {
        "entity": entity,
        "numeric_id": numeric_id,
        "provider": provider,
        "payload": payload,
    })
    now = time.monotonic()
    for key in [k for k, (expires, _) in _results.items() if expires <= now]:
        _results.pop(key, None)
    while len(_results) > _RESULT_MAX:
        _results.popitem(last=False)
    return token


def _take_result(token: str) -> Optional[Dict[str, Any]]:
    """Fetch without consuming — a user may apply, then adjust and apply
    again from the same dialog."""
    if not token:
        return None
    hit = _results.get(token)
    if not hit:
        return None
    expires, entry = hit
    if expires <= time.monotonic():
        _results.pop(token, None)
        return None
    return entry


# --------------------------------------------------------------------------
# Jellyfin <-> scraped payload shaping
# --------------------------------------------------------------------------

def _year_of(date_str: str) -> Optional[int]:
    if not date_str:
        return None
    match = re.match(r"\s*(\d{4})", str(date_str))
    return int(match.group(1)) if match else None


def _iso_date(date_str: str) -> Optional[str]:
    """Jellyfin wants an ISO-8601 timestamp; Stash stores YYYY-MM-DD."""
    year = _year_of(date_str)
    if not year:
        return None
    match = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", str(date_str))
    month, day = (match.group(2), match.group(3)) if match else ("01", "01")
    return f"{year:04d}-{month}-{day}T00:00:00.0000000Z"


def _image_url(payload: Dict[str, Any], entity: str) -> str:
    """Pick a usable image reference for the Jellyfin result card. Both
    http(s) URLs and base64 data URLs are passed through; anything
    absurdly large is dropped rather than shipped to the client."""
    candidates: List[str] = []
    if entity == "performer":
        candidates.extend([i for i in (payload.get("images") or []) if i])
        if payload.get("image"):
            candidates.append(payload["image"])
    elif payload.get("image"):
        candidates.append(payload["image"])
    for value in candidates:
        text = str(value)
        if len(text) <= 400_000:
            return text
    return ""


def _to_result(entity: str, provider: Dict[str, Any], payload: Dict[str, Any], token: str) -> Dict[str, Any]:
    """Shape one scraped record as a Jellyfin `RemoteSearchResult`."""
    if entity == "scene":
        name = payload.get("title") or payload.get("code") or ""
        year = _year_of(payload.get("date") or payload.get("production_date"))
        premiere = _iso_date(payload.get("date") or payload.get("production_date"))
        overview = payload.get("details") or ""
    else:
        name = payload.get("name") or ""
        disambiguation = payload.get("disambiguation")
        if disambiguation:
            name = f"{name} ({disambiguation})"
        year = _year_of(payload.get("birthdate"))
        premiere = _iso_date(payload.get("birthdate"))
        overview = payload.get("details") or ""

    result: Dict[str, Any] = {
        "Name": name,
        "ProviderIds": {"Stash": token},
        "ProductionYear": year,
        "PremiereDate": premiere,
        "IndexNumber": None,
        "IndexNumberEnd": None,
        "ParentIndexNumber": None,
        "ImageUrl": _image_url(payload, entity),
        "SearchProviderName": provider["name"],
        "SearchProviderPluginName": provider["name"],
        "Overview": overview,
        "Artists": [],
        "AlbumArtist": None,
        "Album": None,
    }
    # A stable secondary id makes the row recognisable in the dialog even
    # for users who never look at the provider list.
    remote_id = payload.get("remote_site_id")
    if remote_id:
        result["ProviderIds"][provider["name"]] = str(remote_id)
    return result


# --------------------------------------------------------------------------
# Write-back — scraped payload -> Stash mutations
# --------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _to_int(value: Any) -> Optional[int]:
    """Scrapers are loose with units ("170", "170 cm", 170). Take the
    leading number and drop the rest rather than discarding the value."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = _NUMBER_RE.search(str(value))
    if not match:
        return None
    try:
        return int(float(match.group()))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER_RE.search(str(value))
    if not match:
        return None
    try:
        return float(match.group())
    except (TypeError, ValueError):
        return None


_GENDERS = {
    "MALE": "MALE",
    "FEMALE": "FEMALE",
    "TRANSGENDER_MALE": "TRANSGENDER_MALE",
    "TRANSGENDER_FEMALE": "TRANSGENDER_FEMALE",
    "INTERSEX": "INTERSEX",
    "NON_BINARY": "NON_BINARY",
}


def _map_gender(value: Any) -> Optional[str]:
    if not value:
        return None
    return _GENDERS.get(re.sub(r"[\s\-]+", "_", str(value).strip()).upper())


def _map_circumcised(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lower()
    if text in ("cut", "yes", "true", "1"):
        return "CUT"
    if text in ("uncut", "no", "false", "0"):
        return "UNCUT"
    return None


def _names(items: Any) -> List[str]:
    out = []
    for entry in items or []:
        if isinstance(entry, dict):
            name = entry.get("name")
        else:
            name = entry
        if name:
            out.append(str(name))
        if len(out) >= _MAX_RELATIONSHIPS:
            break
    return out


async def _resolve_tag_ids(names: List[str]) -> List[str]:
    ids = []
    for name in names:
        tag_id = await get_or_create_tag(name)
        if tag_id:
            ids.append(tag_id)
    return ids


async def _resolve_performer_ids(names: List[str]) -> List[str]:
    ids = []
    for name in names:
        try:
            found = await stash_query(
                """query FindPerformerByName($name: String!) {
                    findPerformers(performer_filter: {name: {value: $name, modifier: EQUALS}},
                                   filter: {per_page: 1}) {
                        performers { id name }
                    }
                }""",
                {"name": name},
            )
            performers = ((found or {}).get("data") or {}).get("findPerformers", {}).get("performers") or []
            if performers:
                ids.append(performers[0]["id"])
                continue
            created = await stash_query(
                """mutation PerformerCreate($input: PerformerCreateInput!) {
                    performerCreate(input: $input) { id }
                }""",
                {"input": {"name": name}},
            )
            performer = ((created or {}).get("data") or {}).get("performerCreate")
            if performer:
                ids.append(performer["id"])
        except Exception as e:
            logger.warning(f"performer resolve failed for {name!r}: {e}")
    return ids


async def _resolve_studio_id(name: str) -> Optional[str]:
    if not name:
        return None
    try:
        found = await stash_query(
            """query FindStudioByName($name: String!) {
                findStudios(studio_filter: {name: {value: $name, modifier: EQUALS}},
                            filter: {per_page: 1}) {
                    studios { id name }
                }
            }""",
            {"name": name},
        )
        studios = ((found or {}).get("data") or {}).get("findStudios", {}).get("studios") or []
        if studios:
            return studios[0]["id"]
        created = await stash_query(
            """mutation StudioCreate($input: StudioCreateInput!) {
                studioCreate(input: $input) { id }
            }""",
            {"input": {"name": name}},
        )
        studio = ((created or {}).get("data") or {}).get("studioCreate")
        return studio["id"] if studio else None
    except Exception as e:
        logger.warning(f"studio resolve failed for {name!r}: {e}")
        return None


async def _apply_scene(numeric_id: str, payload: Dict[str, Any], replace_images: bool) -> bool:
    update: Dict[str, Any] = {"id": numeric_id}

    if payload.get("title"):
        update["title"] = payload["title"]
    if payload.get("code"):
        update["code"] = payload["code"]
    if payload.get("details"):
        update["details"] = payload["details"]
    if payload.get("director"):
        update["director"] = payload["director"]
    date_value = payload.get("date") or payload.get("production_date")
    if date_value:
        update["date"] = date_value
    urls = [u for u in (payload.get("urls") or []) if u]
    if urls:
        update["urls"] = urls

    if _flag("SCRAPE_APPLY_IMAGES", True) and replace_images and payload.get("image"):
        update["cover_image"] = payload["image"]

    if _flag("SCRAPE_APPLY_RELATIONSHIPS", True):
        studio_name = ((payload.get("studio") or {}) or {}).get("name")
        studio_id = await _resolve_studio_id(studio_name) if studio_name else None
        if studio_id:
            update["studio_id"] = studio_id
        performer_names = _names(payload.get("performers"))
        if performer_names:
            performer_ids = await _resolve_performer_ids(performer_names)
            if performer_ids:
                update["performer_ids"] = performer_ids
        tag_names = _names(payload.get("tags"))
        if tag_names:
            tag_ids = await _resolve_tag_ids(tag_names)
            if tag_ids:
                update["tag_ids"] = tag_ids

    if len(update) == 1:
        logger.info(f"⌕ Scrape for scene-{numeric_id} produced no applicable fields")
        return False

    res = await stash_query(
        """mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }""",
        {"input": update},
    )
    ok = bool(((res or {}).get("data") or {}).get("sceneUpdate"))
    if ok:
        logger.info(f"⌕ Applied scraped metadata to scene-{numeric_id}: {sorted(k for k in update if k != 'id')}")
    else:
        logger.warning(f"⌕ sceneUpdate failed for scene-{numeric_id}: {(res or {}).get('errors')}")
    return ok


async def _apply_performer(numeric_id: str, payload: Dict[str, Any], replace_images: bool) -> bool:
    update: Dict[str, Any] = {"id": numeric_id}

    scalar_map = (
        ("name", "name"),
        ("disambiguation", "disambiguation"),
        ("birthdate", "birthdate"),
        ("death_date", "death_date"),
        ("ethnicity", "ethnicity"),
        ("country", "country"),
        ("eye_color", "eye_color"),
        ("hair_color", "hair_color"),
        ("measurements", "measurements"),
        ("fake_tits", "fake_tits"),
        ("career_start", "career_start"),
        ("career_end", "career_end"),
        ("tattoos", "tattoos"),
        ("piercings", "piercings"),
        ("details", "details"),
    )
    for source_key, stash_key in scalar_map:
        if payload.get(source_key):
            update[stash_key] = payload[source_key]

    for source_key, stash_key, caster in (
        ("height", "height_cm", _to_int),
        ("weight", "weight", _to_int),
        ("penis_length", "penis_length", _to_float),
    ):
        cast = caster(payload.get(source_key))
        if cast is not None:
            update[stash_key] = cast

    gender = _map_gender(payload.get("gender"))
    if gender:
        update["gender"] = gender
    circumcised = _map_circumcised(payload.get("circumcised"))
    if circumcised:
        update["circumcised"] = circumcised

    # ScrapedPerformer.aliases is a single comma-delimited string.
    aliases = [a.strip() for a in str(payload.get("aliases") or "").split(",") if a.strip()]
    if aliases:
        update["alias_list"] = aliases[:_MAX_RELATIONSHIPS]

    urls = [u for u in (payload.get("urls") or []) if u]
    if urls:
        update["urls"] = urls

    if _flag("SCRAPE_APPLY_IMAGES", True) and replace_images:
        image = _image_url(payload, "performer")
        if image:
            update["image"] = image

    if _flag("SCRAPE_APPLY_RELATIONSHIPS", True):
        tag_names = _names(payload.get("tags"))
        if tag_names:
            tag_ids = await _resolve_tag_ids(tag_names)
            if tag_ids:
                update["tag_ids"] = tag_ids

    if len(update) == 1:
        logger.info(f"⌕ Scrape for performer-{numeric_id} produced no applicable fields")
        return False

    res = await stash_query(
        """mutation PerformerUpdate($input: PerformerUpdateInput!) { performerUpdate(input: $input) { id } }""",
        {"input": update},
    )
    ok = bool(((res or {}).get("data") or {}).get("performerUpdate"))
    if ok:
        logger.info(f"⌕ Applied scraped metadata to performer-{numeric_id}: {sorted(k for k in update if k != 'id')}")
    else:
        logger.warning(f"⌕ performerUpdate failed for performer-{numeric_id}: {(res or {}).get('errors')}")
    return ok


# A scene whose metadata was never scraped has no real title: Stash either
# returns "" or the file's basename. Feeding either to a scraper is a
# guaranteed miss — the term lands in the scraper's URL as
# `…/SLAVE TRAINING.mp4` (404) instead of as a title.
_MEDIA_EXT = re.compile(
    r"\.(?:mp4|mkv|wmv|avi|mov|m4v|ts|m2ts|flv|webm|rmvb|mpg|mpeg|iso)$", re.I)
_LEADING_HASH = re.compile(r"^[0-9a-f]{8,}[_\-]+", re.I)


def _clean_search_term(raw: str) -> str:
    """Make a stored title usable as a search term.

    Two conservative edits: drop a trailing media extension and a leading
    content hash. A genuine title never ends in `.mp4` and never starts with
    8+ hex characters plus a separator, so real titles pass through untouched
    (`"❤ スターレ〇ル …"` -> unchanged) while `214ed862_quest-kanu.mp4` ->
    `quest-kanu`.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    text = _MEDIA_EXT.sub("", text)
    text = _LEADING_HASH.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_seed_text(entity: str, numeric_id: str) -> str:
    """A search term for the text pass.

    Prefers the title/name, falls back to the file basename, and cleans the
    result either way — an un-scraped scene stores its *filename* as the
    title, and passing `"2B MAIDBJ, … TRAINING.mp4"` to a scraper yields a
    404 URL rather than a hit.
    """
    try:
        if entity == "scene":
            res = await stash_query(
                """query FindSceneSeed($id: ID!) { findScene(id: $id) {
                     id title files { basename } } }""",
                {"id": numeric_id},
            )
            scene = ((res or {}).get("data") or {}).get("findScene") or {}
            raw = scene.get("title") or ""
            if not _clean_search_term(raw):
                raw = ((scene.get("files") or [{}])[0] or {}).get("basename") or ""
            return _clean_search_term(raw)
        res = await stash_query(
            """query FindPerformerSeed($id: ID!) { findPerformer(id: $id) { id name } }""",
            {"id": numeric_id},
        )
        performer = ((res or {}).get("data") or {}).get("findPerformer") or {}
        return performer.get("name") or ""
    except Exception as e:
        logger.warning(f"seed lookup failed for {entity}-{numeric_id}: {e}")
        return ""


_RANK_TEXT_QUERY = {
    "scene": ("query RankText($id: ID!) { findScene(id: $id) "
              "{ id title files { basename } } }"),
    "performer": "query RankText($id: ID!) { findPerformer(id: $id) { id name } }",
}


async def _fetch_rank_text(entity: str, numeric_id: str) -> str:
    """Every word this item is known by, for relevance ranking ONLY.

    Deliberately not `_fetch_seed_text`, because the two want opposite things.
    `_fetch_seed_text` returns a single cleaned term since it doubles as a
    *search keyword*, where a long string hurts. Ranking wants as many of the
    item's own words as possible: when two sources answer with well-formed
    metadata about *different* objects that share one number, the only signal
    left is whether an answer mentions anything this item mentions.

    scene-118 is what forced the split. Its stored title is the bare number
    `1006291` — which every Fantia shape echoes back inside its URL — so a
    title-only seed scores both candidates identically and the unrelated
    post wins on attempt order. The file's basename, `buena-320s.mp4`, is the
    word that actually matches the product (`buena-320`), so the title and
    every filename are fed in together.

    Failures degrade to "" — no ranking signal, so the caller keeps plain
    attempt order — and never to an error.
    """
    query = _RANK_TEXT_QUERY.get(entity)
    if not query or not numeric_id:
        return ""
    try:
        res = await stash_query(query, {"id": numeric_id})
    except Exception as e:
        logger.warning(f"rank-text lookup failed for {entity}-{numeric_id}: {e}")
        return ""
    node_key = "findScene" if entity == "scene" else "findPerformer"
    node = ((res or {}).get("data") or {}).get(node_key) or {}
    parts = [node.get("title"), node.get("name")]
    for entry in node.get("files") or []:
        if isinstance(entry, dict):
            parts.append(entry.get("basename"))
    return " ".join(p for p in parts if isinstance(p, str) and p)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

async def _json_body(request) -> Dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


async def endpoint_metadata_editor(request):
    """`GET /Items/{id}/MetadataEditor` — the provider list that populates
    the client's Identify dialog."""
    item_id = request.path_params.get("item_id", "")
    entity, _ = _classify(item_id)

    infos: List[Dict[str, Any]] = []
    if _scraping_enabled() and entity in _SUPPORTED_ENTITIES:
        await _load_providers()
        content_type = "Video" if entity == "scene" else "Person"
        infos = [
            {"Name": p["name"], "Key": p["key"], "Type": content_type, "UrlFormatString": None}
            for p in _providers_for(entity)
        ]

    content_type = "Video" if entity == "scene" else "Person" if entity == "performer" else "Unknown"
    return JSONResponse({
        "ContentType": content_type,
        "ContentTypeOptions": [],
        "ExternalIdInfos": infos,
        "ParentalRatingOptions": [],
        "Countries": [],
        "Cultures": [],
    })


async def _named_provider_attempts(entity: str, provider: Dict[str, Any],
                                   term: str, numeric_id: str
                                   ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """The single attempt list for one explicitly chosen scraper.

    A numeric or URL term becomes a URL scrape when the scraper declares a
    matching prefix — otherwise FantiaJp answers "cannot load SCENE by name"
    and the dialog shows nothing. A URL-only scraper with no term at all can
    only work from the item's own stored URL, so that is the fallback rather
    than a fragment scrape that would just echo the filename back.
    """
    id_key = "scene_id" if entity == "scene" else "performer_id"
    term = (term or "").strip()
    patterns = (provider.get("patterns") or {}).get(entity) or []

    urls: List[str] = []
    if _looks_like_url(term):
        urls = [term]
    elif _numeric_query(term):
        urls = _template_urls_for(provider, term)
    if urls:
        claimed = [u for u in urls
                   if not patterns or _patterns_match(patterns, [u])]
        if claimed:
            return [(provider, {_URL_MARKER: u}) for u in claimed]
        logger.info(f"⌕ {provider['name']} does not declare {urls[0]!r} — "
                    "falling back to its name search")

    if not term and entity not in provider.get("entities", []):
        stored = await _fetch_stored_urls(entity, numeric_id)
        if _patterns_match(patterns, stored):
            return [(provider, {id_key: numeric_id})]
        logger.info(f"⌕ {provider['name']} cannot search {entity} by name and "
                    f"{entity}-{numeric_id or '-'} stores no URL it claims — "
                    "give it a number or a URL")
        return []

    return [(provider, {"query": term} if term else {id_key: numeric_id})]


async def _search(entity: str, numeric_id: str, provider_key: str, term: str) -> List[Dict[str, Any]]:
    """Shared body of the two remote-search shapes."""
    if not _scraping_enabled() or entity not in _SUPPORTED_ENTITIES:
        return []
    await _load_providers()

    provider = _resolve_provider(provider_key, entity)
    query_term = term or (await _fetch_seed_text(entity, numeric_id) if numeric_id else "")

    if provider:
        attempts = await _named_provider_attempts(entity, provider, query_term, numeric_id)
    elif _stashbox_enabled() and (box_hit := await _match_stashbox(provider_key)) is not None:
        # The dialog was pointed at one stash box by name ("StashDB") — give
        # it the same normalised query the all-providers path would.
        index, name = box_hit
        pseudo = {"key": f"stashbox{index}", "id": f"stashbox{index}",
                  "name": name, "entities": [entity], "entities_any": [entity]}
        attempts = [(pseudo, {_BOX_MARKER: index,
                              "query": _name_query_from_term(query_term) or query_term})]
        if not attempts[0][1]["query"]:
            return []
    elif provider_key and provider_key.strip().lower() not in ("", "all", "default", "stash"):
        # Unknown provider name — don't silently search everything.
        logger.info(f"remote search: unknown provider {provider_key!r} for {entity}")
        return []
    else:
        # The item's own stored URL first (see _identity_attempts) — only the
        # scraper declaring a matching URL prefix can answer that way, and it
        # is both the most accurate and the fastest source. The name search
        # runs alongside it so a term the client sent still gets a shot, and
        # the configured stash boxes get the filename's own words (`LyaCutie`
        # out of `0541-LyaCutie-2160p`) — no installed scraper covers the
        # sites a box indexes.
        attempts = _merge_attempts(
            await _identity_attempts(entity, numeric_id),
            _scrape_attempts(entity, numeric_id, query_term, _providers_for(entity)),
            await _stashbox_attempts(entity, query_term),
        )

    # Ranking needs the item's own words, never the typed term: the whole
    # point is telling an id-shaped coincidence apart from a match for *this*
    # file, and the term itself is the coincidence. Only looked up when an
    # id/URL route is in play, since that is the only place a well-formed
    # answer about the wrong object can come back. A filename term is the
    # inverse case — the client typed the file's own name — so the term
    # itself IS the ranking seed there.
    item_text = ""
    if numeric_id and (_numeric_query(query_term) or _looks_like_url(query_term)):
        item_text = await _fetch_rank_text(entity, numeric_id)
    elif not _numeric_query(query_term):
        item_text = query_term

    results: List[Dict[str, Any]] = []
    deadline = time.monotonic() + _search_budget()
    for target, payloads in await _fan_out_within(entity, attempts, deadline,
                                                  seed_text=item_text):
        for payload in payloads:
            token = _store_result(entity, target, payload, numeric_id)
            results.append(_to_result(entity, target, payload, token))
    logger.info(f"⌕ remote search {entity}-{numeric_id or '-'} provider={provider_key or 'all'} term={query_term!r} -> {len(results)} result(s)")
    return results


async def endpoint_remote_search(request):
    """`GET /Items/{id}/RemoteSearch/{provider}` — provider-scoped search
    for one item (the classic plugin-remote-search shape)."""
    item_id = request.path_params.get("item_id", "")
    provider_key = request.path_params.get("search_provider_name", "")
    entity, numeric_id = _classify(item_id)
    term = request.query_params.get("searchTerm") or request.query_params.get("SearchTerm") or ""
    results = await _search(entity, numeric_id, provider_key, term)
    return JSONResponse(results)


async def endpoint_remote_search_post(request):
    """`POST /Items/RemoteSearch/{itemType}` — the shape jellyfin-web's
    Identify dialog actually posts (RemoteSearchQuery in the body)."""
    item_type = request.path_params.get("item_type", "")
    body = await _json_body(request)
    search_info = body.get("SearchInfo") or {}
    term = search_info.get("Name") or search_info.get("name") or ""

    # Prefer the item the dialog is anchored on; fall back to the type.
    item_id = body.get("ItemId") or body.get("itemId") or ""
    entity, numeric_id = _classify(item_id)
    if not entity:
        lowered = str(item_type).lower()
        if "person" in lowered:
            entity = "performer"
        elif "movie" in lowered or "video" in lowered or "episode" in lowered:
            entity = "scene"

    provider_key = body.get("SearchProviderName") or body.get("searchProviderName") or ""
    results = await _search(entity, numeric_id, provider_key, term)
    return JSONResponse(results)


async def endpoint_remote_search_image(request):
    """`GET /Items/RemoteSearch/Image?ImageUrl=...` — proxy the candidate
    artwork the client's Identify dialog shows for remote-search results.
    jellyfin-web builds this URL itself; without it every thumbnail 404s
    and the dialog looks broken even when the search worked."""
    url = request.query_params.get("ImageUrl") or request.query_params.get("imageUrl") or ""
    if not url:
        return Response(status_code=404)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        logger.info(f"remote search image: refusing {parsed.scheme or 'empty'}-scheme url")
        return Response(status_code=400)
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            upstream = await client.get(url)
    except Exception as e:
        logger.warning(f"remote search image fetch failed ({parsed.netloc}): {e}")
        return Response(status_code=502)
    media_type = (upstream.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if upstream.status_code >= 400 or not media_type.startswith("image/"):
        logger.info(f"remote search image: upstream {upstream.status_code} {media_type!r} from {parsed.netloc}")
        return Response(status_code=502)
    return Response(content=upstream.content, media_type=media_type)


async def endpoint_remote_search_apply(request):
    """`POST /Items/RemoteSearch/Apply/{id}` — persist the chosen match."""
    item_id = request.path_params.get("item_id", "")
    entity, numeric_id = _classify(item_id)

    body = await _json_body(request)
    replace_images = str(request.query_params.get("replaceAllImages", "true")).lower() != "false"

    token = ((body.get("ProviderIds") or {}).get("Stash")) or ""
    entry = _take_result(str(token))
    payload = entry["payload"] if entry else None

    # Some clients address items by opaque GUID rather than the prefixed id
    # the rest of the proxy speaks. The parked search result still knows
    # which entity and which Stash row the user was looking at.
    if not entity and entry:
        entity = entry.get("entity") or ""
        numeric_id = entry.get("numeric_id") or ""

    if not _scraping_enabled() or entity not in _SUPPORTED_ENTITIES:
        logger.info(f"⌕ apply ignored for unsupported item {item_id!r} ({entity or 'unknown type'})")
        return Response(status_code=204)

    if payload is None:
        # Token expired or the client dropped ProviderIds — re-scrape using
        # whatever the client echoed back.
        provider = _resolve_provider(body.get("SearchProviderName") or "", entity)
        term = body.get("Name") or ""
        if provider and term:
            hits = await _scrape(entity, provider["id"], {"query": term})
            if hits:
                payload = hits[0]
        else:
            payload = await _auto_match(entity, numeric_id, term)

    if not payload:
        logger.info(f"⌕ apply found nothing to write for {item_id!r}")
        return JSONResponse({"error": "no_match"}, status_code=404)

    ok = await _apply_scene(numeric_id, payload, replace_images) if entity == "scene" \
        else await _apply_performer(numeric_id, payload, replace_images)
    if not ok:
        return JSONResponse({"error": "apply_failed"}, status_code=502)
    return Response(status_code=204)


async def endpoint_refresh_metadata(request):
    """`POST|GET /Items/{id}/RefreshMetadata` — auto-match and apply in one
    step, which is what a client's "refresh metadata" action triggers."""
    item_id = request.path_params.get("item_id", "")
    entity, numeric_id = _classify(item_id)
    if not _scraping_enabled() or entity not in _SUPPORTED_ENTITIES:
        logger.info(f"⌕ refresh ignored for unsupported item {item_id!r}")
        return Response(status_code=204)

    replace_images = str(request.query_params.get("replaceAllImages", "true")).lower() != "false"
    seed_text = await _fetch_seed_text(entity, numeric_id)
    # Refresh auto-applies whichever hit it returns, so an id-based scrape that
    # answered about the wrong object would be written into the library. Rank
    # against the item's own words here too, or the /posts/ shape wins the tie
    # on a file whose title is the bare number.
    rank_text = await _fetch_rank_text(entity, numeric_id) if _numeric_query(seed_text) else ""
    payload = await _auto_match(entity, numeric_id, seed_text, rank_text)
    if not payload:
        logger.info(f"⌕ refresh found no match for {item_id!r}")
        return Response(status_code=204)

    ok = await _apply_scene(numeric_id, payload, replace_images) if entity == "scene" \
        else await _apply_performer(numeric_id, payload, replace_images)
    if not ok:
        return JSONResponse({"error": "apply_failed"}, status_code=502)
    return Response(status_code=204)


async def endpoint_library_refresh(request):
    """`POST|GET /Library/Refresh` — kick off a Stash metadata scan. Stash
    runs it as a background job; we return as soon as the job is queued."""
    if not _scraping_enabled():
        return Response(status_code=204)
    try:
        res = await stash_query(
            """mutation MetadataScan($input: ScanMetadataInput!) { metadataScan(input: $input) }""",
            {"input": {}},
        )
        job_id = ((res or {}).get("data") or {}).get("metadataScan")
        if job_id:
            logger.info(f"⌕ Stash metadata scan queued (job {job_id})")
        else:
            logger.warning(f"⌕ Stash metadata scan not queued: {(res or {}).get('errors')}")
    except Exception as e:
        logger.error(f"⌕ metadata scan failed: {e}")
    return Response(status_code=204)


async def endpoint_update_item(request):
    """`POST /Items/{id}` (UpdateItem) — write client-edited metadata back
    into Stash. Only fields Stash actually owns are mapped; provider ids
    and Jellyfin-only fields are ignored."""
    item_id = request.path_params.get("item_id", "")
    entity, numeric_id = _classify(item_id)
    if not _scraping_enabled() or entity not in _SUPPORTED_ENTITIES:
        return Response(status_code=204)

    body = await _json_body(request)
    payload: Dict[str, Any] = {}

    name = body.get("Name") or body.get("name")
    if name:
        payload["title" if entity == "scene" else "name"] = name
    overview = body.get("Overview") or body.get("overview")
    if overview:
        payload["details"] = overview

    if entity == "scene":
        premiere = body.get("PremiereDate") or body.get("premiereDate")
        if premiere:
            payload["date"] = str(premiere)[:10]
        elif body.get("ProductionYear"):
            payload["date"] = f"{int(body['ProductionYear']):04d}-01-01"
        genres = body.get("Genres") or []
        tags = body.get("Tags") or []
        payload["tags"] = [{"name": g} for g in list(genres) + list(tags) if g]
    else:
        premiere = body.get("PremiereDate") or body.get("premiereDate")
        if premiere:
            payload["birthdate"] = str(premiere)[:10]
        tags = body.get("Tags") or []
        payload["tags"] = [{"name": t} for t in tags if t]

    ok = await _apply_scene(numeric_id, payload, True) if entity == "scene" \
        else await _apply_performer(numeric_id, payload, True)
    logger.info(f"⌕ UpdateItem {item_id}: {'applied' if ok else 'nothing to write'}")
    return Response(status_code=204)


async def endpoint_item_image_upload(request):
    """`POST|DELETE /Items/{id}/Images/{type}` — accept an uploaded cover
    image and push it into Stash, or acknowledge a delete.

    Stash has no per-image delete for covers, so DELETE is a no-op that
    keeps the client's image editor from erroring out."""
    item_id = request.path_params.get("item_id", "")
    entity, numeric_id = _classify(item_id)
    if request.method == "DELETE":
        logger.info(f"⌕ Image delete requested for {item_id} (unsupported by Stash — acknowledged)")
        return Response(status_code=204)
    if not _scraping_enabled() or entity not in _SUPPORTED_ENTITIES:
        return Response(status_code=204)

    body = await request.body()
    if not body:
        return JSONResponse({"error": "empty_body"}, status_code=400)

    content_type = request.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    data_url = f"data:{content_type};base64,{base64.b64encode(body).decode('ascii')}"

    if entity == "scene":
        update = {"id": numeric_id, "cover_image": data_url}
        res = await stash_query(
            """mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }""",
            {"input": update},
        )
        ok = bool(((res or {}).get("data") or {}).get("sceneUpdate"))
    else:
        update = {"id": numeric_id, "image": data_url}
        res = await stash_query(
            """mutation PerformerUpdate($input: PerformerUpdateInput!) { performerUpdate(input: $input) { id } }""",
            {"input": update},
        )
        ok = bool(((res or {}).get("data") or {}).get("performerUpdate"))

    logger.info(f"⌕ Image upload for {item_id}: {'stored' if ok else 'failed'}")
    return Response(status_code=204 if ok else 502)
