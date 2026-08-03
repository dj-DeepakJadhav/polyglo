"""Tests for reading asset bytes back off a file:// URL."""

from __future__ import annotations

import pytest

from polyglo.assets_io import AssetIOError, read_asset_bytes
from genblaze_core.models import Asset


def make_asset(url: str, media_type: str = "audio/wav") -> Asset:
    return Asset(asset_id="a1", url=url, media_type=media_type, sha256="a" * 64,
                size_bytes=10)


def test_reads_bytes_from_a_file_uri(tmp_path):
    p = tmp_path / "x.wav"
    p.write_bytes(b"hello audio")
    asset = make_asset(p.as_uri())
    assert read_asset_bytes(asset) == b"hello audio"


def test_raises_clearly_for_missing_file(tmp_path):
    asset = make_asset((tmp_path / "missing.wav").as_uri())
    with pytest.raises(AssetIOError, match="not found"):
        read_asset_bytes(asset)


def test_raises_clearly_for_unsupported_scheme():
    asset = make_asset("https://example.com/audio.wav")
    with pytest.raises(AssetIOError, match="only file:// is implemented"):
        read_asset_bytes(asset)


def test_handles_windows_drive_letter_paths(tmp_path):
    """file:///C:/foo parses with a leading slash before the drive letter on Windows —
    must be stripped or Path() treats it as a relative/invalid path."""
    p = tmp_path / "win.wav"
    p.write_bytes(b"data")
    uri = p.as_uri()
    assert read_asset_bytes(make_asset(uri)) == b"data"
