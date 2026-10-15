"""Scene-level Similar / InstantMix / Suggestions.

`/Items/{scene}/Similar` used to be hard-wired empty ("Stash has no
similarity relation"). similar_scenes_for now derives "more like this"
from shared performers (weight 3), tags (weight 1) and the same studio
(weight 1), over a bounded concurrent candidate pool. These tests pin
ranking, self-exclusion, pagination, id resolution, and the
InstantMix/Suggestions rails.
"""
import asyncio
import json

import pytest
from starlette.requests import Request

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.endpoints import items as items_mod
from stash_jellyfin_proxy.endpoints import stubs as stubs_mod
from stash_jellyfin_proxy.endpoints.items import (
    instant_mix_for,
    similar_scenes_for,
    suggested_items,
)

# Seed scene 1: performers 10+11, tag 100, studio 1.
# Candidate graph — scores against the seed:
#   scene 2: performer 10 + studio 1        -> 3+1 = 4
#   scene 3: performers 10+11               -> 6
#   scene 4: tag 100 + studio 1             -> 1+1 = 2
#   scene 5: studio 1                       -> 1
DATA = {
    1: {"p": [10, 11], "t": [100], "s": True},
    2: {"p": [10], "t": [], "s": True},
    3: {"p": [10, 11], "t": [], "s": False},
    4: {"p": [], "t": [100], "s": True},
    5: {"p": [], "t": [], "s": True},
    # 7 is only used by the detail/MediaSegments HTTP smoke tests.
    7: {"p": [], "t": [], "s": False},
}

DEFAULT_SEED = {
    "id": "1",
    "performers": [{"id": "10"}, {"id": "11"}],
    "tags": [{"id": "100"}],
    "studio": {"id": "1"},
}


def _scene_payload(i):
    d = DATA[i]
    return {
        "id": str(i),
        "title": f"Scene {i}",
        "date": f"2024-01-0{i}",
        "files": [{"path": f"/media/{i}.mp4", "duration": 100}],
        "performers": [{"id": str(p)} for p in d["p"]],
        "tags": [{"id": str(t), "name": f"tag{t}"} for t in d["t"]],
        "studio": {"id": "1", "name": "Studio", "tags": []} if d["s"] else None,
        "captions": [],
        "stash_ids": [],
    }


@pytest.fixture
def fake_stash(monkeypatch):
    calls = []
    mode = {"seed": DEFAULT_SEED}

    async def _fake(query, variables=None):
        calls.append((query, variables or {}))
        if "SimilaritySeed" in query:
            return {"data": {"findScene": mode["seed"]}}
        if "RelatedScenes" in query:
            if "performers: {value:" in query:
                ids = [1, 2, 3]
            elif "tags: {value:" in query:
                ids = [1, 4]
            else:  # studios edge
                ids = [1, 2, 4, 5]
            return {"data": {"findScenes": {"scenes": [_scene_payload(i) for i in ids]}}}
        if "CountScenes" in query:
            return {"data": {"findScenes": {"count": 90}}}
        if "PagedScenes" in query:
            return {"data": {"findScenes": {"scenes": [_scene_payload(2)]}}}
        return {"data": {}}

    async def _fake_pair(count_query, count_vars, page_query, page_vars):
        return await _fake(count_query, count_vars), await _fake(page_query, page_vars)

    monkeypatch.setattr(items_mod, "stash_query", _fake)
    monkeypatch.setattr(items_mod, "stash_query_pair", _fake_pair)
    return {"calls": calls, "mode": mode}


def _req(path_params=None, qs: bytes = b""):
    return Request({
        "type": "http", "method": "GET",
        "path_params": path_params or {}, "query_string": qs, "headers": [],
    })


# --- ranking -------------------------------------------------------------

def test_ranking_by_overlap_and_self_exclusion(fake_stash):
    data = asyncio.run(similar_scenes_for("scene-1"))
    ids = [i["Id"] for i in data["Items"]]
    assert ids == ["scene-3", "scene-2", "scene-4", "scene-5"]
    assert "scene-1" not in ids
    assert data["TotalRecordCount"] == 4
    assert data["StartIndex"] == 0


