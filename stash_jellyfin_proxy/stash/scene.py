"""Lightweight scene-info lookups used for logging, dashboard display,
and stream tracking. The full scene→Item mapping lives in
proxy/mapping/scene.py; this module only fetches the handful of fields
the RequestLoggingMiddleware and /api/streams endpoint need."""
from typing import Dict

from .client import stash_query


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
    """
    query = """query($id: ID!) {
        findScene(id: $id) {
            files { duration }
            markers { id title seconds primary_tag { id name } tags { id name } }
        }
    }"""
    try:
        result = await stash_query(query, {"id": str(numeric_id)})
        return result.get("data", {}).get("findScene")
    except Exception:
        return None
