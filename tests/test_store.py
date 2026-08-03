"""Tests for the content-addressed blob store.

Everything here runs on ``LocalBackend`` — no credentials, no network. That is the
point: the storage layer is fully exercised before an API key exists.
"""

from __future__ import annotations

import pytest

from polyglo.store import (
    BlobStore,
    LocalBackend,
    StorageBackend,
    blob_key,
    make_store,
    sha256_bytes,
    sha256_file,
)


@pytest.fixture()
def store(tmp_path):
    return BlobStore(LocalBackend(tmp_path / "blobs"))


# ---------------------------------------------------------------------------
# Hashing and key layout
# ---------------------------------------------------------------------------


def test_sha256_matches_known_vector():
    # Well-known empty-string digest — guards against an accidental algo swap.
    assert sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_blob_key_fans_out_by_prefix():
    sha = "ab" + "cd" + "e" * 60
    assert blob_key(sha) == f"blobs/ab/cd/{sha}"


def test_blob_key_rejects_malformed_hash():
    with pytest.raises(ValueError, match="64-char sha256"):
        blob_key("tooshort")


def test_sha256_file_matches_sha256_bytes(tmp_path):
    p = tmp_path / "x.bin"
    data = b"polyglo" * 1000
    p.write_bytes(data)
    assert sha256_file(p) == sha256_bytes(data)


def test_local_backend_satisfies_protocol(tmp_path):
    assert isinstance(LocalBackend(tmp_path), StorageBackend)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_put_then_get_roundtrip(store):
    data = b"the quick brown fox"
    res = store.put_bytes(data)
    assert res.sha256 == sha256_bytes(data)
    assert res.deduped is False
    assert res.size == len(data)
    assert store.get(res.sha256) == data
    assert store.exists(res.sha256)


def test_put_file_roundtrip(store, tmp_path):
    p = tmp_path / "scene.png"
    p.write_bytes(b"\x89PNG fake")
    res = store.put_file(p)
    assert store.get(res.sha256) == b"\x89PNG fake"


def test_missing_blob_reports_absent(store):
    assert store.exists("f" * 64) is False


# ---------------------------------------------------------------------------
# Dedup — the behaviour the product is built on
# ---------------------------------------------------------------------------


def test_identical_bytes_dedupe(store):
    data = b"a scene illustration"
    first = store.put_bytes(data)
    second = store.put_bytes(data)

    assert first.sha256 == second.sha256
    assert first.deduped is False
    assert second.deduped is True
    assert store.stats.writes == 1
    assert store.stats.dedup_hits == 1


def test_different_bytes_do_not_dedupe(store):
    a = store.put_bytes(b"scene one")
    b = store.put_bytes(b"scene two")
    assert a.sha256 != b.sha256
    assert store.stats.writes == 2
    assert store.stats.dedup_hits == 0


def test_locale_fanout_stores_image_once(store):
    """The headline scenario: one image referenced by twenty locales.

    This mirrors what the pipeline does — each locale bundle 'puts' the same scene
    image. Nineteen of those twenty puts must be free.
    """
    image = b"<scene image bytes>"
    results = [store.put_bytes(image) for _ in range(20)]

    assert len({r.sha256 for r in results}) == 1
    assert store.stats.writes == 1
    assert store.stats.dedup_hits == 19
    assert store.stats.hit_rate == pytest.approx(0.95)
    assert store.stats.bytes_written == len(image)
    assert store.stats.bytes_deduped == len(image) * 19


def test_dedup_summary_is_human_readable(store):
    for _ in range(4):
        store.put_bytes(b"same")
    assert "4 puts -> 1 stored, 3 deduped" in store.dedup_summary()


def test_hit_rate_is_zero_on_empty_store(store):
    assert store.stats.hit_rate == 0.0


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def test_verify_passes_for_intact_blob(store):
    res = store.put_bytes(b"intact")
    assert store.verify(res.sha256) is True


def test_verify_detects_corruption(store, tmp_path):
    """Content addressing makes tampering detectable — the key IS the checksum."""
    res = store.put_bytes(b"original content")
    path = tmp_path / "blobs" / blob_key(res.sha256)
    path.write_bytes(b"tampered content")

    assert store.verify(res.sha256) is False


def test_verify_returns_false_for_missing_blob(store):
    assert store.verify("0" * 64) is False


def test_partial_write_leaves_no_blob(tmp_path, monkeypatch):
    """A crash mid-write must not leave truncated bytes at a hash that lies."""
    backend = LocalBackend(tmp_path / "blobs")
    store = BlobStore(backend)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("polyglo.store.shutil.move", boom)
    with pytest.raises(OSError):
        store.put_bytes(b"never lands")

    assert store.exists(sha256_bytes(b"never lands")) is False


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_make_store_falls_back_to_local_without_credentials(monkeypatch, tmp_path):
    from polyglo.config import Config, B2Config, QAConfig, GeminiConfig

    cfg = Config(
        b2=B2Config("", "", "polyglo", ""),
        qa=QAConfig(),
        gemini=GeminiConfig(),
        nvidia_api_key="",
        gemini_api_key="",
        openrouter_api_key="",
        data_dir=tmp_path,
        db_path=tmp_path / "p.db",
    )
    assert cfg.has_b2 is False
    store = make_store(cfg)
    assert isinstance(store.backend, LocalBackend)


def test_b2_backend_refuses_to_construct_without_credentials(tmp_path):
    from polyglo.config import Config, B2Config, QAConfig, GeminiConfig
    from polyglo.store import B2Backend

    cfg = Config(
        b2=B2Config("", "", "b", ""),
        qa=QAConfig(),
        gemini=GeminiConfig(),
        nvidia_api_key="",
        gemini_api_key="",
        openrouter_api_key="",
        data_dir=tmp_path,
        db_path=tmp_path / "p.db",
    )
    with pytest.raises(RuntimeError, match="B2 credentials absent"):
        B2Backend(cfg)
