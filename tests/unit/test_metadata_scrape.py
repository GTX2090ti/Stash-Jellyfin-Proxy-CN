"""Unit tests for the Jellyfin <-> Stash metadata scraping bridge
(`endpoints/metadata.py`).

Everything tested here is pure or cache-local — no Stash connection is
made, which keeps the suite runnable in CI without a live server.
"""
import re
import time

import pytest

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.endpoints import metadata


@pytest.fixture(autouse=True)
def _isolated_state():
    """Pin the scraper registry and config dict so tests don't leak into
    each other (or into the real runtime state)."""
    saved_config = dict(runtime.config)
    saved_providers = metadata._providers["items"]

    metadata._providers["items"] = [
        {"key": "stashdb", "id": "StashDB", "name": "StashDB", "entities": ["scene", "performer"]},
        {"key": "theporndb", "id": "ThePornDB", "name": "ThePornDB", "entities": ["performer"]},
        {"key": "url-only", "id": "URLOnly", "name": "URL Only", "entities": []},
    ]
    metadata._providers["at"] = time.monotonic()
    yield
    runtime.config.clear()
    runtime.config.update(saved_config)
    metadata._providers["items"] = saved_providers


# --- entity classification ------------------------------------------------

@pytest.mark.parametrize("item_id,expected", [
    ("scene-123", ("scene", "123")),
    ("scene-123-f456", ("scene", "123")),
    ("performer-9", ("performer", "9")),
    ("person-9", ("performer", "9")),
    ("person-performer-9", ("performer", "9")),
    ("studio-5", ("studio", "5")),
    ("group-7", ("group", "7")),
    ("tag-3", ("tag", "3")),
    ("genre-44", ("tag", "44")),
    ("not-an-item", ("", "")),
    ("", ("", "")),
])
def test_classify_item_ids(item_id, expected):
    assert metadata._classify(item_id) == expected


# --- provider registry -----------------------------------------------------

def test_slug_is_url_safe():
    assert metadata._slug("StashDB") == "StashDB"
    assert metadata._slug("a/b c") == "a-b-c"
    assert metadata._slug("!!!") == "scraper"


def test_providers_for_filters_by_entity():
    assert [p["key"] for p in metadata._providers_for("scene")] == ["stashdb"]
    assert [p["key"] for p in metadata._providers_for("performer")] == ["stashdb", "theporndb"]
    assert metadata._providers_for("studio") == []


def test_resolve_provider_by_key_name_and_entity():
    assert metadata._resolve_provider("stashdb", "scene")["id"] == "StashDB"
    # Clients sometimes echo the display name instead of the key.
    assert metadata._resolve_provider("StashDB", "scene")["id"] == "StashDB"
    assert metadata._resolve_provider("StashDB", "performer")["id"] == "StashDB"
    # Performer-only scraper must not be offered for scenes.
    assert metadata._resolve_provider("theporndb", "scene") is None
    assert metadata._resolve_provider("nope", "scene") is None
    assert metadata._resolve_provider("", "scene") is None


# --- config helpers --------------------------------------------------------

def test_flag_conf_file_wins_over_default():
    assert metadata._flag("SCRAPE_APPLY_IMAGES", True) is True
    runtime.config["SCRAPE_APPLY_IMAGES"] = "false"
    assert metadata._flag("SCRAPE_APPLY_IMAGES", True) is False
    runtime.config["SCRAPE_APPLY_IMAGES"] = "yes"
    assert metadata._flag("SCRAPE_APPLY_IMAGES", True) is True


def test_scraping_master_switch():
    assert metadata._scraping_enabled() is True
    runtime.config["ENABLE_SCRAPING"] = "off"
    assert metadata._scraping_enabled() is False


# --- value coercion --------------------------------------------------------

def test_to_int_takes_leading_number():
    assert metadata._to_int("170 cm") == 170
    assert metadata._to_int(170) == 170
    assert metadata._to_int("170.6") == 170
    assert metadata._to_int("") is None
    assert metadata._to_int(None) is None
    assert metadata._to_int(True) is None
    assert metadata._to_int("abc") is None


def test_to_float_takes_leading_number():
    assert metadata._to_float("14.5") == 14.5
    assert metadata._to_float("14 cm") == 14.0
    assert metadata._to_float("") is None
    assert metadata._to_float("abc") is None


def test_map_gender():
    assert metadata._map_gender("Female") == "FEMALE"
    assert metadata._map_gender("Transgender Female") == "TRANSGENDER_FEMALE"
    assert metadata._map_gender("Non-Binary") == "NON_BINARY"
    assert metadata._map_gender("unknown") is None
    assert metadata._map_gender("") is None


def test_map_circumcised():
    assert metadata._map_circumcised("Yes") == "CUT"
    assert metadata._map_circumcised("Uncut") == "UNCUT"
    assert metadata._map_circumcised("maybe") is None
    assert metadata._map_circumcised(None) is None


def test_year_and_iso_date():
    assert metadata._year_of("2021-05-04") == 2021
    assert metadata._year_of("2021") == 2021
    assert metadata._year_of("") is None
    assert metadata._iso_date("2021-05-04") == "2021-05-04T00:00:00.0000000Z"
    assert metadata._iso_date("2021") == "2021-01-01T00:00:00.0000000Z"
    assert metadata._iso_date("") is None


def test_names_caps_and_tolerates_shapes():
    assert metadata._names([{"name": "a"}, "b", {"nope": 1}]) == ["a", "b"]
    assert metadata._names(None) == []
    assert len(metadata._names([{"name": f"t{i}"} for i in range(100)])) == metadata._MAX_RELATIONSHIPS


# --- result handoff --------------------------------------------------------

def _scene_provider():
    return {"key": "stashdb", "id": "StashDB", "name": "StashDB", "entities": ["scene"]}


def test_result_token_roundtrip():
    token = metadata._store_result("scene", _scene_provider(), {"title": "X"}, "123")
    assert token.startswith("sc-")

    entry = metadata._take_result(token)
    assert entry["entity"] == "scene"
    assert entry["numeric_id"] == "123"
    assert entry["payload"] == {"title": "X"}
    # A user may apply, adjust and apply again from the same dialog.
    assert metadata._take_result(token) is not None


def test_expired_result_is_dropped():
    runtime.config["SCRAPE_RESULT_TTL_SECONDS"] = "-1"
    token = metadata._store_result("scene", _scene_provider(), {"title": "X"}, "1")
    assert metadata._take_result(token) is None


def test_unknown_token_returns_none():
    assert metadata._take_result("") is None
    assert metadata._take_result("nope") is None


# --- Jellyfin result shaping ----------------------------------------------

def test_scene_result_shape():
    payload = {
        "title": "A Scene", "date": "2019-07-02", "details": "desc",
        "image": "http://img/x.jpg", "remote_site_id": "abc",
    }
    result = metadata._to_result("scene", _scene_provider(), payload, "sc-token")
    assert result["Name"] == "A Scene"
    assert result["ProductionYear"] == 2019
    assert result["PremiereDate"] == "2019-07-02T00:00:00.0000000Z"
    assert result["ProviderIds"]["Stash"] == "sc-token"
    assert result["ProviderIds"]["StashDB"] == "abc"
    assert result["SearchProviderName"] == "StashDB"
    assert result["ImageUrl"] == "http://img/x.jpg"
    assert result["Overview"] == "desc"


def test_performer_result_prefers_images_and_appends_disambiguation():
    provider = {"key": "p", "id": "P", "name": "P", "entities": ["performer"]}
    payload = {
        "name": "Jane", "disambiguation": "US", "birthdate": "1980-03-09",
        "images": ["http://img/1.jpg"], "image": "data:image/jpeg;base64,AAA",
    }
    result = metadata._to_result("performer", provider, payload, "pe-token")
    assert result["Name"] == "Jane (US)"
    assert result["ImageUrl"] == "http://img/1.jpg"
    assert result["ProductionYear"] == 1980


