"""Shared runtime state — the single source of truth for config and
per-process state every module reads from.

**Usage pattern:**

    from stash_jellyfin_proxy import runtime
    resp = requests.get(runtime.STASH_URL + "/something")

Never `from stash_jellyfin_proxy.runtime import STASH_URL` — that captures the value at
import time. Always reach attributes through the module object so reads
see current values (config can change via the Web UI hot-reload).

**Write ownership:** only `config.bootstrap.run_bootstrap()` (at startup)
and `ui.api.ui_api_config` (at Web UI save) mutate these attributes. Every
other module is read-only. Keeping writes pinned to those two call sites
is what makes hot-reload predictable.
"""
from typing import Any, Dict, List, Optional, Set

# --- Stash connection ---
STASH_URL: str = "https://stash:9999"
STASH_API_KEY: str = ""
STASH_GRAPHQL_PATH: str = "/graphql"
STASH_VERIFY_TLS: bool = False
STASH_TIMEOUT: int = 30
STASH_RETRIES: int = 3
GRAPHQL_URL: str = ""  # derived: STASH_URL + STASH_GRAPHQL_PATH
STASH_SESSION: Any = None
STASH_VERSION: str = ""
STASH_CONNECTED: bool = False

# --- Proxy identity + bind ---
PROXY_BIND: str = "0.0.0.0"
PROXY_PORT: int = 8096
UI_PORT: int = 8097
SERVER_NAME: str = "Stash Media Server"
SERVER_ID: str = ""
# Externally-reachable base URL for the Jellyfin API, shown on the dashboard
# "Connect a Player" card. Set this when the proxy sits behind a reverse proxy
# (e.g. SWAG) where the public hostname/scheme/port differ from the bind. Empty
# → the UI falls back to the browser's host + PROXY_PORT.
PUBLIC_URL: str = ""

# --- Client auth ---
SJS_USER: str = ""
SJS_PASSWORD: str = ""
ACCESS_TOKEN: str = ""

# --- Libraries / content ---
TAG_GROUPS: List[str] = []
FAVORITE_TAG: str = ""
LATEST_GROUPS: List[str] = []
BANNER_MODE: str = "recent"
BANNER_POOL_SIZE: int = 200
BANNER_TAGS: List[str] = []

# --- Multi-file scenes (Stash "Merge" results) ---
# Stash streams only a scene's PRIMARY file — see util/local_media.py for
# the full explanation. When True, a scene holding more than one video
# file is exposed as a single Jellyfin item carrying one MediaSource per
# file, so clients render a version picker instead of silently playing
# only files[0]. Requires the media library to be mounted into this
# container plus a LIBRARY_PATH_MAP; without either, non-primary files
# fall back to the Stash stream (which serves the primary file).
MULTI_FILE_SCENES: bool = False
# Comma-separated `stash_path:container_path` mappings, e.g.
# "/data:/library". Empty disables direct disk reads.
LIBRARY_PATH_MAP: str = ""

# --- Config UI language ---
# Server-side default for the configuration UI. "auto" lets each browser
# decide from its Accept-Language/navigator.language; "en" and "zh" pin
# it. This is only a *default* — the sidebar switcher stores a per-browser
# override in localStorage, which always wins so one operator can read the
# UI in a language other than the deployment default.
UI_LANGUAGE: str = "auto"

# --- Feature toggles ---
ENABLE_FILTERS: bool = True
ENABLE_IMAGE_RESIZE: bool = True
ENABLE_TAG_FILTERS: bool = False
ENABLE_ALL_TAGS: bool = False
REQUIRE_AUTH_FOR_CONFIG: bool = False

