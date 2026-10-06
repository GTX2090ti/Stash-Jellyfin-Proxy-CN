"""`/Persons` (the People rail) must carry the same fields as
`/Items/performer-{id}` (the detail page).

The list endpoint used to emit a 6-key stub — Name, Id, Type, ImageTags,
ImageBlurHashes, BackdropImageTags — so every field a client renders in a
grid was missing exactly where it is displayed:

  * no `UserData`      → the favourite heart never filled in on the rail
  * no `SortName`      → name sorting used the raw display string
  * no `Overview`      → cards had no subtitle text
  * no `PremiereDate`  → no birth year anywhere in a list view

Both endpoints now build items through `mapping.performer.performer_fields`.
These tests pin both directions: that the shared builder emits the fields,
and that the list endpoint actually uses it.
"""
import asyncio

import pytest
from starlette.requests import Request

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.endpoints import search as search_mod
from stash_jellyfin_proxy.mapping import genre
from stash_jellyfin_proxy.mapping.performer import (
    LIST_FIELDS,
    PACKET_FIELDS,
    build_overview,
    performer_fields,
)


def _req(qs: bytes = b"") -> Request:
    return Request({
        "type": "http", "method": "GET", "path_params": {},
        "query_string": qs, "headers": [],
    })


def _performer(**over):
    p = {
        "id": "45", "name": "Some Performer", "gender": "FEMALE",
        "birthdate": "1996-05-14", "death_date": None,
        "country": "Japan", "career_start": "2021", "career_end": None,
        "height_cm": "150", "weight": "45", "measurements": "B85 W58 H85",
        "fake_tits": "Natural", "ethnicity": "Japanese",
        "hair_color": "Black", "eye_color": "Brown",
        "tattoos": None, "piercings": None,
        "alias_list": ["_alias_"], "details": "A hand-written bio.",
        "rating100": 87, "favorite": True, "scene_count": 12,
        "image_path": "/performer.jpg",
        "tags": [{"id": "1", "name": "TagA"}, {"id": "2", "name": "TagB"}],
        "stash_ids": [{"endpoint": "default", "stash_id": "sjp-45"}],
    }
    p.update(over)
    return p


# --- the shared builder -------------------------------------------------

def test_builder_emits_every_field_a_grid_renders():
    item = performer_fields(_performer(), "performer-45", "Person")
    for key in ("Name", "SortName", "Id", "ServerId", "Type", "IsFolder",
                "Overview", "UserData", "ChildCount", "RecursiveItemCount",
                "PremiereDate", "ProductionYear", "ImageTags",
                "BackdropImageTags", "PrimaryImageAspectRatio"):
        assert key in item, f"missing {key}"


def test_favourite_state_is_reported():
    """The heart on a People-rail card is driven by UserData.IsFavorite."""
    assert performer_fields(_performer(favorite=True), "performer-45", "Person"
                           )["UserData"]["IsFavorite"] is True
    assert performer_fields(_performer(favorite=False), "performer-45", "Person"
                           )["UserData"]["IsFavorite"] is False


def test_unfavourited_performer_still_has_a_userdata_block():
    """Absence vs false are different to a strict client — always emit it."""
    assert performer_fields(_performer(favorite=False), "performer-45", "Person"
                           )["UserData"]["IsFavorite"] is False


def test_birth_year_is_exposed():
    item = performer_fields(_performer(), "performer-45", "Person")
    assert item["ProductionYear"] == 1996
    assert item["PremiereDate"].startswith("1996-05-14")


def test_no_birthdate_means_no_year_keys():
    """Must not emit a bogus year for performers Stash has no DOB for."""
    item = performer_fields(_performer(birthdate=None), "performer-45", "Person")
    assert "ProductionYear" not in item
    assert "PremiereDate" not in item


def test_scene_count_drives_both_child_counts():
    item = performer_fields(_performer(scene_count=12), "performer-45", "Person")
    assert item["ChildCount"] == 12
    assert item["RecursiveItemCount"] == 12


def test_collection_type_only_for_boxset():
    """Swiftfin's Person screen ignores it; Infuse/SenPlayer need it."""
    assert "CollectionType" not in performer_fields(_performer(), "p-45", "Person")
    assert performer_fields(_performer(), "p-45", "BoxSet")["CollectionType"] == "movies"


def test_image_tags_reflect_whether_an_image_exists():
    with_img = performer_fields(_performer(image_path="/p.jpg"), "p-45", "Person")
    assert with_img["ImageTags"] == {"Primary": "img"}
    assert with_img["BackdropImageTags"] == ["img"]
    # No image -> no tags, or clients request an image that 404s.
    no_img = performer_fields(_performer(image_path=None), "p-45", "Person")
    assert no_img["ImageTags"] == {}
    assert no_img["BackdropImageTags"] == []


def test_tags_map_to_genres_and_tags():
    item = performer_fields(_performer(), "p-45", "Person")
    assert item["Genres"] == ["TagA", "TagB"]
    assert item["Tags"] == ["TagA", "TagB"]


def test_untagged_performer_has_no_genres_key():
    item = performer_fields(_performer(tags=[]), "p-45", "Person")
    assert "Genres" not in item


def test_rating_is_scaled_to_ten():
    assert performer_fields(_performer(rating100=87), "p-45", "Person"
                           )["CommunityRating"] == 8.7


def test_unrated_performer_has_no_community_rating():
    assert "CommunityRating" not in performer_fields(
        _performer(rating100=None), "p-45", "Person")


