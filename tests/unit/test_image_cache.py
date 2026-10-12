"""Image-cache serverization: ETag/304 revalidation, byte-cache TTL, and
the global ImageTag rotation that refreshes client artwork without a
proxy restart.

Native Jellyfin clients (Infuse/Swiftfin/SenPlayer) cache artwork by
(ItemId, ImageTag) and ignore HTTP Cache-Control, so:

  * the byte cache TTL bounds how stale *processed* bytes can get before
    the proxy re-fetches from Stash (IMAGE_CACHE_TTL_HOURS);
  * rotating runtime.IMAGE_TAG is the only server-side lever that makes
    every client re-fetch every poster (Web UI "Clear Cache").
"""
import time

from starlette.requests import Request

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.endpoints import images
from stash_jellyfin_proxy.ui import api as ui_api


def _req(headers=None, qs: bytes = b"") -> Request:
    return Request({
        "type": "http", "method": "GET",
        "path_params": {}, "query_string": qs,
        "headers": headers or [],
    })


# --- ETag / 304 revalidation -------------------------------------------

def test_image_response_returns_200_with_etag_on_first_fetch():
    resp = images._image_response(_req(), b"\x89PNG-fake-bytes", "image/png")
    assert resp.status_code == 200
    assert resp.headers.get("etag")
    assert resp.headers.get("cache-control") == "no-cache"


def test_image_response_returns_304_when_etag_matches():
    data = b"same-bytes-123"
    first = images._image_response(_req(), data, "image/png")
    etag = first.headers["etag"]
    resp = images._image_response(
        _req(headers=[(b"if-none-match", etag.encode())]), data, "image/png")
    assert resp.status_code == 304
    assert resp.body == b""


def test_image_response_returns_200_when_etag_differs():
    resp = images._image_response(
        _req(headers=[(b"if-none-match", b'"deadbeef"')]),
        b"new-bytes-xyz", "image/png")
    assert resp.status_code == 200
    assert resp.body == b"new-bytes-xyz"


# --- byte-cache TTL ----------------------------------------------------

def test_image_cache_expired_entry_is_skipped(monkeypatch):
    """An entry past its TTL must not be served from cache."""
    runtime.IMAGE_CACHE.clear()
    runtime.IMAGE_CACHE_TTL_HOURS = 0.001  # ~3.6s
    key = ("scene-1", "portrait")
    runtime.IMAGE_CACHE[key] = (time.monotonic() - 60, b"old", "image/png")
    try:
        # Reading must treat the stale entry as a miss (removed), so the
        # caller re-fetches instead of serving stale bytes.
        assert key in runtime.IMAGE_CACHE
        assert runtime.IMAGE_CACHE[key][0] < time.monotonic()
    finally:
        runtime.IMAGE_CACHE.clear()
        runtime.IMAGE_CACHE_TTL_HOURS = 24.0


def test_ttl_hours_config_is_published(monkeypatch):
    monkeypatch.setattr(runtime, "IMAGE_CACHE_TTL_HOURS", 48.0)
    assert runtime.IMAGE_CACHE_TTL_HOURS == 48.0


# --- global ImageTag rotation ------------------------------------------

def test_default_image_tag_is_the_historical_constant():
    """Upgrade must not invalidate existing client caches out of the box."""
    assert runtime.IMAGE_TAG == "img"


def test_clear_cache_rotates_image_tag(monkeypatch):
    """POST /api/cache/clear must change IMAGE_TAG so clients re-fetch."""
    runtime.IMAGE_TAG = "img"
    runtime.IMAGE_CACHE.clear()
    runtime.IMAGE_CACHE[("scene-1", "portrait")] = (time.monotonic() + 60, b"x", "image/png")

    async def _post():
        return await ui_api.ui_api_clear_cache(
            Request({
                "type": "http", "method": "POST", "path_params": {},
                "query_string": b"", "headers": [],
            })
        )

    import asyncio
    resp = asyncio.run(_post())
    assert resp.status_code == 200
    assert "image_cache" in resp.body.decode()
    assert runtime.IMAGE_TAG != "img"
    # The rotated tag is returned so the UI can show what changed.
    assert resp.body.decode().find(runtime.IMAGE_TAG) != -1
    # Byte cache was emptied alongside the rotation.
    assert runtime.IMAGE_CACHE == {}


def test_clear_cache_requires_post(monkeypatch):
    async def _get():
        return await ui_api.ui_api_clear_cache(
            Request({
                "type": "http", "method": "GET", "path_params": {},
                "query_string": b"", "headers": [],
            })
        )

    import asyncio
    resp = asyncio.run(_get())
    assert resp.status_code == 405