# --- Metadata scraping / Identify (endpoints/metadata.py) ---
# Master switch for the Jellyfin metadata endpoints that bridge to Stash's
# own scrapers (MetadataEditor / RemoteSearch / Apply / RefreshMetadata /
# Library-Refresh / UpdateItem / image upload). False makes the proxy
# behave exactly as it did before the feature existed: no providers are
# advertised and every write endpoint becomes a logged no-op 204.
ENABLE_SCRAPING: bool = True
# Resolve and write studio / performers / tags from a scraped payload.
# These *replace* the existing association lists rather than merging, so
# disable this to keep hand-curated relationships while still applying
# scalar metadata (title, details, date, urls, images, performer fields).
SCRAPE_APPLY_RELATIONSHIPS: bool = True
# Write the scraper's cover image back to Stash on apply/refresh.
SCRAPE_APPLY_IMAGES: bool = True
# How long a RemoteSearch result stays applicable, keyed by the opaque
# token handed to the client in ProviderIds["Stash"]. Seconds.
SCRAPE_RESULT_TTL_SECONDS: int = 1800
# Comma-separated scraper names/ids to try FIRST when the search term is a
# bare number (Fantia post ids, Getchu product codes). Unknown names are
# ignored; if none is installed the default provider order is kept.
SCRAPE_NUMERIC_SCRAPERS: str = "fantiajp,GetchuDL"
# How a bare number becomes a URL for the scrapers above, which are
# FRAGMENT/URL-only and refuse "by name" lookups. Format:
#   <scraper-name-substring>=<url-template with {id}>, ...
# One scraper may list SEVERAL shapes for the same number, separated by `|`.
# Fantia needs exactly that: /posts/<id> and /products/<id> are different
# objects that share the id, so both are tried and then ranked against the
# item's own words before either reaches the client.
SCRAPE_NUMERIC_URL_TEMPLATES: str = (
    "fantiajp=https://fantia.jp/posts/{id}|https://fantia.jp/products/{id},"
    "getchudl=https://dl.getchu.com/i/item{id}"
)
# Cap on one scraper round-trip, and the overall budget for one search. A
# scrape really does hit a third-party site per attempt; without these the
# "search all providers" fan-out outlives the client's own request timeout.
SCRAPE_ATTEMPT_TIMEOUT_SECONDS: int = 20
SCRAPE_SEARCH_BUDGET_SECONDS: int = 25
# Also query the configured stash boxes (StashDB, PMV Stash, ThePornDB, …)
# on every "search all providers" Identify. The raw filename is normalised
# first — `0541-LyaCutie-2160p` becomes the query `Lya Cutie` — because a
# stash box answers people and scene titles, never catalogue noise.
SCRAPE_STASHBOX_ENABLED: bool = True

# --- Pagination / image cache sizing ---
DEFAULT_PAGE_SIZE: int = 50
MAX_PAGE_SIZE: int = 200
IMAGE_CACHE_MAX_SIZE: int = 100

# --- Logging ---
LOG_DIR: str = "."
LOG_FILE: str = "stash_jellyfin_proxy.log"
LOG_LEVEL: str = "INFO"
LOG_MAX_SIZE_MB: int = 10
LOG_BACKUP_COUNT: int = 3

# --- IP ban state ---
BANNED_IPS: Set[str] = set()
BAN_THRESHOLD: int = 10
BAN_WINDOW_MINUTES: int = 15

# --- Runtime flags ---
PROXY_RUNNING: bool = False
PROXY_START_TIME: Optional[float] = None

# --- Config file paths ---
CONFIG_FILE: str = ""
LOCAL_CONFIG_FILE: str = ""
# Set to False at bootstrap if the config file (or its parent dir, when
# the file doesn't yet exist) isn't writable. Surfaced on the dashboard
# because a non-persistent config means SERVER_ID and ACCESS_TOKEN
# rotate on every restart, breaking client reconnects (issue #16).
CONFIG_WRITABLE: bool = True

# Result of the boot-time persistence probe (issue #17). One of:
#   "persisted"      — file survived the prior boot (LAST_BOOT_AT marker found)
#   "not_persistent" — INTRODUCED marker present but LAST_BOOT_AT missing,
#                      meaning the file got wiped between boots (anonymous
#                      Docker volume, tmpfs, missing /config mount)
#   "not_writable"   — couldn't write the marker this boot
#   "unverified"     — first run, or first boot after upgrading to a build
#                      that writes these markers; can't classify yet
CONFIG_PERSISTENCE: str = "unverified"
# ISO-8601 UTC timestamp written to the config every successful boot.
# Surfaced read-only on the dashboard so operators can see the last
# boot time even when the file was just wiped.
CONFIG_LAST_BOOT_AT: str = ""

# --- Fixed identity strings ---
# The Jellyfin protocol version we pretend to be. Bump when clients
# require newer API features.
JELLYFIN_VERSION: str = "10.11.0"

# Stable per-user UUID derived from SERVER_ID + SJS_USER; computed once
# at bootstrap and published here.
USER_ID: str = ""

