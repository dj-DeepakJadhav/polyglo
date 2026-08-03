"""End-to-end orchestrator tests, entirely on mocks/simulated providers.

``test_images_generated_once_per_scene_not_per_locale`` is the orchestration-layer
version of the invariant already proven in test_visuals.py — here proven through the
FULL pipeline, including persistence, not just the isolated visuals stage.
"""

from __future__ import annotations

import json
import re

import pytest

from polyglo import db as dbm
from polyglo.authoring import AuthoringError
from polyglo.chat import MockChatCompleter
from polyglo.models import QAStatus, Story
from polyglo.orchestrator import Providers, ProgressEvent, run_story_pipeline
from polyglo.qa.gate import MockTranscriber, VoicePlan
from polyglo.store import BlobStore, LocalBackend
from polyglo.telemetry import TelemetryStore
from polyglo.narrate import SimulatedNarrator
from polyglo.visuals import SimulatedVisualGenerator


def scenes_json(n: int) -> str:
    return json.dumps({
        "style_guide": "a small orange tabby cat, flat children's-book watercolor style",
        "scenes": [
            {"text": f"Scene {i} happens here.", "visual_prompt": f"illustration {i}"}
            for i in range(n)
        ]
    })


class DistinctTranslationCompleter:
    """A chat double that answers the split call once, then produces a genuinely
    distinct plausible-Spanish translation per scene (keyed off the scene index
    embedded in the prompt by localize.py's TRANSLATE_PROMPT).

    ``MockChatCompleter`` repeats its last scripted response forever once exhausted —
    fine for most tests, but it means every scene in a locale gets byte-identical
    translated text, which then makes their audio genuinely (and correctly) dedupe.
    That's real content-addressing behaviour, not a bug, but it defeats tests whose
    whole point is to check per-scene distinctness. Use this instead wherever the test
    needs scene 0 and scene 1's audio to actually differ.
    """

    def __init__(self, n_scenes: int):
        self._split_response = scenes_json(n_scenes)
        self._calls = 0

    def complete(self, prompt: str, *, model: str) -> str:
        self._calls += 1
        # Dispatch on prompt content, not call order — task #25 inserted a grading
        # call before the split call, so "first call" no longer means "split call".
        if "Correct any spelling and grammar errors" in prompt:
            return json.dumps({"corrected_text": "a graded source story"})
        if "Split this story into" in prompt:
            return self._split_response
        match = re.search(r"Scene (\d+) happens here", prompt)
        idx = match.group(1) if match else str(self._calls)
        return f"la escena {idx} sucede en la casa con mucha luz natural"


class DecodingTranscriber:
    """For use only with SimulatedNarrator: its 'audio' is a deterministic encoding
    of ``model|locale|text``, not real audio, so the original text can be decoded
    straight back out — giving a "perfect" transcript per call without needing to
    know in advance what each scene's translation will be."""

    def transcribe(self, audio: bytes, locale: str) -> str:
        parts = audio.decode().split("|", 3)
        return parts[3] if len(parts) == 4 else ""


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


def make_providers(n_scenes: int, locales: list[str], translation: str = "una traduccion aceptable con palabras diferentes") -> Providers:
    # First response is for the new grading call (task #25) that now runs before
    # scene-splitting; second is the split call; the rest repeat as translations.
    chat = MockChatCompleter(
        [json.dumps({"corrected_text": "a graded source story"}), scenes_json(n_scenes), translation]
    )
    return Providers(
        chat=chat,
        visuals=SimulatedVisualGenerator(),
        narrator=SimulatedNarrator(),
        transcriber=MockTranscriber([translation]),
        chat_model="mock-chat",
        visual_model="mock-image",
        voice_plan=VoicePlan(primary="voice-a", alternates=["voice-b"]),
    )


def test_full_pipeline_persists_story_and_scenes(conn, blob_store, telemetry):
    story = Story.create("The Lost Umbrella", cefr="B1")
    providers = make_providers(3, ["es-ES"])

    outcome = run_story_pipeline(
        story, "a long source story", 3, ["es-ES"],
        conn, blob_store, telemetry, providers,
    )

    saved = dbm.get_story(conn, story.story_id)
    assert saved is not None
    assert len(saved.scenes) == 3
    assert all(s.image_sha256 for s in saved.scenes)
    assert len(outcome.bundles) == 1


