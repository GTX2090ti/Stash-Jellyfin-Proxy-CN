"""Lightweight scene-info lookups used for logging, dashboard display,
and stream tracking. The full scene→Item mapping lives in
proxy/mapping/scene.py; this module only fetches the handful of fields
the RequestLoggingMiddleware and /api/streams endpoint need."""
import logging
import time
from typing import Any, Dict, Optional

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping.trickplay import (
    rebase_stash_url,
    trickplay_info_from_vtt,
)

from .client import _get_async_client, fetch_from_stash, stash_query

logger = logging.getLogger("stash-jellyfin-proxy")

# Resolved trickplay per scene id -> (expires_at_monotonic, result|None).
# A scene's sprite is fixed once generated, so the derived geometry is
# stable and the detail DTO plus every tile request can share one parse.
_TRICKPLAY_CACHE: Dict[str, tuple] = {}
_TRICKPLAY_TTL_SECONDS = 600.0


async def get_scene_info(scene_id: str) -> Dict:
    """Fetch title, performer(s), duration, and file size for a scene.
    Returns a dict with safe defaults if the query fails or the scene
    doesn't exist — callers rely on these keys always being present."""
    try:
        numeric_id = scene_id.replace("scene-", "")
        query = """query($id: ID!) {
            findScene(id: $id) {
                title
                files { basename duration size }
                performers { name }
            }
        }"""
        result = await stash_query(query, {"id": numeric_id})
        scene = result.get("data", {}).get("findScene")
        if scene:
            title = scene.get("title")
            duration = 0
            file_size = 0
            files = scene.get("files", [])
            if files:
                if not title and files[0].get("basename"):
                    title = files[0]["basename"]
                duration = files[0].get("duration", 0) or 0
                file_size = files[0].get("size", 0) or 0
            if not title:
                title = scene_id

            performers = scene.get("performers", [])
            performer = performers[0]["name"] if performers else ""
            if len(performers) > 1:
                performer = f"{performer} +{len(performers)-1}"

            return {"title": title, "performer": performer, "duration": duration, "file_size": file_size}
    except Exception:
        pass
    return {"title": scene_id, "performer": "", "duration": 0, "file_size": 0}


async def get_scene_title(scene_id: str) -> str:
    """Shortcut for log messages."""
    return (await get_scene_info(scene_id)).get("title", scene_id)


async def get_scene_markers(numeric_id: str):
    """Fetch a scene's markers plus the primary file's duration.

    Backs `GET /MediaSegments/{itemId}`: typed skip ranges need start
    markers AND an end boundary (next marker, or scene duration for the
    final one). Returns the Stash scene dict
    (``markers`` / ``files[0].duration``) or None when the scene is
    missing or the query fails — callers answer an empty segment list
    in both cases.

    Field name: Stash spells the scene's marker list ``scene_markers``
    (verified against v0.31.1 by schema introspection — ``markers`` is
    not a field on ``Scene`` and Stash answers such a query with
    `Cannot query field "markers" on type "Scene"`, a 400 that fails the
    whole detail payload). We alias it back to ``markers`` so the mapping
    layer keeps reading one stable key.
    """
    query = """query($id: ID!) {
        findScene(id: $id) {
            files { duration }
            markers: scene_markers { id title seconds primary_tag { id name } tags { id name } }
        }
    }"""
    try:
        result = await stash_query(query, {"id": str(numeric_id)})
        return result.get("data", {}).get("findScene")
    except Exception:
        return None


async def get_scene_trickplay(numeric_id: str) -> Optional[Dict[str, Any]]:
    """Resolve a scene's Stash sprite into the scrubber-preview geometry
    Jellyfin needs.

    Returns ``{"info": TrickplayInfoDto, "sprite_url": <proxied Stash
    URL>}`` or None when the scene has no generated sprite, the scene is
    missing, or the VTT is unreadable — every failure mode collapses to
    "no trickplay", which is what a client shows for a video whose
    sprites were never generated.

    Stash stores the sprite paths on ``scene.paths`` (web-only fields it
    derives from the file hash), so one GraphQL call yields both the
    sheet URL and the index; the index text is then parsed for the grid
    and interval (see mapping/trickplay.py). Cached per scene because the
    detail DTO and every tile request read through here.
    """
    key = str(numeric_id)
    now = time.monotonic()
    hit = _TRICKPLAY_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]

    result = await _load_scene_trickplay(key)
    _TRICKPLAY_CACHE[key] = (now + _TRICKPLAY_TTL_SECONDS, result)
    return result


async def _load_scene_trickplay(numeric_id: str) -> Optional[Dict[str, Any]]:
    try:
        query = """query($id: ID!) { findScene(id: $id) { paths { vtt sprite } } }"""
        result = await stash_query(query, {"id": numeric_id})
        paths = (result.get("data", {}).get("findScene") or {}).get("paths") or {}
        vtt_url = rebase_stash_url(paths.get("vtt") or "", runtime.STASH_URL)
        sprite_url = rebase_stash_url(paths.get("sprite") or "", runtime.STASH_URL)
        if not vtt_url or not sprite_url:
            return None
        body, _content_type, _headers = await fetch_from_stash(vtt_url, timeout=15)
        info = trickplay_info_from_vtt(body.decode("utf-8", errors="replace")) if body else None
        if not info:
            return None
        return {"info": info, "sprite_url": sprite_url}
    except Exception as e:
        logger.debug(f"trickplay lookup failed for scene {numeric_id}: {e}")
        return None
