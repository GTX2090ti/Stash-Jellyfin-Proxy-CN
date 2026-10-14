"""Stash markers → Jellyfin Chapters / MediaSegments mapping.

Covers mapping/markers.py (classification, chapter points, typed
ranges), the Chapters[] hook inside format_jellyfin_item, and the
/MediaSegments endpoint delegation in stubs.py.
"""
import asyncio
import json

import pytest
from starlette.requests import Request

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping import genre
from stash_jellyfin_proxy.mapping.markers import (
    TICKS_PER_SECOND,
    chapters_from_scene,
    classify_marker,
    media_segments_from_scene,
)
from stash_jellyfin_proxy.mapping.scene import format_jellyfin_item
from stash_jellyfin_proxy.endpoints import stubs as stubs_mod
from stash_jellyfin_proxy.stash import scene as stash_scene


def _marker(id, seconds, title="", primary=None, tags=None):
    return {
        "id": str(id),
        "seconds": seconds,
        "title": title,
        "primary_tag": {"id": "t", "name": primary} if primary else None,
        "tags": [{"id": str(i), "name": n} for i, n in enumerate(tags or [])],
    }


# --- classify_marker -----------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Intro", "Intro"),
    ("intro", "Intro"),
    ("Cold Open", "Intro"),
    ("Opening Credits", "Intro"),          # checked before Outro/credits
    ("Outro", "Outro"),
    ("Ending", "Outro"),
    ("End Credits", "Outro"),
    ("closing", "Outro"),
    ("Commercial", "Commercial"),
    ("AD", "Commercial"),                  # standalone token, not -ad- inside a word
    ("Recap", "Recap"),
    ("Previously on Scene 5", "Recap"),
    ("Preview", "Preview"),
    ("Up Next", "Preview"),
    ("official trailer", "Preview"),
])
def test_classify_known_types(text, expected):
    assert classify_marker(_marker(1, 0, title=text)) == expected


def test_classify_reads_primary_tag_and_extra_tags():
    assert classify_marker(_marker(1, 0, title="", primary="Intro")) == "Intro"
    assert classify_marker(_marker(1, 0, title="", primary="Hottest",
                                   tags=["skip intro"])) == "Intro"


def test_classify_word_boundary_no_false_positives():
    # "introduction" must not match the "intro" token...
    assert classify_marker(_marker(1, 0, title="Introduction speech")) is None
    # ...and "Hottest" has no typed meaning in Jellyfin.
    assert classify_marker(_marker(1, 0, title="Hottest", primary="Hottest")) is None


def test_classify_empty_marker():
    assert classify_marker({}) is None


# --- chapters_from_scene -------------------------------------------------

def test_chapters_sorted_ticks_and_names():
    scene = {"markers": [
        _marker(2, 100, title="Later"),
        _marker(1, 10, title="Start"),
    ]}
    chapters = chapters_from_scene(scene)
    assert [c["Name"] for c in chapters] == ["Start", "Later"]
    assert [c["StartPositionTicks"] for c in chapters] == [
        10 * TICKS_PER_SECOND,
        100 * TICKS_PER_SECOND,
    ]


def test_chapter_name_falls_back_to_primary_tag_then_index():
    scene = {"markers": [
        _marker(1, 5, title="", primary="Hottest"),
        _marker(2, 15, title="", primary=None),
    ]}
    chapters = chapters_from_scene(scene)
    assert chapters[0]["Name"] == "Hottest"
    assert chapters[1]["Name"] == "Chapter 2"


def test_chapters_collapse_duplicate_timestamps():
    scene = {"markers": [
        _marker(1, 10, title="A"),
        _marker(2, 10, title="B"),
        _marker(3, 20, title="C"),
    ]}
    chapters = chapters_from_scene(scene)
    assert [c["Name"] for c in chapters] == ["A", "C"]


def test_chapters_none_without_markers():
    assert chapters_from_scene({}) is None
    assert chapters_from_scene({"markers": []}) is None


# --- media_segments_from_scene ------------------------------------------

