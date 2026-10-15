"""Stash scene sprites → Jellyfin Trickplay (scrubber thumbnails).

Stash's preview generator writes two web-only assets per scene (see
stashapp/stash `internal/api/urlbuilders/scene.go`):

* ``<hash>_sprite.jpg`` — ONE square sprite sheet holding every
  thumbnail, laid out row-major in a ``ceil(sqrt(n))`` × same grid
  (`pkg/scene/generate/sprite.go`).
* ``<hash>_thumbs.vtt`` — a WebVTT index whose cues give the pixel
  rectangle of each thumbnail as ``…_sprite.jpg#xywh=x,y,w,h``.

Jellyfin models the same thing but keys on geometry the Stash files
never state outright: per-thumbnail ``Width``/``Height``, thumbnails per
row and per column (``TileWidth``/``TileHeight``) and the
``Interval`` in milliseconds. All of it is derivable from the VTT
alone — the grid is square and each cue spans exactly one step — so
``trickplay_info_from_vtt`` is the entire conversion.

Because Stash emits a single sheet and Jellyfin addresses sheets by
``index = thumbnailIndex // (TileWidth * TileHeight)``, Stash's sheet is
always Jellyfin's ``0.jpg``. ThumbnailCount is the cue count, which is
≤ TileWidth*TileHeight by construction, so no thumbnail ever lands on a
second sheet.
"""
import math
import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

# A cue block: "HH:MM:SS.mmm --> HH:MM:SS.mmm".
_CUE_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})\.(\d{3})"
)
_CUE_XYWH = re.compile(r"#xywh=(\d+),(\d+),(\d+),(\d+)")


def _cue_seconds(groups) -> float:
    hours, minutes, seconds, millis = (int(g) for g in groups)
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def trickplay_info_from_vtt(vtt_text: str) -> Optional[Dict[str, Any]]:
    """Build a Jellyfin ``TrickplayInfoDto`` from Stash's sprite VTT.

    Returns None — never a partial/guessed manifest — when the text is
    missing, has no ``#xywh`` rectangles, has no timed cue, or describes
    a zero-sized/zero-length grid. Callers treat None as "no trickplay"
    and omit the DTO, which is exactly what a client sees for a video
    whose sprites were never generated.
    """
    if not vtt_text:
        return None

    rects = _CUE_XYWH.findall(vtt_text)
    times = _CUE_TIME.findall(vtt_text)
    if not rects or not times:
        return None

    thumbnail_count = len(rects)
    _, _, width, height = (int(v) for v in rects[0])
    if width <= 0 or height <= 0:
        return None

    # Stash writes one cue per thumbnail, each spanning one step, so the
    # first cue's own duration IS the interval between thumbnails — no
    # need to diff successive starts (which a single-cue VTT couldn't).
    start = _cue_seconds(times[0][:4])
    end = _cue_seconds(times[0][4:])
    interval_ms = int(round((end - start) * 1000))
    if interval_ms <= 0:
        return None

    grid = int(math.ceil(math.sqrt(thumbnail_count)))
    return {
        "Width": width,
        "Height": height,
        "TileWidth": grid,
        "TileHeight": grid,
        "ThumbnailCount": thumbnail_count,
        "Interval": interval_ms,
    }


def rebase_stash_url(url: str, stash_base: str) -> str:
    """Repoint a Stash-emitted absolute URL at the proxy's configured
    Stash base.

    GraphQL's ``paths.vtt`` / ``paths.sprite`` are built from Stash's own
    base URL, which behind a reverse proxy can name a host the proxy
    cannot reach. Only the path is trustworthy, and ``STASH_URL`` is
    where every other asset fetch already goes (see endpoints/images.py),
    so swap the origin and keep the path. Relative input (Stash returns
    ``/scene/…`` when no base URL is configured) is passed through.
    """
    if not url:
        return ""
    path = urlsplit(url).path
    return f"{stash_base.rstrip('/')}{path or url}"


def _format_seconds(value: float) -> str:
    """Jellyfin's ``{0:0.###}`` in invariant culture — up to three
    decimals, trailing zeros dropped (``10.0`` -> ``10``)."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def tiles_playlist(info: Dict[str, Any]) -> str:
    """Build the ``tiles.m3u8`` body for one TrickplayInfoDto.

    HLS with Jellyfin's two extensions: ``#EXT-X-IMAGES-ONLY`` marks the
    "segments" as images and ``#EXT-X-TILES`` carries each sheet's
    geometry, which is what the client's player parses to crop a tile.
    The layout is copied from Jellyfin's own
    ``TrickplayManager.GetHlsPlaylist`` so a client cannot tell the two
    servers apart.

    Stash emits ONE sprite sheet holding every thumbnail, so there is
    always exactly one tile: the full square grid, whose #EXTINF covers
    every thumbnail on it. Returns "" for a zero-sized/zero-count info,
    which callers answer as 404 — same as a video whose sprites were
    never generated.
    """
    width = int(info.get("Width") or 0)
    height = int(info.get("Height") or 0)
    tile_w = int(info.get("TileWidth") or 0)
    tile_h = int(info.get("TileHeight") or 0)
    count = int(info.get("ThumbnailCount") or 0)
    interval_ms = int(info.get("Interval") or 0)
    if count <= 0 or tile_w <= 0 or tile_h <= 0 or interval_ms <= 0:
        return ""

    per_tile = tile_w * tile_h
    tile_count = int(math.ceil(count / per_tile))
    thumbnail_duration = interval_ms / 1000.0

    lines = [
        "#EXTM3U",
        f"#EXT-X-TARGETDURATION:{tile_count}",
        "#EXT-X-VERSION:7",
        "#EXT-X-MEDIA-SEQUENCE:1",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-IMAGES-ONLY",
    ]
    for index in range(tile_count):
        # Jellyfin pads every tile but the last to a full grid; Stash's
        # grid is square and >= ThumbnailCount, so tile_count is always 1
        # and this reduces to the full count.
        thumbs_on_tile = per_tile if index < tile_count - 1 else count - index * per_tile
        lines.append(f"#EXTINF:{_format_seconds(thumbnail_duration * thumbs_on_tile)},")
        lines.append(
            f"#EXT-X-TILES:RESOLUTION={width}x{height},"
            f"LAYOUT={tile_w}x{tile_h},"
            f"DURATION={_format_seconds(thumbnail_duration)}"
        )
        # Relative, exactly as Jellyfin emits it: the client resolves it
        # against the playlist URL, landing back on our tile endpoint.
        lines.append(f"{index}.jpg")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
