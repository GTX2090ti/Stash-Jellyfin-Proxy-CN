"""Endpoint-level tests for the metadata scraping bridge.

These drive the handlers directly (no live Stash, no HTTP server) with a
faked `stash_query`, so the full request -> Stash-call -> response path is
covered: status codes, JSON shapes and the exact GraphQL payloads the
proxy hands to Stash.
"""
import asyncio
import json

import pytest
from starlette.requests import Request

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.endpoints import metadata


# --- harness ---------------------------------------------------------------

def make_request(method="GET", path="/", path_params=None, query="",
                 headers=None, body=b""):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": raw_headers,
        "path_params": path_params or {},
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8096),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def run(coro):
    return asyncio.run(coro)


def body_of(response):
    return json.loads(response.body.decode("utf-8")) if response.body else None


class FakeStash:
    """Records GraphQL calls and answers per-operation."""

    def __init__(self, scraper_list=None, scrape_results=None, mutations_ok=True):
        self.calls = []
        self.scraper_list = scraper_list if scraper_list is not None else [
            {"id": "StashDB", "name": "StashDB",
             "scene": {"supported_scrapes": ["NAME", "FRAGMENT"]},
             "performer": {"supported_scrapes": ["NAME", "FRAGMENT"]}},
            {"id": "ThePornDB", "name": "ThePornDB",
             "scene": None, "performer": {"supported_scrapes": ["NAME"]}},
            {"id": "URL Only", "name": "URL Only",
             "scene": {"supported_scrapes": ["URL"]}, "performer": None},
        ]
        # op -> list of payloads returned by the scrape queries
        self.scrape_results = scrape_results if scrape_results is not None else {}
        self.mutations_ok = mutations_ok

    async def __call__(self, query, variables=None, retries=None):
        variables = variables or {}
        self.calls.append((query, variables))

        if "listScrapers" in query:
            return {"data": {"listScrapers": self.scraper_list}}
        if "scrapeSingleScene" in query:
            return {"data": {"scrapeSingleScene": self.scrape_results.get("scene", [])}}
        if "scrapeSinglePerformer" in query:
            return {"data": {"scrapeSinglePerformer": self.scrape_results.get("performer", [])}}
        if "findScene" in query:
            return {"data": {"findScene": {"id": variables.get("id"), "title": "Seed Title"}}}
        # Plural before singular — "findPerformers" contains "findPerformer".
        if "findPerformers" in query:
            return {"data": {"findPerformers": {"performers": [{"id": "perf-1", "name": variables.get("name")}]}}}
        if "findPerformer" in query:
            return {"data": {"findPerformer": {"id": variables.get("id"), "name": "Seed Name"}}}
        if "findStudios" in query:
            return {"data": {"findStudios": {"studios": [{"id": "studio-1", "name": variables.get("name")}]}}}
        if "findTags" in query:
            return {"data": {"findTags": {"tags": [{"id": "tag-1", "name": variables.get("q")}]}}}
        if "tagCreate" in query:
            return {"data": {"tagCreate": {"id": "tag-new", "name": variables.get("input", {}).get("name")}}}
        if "performerCreate" in query:
            return {"data": {"performerCreate": {"id": "perf-new"}}}
        if "studioCreate" in query:
            return {"data": {"studioCreate": {"id": "studio-new"}}}
        if "metadataScan" in query:
            return {"data": {"metadataScan": "job-1"}}
        if "sceneUpdate" in query:
            return {"data": {"sceneUpdate": {"id": variables.get("input", {}).get("id")}}} if self.mutations_ok else {"errors": ["boom"], "data": {}}
        if "performerUpdate" in query:
            return {"data": {"performerUpdate": {"id": variables.get("input", {}).get("id")}}} if self.mutations_ok else {"errors": ["boom"], "data": {}}
        return {"data": {}}

    def inputs(self, marker):
        """All `input` payloads passed to a mutation whose query contains marker."""
        return [v["input"] for q, v in self.calls if marker in q and "input" in v]


@pytest.fixture
def stash(monkeypatch):
    fake = FakeStash()
    monkeypatch.setattr(metadata, "stash_query", fake)
    # `stash.tags` holds its own reference to the client (imported by name)
    # plus a process-wide tag-id cache — patch and clear both.
    from stash_jellyfin_proxy.stash import tags as stash_tags
    monkeypatch.setattr(stash_tags, "stash_query", fake)
    monkeypatch.setattr(stash_tags, "_tag_id_cache", {})
    # The provider registry is process-global; make each test start cold.
    monkeypatch.setattr(metadata, "_providers", {"at": 0.0, "items": []})
    return fake


@pytest.fixture(autouse=True)
def _clean_config():
    saved = dict(runtime.config)
    runtime.config.clear()
    yield
    runtime.config.clear()
    runtime.config.update(saved)


# --- MetadataEditor --------------------------------------------------------

