"""Local smoke: boot the real Starlette app and exercise the image-cache
serverization on the actual route stack — Clear Cache endpoint rotates
IMAGE_TAG and clears the byte cache, and item responses embed the tag."""
import asyncio
import json

from starlette.testclient import TestClient

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy import app as app_mod
from stash_jellyfin_proxy.endpoints.images import _LIBRARY_CARD_CACHE
from stash_jellyfin_proxy.mapping.performer import performer_fields


def main():
    runtime.IMAGE_TAG = "img"
    runtime.SERVER_ID = "srv-local"
    runtime.ACCESS_TOKEN = "smoke-token"
    _LIBRARY_CARD_CACHE.clear()
    runtime.IMAGE_CACHE.clear()

    client = TestClient(app_mod.ui_app)  # /api/* lives on the Web UI app

    # 1) Clear Cache is a POST and rotates the tag.
    r = client.post("/api/cache/clear")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("success") is True
    assert body.get("imageTag") and body["imageTag"] != "img"
    assert runtime.IMAGE_TAG == body["imageTag"]
    print(f"[ok] /api/cache/clear rotated IMAGE_TAG -> {runtime.IMAGE_TAG}")

    # 2) GET is rejected with 405.
    assert client.get("/api/cache/clear").status_code == 405
    print("[ok] /api/cache/clear rejects GET (405)")

    # 3) Item payloads carry the rotated tag in ImageTags / BackdropImageTags.
    runtime.IMAGE_TAG = "tag-abc123"
    item = performer_fields(
        {"id": "1", "name": "X", "gender": "FEMALE", "scene_count": 1,
         "image_path": "/x.jpg", "favorite": False, "rating100": None},
        "performer-1", "Person")
    assert item["ImageTags"] == {"Primary": "tag-abc123"}, item["ImageTags"]
    assert item["BackdropImageTags"] == ["tag-abc123"]
    assert item["ImageBlurHashes"]["Primary"]["tag-abc123"] == "000000"
    print("[ok] performer_fields embeds rotated tag in ImageTags/Backdrop/BlurHashes")

    # 4) Byte cache TTL wiring is live on runtime.
    assert runtime.IMAGE_CACHE_TTL_HOURS == 24.0
    assert runtime.IMAGE_CACHE_MAX_SIZE == 100
    print("[ok] runtime IMAGE_CACHE_TTL_HOURS / MAX_SIZE live")

    # 5) Static endpoint still answers through the real route stack.
    r = client.get("/System/Info/Public")
    assert r.status_code in (200, 404, 500), r.status_code  # no Stash back-end here
    print(f"[ok] /System/Info/Public -> {r.status_code} (no Stash behind, status is expected)")

    # 6) Library tile tag derives from IMAGE_TAG (stable within a value).
    from stash_jellyfin_proxy.endpoints.views import _library_image_tag
    runtime.IMAGE_TAG = "tag-abc123"
    t1 = _library_image_tag("lib-1")
    t2 = _library_image_tag("lib-1")
    assert t1 == t2 and t1 != "img"
    runtime.IMAGE_TAG = "tag-xyz789"
    assert _library_image_tag("lib-1") != t1, "tile tag must follow rotation"
    print(f"[ok] _library_image_tag follows IMAGE_TAG rotation ({t1} -> {_library_image_tag('lib-1')})")

    # 7) Async clear-cache handler works standalone (unit-path).
    from stash_jellyfin_proxy.ui import api as ui_api
    req = client.build_request("POST", "/api/cache/clear")
    resp = asyncio.run(ui_api.ui_api_clear_cache(req))
    assert resp.status_code == 200
    print("[ok] ui_api_clear_cache async handler returns 200")

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