def test_stash_provider_id_passthrough():
    item = performer_fields(_performer(), "p-45", "Person")
    assert item["ProviderIds"] == {"StashDb": "sjp-45"}


def test_sort_name_is_derived():
    assert performer_fields(_performer(name="The Performer"), "p-45", "Person"
                           )["SortName"]


# --- overview synthesis -------------------------------------------------

def test_overview_leads_with_hand_written_bio():
    ov = build_overview(_performer())
    assert ov.startswith("A hand-written bio.")


def test_overview_summarises_structured_attributes():
    ov = build_overview(_performer(details=None))
    assert "Female performer" in ov
    assert "1996-05-14" in ov
    assert "from Japan" in ov
    assert "12 scenes in library" in ov


def test_overview_handles_a_performer_with_nothing_set():
    """Must still return non-empty text — a blank card looks broken."""
    bare = {"id": "1", "name": "X", "scene_count": 0}
    ov = build_overview(bare)
    assert ov.strip()
    assert "Performer" in ov


def test_overview_includes_measurements_and_aliases():
    ov = build_overview(_performer())
    assert "Height: 150 cm" in ov
    assert "Measurements: B85 W58 H85" in ov
    assert "_alias_" in ov


# --- field sets ---------------------------------------------------------

def test_list_fields_are_a_subset_of_packet_fields():
    """The list query must not ask for a field the detail query lacks."""
    packet_tokens = set(PACKET_FIELDS.split())
    # tags/stash_ids are brace sub-selections; compare the top-level names.
    list_top = {t.split("{")[0] for t in LIST_FIELDS.split()}
    packet_top = {t.split("{")[0] for t in PACKET_FIELDS.split()}
    assert list_top <= packet_top, list_top - packet_top
    assert packet_tokens  # sanity


# --- the /Persons endpoint uses the builder -----------------------------

def test_persons_endpoint_returns_full_items(monkeypatch):
    """Regression: the rail used to be a 6-key stub."""
    monkeypatch.setattr(runtime, "SERVER_ID", "srv", raising=False)
    monkeypatch.setattr(runtime, "SEARCH_INCLUDE_PERFORMERS", True, raising=False)
    monkeypatch.setattr(runtime, "DEFAULT_PAGE_SIZE", 20, raising=False)
    monkeypatch.setattr(runtime, "MAX_PAGE_SIZE", 100, raising=False)
    genre.invalidate_allowed_cache()

    async def _fake(query, variables=None):
        return {"data": {"findPerformers": {
            "count": 1,
            "performers": [_performer()],
        }}}

    monkeypatch.setattr(search_mod, "stash_query", _fake)
    monkeypatch.setattr(search_mod, "performer_item_type", lambda request: "Person")

    import json
    resp = asyncio.run(search_mod.endpoint_persons(_req(b"limit=5")))
    body = json.loads(resp.body)
    assert body["TotalRecordCount"] == 1
    item = body["Items"][0]
    for key in ("Overview", "SortName", "UserData", "ProductionYear",
                "PremiereDate", "IsFolder", "RecursiveItemCount"):
        assert key in item, f"/Persons item missing {key}"
    assert item["UserData"]["IsFavorite"] is True
    assert item["Id"] == "performer-45"


def test_persons_search_path_also_returns_full_items(monkeypatch):
    """The search branch had its own query string — it must not regress."""
    monkeypatch.setattr(runtime, "SERVER_ID", "srv", raising=False)
    monkeypatch.setattr(runtime, "SEARCH_INCLUDE_PERFORMERS", True, raising=False)
    monkeypatch.setattr(runtime, "DEFAULT_PAGE_SIZE", 20, raising=False)
    monkeypatch.setattr(runtime, "MAX_PAGE_SIZE", 100, raising=False)
    genre.invalidate_allowed_cache()

    seen = {}

    async def _fake(query, variables=None):
        seen["q"] = query
        return {"data": {"findPerformers": {"count": 1, "performers": [_performer()]}}}

    monkeypatch.setattr(search_mod, "stash_query", _fake)
    monkeypatch.setattr(search_mod, "performer_item_type", lambda request: "Person")

    import json
    resp = asyncio.run(search_mod.endpoint_persons(_req(b"limit=5&searchTerm=some")))
    item = json.loads(resp.body)["Items"][0]
    assert "Overview" in item and "UserData" in item
    # The query must request the fields the builder reads.
    assert "favorite" in seen["q"] and "birthdate" in seen["q"]


def test_persons_favourites_filter_still_applies(monkeypatch):
    monkeypatch.setattr(runtime, "SERVER_ID", "srv", raising=False)
    monkeypatch.setattr(runtime, "SEARCH_INCLUDE_PERFORMERS", True, raising=False)
    monkeypatch.setattr(runtime, "DEFAULT_PAGE_SIZE", 20, raising=False)
    monkeypatch.setattr(runtime, "MAX_PAGE_SIZE", 100, raising=False)
    genre.invalidate_allowed_cache()

    seen = {}

    async def _fake(query, variables=None):
        seen["q"] = query
        return {"data": {"findPerformers": {"count": 0, "performers": []}}}

    monkeypatch.setattr(search_mod, "stash_query", _fake)
    asyncio.run(search_mod.endpoint_persons(_req(b"limit=5&Filters=IsFavorite")))
    assert "filter_favorites" in seen["q"]


def test_empty_search_term_still_short_circuits():
    """A search view with an empty box must not list everyone."""
    import json
    resp = asyncio.run(search_mod.endpoint_persons(_req(b"searchTerm=")))
    body = json.loads(resp.body)
    assert body["Items"] == []
    assert body["TotalRecordCount"] == 0