def test_narrated_audio_is_actually_persisted_to_the_blob_store(conn, blob_store, telemetry):
    """Regression test for a real bug found live 2026-08-01: gate.run() computed
    audio_sha256 from real narrated bytes but never uploaded those bytes anywhere —
    every localized scene's audio_sha256 pointed at a hash the blob store had never
    seen, so every real "Listen to it" audio link 404'd. Silently masked in the UI by
    the reader's own onerror fallback, which looks identical to the *intentional*
    simulated-audio case, so this went unnoticed through every prior live check.
    Confirmed live against the real dev server (real OpenRouter-narrated MP3, real
    200 with valid ID3 bytes) before writing this as a permanent regression test."""
    story = Story.create("Story", cefr="B1")
    providers = make_providers(1, ["es-ES"])

    run_story_pipeline(story, "a short story", 1, ["es-ES"], conn, blob_store,
                       telemetry, providers)

    locs = dbm.get_localized(conn, story.story_id, "es-ES")
    assert len(locs) == 1
    assert locs[0].audio_sha256
    assert blob_store.exists(locs[0].audio_sha256)          # the actual bug: this was always False
    assert len(blob_store.get(locs[0].audio_sha256)) > 0    # real bytes, not an empty placeholder


def test_full_pipeline_persists_graded_source_text(conn, blob_store, telemetry):
    """Task #25: both the raw and corrected/leveled source text must be readable
    back afterward, not just used transiently for scene splitting."""
    story = Story.create("Story", cefr="B1")
    providers = make_providers(2, ["es-ES"])

    run_story_pipeline(story, "raw input text", 2, ["es-ES"], conn, blob_store,
                       telemetry, providers)

    saved = dbm.get_story(conn, story.story_id)
    assert saved.original_source_text == "raw input text"
    assert saved.corrected_source_text == "a graded source story"


def test_graded_source_text_survives_a_later_scene_splitting_failure(conn, blob_store, telemetry):
    """Regression test for a real bug found by live Docker testing: the graded text
    was computed but never persisted if split_story failed afterward (a real,
    observed failure mode — the model occasionally doesn't honor the requested
    scene count), because the save call happened only after splitting succeeded."""
    story = Story.create("Story", cefr="B1")

    class GradesFineThenFailsSplitting:
        def complete(self, prompt, *, model):
            if "Correct any spelling and grammar errors" in prompt:
                return json.dumps({"corrected_text": "the corrected text"})
            return "not valid scene json"  # split_story will fail on this

    providers = Providers(
        chat=GradesFineThenFailsSplitting(), visuals=SimulatedVisualGenerator(),
        narrator=SimulatedNarrator(), transcriber=None,
        chat_model="m", visual_model="m", voice_plan=VoicePlan(primary="voice-a"),
    )

    with pytest.raises(AuthoringError):
        run_story_pipeline(story, "raw text", 1, ["es-ES"], conn, blob_store,
                           telemetry, providers)

    saved = dbm.get_story(conn, story.story_id)
    assert saved is not None
    assert saved.original_source_text == "raw text"
    assert saved.corrected_source_text == "the corrected text"


def test_images_generated_once_per_scene_not_per_locale(conn, blob_store, telemetry):
    """The full-pipeline version of the dedup invariant: 3 scenes x 4 locales must
    still be exactly 3 provider calls to the visual generator."""
    story = Story.create("Story", cefr="A2")
    generator = SimulatedVisualGenerator()

    n_scenes = 3
    locales = ["es-ES", "fr-FR", "de-DE", "hi-IN"]

    providers = Providers(
        chat=DistinctTranslationCompleter(n_scenes),
        visuals=generator,
        narrator=SimulatedNarrator(),
        transcriber=DecodingTranscriber(),
        chat_model="mock-chat", visual_model="mock-image",
        voice_plan=VoicePlan(primary="voice-a"),
    )

    run_story_pipeline(story, "story text", n_scenes, locales, conn, blob_store,
                       telemetry, providers)

    assert len(generator.calls) == n_scenes   # NOT n_scenes * len(locales)


def test_dedup_stats_reflect_shared_images_across_locales(conn, blob_store, telemetry):
    story = Story.create("Story", cefr="B1")
    locales = ["es-ES", "fr-FR"]
    n_scenes = 2

    providers = Providers(
        chat=DistinctTranslationCompleter(n_scenes),
        visuals=SimulatedVisualGenerator(),
        narrator=SimulatedNarrator(),
        transcriber=DecodingTranscriber(),
        chat_model="mock-chat", visual_model="mock-image",
        voice_plan=VoicePlan(primary="voice-a"),
    )

    run_story_pipeline(story, "story text", n_scenes, locales, conn, blob_store,
                       telemetry, providers)

    stats = dbm.dedup_stats(conn, story.story_id)
    # 2 scenes x 2 locales = 4 image refs, but only 2 unique images (shared, generated
    # once). Audio genuinely differs per scene AND per locale (distinct translations,
    # SimulatedNarrator keys on locale too) -> 4 unique audio refs, no sharing.
    assert stats.total_refs == 8             # 4 image refs + 4 audio refs
    assert stats.unique_blobs == 6            # 2 unique images + 4 unique audio
    assert stats.dedup_ratio == pytest.approx(0.25)


