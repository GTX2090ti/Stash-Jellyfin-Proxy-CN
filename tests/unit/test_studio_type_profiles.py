"""studio_type profile resolution — SenPlayer/Yamby get Type=Studio studios
(issue: BoxSet-typed studios landed in the client's Collections view)."""

from stash_jellyfin_proxy.players.profiles import load_profiles


def _sections_with(sections):
    base = {
        "player.default": {"performer_type": "BoxSet", "poster_format": "portrait"},
    }
    base.update(sections)
    return base


def test_senplayer_studio_type_is_studio():
    profiles = {p.name: p for p in load_profiles(_sections_with({
        "player.senplayer": {"user_agent_match": "SenPlayer"},
    }))}
    assert profiles["senplayer"].studio_type == "Studio"
    assert profiles["default"].studio_type == "BoxSet"


def test_yamby_studio_type_is_studio():
    profiles = {p.name: p for p in load_profiles(_sections_with({
        "player.yamby": {"user_agent_match": "Yamby"},
    }))}
    assert profiles["yamby"].studio_type == "Studio"


def test_other_profiles_keep_boxset():
    profiles = {p.name: p for p in load_profiles(_sections_with({
        "player.infuse": {"user_agent_match": "Infuse"},
        "player.swiftfin": {"user_agent_match": "Swiftfin"},
    }))}
    assert profiles["infuse"].studio_type == "BoxSet"
    assert profiles["swiftfin"].studio_type == "BoxSet"


def test_explicit_config_overrides_sentinel():
    profiles = {p.name: p for p in load_profiles(_sections_with({
        "player.senplayer": {"user_agent_match": "SenPlayer", "studio_type": "BoxSet"},
    }))}
    assert profiles["senplayer"].studio_type == "BoxSet"


def test_migration_writes_yamby_profile():
    from stash_jellyfin_proxy.config.migration import V2_DEFAULT_PLAYERS
    names = [n for n, _ in V2_DEFAULT_PLAYERS]
    assert "player.yamby" in names
    body = dict(next(b for n, b in V2_DEFAULT_PLAYERS if n == "player.yamby"))
    assert body["user_agent_match"] == "Yamby"
