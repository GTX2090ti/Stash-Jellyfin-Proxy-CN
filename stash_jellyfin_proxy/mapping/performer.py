"""Performer → Jellyfin Person/BoxSet item shaping.

Extracted from `endpoints/items.py::_fetch_performer_packet` so the LIST
endpoint (`/Persons`) and the DETAIL endpoint (`/Items/performer-{id}`)
cannot drift apart.

Why this module exists
----------------------
The list endpoint used to emit a 6-key stub — Name, Id, Type, ImageTags,
ImageBlurHashes, BackdropImageTags (+ ChildCount) — while the detail
endpoint returned a full packet with Overview, SortName, UserData,
PremiereDate, ProductionYear, CommunityRating, Genres and Tags. Clients
render their People rail straight from the list response, so every one of
those fields was missing where it is actually displayed:

* no `UserData`     → the favourite heart never filled in on the rail
* no `SortName`     → name sorting used the raw display string, so
                      articles / prefixes sorted wrongly
* no `Overview`     → cards and rows had no subtitle text
* no `PremiereDate` / `ProductionYear` → no birth year anywhere in a list

Both paths now build items through `performer_fields`, so a field added
here shows up in both places at once.
"""
from typing import Any, Dict, List, Optional

from stash_jellyfin_proxy import runtime

# Fields the performer packet query must select. Kept as a constant so the
# list query and the detail query cannot ask for different shapes.
PACKET_FIELDS = (
    "id name disambiguation gender birthdate death_date "
    "ethnicity country hair_color eye_color "
    "height_cm weight measurements fake_tits "
    "career_start career_end tattoos piercings "
    "alias_list details rating100 favorite scene_count image_path "
    "tags { id name } "
    "stash_ids { endpoint stash_id }"
)

# Lighter field set for list views: enough for the fields a client renders
# in a grid (favourite state, sort key, birth year, rating, tags) without
# the body-measurement fields that only the About panel reads. Keeps the
# list query cheap across 261 performers.
LIST_FIELDS = (
    "id name disambiguation birthdate death_date "
    "rating100 favorite scene_count image_path alias_list details "
    "gender country career_start career_end "
    "tags { id name } "
    "stash_ids { endpoint stash_id }"
)


def _age_from(birthdate: str) -> Optional[int]:
    try:
        import datetime as _dt
        today = _dt.date.today()
        birth = _dt.date.fromisoformat(birthdate)
        return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    except Exception:
        return None


def build_overview(performer: Dict[str, Any]) -> str:
    """Synthesise a readable summary from the structured attributes.

    Stash rarely carries a hand-written bio, so a Person/BoxSet item with
    no Overview renders as a blank card. Builds one short sentence from
    gender/age/country/career/scene-count, then paragraphs for physical
    attributes, body mods and aliases.
    """
    bits: List[str] = []
    gender = (performer.get("gender") or "").lower()
    if gender == "female":
        bits.append("Female performer")
    elif gender == "male":
        bits.append("Male performer")
    elif gender:
        bits.append(gender.replace("_", " ").capitalize() + " performer")
    else:
        bits.append("Performer")

    bd = performer.get("birthdate")
    if bd:
        if performer.get("death_date"):
            bits[-1] += f", born {bd}"
        else:
            age = _age_from(bd)
            bits[-1] += f", born {bd} ({age})" if age is not None else f", born {bd}"

    if performer.get("country"):
        bits.append(f"from {performer['country']}")

    c_start, c_end = performer.get("career_start"), performer.get("career_end")
    if c_start and c_end and c_start != c_end:
        bits.append(f"active {c_start}–{c_end}")
    elif c_start:
        bits.append(f"active since {c_start}")

    scene_count = int(performer.get("scene_count") or 0)
    if scene_count:
        bits.append(f"{scene_count} scene{'s' if scene_count != 1 else ''} in library")

    parts = [", ".join(bits) + "."]

    phys: List[str] = []
    if performer.get("height_cm"):
        cm = int(performer["height_cm"])
        inches = round(cm / 2.54)
        phys.append(f"Height: {cm} cm ({inches // 12}'{inches % 12}\")")
    if performer.get("weight"):
        phys.append(f"Weight: {performer['weight']} kg")
    if performer.get("measurements"):
        phys.append(f"Measurements: {performer['measurements']}")
    if performer.get("fake_tits"):
        phys.append(f"Breasts: {performer['fake_tits']}")
    if performer.get("ethnicity"):
        phys.append(f"Ethnicity: {performer['ethnicity']}")
    if performer.get("hair_color"):
        phys.append(f"Hair: {performer['hair_color']}")
    if performer.get("eye_color"):
        phys.append(f"Eyes: {performer['eye_color']}")
    if phys:
        parts.append("\n".join(phys))

    mods: List[str] = []
    if performer.get("tattoos"):
        mods.append(f"Tattoos: {performer['tattoos']}")
    if performer.get("piercings"):
        mods.append(f"Piercings: {performer['piercings']}")
    if mods:
        parts.append("\n".join(mods))

    aliases = [a for a in (performer.get("alias_list") or []) if a]
    if aliases:
        parts.append(f"Also known as: {', '.join(aliases)}")

    if performer.get("details"):
        parts.insert(0, performer["details"].strip())

    return "\n\n".join(parts)


