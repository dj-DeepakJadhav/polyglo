"""Regression test for the zero-credential path through the REAL make_providers().

Every other test file constructs its own working mock/simulated providers directly —
none of them call the actual `orchestrator.make_providers()` with zero credentials,
which is exactly the path a judge cloning this repo with no `.env` hits first. That
gap is what let two real production bugs ship past 371 passing tests, caught only by
an actual `docker run` with no credentials mounted (see docs/SESSION-LOG.md, task #16):

1. The chat fallback (`MockChatCompleter(["{}"])`) returned the literal string "{}"
   for the scene-split call, which requires a "scenes" key — every zero-credential
   story creation crashed at the very first stage.
2. The transcriber fallback (`MockTranscriber() if not cfg.has_gemini else None`) only
   works when paired with `gate.py`'s own `MockNarrator` test double, which production
   narrators never satisfy — every segment quarantined at 100% WER regardless of
   locale, so the fixed chat bug alone still shipped empty bundles.

This file exercises `make_providers()` itself, with `has_nvidia`/`has_gemini` forced
False via `reset_config_cache()` (see CLAUDE.md's own warning about `get_config()`
caching — this is exactly the kind of test that needs it), and asserts the full
pipeline actually produces non-empty, shippable content — not just that it completes
without raising.
"""

from __future__ import annotations

import pytest

from polyglo import db as dbm
from polyglo.chat import OfflineChatCompleter
from polyglo.config import get_config, reset_config_cache
from polyglo.models import DEFAULT_LOCALES, QAStatus, Story
from polyglo.orchestrator import make_providers, run_story_pipeline
from polyglo.store import BlobStore, LocalBackend
from polyglo.telemetry import TelemetryStore


@pytest.fixture()
def offline_cfg(monkeypatch, tmp_path):
    """Forces the exact zero-credential state a fresh Docker container starts in,
    without needing Docker itself for every test run."""
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")  # prevent real HTTP calls in offline tests
    monkeypatch.setenv("FAL_API_KEY", "")
    monkeypatch.setenv("REPLICATE_API_KEY", "")
    monkeypatch.setenv("B2_KEY_ID", "")
    monkeypatch.setenv("B2_APP_KEY", "")
    monkeypatch.setenv("POLYGLO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYGLO_DB_PATH", str(tmp_path / "offline.db"))
    reset_config_cache()
    try:
        yield get_config()
    finally:

        reset_config_cache()


@pytest.fixture()
def conn(tmp_path):
    c = dbm.connect(tmp_path / "test.db")
    dbm.init_db(c)
    yield c
    c.close()


@pytest.fixture()
def blob_store(tmp_path):
    return BlobStore(LocalBackend(tmp_path / "blobs"))


@pytest.fixture()
def telemetry(tmp_path):
    return TelemetryStore(tmp_path / "telemetry")


# ---------------------------------------------------------------------------
# make_providers() itself — the actual factory, not a stand-in for it
# ---------------------------------------------------------------------------


def test_make_providers_reports_zero_credentials(offline_cfg):
    assert offline_cfg.has_nvidia is False
    assert offline_cfg.has_gemini is False
    assert offline_cfg.has_image_generation is False
    assert offline_cfg.has_audio_generation is False
    assert offline_cfg.has_generation is False


def test_make_providers_uses_offline_chat_completer_not_a_broken_placeholder(offline_cfg):
    """Pins the actual fix: the real factory must select OfflineChatCompleter, not
    the old MockChatCompleter(["{}"]) that crashed on the first real call."""
    providers = make_providers(offline_cfg, chaos=None)
    assert isinstance(providers.chat, OfflineChatCompleter)


def test_make_providers_transcriber_is_none_not_a_broken_mock(offline_cfg):
    """Pins the second fix: MockTranscriber() here always scored 100% WER against
    production narrators. None correctly degrades to UNVERIFIED instead."""
    providers = make_providers(offline_cfg, chaos=None)
    assert providers.transcriber is None


# ---------------------------------------------------------------------------
# The actual regression: a full pipeline run must produce SHIPPABLE content
# ---------------------------------------------------------------------------


def test_offline_pipeline_produces_non_empty_bundles(offline_cfg, conn, blob_store, telemetry):
    """The regression test itself. Before both fixes this either raised
    AuthoringError immediately (bug #1) or completed with every bundle empty
    (bug #2, once #1 was fixed alone). Neither failure mode raises an exception
    pytest would catch on its own — both silently produced a broken deliverable,
    which is exactly why this needs an explicit content assertion, not just
    "the call didn't raise".
    """
    providers = make_providers(offline_cfg, chaos=None)
    story = Story.create("Offline Regression Story", cefr="B1")

    outcome = run_story_pipeline(
        story, "A short story about a fox in the forest.", 2, list(DEFAULT_LOCALES),
        conn, blob_store, telemetry, providers,
    )

    assert len(outcome.bundles) == len(DEFAULT_LOCALES)
    for bundle in outcome.bundles:
        assert len(bundle.image_refs) > 0, (
            f"{bundle.locale} bundle has no image refs — regression: bug #2 is back"
        )
        assert len(bundle.audio_refs) > 0, (
            f"{bundle.locale} bundle has no audio refs — regression: bug #2 is back"
        )


def test_offline_pipeline_degrades_to_unverified_not_quarantined(
    offline_cfg, conn, blob_store, telemetry,
):
    """The specific mechanism behind bug #2: with no real transcriber, every segment
    must report UNVERIFIED (shippable, honestly flagged as unproven) — never
    QUARANTINED (which bug #2's broken MockTranscriber produced for 100% of
    segments, regardless of locale or script)."""
    providers = make_providers(offline_cfg, chaos=None)
    story = Story.create("Story", cefr="B1")

    run_story_pipeline(story, "A short story.", 1, ["es-ES"], conn, blob_store,
                       telemetry, providers)

    localized = dbm.get_localized(conn, story.story_id, "es-ES")
    assert len(localized) == 1
    assert localized[0].qa_status is QAStatus.UNVERIFIED
    assert localized[0].audio_sha256 is not None


def test_offline_pipeline_dedup_still_works_with_zero_credentials(
    offline_cfg, conn, blob_store, telemetry,
):
    """The dedup invariant must hold even in the degraded offline path — images
    shared across locales is an orchestration-layer property, independent of which
    chat/transcriber fallback is active."""
    providers = make_providers(offline_cfg, chaos=None)
    story = Story.create("Story", cefr="B1")

    outcome = run_story_pipeline(story, "A short story.", 2, list(DEFAULT_LOCALES),
                                 conn, blob_store, telemetry, providers)

    assert outcome.dedup.total_refs > outcome.dedup.unique_blobs
    saved = dbm.get_story(conn, story.story_id)
    assert len({s.image_sha256 for s in saved.scenes}) == 2   # 2 scenes, not 2 x 4 locales


def test_offline_scene_splitting_respects_the_requested_scene_count(
    offline_cfg, conn, blob_store, telemetry,
):
    """A narrower pin of bug #1's exact symptom: OfflineChatCompleter must honour
    whatever n_scenes was actually requested, not return a fixed/wrong count."""
    providers = make_providers(offline_cfg, chaos=None)
    story = Story.create("Story", cefr="A2")

    outcome = run_story_pipeline(story, "text", 4, ["es-ES"], conn, blob_store,
                                 telemetry, providers)

    saved = dbm.get_story(conn, story.story_id)
    assert len(saved.scenes) == 4