# --- Loaded config data ---
# Flat KEY → value dict from the config file (after v2 migration + local
# override merge). Sections dict holds INI-style [section.name] blocks.
config: Dict[str, str] = {}
config_defined_keys: Set[str] = set()
config_sections: Dict[str, Dict[str, str]] = {}

# --- Migration state (surfaced in the Web UI banner) ---
MIGRATION_PERFORMED: bool = False
MIGRATION_LOG: List[str] = []

# --- Image byte cache (Pillow output keyed by (item_id, size)) ---
IMAGE_CACHE: Dict[Any, Any] = {}

# --- scene_id → is_series_scene (bool). Populated by the image endpoint
# when resolving Episode vs Movie poster format. Cleared on config reload.
SERIES_SCENE_CACHE: Dict[str, bool] = {}

# --- Live config state (mutable at runtime via ui_api_config) ---
# Keys that are currently overridden via environment variables — the Web UI
# shows them as read-only. Populated at bootstrap.
env_overrides: List[str] = []
# The Stash tag id for FAVORITE_TAG, cached after first lookup.
favorite_tag_id_cache: Any = None

# --- Series / player profiles (Phase 2) ---
SERIES_TAG: str = "Series"
SERIES_EPISODE_PATTERNS: str = ""
PLAYER_PROFILES: List[Any] = []

# --- Playlists (Phase 7 §9.1) ---
# Stash tag whose direct children become Jellyfin playlists. Each child
# tag = one playlist; the scenes carrying that tag = its items. Empty
# string disables the feature.
PLAYLIST_PARENT_TAG: str = "Playlists"

# --- Image policy (Phase 3) ---
POSTER_CROP_ANCHOR: str = "center"

# --- Genre policy (Phase 3 §7.1) ---
# Mode: "all_tags" | "parent_tag" | "top_n"
GENRE_MODE: str = "parent_tag"
GENRE_PARENT_TAG: str = "GENRE"
GENRE_TOP_N: int = 25

# --- Metadata policy (Phase 3 §7.2) ---
# Articles stripped from the head of SortName so "The X" sorts under X.
SORT_STRIP_ARTICLES: List[str] = ["The", "A", "An"]
OFFICIAL_RATING: str = "NC-17"

# --- Filter panel (Phase 4 §8.5) ---
# Max tags in each Genres / Tags dimension of /Items/Filters.
FILTER_TAGS_MAX: int = 50
# AND  → scene must have every selected tag (INCLUDES_ALL).
# OR   → scene must have any (INCLUDES). Jellyfin's default is OR; AND
# is a proxy-only enhancement per design §5.4.
GENRE_FILTER_LOGIC: str = "AND"
# When True, a selected tag also matches scenes tagged with any of its
# descendants in Stash's tag hierarchy (depth: -1 on scene_filter tags).
FILTER_TAGS_WALK_HIERARCHY: bool = True

# --- Search scope (Phase 4 §8.5) ---
SEARCH_INCLUDE_SCENES: bool = True
SEARCH_INCLUDE_PERFORMERS: bool = True
SEARCH_INCLUDE_STUDIOS: bool = True
SEARCH_INCLUDE_GROUPS: bool = True

# --- Hero image source (Phase 4 §8.2) ---
# Pool the banner/hero pick from:
#   recent            most-recently-added scenes (default)
#   random            uniform random across the library
#   favorites         scenes tagged FAVORITE
#   top_rated         scenes with rating100 >= HERO_MIN_RATING
#   recently_watched  scenes with last_played_at within 30 days
HERO_SOURCE: str = "recent"
HERO_MIN_RATING: int = 75

# --- Per-library default sort (Phase 4 §8.4) ---
# Used when a client issues a list request with no SortBy param.
SCENES_DEFAULT_SORT: str = "DateCreated"
STUDIOS_DEFAULT_SORT: str = "SortName"
PERFORMERS_DEFAULT_SORT: str = "SortName"
GROUPS_DEFAULT_SORT: str = "SortName"
TAG_GROUPS_DEFAULT_SORT: str = "PlayCount"
SAVED_FILTERS_DEFAULT_SORT: str = "PlayCount"


def publish(**kwargs):
    """Bulk-set attributes. Called by config.bootstrap at startup to
    push every derived config value into this namespace in one pass."""
    for k, v in kwargs.items():
        globals()[k] = v