def performer_fields(performer: Dict[str, Any], item_id: str,
                     item_type: str) -> Dict[str, Any]:
    """Build the full Jellyfin item for one performer.

    `item_type` is "Person" for Swiftfin's native screen, "BoxSet" for
    Infuse/SenPlayer (which also need CollectionType for the grid
    renderer). Same signature and output for both callers so the list and
    the detail view can never disagree.
    """
    from stash_jellyfin_proxy.util.sort import sort_name_for

    name = performer.get("name") or f"Performer {performer.get('id', '')}"
    has_image = bool(performer.get("image_path"))
    scene_count = int(performer.get("scene_count") or 0)

    item: Dict[str, Any] = {
        "Name": name,
        "SortName": sort_name_for(name),
        "Id": item_id,
        "ServerId": runtime.SERVER_ID,
        "Type": item_type,
        "IsFolder": True,
        "ImageTags": {"Primary": "img"} if has_image else {},
        "ImageBlurHashes": ({"Primary": {"img": "000000"}, "Backdrop": {"img": "000000"}}
                            if has_image else {}),
        "PrimaryImageAspectRatio": 0.6667,
        # Swiftfin's performer page fires a backdrop request only when this
        # is non-empty.
        "BackdropImageTags": ["img"] if has_image else [],
        "ChildCount": scene_count,
        "RecursiveItemCount": scene_count,
        "Overview": build_overview(performer),
        "UserData": {
            "PlaybackPositionTicks": 0, "PlayCount": 0,
            "IsFavorite": bool(performer.get("favorite")),
            "Played": False, "Key": item_id,
        },
    }

    # BoxSet-typed performers (Infuse/SenPlayer) need CollectionType for the
    # grid renderer. Person-typed ones skip it — Swiftfin renders its native
    # Person screen, which ignores it.
    if item_type == "BoxSet":
        item["CollectionType"] = "movies"

    if performer.get("rating100") is not None:
        try:
            item["CommunityRating"] = round(float(performer["rating100"]) / 10.0, 1)
        except (TypeError, ValueError):
            pass

    # Birthday doubles as PremiereDate / ProductionYear so clients that
    # only understand item vocabulary still show the year.
    bd = performer.get("birthdate")
    if bd:
        try:
            item["PremiereDate"] = f"{bd}T00:00:00.0000000Z"
            item["ProductionYear"] = int(bd[:4])
        except (ValueError, TypeError):
            pass
    if performer.get("death_date"):
        try:
            item["EndDate"] = f"{performer['death_date']}T00:00:00.0000000Z"
        except (ValueError, TypeError):
            pass

    tag_names = [
        (t.get("name") or "").strip()
        for t in (performer.get("tags") or [])
        if t.get("name")
    ]
    if tag_names:
        item["Genres"] = tag_names
        item["Tags"] = tag_names

    stash_ids = performer.get("stash_ids") or []
    if stash_ids and stash_ids[0].get("stash_id"):
        item["ProviderIds"] = {"StashDb": stash_ids[0]["stash_id"]}

    return item
