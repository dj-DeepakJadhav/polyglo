"""Tests for the domain model and SQLite index.

The load-bearing test here is ``test_dedup_stats_proves_image_sharing`` — it encodes
the invariant the whole product rests on: images are shared across locales, audio is
not. If that test ever fails, the dedup claim in the demo is false.
"""

from __future__ import annotations

import pytest

from polyglo import db as dbm
from polyglo.models import (
    Bundle,
    DedupStats,
    LocalizedScene,
    QAStatus,
    Scene,
    Story,
    SUPPORTED_LOCALES,
    locale_flag,
    locale_name,
    slugify,
)


@pytest.fixture()
def conn(tmp_path):
    c = dbm.connect(tmp_path / "test.db")
    dbm.init_db(c)
    yield c
    c.close()


@pytest.fixture()
def story(conn):
    s = Story.create("The Lost Umbrella", cefr="B1", source_locale="en-US")
    s.scenes = [
        Scene(s.story_id, i, f"Scene {i} text.", f"Scene {i} illustration")
        for i in range(3)
    ]
    dbm.save_story(conn, s)
    return s


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------


def test_slugify_strips_accents_and_punctuation():
    assert slugify("El Niño's Café!") == "el-ninos-cafe"
    assert slugify("") == "story"


def test_locale_flag_returns_a_flag_for_every_supported_locale():
    """Task #29: pure display polish — every locale actually offered in the UI
    must have a flag, or the picker would look broken for some of them."""
    for code in SUPPORTED_LOCALES:
        flag = locale_flag(code)
        assert flag != ""
        assert len(flag) == 2  # a flag emoji is a pair of regional indicator symbols


def test_locale_flag_degrades_to_empty_string_for_an_unknown_code():
    """Never breaks anything — an unmapped code just shows no flag, not an error."""
    assert locale_flag("xx-XX") == ""


def test_locale_name_and_flag_stay_in_sync_with_supported_locales():
    for code, name in SUPPORTED_LOCALES.items():
        assert locale_name(code) == name
        assert locale_flag(code)  # every real locale has a real flag


def test_story_ids_are_unique_for_identical_titles():
    a = Story.create("Same Title")
    b = Story.create("Same Title")
    assert a.story_id != b.story_id
    assert a.story_id.startswith("same-title-")


@pytest.mark.parametrize(
    ("status", "good", "terminal"),
    [
        (QAStatus.PASS, True, True),
        (QAStatus.RETRIED, True, True),      # passed eventually — still shippable
        (QAStatus.QUARANTINED, False, True),
        (QAStatus.PENDING, False, False),
        (QAStatus.UNVERIFIED, False, False),
    ],
)
def test_qa_status_semantics(status, good, terminal):
    assert status.is_good is good
    assert status.is_terminal is terminal


def test_dedup_ratio_handles_empty_and_full():
    assert DedupStats(0, 0).dedup_ratio == 0.0
    assert DedupStats(10, 10).dedup_ratio == 0.0          # nothing shared
    assert DedupStats(10, 5).dedup_ratio == pytest.approx(0.5)
    assert DedupStats(bytes_naive=100, bytes_stored=40, total_refs=1, unique_blobs=1).bytes_saved == 60


# ---------------------------------------------------------------------------
# Persistence round-trips
# ---------------------------------------------------------------------------


def test_story_roundtrip(conn, story):
    got = dbm.get_story(conn, story.story_id)
    assert got is not None
    assert got.title == "The Lost Umbrella"
    assert got.cefr == "B1"
    assert len(got.scenes) == 3
    assert [s.ordinal for s in got.scenes] == [0, 1, 2]


def test_get_missing_story_returns_none(conn):
    assert dbm.get_story(conn, "does-not-exist") is None


def test_save_story_is_idempotent(conn, story):
    dbm.save_story(conn, story)
    dbm.save_story(conn, story)
    assert len(dbm.get_scenes(conn, story.story_id)) == 3


# ---------------------------------------------------------------------------
# Task #25: original/corrected source text + the migration that added the columns
# ---------------------------------------------------------------------------


def test_original_and_corrected_source_text_roundtrip(conn, story):
    story.original_source_text = "a kat sits down"
    story.corrected_source_text = "A cat sits down."
    dbm.save_story(conn, story)

    got = dbm.get_story(conn, story.story_id)
    assert got.original_source_text == "a kat sits down"
    assert got.corrected_source_text == "A cat sits down."


def test_source_text_defaults_to_none_when_never_set(conn, story):
    got = dbm.get_story(conn, story.story_id)
    assert got.original_source_text is None
    assert got.corrected_source_text is None


