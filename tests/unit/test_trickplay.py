"""Stash sprite/VTT → Jellyfin Trickplay scrubber previews.

Covers mapping/trickplay.py (VTT → geometry, URL rebase, HLS tile
playlist), the manifest wrapper and both `/Videos/{id}/Trickplay/…`
handlers. Everything is exercised through pure functions and stubbed
fetchers — no network, no generated sprite needed.
"""
import asyncio

import pytest
from starlette.requests import Request

from stash_jellyfin_proxy.endpoints import items as items_mod
from stash_jellyfin_proxy.mapping.trickplay import (
    rebase_stash_url,
    tiles_playlist,
    trickplay_info_from_vtt,
)


def _vtt(count=9, width=160, height=90, interval=10.0):
    """A Stash-style sprite index: one cue per thumbnail, square grid,
    row-major xywh rectangles, each cue spanning `interval` seconds."""
    import math

    grid = int(math.ceil(math.sqrt(count)))
    lines = ["WEBVTT", ""]
    for i in range(count):
        start = i * interval
        end = (i + 1) * interval
        col, row = i % grid, i // grid
        lines.append(f"{_ts(start)} --> {_ts(end)}")
        lines.append(f"sprite.jpg#xywh={col * width},{row * height},{width},{height}")
        lines.append("")
    return "\n".join(lines)