def test_metadata_editor_lists_only_text_searchable_scrapers(stash):
    response = run(metadata.endpoint_metadata_editor(
        make_request(path_params={"item_id": "scene-1"})))
    payload = body_of(response)

    assert response.status_code == 200
    assert payload["ContentType"] == "Video"
    assert [(i["Name"], i["Key"]) for i in payload["ExternalIdInfos"]] == [("StashDB", "StashDB")]
    # "ThePornDB" (performer only) and "URL Only" (no NAME support) are not
    # offered for a scene.
    assert all(i["Type"] == "Video" for i in payload["ExternalIdInfos"])


def test_metadata_editor_for_performer_lists_both_providers(stash):
    response = run(metadata.endpoint_metadata_editor(
        make_request(path_params={"item_id": "person-performer-7"})))
    payload = body_of(response)

    assert payload["ContentType"] == "Person"
    assert sorted(i["Name"] for i in payload["ExternalIdInfos"]) == ["StashDB", "ThePornDB"]


def test_metadata_editor_empty_for_unsupported_entity(stash):
    payload = body_of(run(metadata.endpoint_metadata_editor(
        make_request(path_params={"item_id": "studio-3"}))))
    assert payload["ExternalIdInfos"] == []
    # No scraper enumeration should even be attempted.
    assert stash.calls == []


def test_master_switch_disables_providers(stash):
    runtime.config["ENABLE_SCRAPING"] = "false"
    payload = body_of(run(metadata.endpoint_metadata_editor(
        make_request(path_params={"item_id": "scene-1"}))))
    assert payload["ExternalIdInfos"] == []
    assert stash.calls == []


# --- RemoteSearch ----------------------------------------------------------

def test_remote_search_get_shapes_results_and_hands_out_token(stash):
    stash.scrape_results["scene"] = [{
        "title": "Found Scene", "date": "2020-02-03", "details": "d",
        "image": "http://img/a.jpg", "remote_site_id": "rs-1",
        "studio": {"name": "Acme"}, "performers": [{"name": "Jane"}], "tags": [{"name": "tag1"}],
    }]
    response = run(metadata.endpoint_remote_search(make_request(
        path_params={"item_id": "scene-42", "search_provider_name": "StashDB"},
        query="searchTerm=Found")))

    assert response.status_code == 200
    results = body_of(response)
    assert len(results) == 1
    assert results[0]["Name"] == "Found Scene"
    assert results[0]["SearchProviderName"] == "StashDB"
    token = results[0]["ProviderIds"]["Stash"]
    assert metadata._take_result(token)["payload"]["title"] == "Found Scene"


def test_remote_search_unknown_provider_returns_nothing(stash):
    response = run(metadata.endpoint_remote_search(make_request(
        path_params={"item_id": "scene-42", "search_provider_name": "no-such-scraper"},
        query="searchTerm=x")))
    assert body_of(response) == []


def test_remote_search_post_uses_body_term(stash):
    stash.scrape_results["performer"] = [{"name": "Jane", "images": ["http://i/1.jpg"]}]
    response = run(metadata.endpoint_remote_search_post(make_request(
        method="POST",
        path="/Items/RemoteSearch/Person",
        path_params={"item_type": "Person"},
        headers={"content-type": "application/json"},
        body=json.dumps({
            "SearchInfo": {"Name": "Jane"},
            "ItemId": "person-performer-5",
            "SearchProviderName": "StashDB",
        }).encode(),
    )))

    results = body_of(response)
    assert len(results) == 1
    assert results[0]["Name"] == "Jane"
    # The performer scrape query is the one that ran.
    assert any("scrapeSinglePerformer" in q for q, _ in stash.calls)


# --- Apply -----------------------------------------------------------------

def test_apply_uses_parked_result_and_writes_scene(stash):
    stash.scrape_results["scene"] = [{
        "title": "Applied Title", "details": "Applied details", "date": "2021-01-02",
        "urls": ["http://u/1"], "studio": {"name": "Acme"},
        "performers": [{"name": "Jane"}], "tags": [{"name": "t1"}],
    }]
    results = body_of(run(metadata.endpoint_remote_search(make_request(
        path_params={"item_id": "scene-42", "search_provider_name": "StashDB"},
        query="searchTerm=x"))))
    token = results[0]["ProviderIds"]["Stash"]

    response = run(metadata.endpoint_remote_search_apply(make_request(
        method="POST",
        path="/Items/RemoteSearch/Apply/scene-42",
        path_params={"item_id": "scene-42"},
        headers={"content-type": "application/json"},
        body=json.dumps({"ProviderIds": {"Stash": token}, "Name": "Applied Title"}).encode(),
    )))

    assert response.status_code == 204
    (update,) = stash.inputs("sceneUpdate")
    assert update["id"] == "42"
    assert update["title"] == "Applied Title"
    assert update["details"] == "Applied details"
    assert update["date"] == "2021-01-02"
    assert update["urls"] == ["http://u/1"]
    # Relationships resolve by name through find-or-create.
    assert "studio_id" in update and "performer_ids" in update and "tag_ids" in update