def test_oversized_image_is_dropped():
    provider = {"key": "p", "id": "P", "name": "P", "entities": ["performer"]}
    result = metadata._to_result("performer", provider, {"name": "Jane", "images": ["x" * 500_000]}, "t")
    assert result["ImageUrl"] == ""


def test_scene_result_without_title_falls_back_to_code():
    payload = {"code": "ABC-123"}
    assert metadata._to_result("scene", _scene_provider(), payload, "t")["Name"] == "ABC-123"


# --- routing wiring --------------------------------------------------------

def test_metadata_routes_are_registered():
    """Importing the app also exercises CaseInsensitivePathMiddleware's
    build_path_map over the new routes."""
    from stash_jellyfin_proxy.app import routes

    paths = {getattr(route, "path", "") for route in routes}
    for expected in (
        "/Library/Refresh",
        "/Items/RemoteSearch/Apply/{item_id}",
        "/Items/RemoteSearch/{item_type}",
        "/Items/RemoteSearch/Image",
        "/Items/{item_id}/MetadataEditor",
        "/Items/{item_id}/Refresh",
        "/Items/{item_id}/RefreshMetadata",
        "/Items/{item_id}/RemoteSearch/{search_provider_name}",
        "/Items/{item_id}/Images/{image_type}",
    ):
        assert expected in paths, f"missing route: {expected}"

    # The classic refresh shape must accept POST — that's what clients send.
    # If it only matched the GET-only catch-all it would 405.
    from stash_jellyfin_proxy.app import routes

    for route in routes:
        if getattr(route, "path", "") == "/Items/{item_id}/Refresh":
            assert "POST" in route.methods, "POST /Items/{id}/Refresh must be allowed"
            break
    else:
        raise AssertionError("/Items/{item_id}/Refresh route vanished")


# --- numeric-title routing ---------------------------------------------------

class TestNumericRouting:
    def _mk(self, key, sid, name, entities, entities_any=None):
        return {"key": key, "id": sid, "name": name,
                "entities": entities, "entities_any": entities_any or entities}

    def test_numeric_query_detection(self):
        from stash_jellyfin_proxy.endpoints.metadata import _numeric_query

        assert _numeric_query("1234567")
        assert _numeric_query("  123456 ")
        assert not _numeric_query("abc123")
        assert not _numeric_query("FC2-PPV-1234567")
        assert not _numeric_query("")          # empty term → default order
        assert not _numeric_query("123")       # too short to be an id

    def test_numeric_scraper_names_default_and_override(self, monkeypatch):
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.delattr(runtime, "SCRAPE_NUMERIC_SCRAPERS", raising=False)
        monkeypatch.setattr(runtime, "config", {}, raising=False)
        assert metadata._numeric_scraper_names() == ["fantiajp", "getchudl"]

        monkeypatch.setattr(runtime, "config",
                            {"SCRAPE_NUMERIC_SCRAPERS": "Fanza ,  getchu ,R18"},
                            raising=False)
        assert metadata._numeric_scraper_names() == ["fanza", "getchu", "r18"]

    def test_numeric_targets_reorder_and_fall_back(self, monkeypatch):
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        stashdb = self._mk("stashdb", "stashdb", "StashDB", ["scene"])
        fantia = self._mk("fantiajp", "fantiajp", "FantiaJp", [], ["scene"])
        getchu = self._mk("getchudl", "GetchuDL", "GetchuDL", [], ["scene"])
        other = self._mk("baba", "babasession", "BABAsession", ["scene"])
        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers",
                            {"at": 1.0, "items": [stashdb, fantia, getchu, other]})

        # Non-numeric term: plain name search, install order preserved.
        plan = metadata._scrape_attempts("scene", "13", "Coconut", [stashdb, other])
        assert [(p["id"], sorted(i)) for p, i in plan] == [
            ("stashdb", ["query"]), ("babasession", ["query"])]

        # Numeric term: the configured ID scrapers go first, each handed the
        # URL its own template says the number lives at. They cannot answer a
        # name lookup at all, so this is the only shape that can work — and it
        # must be the URL entry point, not a scene_input fragment (Stash
        # rejects that with "scraper operation not supported").
        plan = metadata._scrape_attempts("scene", "13", "3072923", [stashdb, other])
        shape = [(p["id"], sorted(i)) for p, i in plan]
        # FantiaJp appears twice on purpose: one core serves two unrelated
        # objects under the same number (/posts/<id> and /products/<id>), so
        # both shapes are tried and ranked afterwards rather than guessing.
        assert shape == [
            ("fantiajp", [metadata._URL_MARKER]),
            ("fantiajp", [metadata._URL_MARKER]),
            ("GetchuDL", [metadata._URL_MARKER]),
            ("stashdb", ["query"]),
            ("babasession", ["query"]),
        ]
        assert plan[0][1][metadata._URL_MARKER] == "https://fantia.jp/posts/3072923"
        assert plan[1][1][metadata._URL_MARKER] == "https://fantia.jp/products/3072923"
        assert plan[2][1][metadata._URL_MARKER] == "https://dl.getchu.com/i/item3072923"

        # No blind {scene_id: ...} for an ID scraper: on an item with no stored
        # URL Stash runs that as a fragment scrape and the scraper just echoes
        # the basename back, which is the junk result this replaced.
        assert all("scene_id" not in i for _p, i in plan)

        # No numeric id (search box on an unsaved item) → URL attempt only.
        plan = metadata._scrape_attempts("scene", "", "4067159", [])
        assert [(p["id"], sorted(i)) for p, i in plan] == [
            ("fantiajp", [metadata._URL_MARKER]),
            ("fantiajp", [metadata._URL_MARKER]),
            ("GetchuDL", [metadata._URL_MARKER]),
        ]

    def test_payload_relevance_scores_the_items_own_words(self):
        """The only signal available when a source answers correctly about the
        wrong object: does the answer mention anything this file mentions."""
        from stash_jellyfin_proxy.endpoints.metadata import _payload_relevance

        product = {"title": "…【buena320】", "code": "buena-320",
                   "studio": {"name": "Fantia.jp"}}
        post = {"title": "青橙", "code": "FANTIA-1006291",
                "studio": {"name": "Fantia.jp"}}

        assert _payload_relevance(product, "buena-320s") > 0
        assert _payload_relevance(post, "buena-320s") == 0
        # No seed to compare against → no ranking, callers fall back to order.
        assert _payload_relevance(product, "") == 0

    def test_relevance_puts_the_matching_shape_first(self, monkeypatch):
        """Fantia answers 200 for BOTH /posts/<id> and /products/<id>; they are
        unrelated objects that share the number. Live case: 1006291 is a fanart
        post ("青橙") and an adult product whose code is buena-320, and the file
        being scraped is buena-320s.mp4. Both hits survive — the product is
        ordered first instead of whichever finished first.

        The seed is the *real* rank text, `_fetch_rank_text`'s output for
        scene-118: its stored title is the bare number, so both payloads match
        on that alone (each carries the number inside its own URL) and only the
        filename separates them. Seeding with just the filename would hide that
        — the whole point is that the number alone is not enough."""
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        fantia = {"key": "fantiajp", "id": "FantiaJp", "name": "FantiaJp",
                  "entities": [], "entities_any": ["scene"],
                  "patterns": {"scene": ["fantia.jp/posts/"]}}

        async def scrape(entity, scraper_id, scrape_input):
            url = scrape_input.get(metadata._URL_MARKER, "")
            if "/products/" in url:
                return [{"title": "…【buena320】", "code": "buena-320",
                         "urls": [url], "studio": {"name": "Fantia.jp"}}]
            return [{"title": "青橙", "code": "FANTIA-1006291", "urls": [url],
                     "studio": {"name": "Fantia.jp"}}]

        async def no_identity(*a, **k):
            return []

        async def fake_load():
            return [fantia]

        async def seed(entity, numeric_id):
            return "1006291 buena-320s.mp4"

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": [fantia]})
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_scrape", scrape)
        monkeypatch.setattr(metadata, "_identity_attempts", no_identity)
        monkeypatch.setattr(metadata, "_fetch_rank_text", seed)

        results = asyncio.run(metadata._search("scene", "118", "", "1006291"))
        assert [r["Name"] for r in results] == ["…【buena320】", "青橙"]

    def test_numeric_url_templates_are_configurable(self, monkeypatch):
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        assert dict(metadata._numeric_url_templates())["getchudl"] == \
            "https://dl.getchu.com/i/item{id}"

        monkeypatch.setattr(runtime, "config",
                            {"SCRAPE_NUMERIC_URL_TEMPLATES": "fantia=https://x/{id}"},
                            raising=False)
        assert metadata._numeric_url_templates() == [("fantia", "https://x/{id}")]

    def test_scrape_documents_only_use_fields_this_stash_has(self):
        """Stash rejects the whole document on one unknown field, which turns
        every scrape into a silent empty result — so the field lists are
        pinned here against the live schema (Stash 0.28 ScrapedScene /
        ScrapedPerformer)."""
        from stash_jellyfin_proxy.endpoints.metadata import _SCRAPE_QUERY

        scene_doc = _SCRAPE_QUERY["scene"]
        assert "production_date" not in scene_doc, "not a ScrapedScene field"
        for field in ("title", "code", "details", "director", "date", "urls", "image"):
            assert field in scene_doc

        performer_doc = _SCRAPE_QUERY["performer"]
        assert "images" in performer_doc
        # `image` (singular) does not exist on ScrapedPerformer.
        assert not re.search(r"\bimage\b(?!s)", performer_doc), "ScrapedPerformer has no `image`"