def test_resaving_without_source_text_does_not_clobber_it(conn, story):
    """Mirrors test_scene_image_hash_is_not_clobbered_by_later_upsert: the pipeline
    saves a shell Story record before grading runs, then again afterward with the
    text set — an earlier save must never win over a later one that has real data,
    and a later save with None must not erase what a real grading call already set."""
    story.original_source_text = "raw"
    story.corrected_source_text = "corrected"
    dbm.save_story(conn, story)

    story.original_source_text = None
    story.corrected_source_text = None
    dbm.save_story(conn, story)

    got = dbm.get_story(conn, story.story_id)
    assert got.original_source_text == "raw"
    assert got.corrected_source_text == "corrected"


def test_migration_adds_missing_columns_to_a_pre_task_25_stories_table(tmp_path):
    """A database restored from a B2 snapshot taken before task #25 existed has a
    ``stories`` table without these columns — init_db must add them (not just rely
    on CREATE TABLE IF NOT EXISTS, which never alters an existing table)."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE stories (
               story_id TEXT PRIMARY KEY, title TEXT NOT NULL, cefr TEXT NOT NULL,
               source_locale TEXT NOT NULL, created_at TEXT NOT NULL
           )"""
    )
    conn.commit()
    conn.close()

    conn = dbm.connect(path)
    dbm.init_db(conn)  # must not raise

    s = Story.create("Migrated Story", cefr="A2", source_locale="en-US")
    s.original_source_text = "raw"
    s.corrected_source_text = "fixed"
    dbm.save_story(conn, s)

    got = dbm.get_story(conn, s.story_id)
    assert got.original_source_text == "raw"
    assert got.corrected_source_text == "fixed"
    conn.close()


def test_scene_image_hash_is_not_clobbered_by_later_upsert(conn, story):
    """Re-saving a scene without an image must not wipe an existing one.

    The pipeline saves scenes before generating visuals, so a naive upsert would
    erase image hashes on any subsequent metadata write.
    """
    scene = story.scenes[0]
    scene.image_sha256 = "a" * 64
    dbm.save_scene(conn, scene)

    dbm.save_scene(conn, Scene(story.story_id, 0, "edited text", "edited prompt"))

    got = dbm.get_scenes(conn, story.story_id)[0]
    assert got.source_text == "edited text"
    assert got.image_sha256 == "a" * 64


def test_localized_scene_roundtrip(conn, story):
    ls = LocalizedScene(
        story_id=story.story_id,
        ordinal=0,
        locale="es-ES",
        text="Texto de la escena.",
        audio_sha256="b" * 64,
        qa_status=QAStatus.RETRIED,
        wer=0.08,
        attempts=2,
        transcript="texto de la escena",
        voice_model="riva-alt",
    )
    dbm.save_localized(conn, ls)

    got = dbm.get_localized(conn, story.story_id, "es-ES")[0]
    assert got.text == "Texto de la escena."
    assert got.qa_status is QAStatus.RETRIED
    assert got.wer == pytest.approx(0.08)
    assert got.attempts == 2
    assert got.voice_model == "riva-alt"


def test_foreign_key_rejects_orphan_localized_scene(conn, story):
    orphan = LocalizedScene(story.story_id, 99, "es-ES", "no such scene")
    with pytest.raises(Exception):
        dbm.save_localized(conn, orphan)
        conn.commit()


def test_cascade_delete_removes_children(conn, story):
    dbm.save_localized(conn, LocalizedScene(story.story_id, 0, "es-ES", "hola"))
    conn.execute("DELETE FROM stories WHERE story_id = ?", (story.story_id,))
    conn.commit()
    assert dbm.get_localized(conn, story.story_id) == []


# ---------------------------------------------------------------------------
# Query surfaces
# ---------------------------------------------------------------------------


def test_quarantine_queue_and_summary(conn, story):
    statuses = [QAStatus.PASS, QAStatus.QUARANTINED, QAStatus.RETRIED]
    for i, st in enumerate(statuses):
        dbm.save_localized(
            conn, LocalizedScene(story.story_id, i, "fr-FR", f"t{i}", qa_status=st)
        )

    queue = dbm.get_quarantined(conn)
    assert len(queue) == 1
    assert queue[0].ordinal == 1

    assert dbm.qa_summary(conn, story.story_id) == {
        "pass": 1,
        "quarantined": 1,
        "retried": 1,
    }


# ---------------------------------------------------------------------------
# The invariant that matters
# ---------------------------------------------------------------------------