def test_segments_typed_only_with_ranges():
    # Intro@10, untyped Hottest@100, Outro@500; duration 600.
    scene = {
        "files": [{"duration": 600}],
        "markers": [
            _marker(11, 10, primary="Intro"),
            _marker(12, 100, primary="Hottest"),
            _marker(13, 500, title="Ending"),
        ],
    }
    segments = media_segments_from_scene(scene, "scene-7")
    assert segments == [
        {
            "Id": "scene-7-m11",
            "ItemId": "scene-7",
            "Type": "Intro",
            "StartTicks": 10 * TICKS_PER_SECOND,
            # end = NEXT marker (typed or not)
            "EndTicks": 100 * TICKS_PER_SECOND,
        },
        {
            "Id": "scene-7-m13",
            "ItemId": "scene-7",
            "Type": "Outro",
            "StartTicks": 500 * TICKS_PER_SECOND,
            # final marker ends at scene duration
            "EndTicks": 600 * TICKS_PER_SECOND,
        },
    ]


def test_segment_dropped_when_no_end_boundary():
    # A single typed marker and no known duration → zero-length → drop.
    scene = {"files": [{"duration": 0}], "markers": [_marker(1, 30, primary="Intro")]}
    assert media_segments_from_scene(scene, "scene-9") == []


def test_segments_empty_for_markerless_scene():
    assert media_segments_from_scene({"files": [{"duration": 100}]}, "scene-1") == []


# --- format_jellyfin_item integration -----------------------------------

@pytest.fixture(autouse=True)
def _reset_runtime():
    saved = (runtime.SERVER_ID, runtime.SERIES_TAG, runtime.FAVORITE_TAG,
             runtime.GENRE_PARENT_TAG)
    runtime.SERVER_ID = "test-server-id"
    runtime.SERIES_TAG = ""
    runtime.FAVORITE_TAG = ""
    runtime.GENRE_PARENT_TAG = "GENRE"
    genre.invalidate_allowed_cache()
    yield
    (runtime.SERVER_ID, runtime.SERIES_TAG, runtime.FAVORITE_TAG,
     runtime.GENRE_PARENT_TAG) = saved
    genre.invalidate_allowed_cache()


def _full_scene(markers=None):
    return {
        "id": "42",
        "title": "Test scene",
        "date": "2024-01-01",
        "files": [{"path": "/x/y.mp4", "duration": 600.0}],
        "tags": [],
        "performers": [],
        "studio": None,
        "markers": markers,
    }


def test_item_dto_carries_chapters_when_markers_present():
    item = format_jellyfin_item(_full_scene(markers=[
        _marker(1, 12, title="Intro bit", primary="Intro"),
        _marker(2, 200, title="Main", primary="Hottest"),
    ]))
    assert item["Chapters"] == [
        {"StartPositionTicks": 12 * TICKS_PER_SECOND, "Name": "Intro bit"},
        {"StartPositionTicks": 200 * TICKS_PER_SECOND, "Name": "Main"},
    ]


def test_item_dto_unchanged_without_markers():
    item = format_jellyfin_item(_full_scene(markers=None))
    assert "Chapters" not in item


# --- /MediaSegments endpoint --------------------------------------------

def _req(path_params):
    return Request({
        "type": "http", "method": "GET",
        "path_params": path_params, "query_string": b"", "headers": [],
    })


def test_media_segments_endpoint_maps_scene(monkeypatch):
    async def fake_get(numeric_id):
        assert numeric_id == "7"
        return {
            "files": [{"duration": 100}],
            "markers": [_marker(1, 5, primary="Intro")],
        }

    monkeypatch.setattr(stash_scene, "get_scene_markers", fake_get)

    resp = asyncio.run(stubs_mod.endpoint_media_segments(_req({"item_id": "scene-7"})))
    body = json.loads(resp.body)
    assert len(body["Items"]) == 1
    assert body["Items"][0]["Type"] == "Intro"
    assert body["Items"][0]["ItemId"] == "scene-7"


def test_media_segments_strips_multifile_suffix(monkeypatch):
    seen = {}

    async def fake_get(numeric_id):
        seen["id"] = numeric_id
        return None

    monkeypatch.setattr(stash_scene, "get_scene_markers", fake_get)
    resp = asyncio.run(stubs_mod.endpoint_media_segments(_req({"item_id": "scene-7-f99"})))
    assert seen["id"] == "7"
    assert json.loads(resp.body) == {"Items": []}


def test_media_segments_non_scene_id_is_empty(monkeypatch):
    called = []

    async def fake_get(numeric_id):
        called.append(numeric_id)
        return None

    monkeypatch.setattr(stash_scene, "get_scene_markers", fake_get)
    resp = asyncio.run(stubs_mod.endpoint_media_segments(_req({"item_id": "studio-3"})))
    assert json.loads(resp.body) == {"Items": []}
    assert called == []
