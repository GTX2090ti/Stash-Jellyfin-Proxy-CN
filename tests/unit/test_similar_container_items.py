"""`/Items/{id}/Similar` must return owned scenes for container-like ids.

Live trace (2026-09-16, Yamby Android): opening a studio from the 厂商 list
fetches only `/Items/studio-N`, `/Items/studio-N/Similar` and the backdrop —
it never asks for the studio's children. With Similar hard-wired to an empty
payload the studio page rendered blank, while the same studio opened through
the BoxSet/collection path (which sends GenreIds=studio-N) listed its scenes.
These tests lock the new behaviour in.
"""

import asyncio

import pytest
from starlette.requests import Request

from stash_jellyfin_proxy.endpoints import items as items_mod
from stash_jellyfin_proxy.endpoints import stubs as stubs_mod
from stash_jellyfin_proxy.endpoints.items import similar_items_for


def _req(path_params=None, qs: bytes = b"") -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path_params": path_params or {},
        "query_string": qs,
        "headers": [],
    })


@pytest.fixture
def fake_stash(monkeypatch):
    """Record Stash queries; answer the count + page queries.

    `similar_items_for` fetches the count and the page through
    `stash_query_pair` (one round trip for both, added with the CN.10
    parallel-query work). Patch BOTH entry points: leaving
    `stash_query_pair` unpatched sends the real client at the configured
    Stash host, which shows up as `getaddrinfo failed` after four retries
    and an empty call list — the assertions then fail for what looks like
    an endpoint bug but is really an un-stubbed dependency.
    """
    calls = []

    async def _fake(query, variables=None):
        calls.append((query, variables or {}))
        if "CountSimilar" in query:
            return {"data": {"findScenes": {"count": 90}}}
        return {"data": {"findScenes": {"scenes": [
            {"id": "272", "title": "a", "studio": {"id": "30", "name": "S", "tags": []},
             "files": [], "performers": [], "tags": []},
            {"id": "273", "title": "b", "studio": {"id": "30", "name": "S", "tags": []},
             "files": [], "performers": [], "tags": []},
        ]}}}

    async def _fake_pair(count_query, count_vars, page_query, page_vars):
        count = await _fake(count_query, count_vars)
        page = await _fake(page_query, page_vars)
        return count, page

    monkeypatch.setattr(items_mod, "stash_query", _fake)
    monkeypatch.setattr(items_mod, "stash_query_pair", _fake_pair)
    return calls


def test_studio_similar_returns_own_scenes(fake_stash):
    data = asyncio.run(similar_items_for("studio-30", limit=30, start_index=0))
    assert data["TotalRecordCount"] == 90
    assert len(data["Items"]) == 2
    assert {i["Id"] for i in data["Items"]} == {"scene-272", "scene-273"}
    # the filter must be the studio's scenes, not an empty/scene-less query
    assert any(
        "studios" in q and "INCLUDES" in q and v.get("ids") == ["30"]
        for q, v in fake_stash
    )


def test_performer_and_group_ids_also_supported(fake_stash):
    for pid, key in (("performer-7", "performers"), ("group-3", "movies"), ("tagitem-4", "tags")):
        data = asyncio.run(similar_items_for(pid))
        assert data["Items"], pid
        assert any(key in q for q, _ in fake_stash), pid


def test_scene_id_returns_empty(fake_stash):
    data = asyncio.run(similar_items_for("scene-272"))
    assert data == {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}
    assert fake_stash == []


def test_pagination_maps_to_stash_page(fake_stash):
    asyncio.run(similar_items_for("studio-30", limit=30, start_index=60))
    pages = [v.get("page") for _, v in fake_stash if "per_page" in v]
    assert pages == [3]


def test_limit_is_clamped(fake_stash):
    asyncio.run(similar_items_for("studio-30", limit=100000, start_index=0))
    per_page = [v["per_page"] for _, v in fake_stash if "per_page" in v]
    assert per_page and per_page[0] < 100000


def test_endpoint_routes_studio_to_scenes(fake_stash):
    req = _req({"item_id": "studio-30"}, b"Limit=10&UserId=abc&EnableTotalRecordCount=false")
    resp = asyncio.run(stubs_mod.endpoint_similar(req))
    import json
    body = json.loads(resp.body)
    assert body["TotalRecordCount"] == 90
    assert len(body["Items"]) == 2


def test_endpoint_keeps_scene_similar_empty(fake_stash):
    req = _req({"item_id": "scene-1"}, b"Limit=10")
    resp = asyncio.run(stubs_mod.endpoint_similar(req))
    import json
    assert json.loads(resp.body) == {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}


def test_endpoint_widens_client_limit_for_containers(fake_stash):
    """Yamby always asks /Similar with Limit=10; for containers the endpoint
    must widen the page (SIMILAR_CONTAINER_PAGE_LIMIT) so the rail carries
    the full studio catalogue, not just the first 10 scenes."""
    req = _req({"item_id": "studio-30"}, b"Limit=10&UserId=abc&EnableTotalRecordCount=false")
    asyncio.run(stubs_mod.endpoint_similar(req))
    per_page = [v["per_page"] for _, v in fake_stash if "per_page" in v]
    assert per_page == [stubs_mod.SIMILAR_CONTAINER_PAGE_LIMIT]


def test_container_page_limit_fits_max_page_size():
    """similar_items_for clamps to runtime.MAX_PAGE_SIZE; widening beyond it
    would silently shrink back."""
    from stash_jellyfin_proxy import runtime

    assert stubs_mod.SIMILAR_CONTAINER_PAGE_LIMIT <= runtime.MAX_PAGE_SIZE
