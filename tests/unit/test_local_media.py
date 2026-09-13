"""Tests for local media access (multi-file scenes).

`content_disposition` gets the most attention here because it is the one
function whose failure is invisible: HTTP header values are latin-1, so a
non-ASCII filename raises UnicodeEncodeError inside the ASGI server, which
turns into a bare 500 with an empty body and no traceback in the log. It was
found by hand against a Japanese-named file; these tests make sure it stays
fixed.
"""
import urllib.parse

import pytest

from stash_jellyfin_proxy.util.local_media import (
    content_disposition,
    map_stash_path,
    parse_path_map,
)


class TestParsePathMap:
    def test_empty(self):
        assert parse_path_map("") == []

    def test_single_pair(self):
        assert parse_path_map("/data:/library") == [("/data", "/library")]

    def test_multiple_pairs_and_whitespace(self):
        assert parse_path_map(" /data:/library , /mnt/media:/media ") == [
            ("/data", "/library"),
            ("/mnt/media", "/media"),
        ]

    def test_trailing_slashes_are_stripped(self):
        assert parse_path_map("/data/:/library/") == [("/data", "/library")]

    def test_entries_without_a_colon_are_ignored(self):
        assert parse_path_map("/data, /mnt:/media") == [("/mnt", "/media")]

    def test_windows_style_target_keeps_only_the_first_colon(self):
        # split(":", 1) — a drive letter in the target must not be mistaken
        # for the separator.
        assert parse_path_map("/data:C:/media") == [("/data", "C:/media")]


class TestMapStashPath:
    @pytest.fixture(autouse=True)
    def _map(self, monkeypatch):
        from stash_jellyfin_proxy import runtime
        monkeypatch.setattr(runtime, "LIBRARY_PATH_MAP", "/data:/library", raising=False)

    def test_translates_a_file_path(self):
        assert map_stash_path("/data/PT/a/b.mp4") == "/library/PT/a/b.mp4"

    def test_exact_prefix_match(self):
        assert map_stash_path("/data") == "/library"

    def test_prefix_must_be_a_path_component(self):
        # /database must not match /data
        assert map_stash_path("/database/x.mp4") is None

    def test_unmapped_path_returns_none(self):
        assert map_stash_path("/elsewhere/x.mp4") is None
        assert map_stash_path("") is None

    def test_non_ascii_paths_survive(self):
        got = map_stash_path("/data/PT/アニメ/花火_おまけ_4k.mp4")
        assert got == "/library/PT/アニメ/花火_おまけ_4k.mp4"


class TestContentDisposition:
    @pytest.mark.parametrize("name", [
        "plain.mp4",
        "Fantia-posts-3072923おまけ_4k.mp4",
        "花火-星穹铁道.mp4",
        "❤ スターレ〇ル.mp4",
        "日本語のみ.mp4",
    ])
    def test_header_value_is_latin1_encodable(self, name):
        """The regression: this is what the ASGI server does to every header.

        A non-ASCII character here means the response cannot be sent at all.
        """
        header = content_disposition(name)
        header.encode("latin-1")  # must not raise

    def test_inline_by_default_attachment_for_download(self):
        assert content_disposition("a.mp4").startswith("inline;")
        assert content_disposition("a.mp4", download=True).startswith("attachment;")

    def test_ascii_names_are_kept_verbatim(self):
        assert 'filename="a.mp4"' in content_disposition("a.mp4")

    def test_rfc5987_form_carries_the_real_name(self):
        name = "花火_おまけ_4k.mp4"
        header = content_disposition(name)
        assert "filename*=UTF-8''" in header
        encoded = header.split("filename*=UTF-8''", 1)[1]
        assert urllib.parse.unquote(encoded) == name

    def test_all_non_ascii_basename_gets_usable_ascii_fallback(self):
        header = content_disposition("日本語のみ.mp4")
        assert 'filename="video.mp4"' in header

    def test_quotes_and_backslashes_cannot_break_out_of_the_value(self):
        header = content_disposition('we"ird\\name.mp4')
        assert 'filename="weirdname.mp4"' in header

    def test_extension_is_preserved_when_present(self):
        assert 'filename="video.mkv"' in content_disposition("日本語.mkv")