def test_progress_events_cover_every_stage(conn, blob_store, telemetry):
    story = Story.create("Story", cefr="B1")
    providers = make_providers(2, ["es-ES"])
    events: list[ProgressEvent] = []

    run_story_pipeline(story, "story text", 2, ["es-ES"], conn, blob_store,
                       telemetry, providers, on_progress=events.append)

    stages = {e.stage for e in events}
    assert {"authoring", "visuals", "localize", "narrate", "qa", "bundle", "done"} <= stages
    assert all(e.story_id == story.story_id for e in events)


def test_qa_gate_results_are_persisted_per_locale_scene(conn, blob_store, telemetry):
    story = Story.create("Story", cefr="B1")
    providers = make_providers(2, ["es-ES"])

    run_story_pipeline(story, "story text", 2, ["es-ES"], conn, blob_store,
                       telemetry, providers)

    localized = dbm.get_localized(conn, story.story_id, "es-ES")
    assert len(localized) == 2
    assert all(ls.qa_status.is_good for ls in localized)
    assert all(ls.audio_sha256 for ls in localized)


def test_real_qa_failure_still_produces_a_bundle_with_fewer_refs(conn, blob_store, telemetry):
    """A quarantined segment must not appear in the bundle's refs, but the pipeline
    must still complete and produce a bundle for the segments that passed."""
    story = Story.create("Story", cefr="B1")
    providers = make_providers(2, ["es-ES"])
    # transcriber returns something wildly different from every scene's text -> WER high
    providers.transcriber = MockTranscriber(["completely unrelated garbled output text"])

    outcome = run_story_pipeline(story, "story text", 2, ["es-ES"], conn, blob_store,
                                 telemetry, providers)

    assert outcome.quarantined > 0
    bundle = outcome.bundles[0]
    assert len(bundle.audio_refs) < 2   # at least one scene's audio was excluded


def test_unverified_segments_still_populate_bundle_refs(conn, blob_store, telemetry):
    """Real bug, caught live against the actual dev server: with no transcriber
    configured, every segment comes back UNVERIFIED (real audio/image content, just
    never graded — see QAStatus docstring). The bundle-inclusion check used to be
    `not gate_result.status.is_good`, which treats UNVERIFIED exactly like
    QUARANTINED and silently drops real, already-generated content from the bundle
    for no reason. Only a genuinely QUARANTINED segment should be excluded.
    """
    story = Story.create("Story", cefr="B1")
    providers = make_providers(2, ["es-ES"])
    providers.transcriber = None   # forces every segment to UNVERIFIED, not PASS

    outcome = run_story_pipeline(story, "story text", 2, ["es-ES"], conn, blob_store,
                                 telemetry, providers)

    assert outcome.quarantined == 0        # unverified is not quarantined
    bundle = outcome.bundles[0]
    assert len(bundle.image_refs) == 2     # both scenes' real images are referenced
    assert len(bundle.audio_refs) == 2     # both scenes' real audio is referenced

    localized = dbm.get_localized(conn, story.story_id, "es-ES")
    assert all(ls.qa_status is QAStatus.UNVERIFIED for ls in localized)


def test_telemetry_is_written_for_real_orchestrator_runs(conn, blob_store, telemetry):
    """Closes the gap: providers must persist through a real ParquetSink, not just
    produce a manifest that's immediately discarded."""
    story = Story.create("Story", cefr="B1")
    providers = make_providers(1, ["es-ES"])

    run_story_pipeline(story, "story text", 1, ["es-ES"], conn, blob_store,
                       telemetry, providers)

    qa_rows = telemetry.qa_effectiveness()
    assert len(qa_rows) > 0
    assert sum(r["n"] for r in qa_rows) > 0


def test_locale_isolation_one_locale_failing_does_not_abort_others(conn, blob_store, telemetry):
    """A chat outage on one locale shouldn't lose the other locale's results —
    mirrors the same principle already established in localize.py."""

    class FlakyOnGerman:
        def complete(self, prompt, *, model):
            # Dispatch on prompt content, not call order — task #25 inserted a
            # grading call before the split call.
            if "Correct any spelling and grammar errors" in prompt:
                return json.dumps({"corrected_text": "a graded source story"})
            if "Split this story into" in prompt:
                return scenes_json(1)
            if "German" in prompt:
                raise RuntimeError("outage")
            return "una traduccion aceptable con palabras diferentes"

    story = Story.create("Story", cefr="B1")
    providers = Providers(
        chat=FlakyOnGerman(), visuals=SimulatedVisualGenerator(),
        narrator=SimulatedNarrator(),
        transcriber=MockTranscriber(["una traduccion aceptable con palabras diferentes"]),
        chat_model="m", visual_model="m", voice_plan=VoicePlan(primary="voice-a"),
    )

    outcome = run_story_pipeline(story, "story", 1, ["es-ES", "de-DE"], conn,
                                 blob_store, telemetry, providers)

    # both locales still produce a bundle; the German one just has fewer/no refs
    assert len(outcome.bundles) == 2
    es_bundle = next(b for b in outcome.bundles if b.locale == "es-ES")
    assert len(es_bundle.audio_refs) == 1
