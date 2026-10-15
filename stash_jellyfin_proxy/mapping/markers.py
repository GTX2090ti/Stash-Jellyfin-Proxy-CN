"""Stash scene markers → Jellyfin Chapters / MediaSegments mapping.

Stash markers (``scene.markers``) are named timestamps users (or the
marker tagger) place on a scene, each carrying a ``primary_tag`` plus
optional extra tags. Jellyfin exposes the same idea through two distinct
surfaces:

* ``BaseItemDto.Chapters`` — untyped chapter points rendered on the
  player timeline ("jump to chapter"). ALL Stash markers map here.
* ``GET /MediaSegments/{itemId}`` — TYPED ranges with a start AND an
  end, used for skip buttons (skip intro / skip credits). Only markers
  whose tag/title classify into one of the Jellyfin MediaSegmentType
  values (Commercial / Intro / Outro / Preview / Recap) map here;
  markers like "Hottest" / "Cumshot" have no Jellyfin type and stay
  chapter-only.

Stash only stores a start time per marker. The segment end is the next
marker's start, or the scene duration for the final marker — the same
convention Jellyfin's own Chapter Segments Provider uses.
"""
import re
from typing import Any, Dict, List, Optional

TICKS_PER_SECOND = 10_000_000

# Phrase matches win over single words ("closing credits" must classify
# as Outro even though a naive word scan could meet "intro"-class tokens
# elsewhere in the label). Checked as plain substrings, first match wins.
_PHRASE_TO_TYPE = [
    ("Intro", ("cold open",)),
    ("Recap", ("previously on",)),
    ("Preview", ("next time", "up next", "coming up", "stay tuned")),
]

# Word-boundary token matches. Intro is checked before Outro so
# "opening credits" resolves to Intro (both lists contain "credits"-like
# vocabulary in real Stash tag taxonomies).
_WORD_TO_TYPE = [
    ("Intro", ("intro", "opening", "prologue")),
    ("Outro", ("outro", "ending", "credits", "epilogue", "closing")),
    ("Commercial", ("commercial", "advertisement", "advert", "ad", "intermission", "sponsor")),
    ("Preview", ("preview", "teaser", "trailer")),
    ("Recap", ("recap",)),
]


def _marker_texts(marker: Dict[str, Any]) -> List[str]:
    """Lowered text sources for classification: marker title, primary
    tag name, then any extra tag names."""
    texts: List[str] = []
    title = (marker.get("title") or "").strip().lower()
    if title:
        texts.append(title)
    primary = marker.get("primary_tag") or {}
    if primary.get("name"):
        texts.append(str(primary["name"]).strip().lower())
    for t in marker.get("tags") or []:
        name = (t or {}).get("name")
        if name:
            texts.append(str(name).strip().lower())
    return texts


def classify_marker(marker: Dict[str, Any]) -> Optional[str]:
    """Map a Stash marker to a Jellyfin MediaSegmentType string, or
    None when no typed keyword is present (chapter-only marker).

    Matching is word-bounded (``\\bintro\\b``) so "introduction" does
    not classify as Intro, and case-insensitive.
    """
    texts = _marker_texts(marker)
    if not texts:
        return None
    blob = " ".join(texts)

    for seg_type, phrases in _PHRASE_TO_TYPE:
        if any(p in blob for p in phrases):
            return seg_type

    for seg_type, words in _WORD_TO_TYPE:
        for word in words:
            if re.search(rf"\b{re.escape(word)}\b", blob):
                return seg_type
    return None


def _sorted_markers(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
    markers = scene.get("markers") or []
    return sorted(markers, key=lambda m: float(m.get("seconds") or 0))


def _chapter_name(marker: Dict[str, Any], index: int) -> str:
    name = (marker.get("title") or "").strip()
    if name:
        return name
    primary = marker.get("primary_tag") or {}
    if primary.get("name"):
        return str(primary["name"]).strip()
    return f"Chapter {index + 1}"


def chapter_markers(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The markers that become chapters, in timeline order.

    Sorted by start time, negatives dropped, markers sharing a timestamp
    collapsed to the first. Shared with the thumbnail endpoint so a
    chapter's list index means the same marker on both sides.
    """
    ordered: List[Dict[str, Any]] = []
    seen_seconds: set = set()
    for marker in _sorted_markers(scene):
        seconds = float(marker.get("seconds") or 0)
        if seconds < 0 or seconds in seen_seconds:
            continue
        seen_seconds.add(seconds)
        ordered.append(marker)
    return ordered


def chapter_image_tag(marker: Dict[str, Any]) -> str:
    """ETag a client echoes back when fetching this chapter's thumbnail
    (`/Items/{id}/Images/Chapter/{index}?tag=…`).

    Marker id keeps it unique per marker; the extra start second makes it
    change when the marker moves, so inserting an earlier marker can't
    leave a stale image cached under a now-shifted index.
    """
    seconds = int(float(marker.get("seconds") or 0))
    return f"m{marker.get('id')}s{seconds}"


def chapters_from_scene(scene: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Build Jellyfin ChapterInfo[] from every marker on the scene.

    Returns None (not []) when the scene carries no markers so callers
    can distinguish "chapter data fetched and empty" from "never
    fetched" — and, more importantly, so item DTOs stay byte-identical
    for scenes whose GraphQL payload predates the markers field.

    Each chapter also carries an ImageTag: chapterCardBuilder only draws
    a thumbnail when one is present, and derives its URL from the tag
    plus the chapter's index. Without it clients render a generic icon.
    """
    markers = scene.get("markers")
    if not markers:
        return None

    chapters: List[Dict[str, Any]] = []
    for marker in chapter_markers(scene):
        seconds = float(marker.get("seconds") or 0)
        chapters.append({
            "StartPositionTicks": int(seconds * TICKS_PER_SECOND),
            "Name": _chapter_name(marker, len(chapters)),
            "ImageTag": chapter_image_tag(marker),
        })
    return chapters or None


def _scene_duration_seconds(scene: Dict[str, Any]) -> float:
    files = scene.get("files") or []
    if not files:
        return 0.0
    return float(files[0].get("duration") or 0)


def media_segments_from_scene(scene: Dict[str, Any], item_id: str) -> List[Dict[str, Any]]:
    """Build typed MediaSegmentDto[] for the skip-intro/outro surface.

    Only markers classified into a Jellyfin type are emitted. A
    segment's end is the next marker's start (typed or not — any
    boundary ends the range); the final segment ends at scene
    duration. Zero-length ranges (missing duration, or markers past
    the end) are dropped.
    """
    markers = scene.get("markers") or []
    if not markers:
        return []

    ordered = _sorted_markers(scene)
    starts = [max(0.0, float(m.get("seconds") or 0)) for m in ordered]
    duration = _scene_duration_seconds(scene)

    segments: List[Dict[str, Any]] = []
    for index, marker in enumerate(ordered):
        seg_type = classify_marker(marker)
        if seg_type is None:
            continue
        start = starts[index]
        if index + 1 < len(starts):
            end = starts[index + 1]
        else:
            end = duration
        if end <= start:
            continue
        segments.append({
            "Id": f"{item_id}-m{marker.get('id')}",
            "ItemId": item_id,
            "Type": seg_type,
            "StartTicks": int(start * TICKS_PER_SECOND),
            "EndTicks": int(end * TICKS_PER_SECOND),
        })
    return segments