def test_dedup_stats_proves_image_sharing(conn, story):
    """3 scenes x 4 locales: images shared, audio unique.

    24 total references, but only 15 unique blobs (3 images + 12 audio).
    A regression that generated one image per locale would push unique to 24
    and the ratio to 0 — which is exactly the failure this guards against.
    """
    locales = ["es-ES", "fr-FR", "de-DE", "hi-IN"]
    image_hashes = [f"{i:064x}" for i in range(3)]          # shared by every locale

    for scene, sha in zip(story.scenes, image_hashes, strict=True):
        scene.image_sha256 = sha
        dbm.save_scene(conn, scene)

    for li, locale in enumerate(locales):
        audio_hashes = [f"{(100 + li * 10 + i):064x}" for i in range(3)]
        for i, (sha_a) in enumerate(audio_hashes):
            dbm.save_localized(
                conn,
                LocalizedScene(story.story_id, i, locale, f"text {i}",
                               audio_sha256=sha_a, qa_status=QAStatus.PASS),
            )
        dbm.save_bundle(
            conn,
            Bundle(
                story_id=story.story_id,
                locale=locale,
                manifest_uri=f"b2://polyglo/manifests/{locale}.json",
                canonical_hash=f"{li:064x}",
                image_refs=image_hashes,      # SAME three hashes every time
                audio_refs=audio_hashes,
            ),
        )
    conn.commit()

    stats = dbm.dedup_stats(conn, story.story_id)
    assert stats.total_refs == 24
    assert stats.unique_blobs == 15            # 3 images + 12 audio
    assert stats.dedup_ratio == pytest.approx(0.375)
    assert "37.5% deduplicated" in stats.summary()


def test_bundle_roundtrip_preserves_ref_kinds(conn, story):
    dbm.save_bundle(
        conn,
        Bundle(
            story_id=story.story_id,
            locale="ja-JP",
            manifest_uri="b2://x/m.json",
            canonical_hash="c" * 64,
            image_refs=["d" * 64],
            audio_refs=["e" * 64, "f" * 64],
        ),
    )
    got = dbm.get_bundles(conn, story.story_id)[0]
    assert got.image_refs == ["d" * 64]
    assert sorted(got.audio_refs) == ["e" * 64, "f" * 64]
    assert len(got.all_refs) == 3


def test_save_bundle_replaces_refs_rather_than_appending(conn, story):
    for refs in (["1" * 64, "2" * 64], ["3" * 64]):
        dbm.save_bundle(
            conn,
            Bundle(story.story_id, "it-IT", "u", "h" * 64, image_refs=refs),
        )
    got = dbm.get_bundles(conn, story.story_id)[0]
    assert got.image_refs == ["3" * 64]


def test_rebuild_from_b2_is_explicitly_unimplemented(conn):
    with pytest.raises(NotImplementedError, match="task #17"):
        dbm.rebuild_from_b2(conn, store=None)


# ---------------------------------------------------------------------------
# Whole-file DB snapshot backup/restore — the pragmatic alternative to
# rebuild_from_b2 above, actually implemented. Fixes a real gap: platforms like
# Render wipe local disk on every redeploy, so without this the story/scene index
# is lost even though the B2 blobs it points to survive fine.
# ---------------------------------------------------------------------------


class _FakeB2Backend:
    """In-memory stand-in for store.B2Backend — this project has no moto/real-B2
    test infrastructure, so backup/restore is tested against the same key-value
    contract (put/get/exists) B2Backend actually implements, not against the
    network."""

    _storage: dict[str, bytes] = {}

    def __init__(self, cfg):
        pass

    def put(self, key, data):
        _FakeB2Backend._storage[key] = data

    def get(self, key):
        return _FakeB2Backend._storage[key]

    def exists(self, key):
        return key in _FakeB2Backend._storage


@pytest.fixture(autouse=True)
def _clear_fake_b2_storage():
    _FakeB2Backend._storage.clear()
    yield
    _FakeB2Backend._storage.clear()


def _b2_config(tmp_path, *, has_b2: bool):
    from polyglo.config import B2Config, Config, GeminiConfig, QAConfig

    b2 = B2Config("k", "s", "b", "e") if has_b2 else B2Config("", "", "", "")
    return Config(
        b2=b2, qa=QAConfig(), gemini=GeminiConfig(),
        nvidia_api_key="", gemini_api_key="", openrouter_api_key="",
        data_dir=tmp_path, db_path=tmp_path / "polyglo.db",
    )


def test_backup_is_a_noop_without_b2_credentials(tmp_path):
    cfg = _b2_config(tmp_path, has_b2=False)
    db_path = tmp_path / "polyglo.db"
    db_path.write_bytes(b"real sqlite bytes")
    dbm.backup_db_to_b2(db_path, cfg)  # must not raise
    assert _FakeB2Backend._storage == {}


