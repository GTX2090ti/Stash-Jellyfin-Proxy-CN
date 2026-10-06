"""Regression: `/Videos/{id}` must carry MediaSources so players can
switch versions without leaving playback.

Symptom this pins
-----------------
A merged Stash scene (several files under one scene, e.g. H00/H01/H02 of
the same release) is exposed as several Jellyfin MediaSources, each with
its own `-f<fileId>` id. Clients render an in-player version switcher
from `MediaSources` on the **video document** (`GET /Videos/{id}`), which
they fetch right before playback.

`/Videos/{id}` had no route, so it fell through to `catch_all`, which
answers `{"Items": [], "TotalRecordCount": 0}`. `MediaSources` therefore
came back empty and the alternate files were only selectable from the
item detail page — the player had no switcher at all. `/Items/{id}`
already built the correct multi-source list, so the fix is routing.

These tests assert the contract, not the implementation: the route must
exist and the handler it reaches must emit one MediaSource per file.
"""
import pytest
from starlette.routing import Route

from stash_jellyfin_proxy import app as app_mod
from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping import genre
from stash_jellyfin_proxy.mapping.scene import format_jellyfin_item


def _path_routes():
    return {r.path: r for r in app_mod.routes if isinstance(r, Route)}


def test_videos_item_id_route_exists():
    """`GET /Videos/{item_id}` must be routable, not swallowed by catch_all."""
    routes = _path_routes()
    assert "/Videos/{item_id}" in routes, (
        "/Videos/{item_id} is unrouted — clients get the catch-all empty "
        "paging envelope and see zero MediaSources, so no version switcher"
    )


def test_videos_item_id_route_targets_item_details():
    """It must share the item handler: that is what builds MediaSources."""
    route = _path_routes()["/Videos/{item_id}"]
    assert route.endpoint is app_mod.endpoint_item_details


def test_videos_route_does_not_shadow_stream_routes():
    """The new bare route must not out-prioritise the existing sub-paths.

    Starlette matches in registration order, so a bare `/Videos/{item_id}`
    registered too early would swallow `/Videos/{id}/stream` and every
    subtitle path. This is the ordering bug the addition invites.
    """
    paths = [r.path for r in app_mod.routes if isinstance(r, Route)]
    bare = paths.index("/Videos/{item_id}")
    for sub in ("/Videos/{item_id}/stream", "/Videos/{item_id}/AdditionalParts",
                "/Videos/{item_id}/Subtitles/{subtitle_index}/0/Stream.vtt"):
        assert paths.index(sub) < bare, (
            f"{sub} is registered after the bare /Videos/{{item_id}} route "
            "and would be captured by it"
        )


def _merged_scene():
    """A scene owning three files, as Stash returns after a Merge."""
    return {
        "id": "2887",
        "title": "Merged release",
        "date": "2024-01-01",
        "studio": None,
        "tags": [],
        "performers": [],
        "captions": [],
        "files": [
            {"id": "1", "path": "/data/A/main.mp4", "duration": 35021.2,
             "width": 1920, "height": 1080, "video_codec": "h264",
             "audio_codec": "aac", "size": 3880510256},
            {"id": "46906", "path": "/data/B/alt1.mp4", "duration": 1272.6,
             "width": 1920, "height": 1080, "video_codec": "h264",
             "audio_codec": "aac", "size": 295404199},
            {"id": "47021", "path": "/data/B/alt2.mkv", "duration": 31158.5,
             "width": 1920, "height": 1080, "video_codec": "h264",
             "audio_codec": "aac", "size": 3868281629},
        ],
    }


def test_video_document_exposes_every_file_as_a_media_source(monkeypatch):
    """The payload the player reads must list all versions.

    Guards the actual regression: if this ever drops back to one source,
    the switcher disappears from the player again.
    """
    monkeypatch.setattr(runtime, "MULTI_FILE_SCENES", True, raising=False)
    item = format_jellyfin_item(_merged_scene())
    sources = item["MediaSources"]

    assert len(sources) == 3
    assert [s["Id"] for s in sources] == ["scene-2887", "scene-2887-f46906",
                                          "scene-2887-f47021"]
    # Each version is independently addressable and self-describing.
    assert sources[1]["Path"] == "/data/B/alt1.mp4"
    assert sources[1]["Container"] == "mp4"
    assert sources[2]["Container"] == "mkv"
    # Clients key version rows on Name, so identical rows would merge.
    assert len({s["Name"] for s in sources}) == 3


def test_video_document_keeps_top_level_playback_fields(monkeypatch):
    """SenPlayer reads MediaStreams/Container/VideoType off the top level."""
    monkeypatch.setattr(runtime, "MULTI_FILE_SCENES", True, raising=False)
    item = format_jellyfin_item(_merged_scene())
    assert item["Container"] == "mp4"
    assert item["VideoType"] == "VideoFile"
    assert item["SourceType"] == "Default"
    assert item["MediaStreams"], "top-level MediaStreams must be present"
    assert item["RunTimeTicks"] == 350212000000