def test_seed_queries_use_includes_edges(fake_stash):
    asyncio.run(similar_scenes_for("scene-1"))
    joined = " ".join(q for q, _ in fake_stash["calls"])
    assert "performers: {value: $ids, modifier: INCLUDES}" in joined
    assert "tags: {value: $ids, modifier: INCLUDES}" in joined
    assert "studios: {value: $ids, modifier: INCLUDES}" in joined
    # three pool queries + one seed
    assert sum(1 for q, _ in fake_stash["calls"] if "RelatedScenes" in q) == 3


def test_pagination_slice(fake_stash):
    first = asyncio.run(similar_scenes_for("scene-1", limit=2, start_index=0))
    assert [i["Id"] for i in first["Items"]] == ["scene-3", "scene-2"]

    tail = asyncio.run(similar_scenes_for("scene-1", limit=2, start_index=2))
    assert [i["Id"] for i in tail["Items"]] == ["scene-4", "scene-5"]


def test_multifile_id_resolves_to_scene(fake_stash):
    data = asyncio.run(similar_scenes_for("scene-1-f99"))
    assert "scene-1" not in [i["Id"] for i in data["Items"]]
    assert data["TotalRecordCount"] == 4


def test_missing_seed_returns_empty(fake_stash):
    fake_stash["mode"]["seed"] = None
    data = asyncio.run(similar_scenes_for("scene-404"))
    assert data == {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}
    # seed fetch happened, no pool fanned out
    assert len(fake_stash["calls"]) == 1
    assert "SimilaritySeed" in fake_stash["calls"][0][0]


def test_seed_without_edges_skips_pool_queries(fake_stash):
    fake_stash["mode"]["seed"] = {"id": "9", "performers": [], "tags": [], "studio": None}
    data = asyncio.run(similar_scenes_for("scene-9"))
    assert data["Items"] == []
    assert not any("RelatedScenes" in q for q, _ in fake_stash["calls"])


# --- endpoint dispatch ---------------------------------------------------

def test_endpoint_scene_similar_honours_client_limit(fake_stash):
    """Unlike containers, scene rails must NOT be widened to the
    200-item container page — Yamby's Limit=10 is the intended size."""
    req = _req({"item_id": "scene-1"}, b"Limit=10&StartIndex=0")
    body = json.loads(asyncio.run(stubs_mod.endpoint_similar(req)).body)
    assert [i["Id"] for i in body["Items"]] == ["scene-3", "scene-2", "scene-4", "scene-5"]
    per_pages = [v.get("per_page") for _, v in fake_stash["calls"] if "per_page" in v]
    assert stubs_mod.SIMILAR_CONTAINER_PAGE_LIMIT not in per_pages


def test_endpoint_scene_similar_limit_applied(fake_stash):
    req = _req({"item_id": "scene-1"}, b"Limit=2")
    body = json.loads(asyncio.run(stubs_mod.endpoint_similar(req)).body)
    assert [i["Id"] for i in body["Items"]] == ["scene-3", "scene-2"]


# --- InstantMix ----------------------------------------------------------

def test_instant_mix_scene_draws_from_related_pool(fake_stash):
    data = asyncio.run(instant_mix_for("scene-1", limit=50))
    ids = {i["Id"] for i in data["Items"]}
    assert ids <= {"scene-2", "scene-3", "scene-4", "scene-5"}
    assert data["TotalRecordCount"] == 4


def test_instant_mix_container_is_scoped_random(fake_stash):
    data = asyncio.run(instant_mix_for("studio-1", limit=10))
    assert [i["Id"] for i in data["Items"]] == ["scene-2"]
    assert data["TotalRecordCount"] == 90
    mix_queries = [(q, v) for q, v in fake_stash["calls"] if "PagedScenes" in q]
    assert mix_queries and mix_queries[0][1]["sort"] == "random"
    assert "studios: {value: $ids, modifier: INCLUDES}" in mix_queries[0][0]
    assert mix_queries[0][1]["ids"] == ["1"]