def test_apply_can_skip_relationships(stash):
    runtime.config["SCRAPE_APPLY_RELATIONSHIPS"] = "false"
    stash.scrape_results["scene"] = [{"title": "T", "studio": {"name": "Acme"},
                                      "performers": [{"name": "Jane"}], "tags": [{"name": "t1"}]}]
    results = body_of(run(metadata.endpoint_remote_search(make_request(
        path_params={"item_id": "scene-9", "search_provider_name": "StashDB"},
        query="searchTerm=x"))))
    token = results[0]["ProviderIds"]["Stash"]

    run(metadata.endpoint_remote_search_apply(make_request(
        method="POST", path_params={"item_id": "scene-9"},
        headers={"content-type": "application/json"},
        body=json.dumps({"ProviderIds": {"Stash": token}}).encode())))

    (update,) = stash.inputs("sceneUpdate")
    assert update == {"id": "9", "title": "T"}


def test_apply_writes_performer_scalars(stash):
    stash.scrape_results["performer"] = [{
        "name": "Jane", "birthdate": "1985-06-07", "height": "170 cm",
        "weight": "55 kg", "gender": "Transgender Female", "circumcised": "No",
        "aliases": "J. Doe, Janey", "urls": ["http://u/p"],
    }]
    results = body_of(run(metadata.endpoint_remote_search(make_request(
        path_params={"item_id": "performer-11", "search_provider_name": "StashDB"},
        query="searchTerm=jane"))))
    token = results[0]["ProviderIds"]["Stash"]

    response = run(metadata.endpoint_remote_search_apply(make_request(
        method="POST", path_params={"item_id": "performer-11"},
        headers={"content-type": "application/json"},
        body=json.dumps({"ProviderIds": {"Stash": token}}).encode())))

    assert response.status_code == 204
    (update,) = stash.inputs("performerUpdate")
    assert update["height_cm"] == 170
    assert update["weight"] == 55
    assert update["gender"] == "TRANSGENDER_FEMALE"
    assert update["circumcised"] == "UNCUT"
    assert update["alias_list"] == ["J. Doe", "Janey"]


def test_apply_without_match_returns_404(stash):
    response = run(metadata.endpoint_remote_search_apply(make_request(
        method="POST", path_params={"item_id": "scene-42"},
        headers={"content-type": "application/json"},
        body=json.dumps({"Name": "nothing here"}).encode())))
    assert response.status_code == 404
    assert body_of(response)["error"] == "no_match"


def test_apply_reports_stash_failure(stash):
    stash.mutations_ok = False
    stash.scrape_results["scene"] = [{"title": "T"}]
    results = body_of(run(metadata.endpoint_remote_search(make_request(
        path_params={"item_id": "scene-9", "search_provider_name": "StashDB"},
        query="searchTerm=x"))))
    token = results[0]["ProviderIds"]["Stash"]

    response = run(metadata.endpoint_remote_search_apply(make_request(
        method="POST", path_params={"item_id": "scene-9"},
        headers={"content-type": "application/json"},
        body=json.dumps({"ProviderIds": {"Stash": token}}).encode())))
    assert response.status_code == 502


# --- Refresh / scan / update ----------------------------------------------

def test_refresh_metadata_auto_matches_and_applies(stash):
    stash.scrape_results["scene"] = [{"title": "Auto Matched"}]
    response = run(metadata.endpoint_refresh_metadata(make_request(
        method="POST",
        path_params={"item_id": "scene-77"},
        query="metadataRefreshMode=FullRefresh")))
    assert response.status_code == 204
    (update,) = stash.inputs("sceneUpdate")
    assert update["title"] == "Auto Matched"


def test_refresh_metadata_ignores_unsupported_item(stash):
    response = run(metadata.endpoint_refresh_metadata(
        make_request(method="POST", path_params={"item_id": "group-4"})))
    assert response.status_code == 204
    assert stash.calls == []


def test_library_refresh_queues_scan(stash):
    response = run(metadata.endpoint_library_refresh(make_request(method="POST")))
    assert response.status_code == 204
    assert any("metadataScan" in q for q, _ in stash.calls)


def test_update_item_maps_client_metadata_into_stash(stash):
    response = run(metadata.endpoint_update_item(make_request(
        method="POST",
        path_params={"item_id": "scene-5"},
        headers={"content-type": "application/json"},
        body=json.dumps({
            "Name": "New Name", "Overview": "New overview",
            "PremiereDate": "2019-08-09T00:00:00.0000000Z",
            "Genres": ["g1"], "Tags": ["t1"],
        }).encode())))

    assert response.status_code == 204
    (update,) = stash.inputs("sceneUpdate")
    assert update["title"] == "New Name"
    assert update["details"] == "New overview"
    assert update["date"] == "2019-08-09"


def test_image_upload_pushes_base64_cover(stash):
    response = run(metadata.endpoint_item_image_upload(make_request(
        method="POST",
        path_params={"item_id": "scene-5", "image_type": "Primary"},
        headers={"content-type": "image/png"},
        body=b"\x89PNG-fake-bytes")))

    assert response.status_code == 204
    (update,) = stash.inputs("sceneUpdate")
    assert update["cover_image"].startswith("data:image/png;base64,")


def test_image_delete_is_acknowledged(stash):
    response = run(metadata.endpoint_item_image_upload(make_request(
        method="DELETE", path_params={"item_id": "scene-5", "image_type": "Primary"})))
    assert response.status_code == 204
    assert stash.calls == []