def test_single_file_scene_unaffected(monkeypatch):
    """The no-regression control: one file still yields exactly one source."""
    monkeypatch.setattr(runtime, "MULTI_FILE_SCENES", True, raising=False)
    scene = _merged_scene()
    scene["files"] = scene["files"][:1]
    item = format_jellyfin_item(scene)
    assert len(item["MediaSources"]) == 1
    assert item["MediaSources"][0]["Id"] == "scene-2887"


def test_multi_file_off_yields_single_source(monkeypatch):
    """With the knob off, behaviour must match pre-multi-file exactly."""
    monkeypatch.setattr(runtime, "MULTI_FILE_SCENES", False, raising=False)
    item = format_jellyfin_item(_merged_scene())
    assert len(item["MediaSources"]) == 1


# --- End-to-end through the real ASGI app -------------------------------
#
# The route-table assertions above prove the wiring; this proves the wire
# format. It is the test that would have caught the original bug, because
# the bug was only ever observable as an HTTP response body.

_SCENE_QUERY_MARKER = "FindScene"


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A TestClient over the real app with Stash stubbed out.

    Two things must be neutralised or the test is slow and wrong:

    * `genre_allowed_names()` — the mapper imports it *lazily* from
      `mapping.genre`, so patching the name in `endpoints.items` has no
      effect and the real helper queries Stash. Stub the source module.
    * Auth — the middleware exempts the `/videos/` prefix (it has to:
      players probe media before/while logging in), but `/items/` is
      protected, so item requests must carry a token.

    Set the token to a known value and send it, so both documents come
    from the same authenticated code path.
    """
    from starlette.testclient import TestClient

    from stash_jellyfin_proxy import app as app_mod
    from stash_jellyfin_proxy.endpoints import items as items_mod
    from stash_jellyfin_proxy.endpoints import stream as stream_mod
    from stash_jellyfin_proxy.mapping import genre as genre_mod
    from stash_jellyfin_proxy.middleware import auth as auth_mod
    from stash_jellyfin_proxy.middleware import logging as logging_mod
    from stash_jellyfin_proxy.stash import client as client_mod

    async def _fake_stash(query, variables=None):
        if _SCENE_QUERY_MARKER in query:
            return {"data": {"findScene": _merged_scene()}}
        return {"data": {}}

    async def _fake_genres():
        return None

    async def _fake_fetch(url, extra_headers=None, timeout=30, stream=False):
        return b"", "text/vtt", {}

    async def _fake_scene_info(scene_id):
        # The logging middleware calls this for anything classified as
        # playback (dashboard stream table). Left real, it costs four
        # DNS-failure retries per playback request.
        return {"title": "stub", "performer": "", "duration": 0, "file_size": 0}

    # Patch the *defining* module, not the importing one. These helpers are
    # imported lazily inside functions (genre, stream), so patching the
    # consumer module's attribute has no effect — the real helper runs and
    # burns 4 retries x ~5s of DNS failure per request, which turns a
    # millisecond test into a two-minute one. Stubbing at the source also
    # makes it impossible for these tests to reach the network.
    monkeypatch.setattr(client_mod, "stash_query", _fake_stash)
    monkeypatch.setattr(client_mod, "fetch_from_stash", _fake_fetch)
    monkeypatch.setattr(genre_mod, "genre_allowed_names", _fake_genres)

    # `endpoints.items` and `endpoints.stream` do `from ...client import
    # stash_query` at *module import time*, which binds the name into their
    # own namespace. Overwriting the client module attribute afterwards does
    # not rebind those copies — so the handler would call the real thing
    # and the scene lookup would 404. Patch the consumer namespaces too.
    monkeypatch.setattr(items_mod, "stash_query", _fake_stash)
    monkeypatch.setattr(stream_mod, "fetch_from_stash", _fake_fetch, raising=False)
    monkeypatch.setattr(stream_mod, "stash_query", _fake_stash, raising=False)
    monkeypatch.setattr(logging_mod, "get_scene_info", _fake_scene_info)

    # Playback requests make the logging middleware persist counters, and
    # state/stats.py resolves its path from runtime.CONFIG_FILE — falling
    # back to the CWD, which drops proxy_stats.json into the repo root
    # during test runs. Point it at tmp_path for the duration.
    monkeypatch.setattr(runtime, "CONFIG_FILE", str(tmp_path / "sjp.conf"),
                        raising=False)

    monkeypatch.setattr(runtime, "MULTI_FILE_SCENES", True, raising=False)
    monkeypatch.setattr(runtime, "ACCESS_TOKEN", "test-token", raising=False)
    # A failed auth would otherwise register IP-ban state that leaks into
    # other tests in the session.
    monkeypatch.setattr(auth_mod, "_ip_failures", {})

    # Deliberately NOT `with TestClient(app)`. Entering the context manager
    # runs the ASGI lifespan, and this app's startup performs a *real*
    # synchronous Stash connection probe (5s timeout x 4 attempts). That
    # single line turned each test into 22-44 seconds of DNS failures and
    # buried the assertions in retry noise. A bare TestClient skips
    # lifespan entirely, which is exactly right for route-level tests.
    c = TestClient(app_mod.app)
    c.headers.update({"X-Emby-Token": "test-token"})
    yield c
    c.close()


def test_http_videos_document_lists_all_versions(client):
    """`GET /Videos/{id}` must return every version, not the empty envelope.

    The failure mode this pins: the request used to fall through to
    `catch_all`, which answers `{"Items": [], "TotalRecordCount": 0}`.
    A client sees zero MediaSources, concludes the file has no alternates,
    and renders no in-player switcher.
    """
    r = client.get("/Videos/scene-2887")
    assert r.status_code == 200
    body = r.json()

    # Must NOT be the catch-all paging envelope.
    assert body.get("TotalRecordCount") != 0 or body.get("Items") is None, (
        "response is still the catch-all empty envelope — the client will "
        "see no MediaSources and offer no version switch"
    )

    sources = body["MediaSources"]
    assert len(sources) == 3
    assert [s["Id"] for s in sources] == [
        "scene-2887", "scene-2887-f46906", "scene-2887-f47021",
    ]
    # The fields a player needs to build a stream URL per version.
    for s in sources:
        assert s["Protocol"] == "File"
        assert s["SupportsDirectPlay"] is True
        assert s["Path"]
    # Top-level playback fields SenPlayer reads off the item itself.
    assert body["Container"] == "mp4"
    assert body["VideoType"] == "VideoFile"
    assert body["MediaStreams"]


def test_http_videos_document_matches_items_document(client):
    """`/Videos/{id}` and `/Items/{id}` must agree — same handler, same shape.

    Guards against the two paths drifting if either is re-routed later.
    """
    v = client.get("/Videos/scene-2887").json()
    i = client.get("/Items/scene-2887").json()
    assert [s["Id"] for s in v["MediaSources"]] == \
           [s["Id"] for s in i["MediaSources"]]
    assert v["Id"] == i["Id"] == "scene-2887"


def test_http_subtitle_route_still_routed_after_adding_bare_videos(client):
    """Adding the bare route must not steal the subtitle sub-paths.

    Starlette matches in registration order, so a bare `/Videos/{id}`
    placed above these would take their traffic. Assert the routes resolve
    to the subtitle handler rather than the item handler.
    """
    r = client.get("/Videos/scene-2887/Subtitles/1/0/Stream.vtt")
    # Reaching the subtitle handler means Stash was consulted (stubbed →
    # 404 "not found" for captions), not the item handler (200 item JSON).
    assert r.status_code != 200 or "MediaSources" in r.text


# --- Playback-path classification -------------------------------------
#
# The logging middleware feeds the dashboard's live-stream table from
# requests it classifies as playback. It used to treat *any* `/Videos/`
# path as playback, which the new bare metadata route would have tripped:
# every metadata fetch then opened a phantom stream entry and issued an
# extra Stash query. These lock the narrower rule down.

@pytest.mark.parametrize("path", [
    "/Videos/scene-1/stream",
    "/Videos/scene-1/Stream",
    "/Videos/scene-1/stream.mkv",
    "/Videos/scene-1-f46906/stream.mp4",
    "/Videos/scene-1/Subtitles/1/Stream.srt",
    "/Videos/scene-1/Subtitles/1/0/Stream.vtt",
    "/Videos/scene-1/scene-1/Subtitles/2/0/Stream.vtt",
    "/videos/scene-1/stream",
])
def test_playback_paths_recognised(path):
    from stash_jellyfin_proxy.middleware.logging import is_playback_path
    assert is_playback_path(path) is True


@pytest.mark.parametrize("path", [
    "/Videos/scene-1",              # the metadata document we just added
    "/Videos/scene-1-f46906",       # a file-scoped id, still metadata
    "/Videos/scene-1/AdditionalParts",
    "/Items/scene-1",
    "/Items/scene-1/PlaybackInfo",
    "/",
])
def test_metadata_paths_not_treated_as_playback(path):
    from stash_jellyfin_proxy.middleware.logging import is_playback_path
    assert is_playback_path(path) is False


def test_every_registered_videos_route_is_classified_consistently():
    """No `/Videos/...` route may fall through the classifier unclassified.

    Walks the real route table so a future route added without a matching
    case shows up here instead of silently skipping stream tracking.
    """
    import re

    from stash_jellyfin_proxy.middleware.logging import is_playback_path

    for r in app_mod.routes:
        if not isinstance(r, Route):
            continue
        if not r.path.startswith("/Videos/"):
            continue
        # Collapse placeholders to a slash-free token, as at request time.
        sample = re.sub(r"\{[^}]*\}", "X", r.path)
        classified = is_playback_path(sample)
        # Subtitle + stream routes must track; the bare document must not.
        expects_stream = "stream" in r.path.lower() or "Subtitles" in r.path
        assert classified == expects_stream, (
            f"{r.path} classified as playback={classified}, "
            f"expected {expects_stream}"
        )