def test_instant_mix_library_is_unscoped_random(fake_stash):
    asyncio.run(instant_mix_for("root-scenes", limit=10))
    mix_queries = [(q, v) for q, v in fake_stash["calls"] if "PagedScenes" in q]
    assert mix_queries and mix_queries[0][1]["sort"] == "random"
    assert "scene_filter" not in mix_queries[0][0]


def test_instant_mix_endpoint_wrapper(fake_stash):
    req = _req({"item_id": "studio-1"}, b"Limit=5")
    body = json.loads(asyncio.run(stubs_mod.endpoint_instant_mix(req)).body)
    assert body["Items"] and body["TotalRecordCount"] == 90


# --- Suggestions ---------------------------------------------------------

def test_suggestions_latest_scenes(fake_stash):
    data = asyncio.run(suggested_items(limit=20, start_index=0))
    assert [i["Id"] for i in data["Items"]] == ["scene-2"]
    assert data["TotalRecordCount"] == 90
    page_queries = [(q, v) for q, v in fake_stash["calls"] if "PagedScenes" in q]
    assert page_queries and page_queries[0][1]["sort"] == "created_at"
    assert data["Items"][0]["ParentId"] == "root-scenes"


def test_suggestions_endpoint_wrapper(fake_stash):
    req = _req(qs=b"Limit=12")
    body = json.loads(asyncio.run(stubs_mod.endpoint_items_suggestions(req)).body)
    assert body["Items"] and body["TotalRecordCount"] == 90
    page_queries = [v for _, v in fake_stash["calls"] if v.get("per_page")]
    assert any(v["per_page"] == 12 for v in page_queries)


# --- End-to-end through the real ASGI app --------------------------------
#
# Contract tests at the handler level prove ranking; this proves the
# routes actually resolve through the middleware stack (auth,
# case-insensitive paths) and that the detail DTO carries Chapters.