def test_restore_is_a_noop_without_b2_credentials(tmp_path):
    cfg = _b2_config(tmp_path, has_b2=False)
    assert dbm.restore_db_from_b2(tmp_path / "polyglo.db", cfg) is False


def test_backup_then_restore_round_trip(tmp_path, monkeypatch):
    import polyglo.store as store_mod

    monkeypatch.setattr(store_mod, "B2Backend", _FakeB2Backend)
    cfg = _b2_config(tmp_path, has_b2=True)

    source_path = tmp_path / "source.db"
    source_path.write_bytes(b"the real database contents")
    dbm.backup_db_to_b2(source_path, cfg)

    dest_path = tmp_path / "fresh_container" / "polyglo.db"
    assert not dest_path.exists()
    restored = dbm.restore_db_from_b2(dest_path, cfg)

    assert restored is True
    assert dest_path.read_bytes() == b"the real database contents"


def test_dev_and_prod_snapshots_never_collide(tmp_path, monkeypatch):
    """Regression test for a real, live hazard: the snapshot key used to be a single
    fixed string shared by every environment, so a local dev run overwrote the exact
    object the deployed instance restores from — meaning local testing could replace
    the story list a live visitor sees. Keyed off POLYGLO_ENV now; a dev backup must
    be invisible to a prod restore and vice versa."""
    import polyglo.store as store_mod
    from polyglo.config import reset_config_cache

    monkeypatch.setattr(store_mod, "B2Backend", _FakeB2Backend)

    monkeypatch.setenv("POLYGLO_ENV", "dev")
    reset_config_cache()
    dev_cfg = _b2_config(tmp_path, has_b2=True)
    dev_source = tmp_path / "dev.db"
    dev_source.write_bytes(b"DEV database - local testing junk")
    dbm.backup_db_to_b2(dev_source, dev_cfg)

    monkeypatch.setenv("POLYGLO_ENV", "prod")
    reset_config_cache()
    prod_cfg = _b2_config(tmp_path, has_b2=True)
    prod_source = tmp_path / "prod.db"
    prod_source.write_bytes(b"PROD database - real judge-facing stories")
    dbm.backup_db_to_b2(prod_source, prod_cfg)

    # A fresh prod container must restore PROD's snapshot, never dev's.
    prod_restore = tmp_path / "fresh_prod" / "polyglo.db"
    assert dbm.restore_db_from_b2(prod_restore, prod_cfg) is True
    assert prod_restore.read_bytes() == b"PROD database - real judge-facing stories"

    # ...and the dev snapshot is still independently intact.
    monkeypatch.setenv("POLYGLO_ENV", "dev")
    reset_config_cache()
    dev_restore = tmp_path / "fresh_dev" / "polyglo.db"
    assert dbm.restore_db_from_b2(dev_restore, dev_cfg) is True
    assert dev_restore.read_bytes() == b"DEV database - local testing junk"

    reset_config_cache()


def test_snapshot_key_defaults_to_dev_so_local_runs_are_safe_by_default(monkeypatch):
    """Direction matters: an unset POLYGLO_ENV must mean 'dev', never 'prod' — the
    failure mode being fixed is caused by local runs, so local must be the safe
    default rather than requiring developers to opt out."""
    from polyglo.config import get_config, reset_config_cache

    monkeypatch.delenv("POLYGLO_ENV", raising=False)
    reset_config_cache()
    assert get_config().env == "dev"
    assert "/dev/" in dbm._db_snapshot_key()
    reset_config_cache()


def test_restore_never_overwrites_an_existing_local_file(tmp_path, monkeypatch):
    """The critical safety guard: a real local dev database must never be silently
    clobbered by a stale snapshot from a previous deploy."""
    import polyglo.store as store_mod

    monkeypatch.setattr(store_mod, "B2Backend", _FakeB2Backend)
    cfg = _b2_config(tmp_path, has_b2=True)

    backed_up = tmp_path / "backed_up.db"
    backed_up.write_bytes(b"old snapshot contents")
    dbm.backup_db_to_b2(backed_up, cfg)

    local = tmp_path / "polyglo.db"
    local.write_bytes(b"real local dev data - must survive")
    restored = dbm.restore_db_from_b2(local, cfg)

    assert restored is False
    assert local.read_bytes() == b"real local dev data - must survive"


def test_restore_returns_false_when_no_snapshot_exists_yet(tmp_path, monkeypatch):
    import polyglo.store as store_mod

    monkeypatch.setattr(store_mod, "B2Backend", _FakeB2Backend)
    cfg = _b2_config(tmp_path, has_b2=True)

    fresh = tmp_path / "polyglo.db"
    assert dbm.restore_db_from_b2(fresh, cfg) is False
    assert not fresh.exists()
