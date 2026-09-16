"""`StudioIds` must be able to locate a studio container on its own.

Live regression (2026-09-16 19:18, SenPlayer/6.2.1 on iOS): tapping a studio
tile in the studios tab fired

    /Users/{u}/Items?...&ParentId=root-studios&StartIndex=0&StudioIds=studio-19

and got the unfiltered 50-studio list back, because the endpoint only looked
at StudioIds when there was *no* ParentId. The tap looked like a no-op and a
second tap (which carries ParentId=studio-N) was needed to actually enter the
studio. Issue #29.
"""

from stash_jellyfin_proxy.endpoints.items import (
    _effective_parent_id,
    _normalize_studio_id,
)


# --- _normalize_studio_id -------------------------------------------------

def test_normalize_accepts_prefixed_and_bare_ids():
    assert _normalize_studio_id("studio-19") == "studio-19"
    assert _normalize_studio_id("19") == "studio-19"
    assert _normalize_studio_id(" 19 ") == "studio-19"


def test_normalize_takes_first_of_a_list():
    assert _normalize_studio_id("studio-19,studio-20") == "studio-19"


def test_normalize_rejects_foreign_ids():
    # A real Jellyfin studio GUID (or anything else) must not become a
    # bogus `studio-<guid>` container that would 404 in Stash.
    assert _normalize_studio_id("a1b2c3d4-0000-1111-2222-333344445555") is None
    assert _normalize_studio_id("") is None
    assert _normalize_studio_id(None) is None


# --- _effective_parent_id -------------------------------------------------

def test_studioids_wins_over_root_studios_parent():
    """The SenPlayer first-tap shape — the actual bug."""
    assert _effective_parent_id("root-studios", "studio-19") == "studio-19"


def test_studioids_wins_over_other_root_folders():
    assert _effective_parent_id("root-scenes", "studio-19") == "studio-19"
    assert _effective_parent_id("root-performers", "19") == "studio-19"


def test_studioids_used_when_no_parent():
    """Roku-style: StudioIds alone, no ParentId (pre-existing behaviour)."""
    assert _effective_parent_id(None, "studio-19") == "studio-19"


def test_real_container_parent_is_not_overridden():
    """A concrete container keeps its own branch — StudioIds must not hijack
    a listing under an actual studio/group/tag, where StudioIds is only ever
    a redundant echo of the container the client already navigated into."""
    assert _effective_parent_id("studio-19", "studio-19") == "studio-19"
    assert _effective_parent_id("group-5", "studio-19") == "group-5"
    assert _effective_parent_id("tagitem-7", "studio-19") == "tagitem-7"


def test_foreign_studioid_leaves_parent_untouched():
    assert _effective_parent_id("root-studios", "some-guid") == "root-studios"
    assert _effective_parent_id(None, None) is None
    assert _effective_parent_id("root-studios", None) == "root-studios"
