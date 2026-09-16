"""`/Studios` must not list studios that hold no scenes — regression tests.

Live report (2026-09-16): the client's 厂商 list showed entries that opened
onto an empty list. Stash had 56 studios, 6 with scene_count == 0 (3 orphans a
scraper created, 3 sub-studios whose scenes sit on the parent). The /Studios
endpoint listed all 56 while the root-studios rail in items.py / views.py
already filtered `scene_count > 0`, so only this surface leaked empty tiles.
"""

import asyncio

import pytest
from starlette.requests import Request

from stash_jellyfin_proxy.endpoints import search as search_mod
from stash_jellyfin_proxy.endpoints.search import endpoint_studios

STUDIO_FILTER = "scene_count: {value: 0, modifier: GREATER_THAN}"


def _req(qs: bytes = b"") -> Request:
    return Request({"type": "http", "method": "GET", "query_string": qs, "headers": []})


@pytest.fixture
def captured(monkeypatch):
    """Record every GraphQL query and answer with a plausible studio page."""
    seen = []

    async def _fake(query, variables=None):
        seen.append((query, variables or {}))
        if "findStudios" not in query:
            return {"data": {}}
        return {"data": {"findStudios": {
            "count": 50,
            "studios": [{"id": "19", "name": "Cospuri", "image_path": "/x.jpg",
                         "scene_count": 226}],
        }}}

    monkeypatch.setattr(search_mod, "stash_query", _fake)
    return seen


def test_studios_list_filters_zero_scene_studios(captured):
    res = asyncio.run(endpoint_studios(_req(b"Limit=50")))
    queries = [q for q, _ in captured]
    assert len(queries) == 2, queries  # count + page
    assert all(STUDIO_FILTER in q for q in queries), queries


def test_studios_count_matches_listing_filter(captured):
    res = asyncio.run(endpoint_studios(_req(b"Limit=50")))
    count_q = captured[0][0]
    assert count_q.startswith("query {") and "findStudios" in count_q
    assert STUDIO_FILTER in count_q
    # The count feeds TotalRecordCount; it must not report the unfiltered total.
    assert res.body and b'"TotalRecordCount":50' in res.body


def test_studios_items_still_shaped_for_clients(captured):
    res = asyncio.run(endpoint_studios(_req(b"Limit=50")))
    body = res.body.decode()
    assert '"Id":"studio-19"' in body and '"Type":"Studio"' in body


def test_paged_listing_keeps_filter(captured):
    asyncio.run(endpoint_studios(_req(b"Limit=10&startIndex=20")))
    list_q, vars_ = captured[1]
    assert STUDIO_FILTER in list_q
    assert vars_["page"] == 3 and vars_["per_page"] == 10


def test_parent_scoped_listing_unaffected(captured, monkeypatch):
    """ParentId-scoped listing walks scenes, so it neither needs nor gets the
    scene_count filter — its studios are content-backed by construction."""
    def _clause(parent_id):
        return ", scene_filter: {studios: {value: $ids, modifier: INCLUDES}}", {"ids": ["19"]}

    async def _fake(query, variables=None):
        captured.append((query, variables or {}))
        if "findScenes" in query:
            return {"data": {"findScenes": {"scenes": [
                {"studio": {"id": "19", "name": "Cospuri", "image_path": None}}]}}}
        return {"data": {}}

    monkeypatch.setattr(search_mod, "scene_filter_clause_for_parent", _clause)
    monkeypatch.setattr(search_mod, "stash_query", _fake)

    res = asyncio.run(endpoint_studios(_req(b"ParentId=root-scenes")))
    assert len(captured) == 1
    query = captured[0][0]
    assert "findScenes" in query and STUDIO_FILTER not in query
    assert b'"TotalRecordCount":1' in res.body
