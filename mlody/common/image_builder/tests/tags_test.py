"""Tests for phases/tags.py — tag derivation and label sanitization."""

from __future__ import annotations

import mlody

from mlody.common.image_builder.phases.tags import derive_tag, derive_tags

assert mlody.__name__ == "mlody"


_SHA40 = "abcdef1234567890abcdef1234567890abcdef12"


def test_derive_tag_lsp_server_label() -> None:
    # Spec example: //mlody/lsp:lsp_server + sha -> mlody-lsp-lsp_server-abcdef1234567890
    tag = derive_tag("//mlody/lsp:lsp_server", _SHA40)
    assert tag == "mlody-lsp-lsp_server-abcdef1234567890"


def test_derive_tag_core_worker_label() -> None:
    # Spec example: //mlody/core:worker -> mlody-core-worker-abcdef1234567890
    tag = derive_tag("//mlody/core:worker", _SHA40)
    assert tag == "mlody-core-worker-abcdef1234567890"


def test_derive_tag_strips_double_slash_prefix() -> None:
    tag = derive_tag("//some/path:target", _SHA40)
    assert not tag.startswith("/")


def test_derive_tag_replaces_colon_with_dash() -> None:
    tag = derive_tag("//pkg:name", _SHA40)
    assert ":" not in tag


def test_derive_tag_replaces_unsafe_characters_with_dash() -> None:
    # '@' is outside [A-Za-z0-9_.-], should be replaced with '-'
    tag = derive_tag("//pkg@version:target", _SHA40)
    assert "@" not in tag
    assert "-" in tag


def test_derive_tag_sha16_suffix_is_first_16_chars_of_sha() -> None:
    sha = "1234567890abcdef" + "x" * 24
    tag = derive_tag("//foo:bar", sha)
    assert tag.endswith("-1234567890abcdef")


def test_derive_tag_total_length_does_not_exceed_128_chars() -> None:
    # Label producing a very long sanitized prefix
    long_label = "//" + "a" * 200 + ":target"
    tag = derive_tag(long_label, _SHA40)
    assert len(tag) <= 128


def test_derive_tag_sha_suffix_preserved_when_truncating() -> None:
    # Even when prefix is truncated, the sha16 suffix must be intact
    long_label = "//" + "z" * 200 + ":target"
    tag = derive_tag(long_label, _SHA40)
    assert tag.endswith("-abcdef1234567890")


def test_derive_tag_short_sha_uses_full_sha() -> None:
    # SHA shorter than 16 chars: the whole SHA is used as suffix
    short_sha = "abc123"
    tag = derive_tag("//foo:bar", short_sha)
    assert tag.endswith("-abc123")


def test_derive_tags_returns_one_tag_per_target() -> None:
    targets = ["//a:b", "//c:d", "//e:f"]
    tags = derive_tags(targets, _SHA40)
    assert len(tags) == 3


def test_derive_tags_maps_labels_in_order() -> None:
    targets = ["//mlody/lsp:lsp_server", "//mlody/core:worker"]
    tags = derive_tags(targets, _SHA40)
    assert tags[0] == "mlody-lsp-lsp_server-abcdef1234567890"
    assert tags[1] == "mlody-core-worker-abcdef1234567890"
