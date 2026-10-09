"""studio_collection_type — a studio item only becomes browsable for clients
that key off (Type, CollectionType). Jellyfin clients that cannot browse a
CollectionType-less container render the studio detail page with artwork
only and never request children (Yamby, observed 2026-09-16)."""

from stash_jellyfin_proxy.mapping.image_policy import studio_collection_type
from stash_jellyfin_proxy.players.profiles import load_profiles


def test_boxset_studios_carry_collection_type():
    assert studio_collection_type("BoxSet") == "movies"


def test_collectionfolder_studios_carry_collection_type():
    assert studio_collection_type("CollectionFolder") == "movies"


def test_native_studio_kind_stays_bare():
    """Type=Studio is the plain-detail rendering; adding a CollectionType
    would send SenPlayer back into its Collections view."""
    assert studio_collection_type("Studio") is None


def test_unknown_types_get_nothing():
    assert studio_collection_type("Folder") is None
    assert studio_collection_type("Person") is None
    assert studio_collection_type("") is None


def test_profile_keeps_explicit_collectionfolder_type():
    """The Web UI / conf may pin studio_type per client; a value the sentinel
    table does not know must survive load_profiles untouched."""
    profiles = {p.name: p for p in load_profiles({
        "player.default": {"performer_type": "BoxSet", "poster_format": "portrait"},
        "player.yamby": {"user_agent_match": "Yamby", "studio_type": "CollectionFolder"},
    })}
    assert profiles["yamby"].studio_type == "CollectionFolder"
    assert studio_collection_type(profiles["yamby"].studio_type) == "movies"
