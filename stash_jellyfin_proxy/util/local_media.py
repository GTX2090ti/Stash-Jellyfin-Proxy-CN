"""Local media file access for multi-file (merged) scenes.

Why this module exists
----------------------
Stash can only ever *stream* a scene's PRIMARY file. Its HTTP layer
(`internal/api/routes_scene.go`) hard-codes `scene.Files.Primary()` in
every streaming handler, and there is no route or query parameter that
selects a non-primary file. `scene.sceneStreams` does not help either —
it enumerates container/resolution variants of the primary file only.

So for a scene that carries several files (Stash's "Merge" action), the
proxy can advertise every file as a selectable version, but it cannot
fetch the non-primary ones from Stash. The only workaround that does not
mutate Stash state is to read those files off the filesystem directly,
which requires the media library to be mounted into this container
read-only and a Stash-path -> container-path mapping (LIBRARY_PATH_MAP).

Everything here degrades gracefully: when the mapping is not configured
or the file is not reachable, callers fall back to the Stash stream.
"""
import logging
import os
from typing import Optional

from starlette.responses import FileResponse

from stash_jellyfin_proxy import runtime

logger = logging.getLogger("stash-jellyfin-proxy")


def parse_path_map(raw: str = "") -> list:
    """Parse `LIBRARY_PATH_MAP` into (stash_prefix, local_prefix) tuples.

    Accepts comma-separated `stash:local` pairs, e.g.
    `/data:/library, /mnt/media:/media`. Whitespace is ignored.
    """
    value = raw if raw else getattr(runtime, "LIBRARY_PATH_MAP", "")
    pairs = []
    for chunk in (value or "").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        src, dst = chunk.split(":", 1)
        src = src.strip().rstrip("/")
        dst = dst.strip().rstrip("/")
        if src and dst:
            pairs.append((src, dst))
    return pairs


def map_stash_path(stash_path: str) -> Optional[str]:
    """Translate a path as Stash sees it into a path this container can read.

    Returns None when no mapping applies — callers must then fall back to
    the Stash stream endpoint.
    """
    if not stash_path:
        return None
    for src, dst in parse_path_map():
        if stash_path == src or stash_path.startswith(src + "/"):
            return dst + stash_path[len(src):]
    return None


def resolve_local_path(stash_path: str) -> Optional[str]:
    """Return a readable local path for a Stash path, or None."""
    mapped = map_stash_path(stash_path)
    if not mapped:
        return None
    if os.path.isfile(mapped) and os.access(mapped, os.R_OK):
        return mapped
    logger.warning(f"Path mapping matched but file is not readable: {mapped}")
    return None


def local_file_response(local_path: str, filename: str = "", download: bool = False) -> FileResponse:
    """Serve a media file straight off disk.

    Starlette's FileResponse handles Range requests itself (206 +
    Content-Range + Accept-Ranges), which is what Jellyfin-compatible
    players rely on for seeking.
    """
    media_type = "video/mp4"
    ext = os.path.splitext(local_path)[1].lower().lstrip(".")
    if ext == "mkv":
        media_type = "video/x-matroska"
    elif ext == "webm":
        media_type = "video/webm"
    elif ext in ("ts", "m4v", "mov", "avi", "wmv", "flv"):
        media_type = "video/mp4"

    headers = {}
    if filename:
        disposition = "attachment" if download else "inline"
        headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'

    return FileResponse(local_path, media_type=media_type, headers=headers or None)