def _ts(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# --- trickplay_info_from_vtt --------------------------------------------

def test_vtt_geometry_is_derived_from_the_cues():
    info = trickplay_info_from_vtt(_vtt())
    assert info == {
        "Width": 160,
        "Height": 90,
        "TileWidth": 3,
        "TileHeight": 3,
        "ThumbnailCount": 9,
        "Interval": 10000,
    }


def test_grid_is_square_ceil_sqrt_for_non_square_counts():
    # 5 thumbnails → Stash's generator still emits a 3x3 sheet.
    info = trickplay_info_from_vtt(_vtt(count=5))
    assert info["ThumbnailCount"] == 5
    assert (info["TileWidth"], info["TileHeight"]) == (3, 3)


def test_interval_comes_from_the_first_cues_own_duration():
    info = trickplay_info_from_vtt(_vtt(count=3, interval=2.5))
    assert info["Interval"] == 2500


@pytest.mark.parametrize("text", [
    "",                                            # nothing
    "WEBVTT\n\n",                                  # header only
    "00:00:00.000 --> 00:00:10.000\n",             # timed cue, no xywh
    "sprite.jpg#xywh=0,0,160,90\n",                # xywh, no timed cue
    "00:00:00.000 --> 00:00:10.000\n#xywh=0,0,0,90\n",   # zero width
])
def test_malformed_vtt_yields_no_trickplay(text):
    assert trickplay_info_from_vtt(text) is None


def test_zero_length_cue_yields_no_trickplay():
    # interval 0 → nothing to space the thumbnails by.
    text = "00:00:05.000 --> 00:00:05.000\nsprite.jpg#xywh=0,0,160,90\n"
    assert trickplay_info_from_vtt(text) is None


# --- rebase_stash_url ----------------------------------------------------

def test_rebase_repoints_absolute_url_at_configured_stash():
    assert rebase_stash_url(
        "http://stash.internal:9999/scene/1/sprite.jpg", "http://proxy-stash:9999"
    ) == "http://proxy-stash:9999/scene/1/sprite.jpg"


def test_rebase_passes_relative_path_through():
    assert rebase_stash_url("/scene/1/sprite.jpg", "http://s:9999") == \
        "http://s:9999/scene/1/sprite.jpg"


def test_rebase_empty_is_empty():
    assert rebase_stash_url("", "http://s:9999") == ""


# --- tiles_playlist ------------------------------------------------------

def test_playlist_is_single_tile_hls_block():
    body = tiles_playlist(trickplay_info_from_vtt(_vtt()))
    assert body == (
        "#EXTM3U\n"
        "#EXT-X-TARGETDURATION:1\n"
        "#EXT-X-VERSION:7\n"
        "#EXT-X-MEDIA-SEQUENCE:1\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXT-X-IMAGES-ONLY\n"
        "#EXTINF:90,\n"
        "#EXT-X-TILES:RESOLUTION=160x90,LAYOUT=3x3,DURATION=10\n"
        "0.jpg\n"
        "#EXT-X-ENDLIST\n"
    )


@pytest.mark.parametrize("info", [
    {},
    {"Width": 0, "Height": 0, "TileWidth": 0, "TileHeight": 0,
     "ThumbnailCount": 0, "Interval": 0},
    {"Width": 1, "Height": 1, "TileWidth": 1, "TileHeight": 1,
     "ThumbnailCount": 1, "Interval": 0},
])
def test_playlist_empty_for_unusable_info(info):
    assert tiles_playlist(info) == ""


# --- manifest wrapper ----------------------------------------------------

def test_manifest_keys_by_media_source_id_and_width():
    item = {"Id": "scene-7", "MediaSources": [{"Id": "scene-7"}]}
    info = trickplay_info_from_vtt(_vtt())
    manifest = items_mod._trickplay_manifest(item, {"info": info})
    assert manifest == {"scene-7": {"160": info}}


def test_manifest_falls_back_to_item_id_and_skips_when_absent():
    info = trickplay_info_from_vtt(_vtt())
    assert items_mod._trickplay_manifest({"Id": "scene-7"}, {"info": info}) == \
        {"scene-7": {"160": info}}
    assert items_mod._trickplay_manifest({}, {"info": info}) == {}


# --- endpoints -----------------------------------------------------------

def _req(path_params):
    return Request({
        "type": "http", "method": "GET",
        "path": "/Videos/x/Trickplay/320/0.jpg",
        "path_params": path_params, "query_string": b"", "headers": [],
    })


def test_tile_endpoint_proxies_stash_sprite(monkeypatch):
    info = trickplay_info_from_vtt(_vtt())
    seen = {}

    async def fake_trickplay(numeric_id):
        seen["id"] = numeric_id
        return {"info": info, "sprite_url": "http://s:9999/scene/7/sprite.jpg"}

    async def fake_fetch(url, extra_headers=None, timeout=30):
        seen["url"] = url
        return b"\xff\xd8jpeg", "image/jpeg", {}

    monkeypatch.setattr(items_mod, "get_scene_trickplay", fake_trickplay)
    monkeypatch.setattr(items_mod, "fetch_from_stash", fake_fetch)

    resp = asyncio.run(items_mod.endpoint_trickplay_tile(
        _req({"item_id": "scene-7", "width": "320", "index": "0"})
    ))
    assert resp.status_code == 200
    assert resp.media_type == "image/jpeg"
    assert seen["id"] == "7"
    assert seen["url"] == "http://s:9999/scene/7/sprite.jpg"


def test_tile_endpoint_only_serves_sheet_zero(monkeypatch):
    called = []

    async def fake_trickplay(numeric_id):
        called.append(numeric_id)
        return {"info": {}, "sprite_url": "u"}

    monkeypatch.setattr(items_mod, "get_scene_trickplay", fake_trickplay)
    resp = asyncio.run(items_mod.endpoint_trickplay_tile(
        _req({"item_id": "scene-7", "width": "320", "index": "1"})
    ))
    assert resp.status_code == 404
    assert called == []  # rejected before any Stash lookup


def test_tile_endpoint_404_without_sprite(monkeypatch):
    async def fake_trickplay(numeric_id):
        return None

    monkeypatch.setattr(items_mod, "get_scene_trickplay", fake_trickplay)
    resp = asyncio.run(items_mod.endpoint_trickplay_tile(
        _req({"item_id": "scene-7", "width": "320", "index": "0"})
    ))
    assert resp.status_code == 404


def test_playlist_endpoint_serves_m3u8(monkeypatch):
    info = trickplay_info_from_vtt(_vtt())

    async def fake_trickplay(numeric_id):
        return {"info": info, "sprite_url": "u"}

    monkeypatch.setattr(items_mod, "get_scene_trickplay", fake_trickplay)
    resp = asyncio.run(items_mod.endpoint_trickplay_playlist(
        _req({"item_id": "scene-7", "width": "320"})
    ))
    assert resp.status_code == 200
    assert resp.media_type == "application/vnd.apple.mpegurl"
    assert "#EXT-X-TILES:RESOLUTION=160x90" in resp.body.decode()


def test_playlist_endpoint_404_without_sprite(monkeypatch):
    async def fake_trickplay(numeric_id):
        return None

    monkeypatch.setattr(items_mod, "get_scene_trickplay", fake_trickplay)
    resp = asyncio.run(items_mod.endpoint_trickplay_playlist(
        _req({"item_id": "scene-7", "width": "320"})
    ))
    assert resp.status_code == 404


# --- playback-path classification ---------------------------------------

@pytest.mark.parametrize("path", [
    "/Videos/scene-1/Trickplay/320/0.jpg",
    "/Videos/scene-1/Trickplay/320/tiles.m3u8",
])
def test_trickplay_paths_are_not_live_streams(path):
    """Scrubber assets are metadata/image fetches, not media delivery —
    tracking them would spam the dashboard's live-stream table."""
    from stash_jellyfin_proxy.middleware.logging import is_playback_path
    assert is_playback_path(path) is False
