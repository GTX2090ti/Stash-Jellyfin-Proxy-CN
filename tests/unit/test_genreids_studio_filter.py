"""Yamby studio navigation via GenreIds=studio-N — regression tests.

Live trace (2026-09-16): tapping a studio tile in Yamby Android fires
/Users/{u}/Items?IncludeItemTypes=Movie,Series&GenreIds=studio-N — it never
fetches /Users/{u}/Items/studio-N. Unparsed, the studio id dropped silently
and the "studio page" didn't show the studio's scenes. _parse_filter_params
now extracts studio-N shapes into a Stash `studios` filter."""

import asyncio

import pytest
from starlette.requests import Request

from stash_jellyfin_proxy.endpoints import items as items_mod
from stash_jellyfin_proxy.endpoints.items import (
    _filter_clause,
    _filter_var_defs,
    _parse_filter_params,
)


def _req(qs: bytes) -> Request:
    return Request({"type": "http", "method": "GET", "query_string": qs, "headers": []})


def test_genreids_studio_shape_parsed():
    genres, tags, years, studio_ids = _parse_filter_params(_req(b"GenreIds=studio-59"))
    assert studio_ids == ["59"]
    assert genres == [] and tags == [] and years == []


def test_genreids_mixed_shapes_only_studio_collected():
    genres, tags, years, studio_ids = _parse_filter_params(
        _req(b"GenreIds=genre-3,studio-7,studio-x,studio-")
    )
    # genre-3 handled by the existing genre-name path (cache miss drops);
    # studio-x / studio- are non-numeric and ignored.
    assert studio_ids == ["7"]


def test_filter_clause_emits_studios_part():
    parts, vars_ = asyncio.run(_filter_clause(_req(b"GenreIds=studio-59")))
    assert any("_filter_studio_ids" in p and "studios:" in p for p in parts)
    assert vars_["_filter_studio_ids"] == ["59"]


def test_filter_var_defs_includes_studio_ids():
    defs, args = _filter_var_defs({"_filter_studio_ids": ["59"]})
    assert "$_filter_studio_ids: [ID!]" in defs
    assert "_filter_studio_ids" in args


def test_filter_clause_without_studio_ids_is_unchanged():
    parts, vars_ = asyncio.run(_filter_clause(_req(b"GenreIds=genre-3")))
    assert not any("studios:" in p for p in parts)
    assert "_filter_studio_ids" not in vars_


@pytest.fixture
def no_stash(monkeypatch):
    """Neutralize Stash calls so _filter_clause runs fully offline. The fake
    answers findTags lookups with one matching tag so the tag-name path
    still produces its clause."""
    async def _fake(query, variables=None):
        name = (variables or {}).get("n", "")
        return {"data": {"findTags": {"tags": [{"id": "7", "name": name}]}}}
    monkeypatch.setattr(items_mod, "stash_query", _fake)


def test_plain_genres_still_resolve_via_tags(no_stash):
    parts, vars_ = asyncio.run(_filter_clause(_req(b"Genres=POV")))
    assert any("tags:" in p for p in parts)
    assert "_filter_studio_ids" not in vars_