# --- time budget / attempt timeout ------------------------------------------

class TestTimeBudget:
    """A scrape is a live third-party request, so the search fan-out has to be
    bounded — otherwise the client's own request timeout fires first and the
    Identify dialog just spins."""

    def test_slow_attempt_is_skipped_not_fatal(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        async def never_returns(entity, scraper_id, scrape_input):
            await asyncio.sleep(5)
            return [{"title": "late"}]

        monkeypatch.setattr(runtime, "config",
                            {"SCRAPE_ATTEMPT_TIMEOUT_SECONDS": 1}, raising=False)
        monkeypatch.setattr(metadata, "_scrape", never_returns)
        assert asyncio.run(metadata._scrape_timed("scene", "x", {"query": "a"})) == []

    def test_fast_attempt_passes_through(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        async def fast(entity, scraper_id, scrape_input):
            return [{"title": "hit"}]

        monkeypatch.setattr(runtime, "config",
                            {"SCRAPE_ATTEMPT_TIMEOUT_SECONDS": 5}, raising=False)
        monkeypatch.setattr(metadata, "_scrape", fast)
        assert asyncio.run(metadata._scrape_timed("scene", "x", {"query": "a"})) == [{"title": "hit"}]

    def test_fan_out_runs_concurrently(self, monkeypatch):
        """Scrapers are independent third-party sites, so N of them must not
        cost N round-trips of wall-clock. Twelve 0.2s scrapes would take 2.4s
        serially; fanned out they finish in roughly two waves."""
        import asyncio
        import time
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        calls = []

        async def slow_miss(entity, scraper_id, scrape_input):
            calls.append(scraper_id)
            await asyncio.sleep(0.2)
            return []

        providers = [
            {"key": f"p{i}", "id": f"p{i}", "name": f"P{i}", "patterns": {},
             "entities": ["scene"], "entities_any": ["scene"]}
            for i in range(12)
        ]

        async def fake_load():
            return providers

        monkeypatch.setattr(runtime, "config",
                            {"SCRAPE_SEARCH_BUDGET_SECONDS": 5,
                             "SCRAPE_ATTEMPT_TIMEOUT_SECONDS": 30}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": providers}, raising=False)
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_scrape", slow_miss)

        async def no_stored_urls(entity, numeric_id):
            return []

        monkeypatch.setattr(metadata, "_fetch_stored_urls", no_stored_urls)

        started = time.monotonic()
        assert asyncio.run(metadata._search("scene", "13", "", "Coconut")) == []
        elapsed = time.monotonic() - started

        assert len(calls) == len(providers), "every scraper still gets its turn"
        assert elapsed < len(providers) * 0.2 * 0.6, (
            "12×0.2s scrapes took %.2fs — the fan-out is still serial" % elapsed)

    def test_budget_caps_latency_when_scrapers_hang(self, monkeypatch):
        """A hanging scraper must not hold the client's request open. Once the
        budget is gone the search returns what it has, without waiting for the
        stragglers."""
        import asyncio
        import time
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        async def hang(entity, scraper_id, scrape_input):
            await asyncio.sleep(30)
            return []

        providers = [
            {"key": f"p{i}", "id": f"p{i}", "name": f"P{i}", "patterns": {},
             "entities": ["scene"], "entities_any": ["scene"]}
            for i in range(10)
        ]

        async def fake_load():
            return providers

        monkeypatch.setattr(runtime, "config",
                            {"SCRAPE_SEARCH_BUDGET_SECONDS": 0.5,
                             "SCRAPE_ATTEMPT_TIMEOUT_SECONDS": 30}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": providers}, raising=False)
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_scrape", hang)

        async def no_stored_urls(entity, numeric_id):
            return []

        monkeypatch.setattr(metadata, "_fetch_stored_urls", no_stored_urls)

        started = time.monotonic()
        assert asyncio.run(metadata._search("scene", "13", "", "Coconut")) == []
        elapsed = time.monotonic() - started
        assert elapsed < 3.0, "budget did not cap latency (%.2fs)" % elapsed

    def test_first_wave_with_a_hit_returns_without_waiting(self, monkeypatch):
        """The client shows one candidate list, so once any scraper answers the
        response goes out — the slow ones are dropped, not waited on."""
        import asyncio
        import time
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        async def fast_hit(entity, scraper_id, scrape_input):
            if scraper_id == "slow":
                await asyncio.sleep(20)
                return []
            return [{"title": "Found it", "code": "ABC-123"}]

        providers = [
            {"key": "slow", "id": "slow", "name": "Slow", "patterns": {},
             "entities": ["scene"], "entities_any": ["scene"]},
            {"key": "quick", "id": "quick", "name": "Quick", "patterns": {},
             "entities": ["scene"], "entities_any": ["scene"]},
        ]

        async def fake_load():
            return providers

        monkeypatch.setattr(runtime, "config",
                            {"SCRAPE_SEARCH_BUDGET_SECONDS": 30,
                             "SCRAPE_ATTEMPT_TIMEOUT_SECONDS": 30}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": providers}, raising=False)
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_scrape", fast_hit)

        async def no_stored_urls(entity, numeric_id):
            return []

        monkeypatch.setattr(metadata, "_fetch_stored_urls", no_stored_urls)

        started = time.monotonic()
        results = asyncio.run(metadata._search("scene", "13", "", "Coconut"))
        elapsed = time.monotonic() - started

        assert [r["Name"] for r in results] == ["Found it"]
        assert elapsed < 5.0, "waited %.2fs on the slow scraper" % elapsed

    def test_documented_defaults(self, monkeypatch):
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.delattr(runtime, "SCRAPE_ATTEMPT_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delattr(runtime, "SCRAPE_SEARCH_BUDGET_SECONDS", raising=False)
        monkeypatch.setattr(runtime, "config", {}, raising=False)
        assert metadata._attempt_timeout() == 20.0
        assert metadata._search_budget() == 25.0


# --- search-term cleaning ---------------------------------------------------
#
# An un-scraped scene has no real title: Stash returns "" or the file's
# basename. Passed through verbatim, that lands in a scraper's URL as
# `…/SLAVE TRAINING.mp4` (404) — which is indistinguishable from "no match"
# and was the visible "scraping is broken" symptom.

class TestSearchTermCleaning:
    def test_strips_media_extension(self):
        from stash_jellyfin_proxy.endpoints.metadata import _clean_search_term

        assert _clean_search_term("2B MAIDBJ, BUKKAKE AND SLAVE TRAINING.mp4") == \
            "2B MAIDBJ, BUKKAKE AND SLAVE TRAINING"
        assert _clean_search_term("movie.MKV") == "movie"
        assert _clean_search_term("clip.wmv") == "clip"

    def test_strips_leading_content_hash(self):
        from stash_jellyfin_proxy.endpoints.metadata import _clean_search_term

        assert _clean_search_term("214ed862_quest-kanu.mp4") == "quest-kanu"
        assert _clean_search_term("deadbeefcafe-video.mp4") == "video"

    def test_real_titles_pass_through_untouched(self):
        from stash_jellyfin_proxy.endpoints.metadata import _clean_search_term

        # No extension and no leading hash → must survive byte-for-byte, CJK
        # punctuation included.
        title = "❤ スターレ〇ル 花〇 フル主観動画２本セット【唾液脳ハメ中〇し性癖破壊 包茎童貞逆NTR】"
        assert _clean_search_term(title) == title
        assert _clean_search_term("  Coconut  ") == "Coconut"
        assert _clean_search_term("") == ""
        assert _clean_search_term(None) == ""

    def test_seed_falls_back_to_basename_when_title_is_empty(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        async def fake(query, variables=None, retries=None):
            return {"data": {"findScene": {"id": "62", "title": "",
                                           "files": [{"basename": "SOME TITLE.mp4"}]}}}

        monkeypatch.setattr(metadata, "stash_query", fake)
        assert asyncio.run(metadata._fetch_seed_text("scene", "62")) == "SOME TITLE"

    def test_seed_cleans_a_filename_style_title(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        async def fake(query, variables=None, retries=None):
            return {"data": {"findScene": {"id": "220", "title": "214ed862_quest-kanu.mp4",
                                           "files": [{"basename": "214ed862_quest-kanu.mp4"}]}}}

        monkeypatch.setattr(metadata, "stash_query", fake)
        assert asyncio.run(metadata._fetch_seed_text("scene", "220")) == "quest-kanu"

    def test_seed_is_empty_when_nothing_usable(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        async def fake(query, variables=None, retries=None):
            return {"data": {"findScene": {"id": "9", "title": "", "files": []}}}

        monkeypatch.setattr(metadata, "stash_query", fake)
        assert asyncio.run(metadata._fetch_seed_text("scene", "9")) == ""


# --- rank text: every word the item carries, not just a search keyword -----
#
# Ranking and searching want opposite things. `_fetch_seed_text` hands a
# scraper ONE clean keyword; ranking needs as many of the item's own words as
# possible, because an id-based source can legitimately answer about a
# different object that happens to share the number.

class TestRankText:
    def test_it_carries_the_title_and_every_filename(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        async def fake(query, variables=None, retries=None):
            return {"data": {"findScene": {
                "id": "118", "title": "1006291",
                "files": [{"basename": "buena-320s.mp4"},
                          {"basename": "buena-320s-b.mp4"}]}}}

        monkeypatch.setattr(metadata, "stash_query", fake)
        assert asyncio.run(metadata._fetch_rank_text("scene", "118")) == \
            "1006291 buena-320s.mp4 buena-320s-b.mp4"

    def test_it_scores_the_product_above_the_post_that_shares_the_id(
            self, monkeypatch):
        """The live case. A title-only seed cannot separate Fantia's two shapes
        for 1006291 — both echo the number back in their URL — so the unrelated
        post won on attempt order. The filename is the word that breaks the
        tie."""
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        async def fake(query, variables=None, retries=None):
            return {"data": {"findScene": {
                "id": "118", "title": "1006291",
                "files": [{"basename": "buena-320s.mp4"}]}}}

        monkeypatch.setattr(metadata, "stash_query", fake)
        rank = asyncio.run(metadata._fetch_rank_text("scene", "118"))

        post = {"title": "青橙", "code": "FANTIA-1006291",
                "urls": ["https://fantia.jp/posts/1006291"]}
        product = {"title": "…【buena320】…", "code": "buena-320",
                   "urls": ["https://fantia.jp/products/1006291"]}

        assert metadata._payload_relevance(product, rank) > \
            metadata._payload_relevance(post, rank)

    def test_it_degrades_to_no_signal_when_the_lookup_fails(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        async def boom(query, variables=None, retries=None):
            raise RuntimeError("stash is down")

        monkeypatch.setattr(metadata, "stash_query", boom)
        assert asyncio.run(metadata._fetch_rank_text("scene", "118")) == ""
        assert asyncio.run(metadata._fetch_rank_text("scene", "")) == ""


# --- slow-scraper deprioritisation -----------------------------------------
#
# The second half of the same symptom: AdultTime times out on every search and
# spends the entire budget, so no provider that *could* answer ever runs.

class TestSlowScraperDeprioritisation:
    def _mk(self, key, sid, name):
        return {"key": key, "id": sid, "name": name,
                "entities": ["scene"], "entities_any": ["scene"]}

    def test_timeout_marks_the_scraper(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.setattr(metadata, "_slow_until", {}, raising=False)

        async def never(entity, scraper_id, scrape_input):
            await asyncio.sleep(5)
            return []

        monkeypatch.setattr(runtime, "config",
                            {"SCRAPE_ATTEMPT_TIMEOUT_SECONDS": 1}, raising=False)
        monkeypatch.setattr(metadata, "_scrape", never)
        asyncio.run(metadata._scrape_timed("scene", "adulttime", {"query": "a"}))
        assert metadata._is_slow("adulttime")

    def test_timing_out_scraper_is_sorted_last(self, monkeypatch):
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.setattr(metadata, "_slow_until", {}, raising=False)
        slow = self._mk("adult", "adulttime", "AdultTime")
        fast1 = self._mk("a", "aaa", "AAA")
        fast2 = self._mk("b", "bbb", "BBB")

        before = [p["id"] for p, _ in metadata._scrape_attempts(
            "scene", "62", "Coconut", [slow, fast1, fast2])]
        assert before == ["adulttime", "aaa", "bbb"]

        metadata._mark_slow("adulttime")
        after = [p["id"] for p, _ in metadata._scrape_attempts(
            "scene", "62", "Coconut", [slow, fast1, fast2])]
        assert after == ["aaa", "bbb", "adulttime"]

    def test_cooldown_expires(self, monkeypatch):
        import time as _t
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.setattr(metadata, "_slow_until", {}, raising=False)
        metadata._mark_slow("x")
        assert metadata._is_slow("x")
        metadata._slow_until["x"] = _t.monotonic() - 1
        assert not metadata._is_slow("x")

    def test_numeric_prefix_keeps_priority(self, monkeypatch):
        """A timed-out configured ID scraper still goes first — the user asked
        for it explicitly, and reordering must not silently drop that."""
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.setattr(metadata, "_slow_until", {}, raising=False)
        monkeypatch.setattr(runtime, "config", {}, raising=False)
        fantia = {"key": "fantiajp", "id": "fantiajp", "name": "FantiaJp",
                  "entities": [], "entities_any": ["scene"]}
        other = self._mk("b", "bbb", "BBB")
        monkeypatch.setattr(metadata, "_providers",
                            {"at": 1.0, "items": [fantia, other]})

        metadata._mark_slow("fantiajp")
        plan = metadata._scrape_attempts("scene", "13", "3072923", [other])
        assert [p["id"] for p, _ in plan][:1] == ["fantiajp"]


class TestUrlIdentityAttempts:
    """The Identify path must be able to scrape an item from the URL Stash
    already holds. That is the cheap accurate route, and the only one a
    FRAGMENT/URL-only scraper such as FantiaJp can answer at all."""

    def test_pattern_matching_ignores_scheme_and_www(self):
        from stash_jellyfin_proxy.endpoints import metadata

        m = metadata._patterns_match
        assert m(["fantia.jp/posts/"], ["https://fantia.jp/posts/3072923"])
        assert m(["https://www.javbus.com"], ["https://javbus.com/ABP-123"])
        assert m(["*.javbus.com"], ["https://www.javbus.com/ABP-123"])
        assert m(["dl.getchu.com/i/item"], ["https://dl.getchu.com/i/item4067159"])
        assert not m(["fantia.jp/posts/"], ["https://dl.getchu.com/i/item4067159"])
        assert not m([], ["https://fantia.jp/posts/1"])
        assert not m(["fantia.jp/posts/"], [])

    def test_identity_attempts_narrow_to_the_matching_scraper(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        fantia = {"key": "fantiajp", "id": "fantiajp", "name": "FantiaJp",
                  "entities": [], "entities_any": ["scene"],
                  "patterns": {"scene": ["fantia.jp/posts/"]}}
        javbus = {"key": "javbus", "id": "javbus", "name": "Javbus",
                  "entities": ["scene"], "entities_any": ["scene"],
                  "patterns": {"scene": ["https://www.javbus.com"]}}
        monkeypatch.setattr(metadata, "_providers",
                            {"at": 1.0, "items": [fantia, javbus]})

        async def stored(entity, numeric_id):
            return ["https://fantia.jp/posts/3072923"]

        monkeypatch.setattr(metadata, "_fetch_stored_urls", stored)
        plan = asyncio.run(metadata._identity_attempts("scene", "13"))
        assert [(p["id"], i) for p, i in plan] == [("fantiajp", {"scene_id": "13"})]

    def test_no_stored_url_means_no_identity_attempt(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        async def nothing(entity, numeric_id):
            return []

        monkeypatch.setattr(metadata, "_fetch_stored_urls", nothing)
        assert asyncio.run(metadata._identity_attempts("scene", "62")) == []

    def test_a_source_with_no_scraper_is_never_sent(self, monkeypatch):
        """Stash rejects `source: {}` outright — "scraper_id or stash_box_index
        must be set" — so the call must not be attempted at all."""
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        sent = []

        async def spy(query, variables=None):
            sent.append(variables)
            return {"data": {"scrapeSingleScene": []}}

        monkeypatch.setattr(metadata, "stash_query", spy)
        assert asyncio.run(metadata._scrape("scene", None, {"scene_id": "13"})) == []
        assert sent == []

    def test_search_uses_the_items_own_url_when_the_client_sends_a_term(self, monkeypatch):
        """An item carrying a Fantia URL resolves through FantiaJp even though
        the client sent a name term: the URL match is exact, and the name
        search would only ever return a fuzzy guess."""
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        fantia = {"key": "fantiajp", "id": "fantiajp", "name": "FantiaJp",
                  "entities": [], "entities_any": ["scene"],
                  "patterns": {"scene": ["fantia.jp/posts/"]}}
        javbus = {"key": "javbus", "id": "javbus", "name": "Javbus",
                  "entities": ["scene"], "entities_any": ["scene"],
                  "patterns": {"scene": ["https://www.javbus.com"]}}

        async def fake_load():
            return [fantia, javbus]

        async def scrape(entity, scraper_id, scrape_input):
            if scraper_id == "fantiajp":
                return [{"title": "Fantia post 3072923", "date": "2024-01-02"}]
            return []

        async def stored(entity, numeric_id):
            return ["https://fantia.jp/posts/3072923"]

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": [fantia, javbus]})
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_fetch_stored_urls", stored)
        monkeypatch.setattr(metadata, "_scrape", scrape)

        results = asyncio.run(metadata._search("scene", "13", "", "a typed term"))
        assert [r["Name"] for r in results] == ["Fantia post 3072923"]


class TestConcurrentFanOut:
    """Scrapers run at the same time, so the winner has to be picked by
    attempt order rather than by whoever happens to finish first."""

    def test_list_scrapers_query_fetches_url_patterns(self):
        """_load_providers derives each scraper's URL patterns from this
        document. If `urls` is dropped from it the identity pass matches
        nothing at all and every search silently degrades to a name fan-out —
        which is exactly how this broke once already."""
        import re as _re
        from stash_jellyfin_proxy.endpoints import metadata

        for entity in metadata._SUPPORTED_ENTITIES:
            block = _re.search(r"\b%s\s*\{([^}]*)\}" % entity, metadata._LIST_SCRAPERS)
            assert block, "no %s block in _LIST_SCRAPERS" % entity
            assert "urls" in block.group(1), "%s must request urls" % entity

    def test_earlier_attempt_of_a_repeated_scraper_is_ranked_first(self, monkeypatch):
        """One scraper legitimately appears more than once — once from the
        item's stored URL and once per synthesized shape of the number. All are
        targeted, so all survive, but nothing in the item's own text promotes
        any of them, so plain attempt order decides: the stored-URL attempt
        (the most accurate input we hold) stays first, the synthesized shapes
        follow in conf order."""
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        fantia = {"key": "fantiajp", "id": "fantiajp", "name": "FantiaJp",
                  "entities": [], "entities_any": ["scene"],
                  "patterns": {"scene": ["fantia.jp/posts/"]}}

        async def scrape(entity, scraper_id, scrape_input):
            if metadata._URL_MARKER in scrape_input:
                url = scrape_input[metadata._URL_MARKER]
                shape = "products" if "/products/" in url else "posts"
                return [{"title": f"from the synthesized {shape} URL"}]
            return [{"title": "from the stored URL"}]

        async def stored(entity, numeric_id):
            return ["https://fantia.jp/posts/3072923"]

        async def seed(entity, numeric_id):
            return ""            # nothing to rank against → plain attempt order

        async def fake_load():
            return [fantia]

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": [fantia]})
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_scrape", scrape)
        monkeypatch.setattr(metadata, "_fetch_stored_urls", stored)
        monkeypatch.setattr(metadata, "_fetch_rank_text", seed)

        results = asyncio.run(metadata._search("scene", "13", "", "3072923"))
        assert [r["Name"] for r in results] == [
            "from the stored URL",
            "from the synthesized posts URL",
            "from the synthesized products URL",
        ]

    def test_two_attempts_opening_the_same_page_are_listed_once(self, monkeypatch):
        """The stored URL and its synthesized twin open one page and the scraper
        answers with one payload carrying one URL. Listing the object twice in
        the Identify dialog would be noise, so the second is dropped once the
        first has claimed that URL."""
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        fantia = {"key": "fantiajp", "id": "fantiajp", "name": "FantiaJp",
                  "entities": [], "entities_any": ["scene"],
                  "patterns": {"scene": ["fantia.jp/posts/"]}}

        async def scrape(entity, scraper_id, scrape_input):
            url = scrape_input.get(metadata._URL_MARKER) or "https://fantia.jp/posts/1006291"
            if "/products/" in url:
                return [{"title": "…【buena320】", "code": "buena-320",
                         "urls": ["https://fantia.jp/products/1006291"]}]
            # Both the id-keyed attempt and the /posts/ URL attempt land here.
            return [{"title": "青橙", "code": "FANTIA-1006291",
                     "urls": ["https://fantia.jp/posts/1006291"]}]

        async def stored(entity, numeric_id):
            return ["https://fantia.jp/posts/1006291"]

        async def seed(entity, numeric_id):
            return ""            # no ranking signal → plain attempt order

        async def fake_load():
            return [fantia]

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": [fantia]})
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_scrape", scrape)
        monkeypatch.setattr(metadata, "_fetch_stored_urls", stored)
        monkeypatch.setattr(metadata, "_fetch_rank_text", seed)

        results = asyncio.run(metadata._search("scene", "118", "", "1006291"))
        # 青橙 is reached twice (by id, by /posts/ URL) but shown once.
        assert [r["Name"] for r in results] == ["青橙", "…【buena320】"]


# --- the URL entry point ----------------------------------------------------
#
# `scrapeSingleScene` cannot turn a bare number into metadata for a
# FRAGMENT/URL-only scraper: a `scene_input` payload is run as a fragment
# scrape ("scraper operation not supported") and `by name` is refused
# ("cannot load SCENE by name"). `scrapeURL` takes the URL and lets Stash route
# it by prefix — the same door the Stash UI's own "Scrape with → URL" uses.

class TestUrlEntryPoint:
    def test_url_like_term_detection(self):
        from stash_jellyfin_proxy.endpoints.metadata import _looks_like_url

        assert _looks_like_url("https://fantia.jp/posts/4064942")
        assert _looks_like_url("http://dl.getchu.com/i/item4067159")
        assert _looks_like_url("fantia.jp/posts/4064942")
        # Not links: a filename, a hashed filename, a catalogue number.
        assert not _looks_like_url("movie.mp4")
        assert not _looks_like_url("5ff33487_Buncos_kfk.mp4")
        assert not _looks_like_url("2B MAIDBJ, BUKKAKE AND SLAVE TRAINING.mp4")
        assert not _looks_like_url("ABP-123")
        assert not _looks_like_url("4064942")
        assert not _looks_like_url("")

    def test_url_term_routes_to_the_scrapers_that_claim_it(self, monkeypatch):
        """A pasted link used to be name-searched: every scraper got the URL as
        a keyword, produced a 404, and the dialog showed nothing."""
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        fantia = {"key": "fantiajp", "id": "FantiaJp", "name": "FantiaJp",
                  "entities": [], "entities_any": ["scene"],
                  "patterns": {"scene": ["fantia.jp/posts/"]}}
        getchu = {"key": "getchudl", "id": "GetchuDL", "name": "GetchuDL",
                  "entities": [], "entities_any": ["scene"],
                  "patterns": {"scene": ["dl.getchu.com/i/item"]}}
        javbus = {"key": "javbus", "id": "javbus", "name": "JavBus",
                  "entities": ["scene"], "entities_any": ["scene"],
                  "patterns": {"scene": ["https://www.javbus.com"]}}
        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers",
                            {"at": 1.0, "items": [fantia, getchu, javbus]})

        url = "https://fantia.jp/posts/4064942"
        plan = metadata._scrape_attempts("scene", "807", url, [javbus])
        assert [(p["id"], i) for p, i in plan] == [("FantiaJp", {metadata._URL_MARKER: url})]

        # A link nobody declares produces no attempts at all, instead of a
        # fan-out of meaningless keyword searches.
        assert metadata._scrape_attempts("scene", "807", "https://example.com/x", [javbus]) == []

    def test_scrape_runs_the_url_document(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        seen = {}

        async def spy(query, variables=None):
            seen["query"] = query
            seen["variables"] = variables
            return {"data": {"scrapeURL": {"__typename": "ScrapedScene",
                                           "title": "the real title",
                                           "urls": ["https://fantia.jp/posts/4064942"]}}}

        monkeypatch.setattr(metadata, "stash_query", spy)
        out = asyncio.run(metadata._scrape("scene", "FantiaJp",
                                           {metadata._URL_MARKER: "https://fantia.jp/posts/4064942"}))
        assert [p["title"] for p in out] == ["the real title"]
        assert "scrapeURL(" in seen["query"]
        assert "ty: SCENE" in seen["query"]
        assert seen["variables"] == {"url": "https://fantia.jp/posts/4064942"}
        # The marker must never travel as a GraphQL variable.
        assert metadata._URL_MARKER not in str(seen["variables"])

    def test_scrape_url_null_is_not_an_error(self, monkeypatch):
        """Stash returns null when no installed scraper claims the URL."""
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        async def spy(query, variables=None):
            return {"data": {"scrapeURL": None}}

        monkeypatch.setattr(metadata, "stash_query", spy)
        assert asyncio.run(metadata._scrape("scene", "x", {metadata._URL_MARKER: "https://nope/x"})) == []

    def test_scrape_url_rejects_a_mismatched_typename(self, monkeypatch):
        """`scrapeURL` returns a union; anything that is not the entity we asked
        for must not be turned into a result."""
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        async def spy(query, variables=None):
            return {"data": {"scrapeURL": {"__typename": "ScrapedPerformer", "name": "someone"}}}

        monkeypatch.setattr(metadata, "stash_query", spy)
        assert asyncio.run(metadata._scrape("scene", "x", {metadata._URL_MARKER: "https://a/b"})) == []

    def test_scrape_by_url_document_only_uses_fields_this_stash_has(self):
        from stash_jellyfin_proxy.endpoints.metadata import _URL_SCRAPE_QUERY

        scene_doc = _URL_SCRAPE_QUERY["scene"]
        assert "production_date" not in scene_doc
        for field in ("title", "code", "details", "director", "date", "urls", "image"):
            assert field in scene_doc
        assert not re.search(r"\bimage\b(?!s)", _URL_SCRAPE_QUERY["performer"])

    def test_scrape_by_url_selects_fields_inside_an_inline_fragment(self):
        """`scrapeURL` returns the `ScrapedContent` union, so concrete fields
        must sit inside `... on ScrapedScene { }`. Selecting them straight off
        the union is rejected with a bare HTTP 400 that carries no GraphQL
        error body — it reads like a transport fault, not a query fault."""
        from stash_jellyfin_proxy.endpoints.metadata import _URL_SCRAPE_QUERY

        for entity, typename in (("scene", "ScrapedScene"),
                                 ("performer", "ScrapedPerformer")):
            doc = _URL_SCRAPE_QUERY[entity]
            assert re.search(
                r"scrapeURL\([^)]*\)\s*\{\s*__typename\s*\.\.\.\s*on\s+%s\s*\{" % typename,
                doc), "%s: concrete fields must be inside `... on %s`" % (entity, typename)


class TestFragmentEchoGuard:
    """The junk hit, pinned.

    `{scene_id: <scene with no stored URL>}` is run as a fragment scrape. A
    URL-only scraper cannot fetch anything from a fragment, so it echoes it —
    observed live as one "hit" whose title was `5ff33487_Buncos_kfk.mp4`, no
    URLs, in 846ms, winning the fan-out and reaching the app as the only
    candidate while the correct scrape never got a look in.
    """

    def test_basename_echo_from_a_fragment_attempt_is_dropped(self):
        from stash_jellyfin_proxy.endpoints import metadata

        echoed = {"title": "5ff33487_Buncos_kfk.mp4", "urls": []}
        assert metadata._is_fragment_echo({"scene_id": "807"}, echoed) is True

    def test_a_fragment_attempt_that_carries_a_url_is_kept(self):
        """Scene 13's stored fantia URL resolves through the same shape — and
        that payload does carry the URL."""
        from stash_jellyfin_proxy.endpoints import metadata

        real = {"title": "❤ スターレ〇ル …", "url": "https://fantia.jp/posts/3072923",
                "urls": ["https://fantia.jp/posts/3072923"]}
        assert metadata._is_fragment_echo({"scene_id": "13"}, real) is False

    def test_a_name_search_is_never_treated_as_an_echo(self):
        from stash_jellyfin_proxy.endpoints import metadata

        hit = {"title": "some.movie.mp4"}
        assert metadata._is_fragment_echo({"query": "some title"}, hit) is False

    def test_a_url_scrape_is_never_treated_as_an_echo(self):
        from stash_jellyfin_proxy.endpoints import metadata

        assert metadata._is_fragment_echo(
            {metadata._URL_MARKER: "https://fantia.jp/posts/1"},
            {"title": "whatever.mp4"}) is False

    def test_an_empty_shell_is_unusable(self):
        """Live shape for `https://fantia.jp/posts/99999999`: the scraper
        claims the URL, finds nothing, and returns an empty object rather than
        null. As a candidate it has no name at all."""
        from stash_jellyfin_proxy.endpoints import metadata

        shell = {"__typename": "ScrapedScene", "title": None, "code": None,
                 "date": None, "url": None, "urls": None, "images": None,
                 "studio": None, "performers": None, "tags": [], "details": ""}
        assert metadata._is_empty_payload(shell) is True

        assert metadata._is_empty_payload({"title": "a real title"}) is False
        assert metadata._is_empty_payload({"code": "FANTIA-4064942"}) is False
        assert metadata._is_empty_payload({"urls": ["https://x/1"]}) is False
        assert metadata._is_empty_payload({"studio": {"name": "Fantia.jp"}}) is False
        # A performer result is named, not titled.
        assert metadata._is_empty_payload({"name": "ぶんちゃん"}) is False

    def test_an_empty_url_scrape_never_becomes_a_result(self, monkeypatch):
        """End to end: a bogus number must yield nothing, not a nameless row."""
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        fantia = {"key": "fantiajp", "id": "FantiaJp", "name": "FantiaJp",
                  "entities": [], "entities_any": ["scene"],
                  "patterns": {"scene": ["fantia.jp/posts/"]}}

        async def empty_shell(query, variables=None):
            return {"data": {"scrapeURL": {"__typename": "ScrapedScene",
                                           "title": None, "code": None, "urls": None,
                                           "studio": None, "performers": None,
                                           "tags": [], "details": ""}}}

        async def no_stored_urls(entity, numeric_id):
            return []

        async def fake_load():
            return [fantia]

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": [fantia]})
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_fetch_stored_urls", no_stored_urls)
        monkeypatch.setattr(metadata, "stash_query", empty_shell)

        assert asyncio.run(metadata._search("scene", "807", "", "99999999")) == []

    def test_the_echo_never_becomes_a_result(self, monkeypatch):
        """End to end through _search: a scene with no stored URL and only an
        echoing scraper must come back empty, not with a filename."""
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        fantia = {"key": "fantiajp", "id": "FantiaJp", "name": "FantiaJp",
                  "entities": [], "entities_any": ["scene"],
                  "patterns": {"scene": ["fantia.jp/posts/"]}}

        async def scrape(entity, scraper_id, scrape_input):
            if metadata._URL_MARKER in scrape_input:
                return []
            return [{"title": "5ff33487_Buncos_kfk.mp4", "urls": []}]

        async def no_stored_urls(entity, numeric_id):
            return []

        async def fake_load():
            return [fantia]

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": [fantia]})
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_scrape", scrape)
        monkeypatch.setattr(metadata, "_fetch_stored_urls", no_stored_urls)

        assert asyncio.run(metadata._search("scene", "807", "", "4064942")) == []


class TestAttemptPriority:
    """A targeted attempt must not be beaten by a faster guess."""

    def _mk(self, sid, name, entities):
        return {"key": sid, "id": sid, "name": name, "patterns": {},
                "entities": entities, "entities_any": ["scene"]}

    def test_targeted_attempt_beats_a_faster_name_search(self, monkeypatch):
        import asyncio
        import time
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        fast_search = self._mk("javdb", "JavDB", ["scene"])
        fantia = self._mk("FantiaJp", "FantiaJp", [])

        async def scrape(entity, scraper_id, scrape_input):
            if metadata._URL_MARKER in scrape_input:
                await asyncio.sleep(0.3)
                return [{"title": "the correct post", "urls": ["https://fantia.jp/posts/4064942"]}]
            return [{"title": "something unrelated"}]

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": [fantia, fast_search]})
        monkeypatch.setattr(metadata, "_scrape", scrape)

        attempts = [(fantia, {metadata._URL_MARKER: "https://fantia.jp/posts/4064942"}),
                    (fast_search, {"query": "4064942"})]
        deadline = time.monotonic() + 10
        winners = asyncio.run(metadata._fan_out_within("scene", attempts, deadline))
        assert [p["title"] for _prov, payloads in winners for p in payloads] == \
            ["the correct post"]

    def test_a_name_search_does_not_wait_for_other_name_searches(self, monkeypatch):
        """The wait is only for targeted attempts. Otherwise the Identify
        dialog would hang on the slowest scraper every time."""
        import asyncio
        import time
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        slow = self._mk("adulttime", "AdultTime", ["scene"])
        quick = self._mk("javdb", "JavDB", ["scene"])

        async def scrape(entity, scraper_id, scrape_input):
            if scraper_id == "adulttime":
                await asyncio.sleep(20)
                return []
            return [{"title": "quick hit"}]

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": [slow, quick]})
        monkeypatch.setattr(metadata, "_scrape", scrape)

        attempts = [(slow, {"query": "a"}), (quick, {"query": "a"})]
        started = time.monotonic()
        winners = asyncio.run(metadata._fan_out_within(
            "scene", attempts, time.monotonic() + 30))
        assert [p["title"] for _prov, payloads in winners for p in payloads] == ["quick hit"]
        assert time.monotonic() - started < 3.0


class TestNamedProviderAttempts:
    """The provider-scoped search (the app's per-provider Identify)."""

    def _fantia(self, patterns=None):
        return {"key": "fantiajp", "id": "FantiaJp", "name": "FantiaJp",
                "entities": [], "entities_any": ["scene"],
                "patterns": patterns or {"scene": ["fantia.jp/posts/"]}}

    def test_a_url_only_scraper_resolves_by_display_name(self, monkeypatch):
        """Every result we hand out carries `SearchProviderName` = the
        scraper's name, and a client may echo it back on the next request.
        Resolving it against the NAME-capable pool alone answered "unknown
        provider" and returned nothing, because FantiaJp never appears there.
        """
        from stash_jellyfin_proxy.endpoints import metadata

        fantia = self._fantia()
        javbus = {"key": "javbus", "id": "javbus", "name": "JavBus",
                  "entities": ["scene"], "entities_any": ["scene"], "patterns": {}}
        monkeypatch.setattr(metadata, "_providers",
                            {"at": 1.0, "items": [fantia, javbus]})

        assert metadata._resolve_provider("FantiaJp", "scene")["id"] == "FantiaJp"
        assert metadata._resolve_provider("fantiajp", "scene")["id"] == "FantiaJp"
        assert metadata._resolve_provider("JavBus", "scene")["id"] == "javbus"
        assert metadata._resolve_provider("nope", "scene") is None

    def _fantia(self, patterns=None):
        return {"key": "fantiajp", "id": "FantiaJp", "name": "FantiaJp",
                "entities": [], "entities_any": ["scene"],
                "patterns": patterns or {"scene": ["fantia.jp/posts/"]}}

    def _javdb(self):
        return {"key": "javdb", "id": "javdb", "name": "JavDB",
                "entities": ["scene"], "entities_any": ["scene"], "patterns": {}}

    def test_numeric_term_becomes_a_url_scrape(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        plan = asyncio.run(metadata._named_provider_attempts(
            "scene", self._fantia(), "4064942", "807"))
        assert [(p["id"], i) for p, i in plan] == [
            ("FantiaJp", {metadata._URL_MARKER: "https://fantia.jp/posts/4064942"})]

    def test_a_pasted_url_is_used_as_is(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        url = "https://fantia.jp/posts/4064942"
        monkeypatch.setattr(runtime, "config", {}, raising=False)
        plan = asyncio.run(metadata._named_provider_attempts(
            "scene", self._fantia(), url, "807"))
        assert [(p["id"], i) for p, i in plan] == [("FantiaJp", {metadata._URL_MARKER: url})]

    def test_a_numeric_code_for_a_name_scraper_stays_a_query(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        plan = asyncio.run(metadata._named_provider_attempts(
            "scene", self._javdb(), "1234567", "807"))
        assert [(p["id"], i) for p, i in plan] == [("javdb", {"query": "1234567"})]

    def test_url_only_scraper_with_no_term_needs_a_stored_url(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.setattr(runtime, "config", {}, raising=False)

        async def no_urls(entity, numeric_id):
            return []

        monkeypatch.setattr(metadata, "_fetch_stored_urls", no_urls)
        assert asyncio.run(metadata._named_provider_attempts(
            "scene", self._fantia(), "", "807")) == []

        async def stored(entity, numeric_id):
            return ["https://fantia.jp/posts/3072923"]

        monkeypatch.setattr(metadata, "_fetch_stored_urls", stored)
        plan = asyncio.run(metadata._named_provider_attempts(
            "scene", self._fantia(), "", "13"))
        assert [(p["id"], i) for p, i in plan] == [("FantiaJp", {"scene_id": "13"})]

    def test_a_url_the_provider_does_not_declare_falls_back_to_its_search(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        javbus = {"key": "javbus", "id": "javbus", "name": "JavBus",
                  "entities": ["scene"], "entities_any": ["scene"],
                  "patterns": {"scene": ["https://www.javbus.com"]}}
        monkeypatch.setattr(runtime, "config", {}, raising=False)
        plan = asyncio.run(metadata._named_provider_attempts(
            "scene", javbus, "https://fantia.jp/posts/1", "807"))
        # JavBus supports NAME, so the term survives as a query rather than
        # being bounced through a URL entry point another scraper would answer.
        assert [(p["id"], i) for p, i in plan] == [("javbus", {"query": "https://fantia.jp/posts/1"})]


# --- config-UI exposure ----------------------------------------------------

_SCRAPING_KEYS = (
    "ENABLE_SCRAPING",
    "SCRAPE_APPLY_RELATIONSHIPS",
    "SCRAPE_APPLY_IMAGES",
    "SCRAPE_RESULT_TTL_SECONDS",
    "SCRAPE_NUMERIC_SCRAPERS",
    "SCRAPE_NUMERIC_URL_TEMPLATES",
    "SCRAPE_STASHBOX_ENABLED",
)


def test_scraping_keys_are_exposed_by_the_config_api():
    """`_P5B_KEYS` is the single registry that drives both `GET /api/config`
    and the config writer's global-scope insert. A key missing from it has no
    effective value to report and cannot be persisted from the UI, which reads
    as the feature being broken even though the code works."""
    from stash_jellyfin_proxy.ui.api import _P5B_KEYS, _p5b_get_value

    registered = {key for key, *_ in _P5B_KEYS}
    for key in _SCRAPING_KEYS:
        assert key in registered, f"{key} missing from _P5B_KEYS"

    # And the accessor the config endpoint actually calls must resolve them.
    for key in _SCRAPING_KEYS:
        assert _p5b_get_value(key) is not None, f"{key} resolves to None"


def test_scraping_keys_are_read_live():
    """These gates are consulted per request, so marking them non-live would
    make the UI report a restart as required when it is not."""
    from stash_jellyfin_proxy.ui.api import _P5B_KEYS

    by_key = {key: live for key, _attr, _kind, _default, live in _P5B_KEYS}
    for key in _SCRAPING_KEYS:
        assert by_key[key] is True, f"{key} should be live"


# --- stash boxes: the configured boxes as a name source ---------------------

class TestStashBoxSearch:
    """The configured stash boxes (StashDB, ThePornDB, …) are the only source
    that knows most western studio content, yet no installed scraper covers
    them. A filename like `0541-LyaCutie-2160p` must reach them as `Lya
    Cutie` — via `source: {stash_box_index: N}`, the other half of
    ScraperSourceInput that this proxy never spoke."""

    def test_name_query_from_term(self):
        from stash_jellyfin_proxy.endpoints.metadata import _name_query_from_term

        assert _name_query_from_term("0541-LyaCutie-2160p") == "Lya Cutie"
        assert _name_query_from_term("kv-139") == "kv 139"
        assert _name_query_from_term("cospuri-539") == "cospuri 539"
        # scene numbers up to three digits ride along; ids and years do not
        assert _name_query_from_term("3072923") == ""
        assert _name_query_from_term("1006291") == ""
        assert _name_query_from_term("") == ""

    def test_scrape_sends_the_stash_box_source(self, monkeypatch):
        """`_BOX_MARKER` must be translated into
        `source: {stash_box_index: N}` and never reach the wire."""
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        sent = []

        async def spy(query, variables=None, retries=None):
            sent.append(variables)
            return {"data": {"scrapeSingleScene": [{"title": "found"}]}}

        monkeypatch.setattr(metadata, "stash_query", spy)
        out = asyncio.run(metadata._scrape(
            "scene", "stashbox0", {metadata._BOX_MARKER: 0, "query": "Lya Cutie"}))
        assert out == [{"title": "found"}]
        assert sent and sent[0]["source"] == {"stash_box_index": 0}
        assert metadata._BOX_MARKER not in str(sent[0]["input"])

    def test_filename_term_searches_the_boxes(self, monkeypatch):
        """The live case: scene-320, no stored URL, term `0541-LyaCutie-2160p`.
        StashDB (queried as `Lya Cutie`) returns the one result the dialog
        should show."""
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        async def fake_load():
            return []

        async def boxes():
            return ["StashDB", "ThePornDB"]

        async def stored(entity, numeric_id):
            return []

        async def scrape(entity, scraper_id, scrape_input):
            if scrape_input.get(metadata._BOX_MARKER) == 0:
                return [{"title": "Lya Cutie #1", "date": "2024-09-27",
                         "urls": ["https://www.cospuri.com/sample?id=0541abcd"],
                         "performers": [{"name": "Lya Cutie"}]}]
            return []

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": []})
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_load_stash_boxes", boxes)
        monkeypatch.setattr(metadata, "_fetch_stored_urls", stored)
        monkeypatch.setattr(metadata, "_scrape", scrape)

        results = asyncio.run(metadata._search("scene", "320", "", "0541-LyaCutie-2160p"))
        assert any(r["Name"] == "Lya Cutie #1" for r in results)
        assert any(r["SearchProviderName"] == "StashDB" for r in results)

    def test_explicit_stashdb_provider_queried_alone(self, monkeypatch):
        """Naming a box in the dialog's provider field scopes the search to
        that box instead of the full fan-out."""
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        seen = []

        async def fake_load():
            return []

        async def boxes():
            return ["StashDB", "ThePornDB"]

        async def scrape(entity, scraper_id, scrape_input):
            seen.append((scraper_id, dict(scrape_input)))
            return [{"title": "Lya Cutie #1"}]

        monkeypatch.setattr(runtime, "config", {}, raising=False)
        monkeypatch.setattr(metadata, "_providers", {"at": 1.0, "items": []})
        monkeypatch.setattr(metadata, "_load_providers", fake_load)
        monkeypatch.setattr(metadata, "_load_stash_boxes", boxes)
        monkeypatch.setattr(metadata, "_scrape", scrape)

        results = asyncio.run(metadata._search("scene", "320", "StashDB", "Lya Cutie"))
        assert [r["Name"] for r in results] == ["Lya Cutie #1"]
        assert len(seen) == 1 and seen[0][1][metadata._BOX_MARKER] == 0

    def test_disabled_gate_removes_the_attempts(self, monkeypatch):
        import asyncio
        from stash_jellyfin_proxy import runtime
        from stash_jellyfin_proxy.endpoints import metadata

        monkeypatch.setattr(runtime, "config", {"SCRAPE_STASHBOX_ENABLED": "false"},
                            raising=False)
        assert asyncio.run(metadata._stashbox_attempts("scene", "Lya Cutie")) == []

    def test_fallback_ranks_the_name_search_hits(self, monkeypatch):
        """With no targeted attempt in play — the every-box-is-a-name-search
        case — the returned list is the ranked set, not just one winner: the
        dialog wants the plausible candidates, best first."""
        import asyncio
        from stash_jellyfin_proxy.endpoints import metadata

        javbus = {"key": "javbus", "id": "javbus", "name": "JavBus",
                  "entities": ["scene"], "entities_any": ["scene"]}
        box = {"key": "stashbox0", "id": "stashbox0", "name": "StashDB",
               "entities": ["scene"], "entities_any": ["scene"]}

        async def scrape(entity, scraper_id, scrape_input):
            if scraper_id == "javbus":
                return [{"title": "Totally Unrelated Video"}]
            return [{"title": "Lya Cutie #1",
                     "performers": [{"name": "Lya Cutie"}]}]

        monkeypatch.setattr(metadata, "_scrape", scrape)
        ordered = asyncio.run(metadata._fan_out_within(
            "scene", [(javbus, {"query": "0541-LyaCutie"}),
                      (box, {"query": "Lya Cutie"})],
            time.monotonic() + 30, seed_text="0541-LyaCutie-2160p"))
        names = [p["title"] for _prov, hits in ordered for p in hits]
        assert names == ["Lya Cutie #1", "Totally Unrelated Video"]