@pytest.fixture
def client(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    from stash_jellyfin_proxy import app as app_mod
    from stash_jellyfin_proxy import runtime
    from stash_jellyfin_proxy.mapping import genre as genre_mod
    from stash_jellyfin_proxy.middleware import auth as auth_mod
    from stash_jellyfin_proxy.stash import scene as stash_scene_mod

    async def _fake(query, variables=None):
        if "SimilaritySeed" in query:
            return {"data": {"findScene": DEFAULT_SEED}}
        if "RelatedScenes" in query:
            ids = [1, 2, 3] if "performers: {value:" in query else [1, 4]
            return {"data": {"findScenes": {"scenes": [_scene_payload(i) for i in ids]}}}
        if "CountScenes" in query:
            return {"data": {"findScenes": {"count": 90}}}
        if "PagedScenes" in query:
            return {"data": {"findScenes": {"scenes": [_scene_payload(2)]}}}
        # Detail fetch (/Items/{id}) — scene WITH markers.
        scene = _scene_payload(7)
        scene.update({
            "markers": [
                {"id": "1", "seconds": 5, "title": "Intro",
                 "primary_tag": {"id": "t1", "name": "Intro"}, "tags": []},
            ],
        })
        return {"data": {"findScene": scene}}

    async def _fake_pair(count_query, count_vars, page_query, page_vars):
        return await _fake(count_query, count_vars), await _fake(page_query, page_vars)

    async def _fake_markers(numeric_id):
        return {
            "files": [{"duration": 100}],
            "markers": [{"id": "1", "seconds": 5, "title": "Intro",
                         "primary_tag": {"id": "t1", "name": "Intro"}, "tags": []}],
        }

    async def _fake_genres():
        return None

    monkeypatch.setattr(items_mod, "stash_query", _fake)
    monkeypatch.setattr(items_mod, "stash_query_pair", _fake_pair)
    monkeypatch.setattr(stash_scene_mod, "get_scene_markers", _fake_markers)
    monkeypatch.setattr(genre_mod, "genre_allowed_names", _fake_genres)
    monkeypatch.setattr(runtime, "CONFIG_FILE", str(tmp_path / "sjp.conf"), raising=False)
    monkeypatch.setattr(runtime, "ACCESS_TOKEN", "test-token", raising=False)
    monkeypatch.setattr(auth_mod, "_ip_failures", {})

    c = TestClient(app_mod.app)
    c.headers.update({"X-Emby-Token": "test-token"})
    yield c
    c.close()


def test_http_scene_similar_route(client):
    r = client.get("/Items/scene-1/Similar", params={"Limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["Items"] and body["Items"][0]["Id"] == "scene-3"


def test_http_instant_mix_both_route_shapes(client):
    assert client.get("/Items/studio-1/InstantMix").status_code == 200
    r = client.get("/Users/abc/Items/scene-1/InstantMix")
    assert r.status_code == 200
    assert {i["Id"] for i in r.json()["Items"]} <= {
        "scene-2", "scene-3", "scene-4", "scene-5",
    }


def test_http_suggestions_route(client):
    r = client.get("/Items/Suggestions", params={"Limit": 5})
    assert r.status_code == 200
    assert [i["Id"] for i in r.json()["Items"]] == ["scene-2"]


def test_http_media_segments_route(client):
    r = client.get("/MediaSegments/scene-7")
    assert r.status_code == 200
    items = r.json()["Items"]
    assert len(items) == 1
    assert items[0]["Type"] == "Intro"
    assert items[0]["ItemId"] == "scene-7"


def test_http_detail_dto_carries_chapters(client):
    r = client.get("/Items/scene-7")
    assert r.status_code == 200
    chapters = r.json()["Chapters"]
    assert chapters == [{"StartPositionTicks": 5 * 10_000_000, "Name": "Intro",
                         "ImageTag": "m1s5"}]


# --- paged-query GraphQL shape ------------------------------------------
#
# `_paged_scenes` builds the CountScenes/PagedScenes pair shared by
# Suggestions and container-scoped InstantMix. Two ways it could emit a
# document Stash rejects, both invisible to the assertions above because
# the fake answers any query:
#   * the clause from scene_filter_clause_for_parent already spells
#     `scene_filter: { … }`, so re-wrapping it nests a second
#     `scene_filter` field → `Field "scene_filter" is not defined by type
#     "SceneFilterType"`;
#   * declaring `$ids` for a clause-less page → `Variable "$ids" is never
#     used in operation "PagedScenes"`.
# Either failure degraded the endpoint to a silent empty page, which is
# what a real Stash v0.31.1 showed for Suggestions (TotalRecordCount 0)
# and for studio-/performer-/group- InstantMix (Items []).

def _paged_docs(fake_stash):
    return [(q, v) for q, v in fake_stash["calls"]
            if "PagedScenes" in q or "CountScenes" in q]


def test_clauseless_paged_queries_declare_no_unused_variable(fake_stash):
    asyncio.run(suggested_items(limit=10))
    asyncio.run(instant_mix_for("root-scenes", limit=10))
    docs = _paged_docs(fake_stash)
    assert len(docs) == 4
    for query, variables in docs:
        assert "$ids" not in query
        assert "scene_filter" not in query
        assert "ids" not in variables
    # …while the page document still carries its paging variables (the
    # count document takes none once the edge declaration is dropped).
    page_docs = [v for q, v in docs if "PagedScenes" in q]
    assert page_docs
    for variables in page_docs:
        assert {"page", "per_page", "sort", "direction"} <= set(variables)


def test_container_paged_queries_wire_scene_filter_exactly_once(fake_stash):
    asyncio.run(instant_mix_for("studio-1", limit=10))
    docs = _paged_docs(fake_stash)
    assert len(docs) == 2
    for query, variables in docs:
        assert query.count("scene_filter:") == 1
        assert "scene_filter: {scene_filter" not in query
        assert "scene_filter: { scene_filter" not in query
        assert "$ids: [ID!]" in query
        assert variables["ids"] == ["1"]
