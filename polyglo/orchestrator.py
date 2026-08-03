"""Ties authoring -> visuals -> localize -> narrate -> QA gate -> bundling together.

One function, `run_story_pipeline`, drives the whole thing end to end and persists
every result via `db.py` (index) and `store.py` (content-addressed blobs), while
emitting progress events a caller can forward over SSE.

Provider selection is centralized in `make_providers` so the app, the CLI, and tests
all get the same "use real if it works, simulate if it doesn't" logic in one place —
see `config.has_image_generation` / `config.has_audio_generation` for why that's not
just "has an API key", and why image and audio are gated independently rather than
by one combined flag (task #22 follow-up: image actually works, audio doesn't).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from polyglo import db as dbm
from polyglo.chat import ChatCompleter, NvidiaChatCompleter, OfflineChatCompleter
from polyglo.chaos import ChaosRegistry
from polyglo.config import Config, get_config
from polyglo.localize import LocalizationError, localize_scene, to_localized_scene
from polyglo.models import Bundle, DedupStats, QAStatus, Scene, Story
from polyglo.narrate import GeminiNarrator, NvidiaNarrator, OpenRouterNarrator, SimulatedNarrator
from polyglo.qa.budget import GeminiBudget
from polyglo.qa.gate import GateResult, Narrator, QAGate, Transcriber, VoicePlan
from polyglo.qa.gemini_transcriber import GeminiTranscriber
from polyglo.store import BlobStore, make_store
from polyglo.telemetry import TelemetryStore
from polyglo.providers.video_gen import (
    FalVideoGenerator,
    OpenRouterVideoGenerator,
    ReplicateVideoGenerator,
    SimulatedVideoGenerator,
    VideoGenerator,
)
from polyglo.visuals import (
    FallbackVisualGenerator,
    NvidiaVisualGenerator,
    OpenRouterVisualGenerator,
    SimulatedVisualGenerator,
    VisualGenerator,
    generate_story_visuals,
)

__all__ = [
    "ProgressEvent",
    "Providers",
    "PipelineOutcome",
    "make_providers",
    "run_story_pipeline",
]

ProgressCallback = Callable[["ProgressEvent"], None]


@dataclass
class ProgressEvent:
    stage: str                 # authoring | visuals | localize | narrate | qa | bundle | done
    story_id: str
    detail: str
    locale: str | None = None
    ordinal: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Providers:
    chat: ChatCompleter
    visuals: VisualGenerator
    narrator: Narrator
    transcriber: Transcriber | None
    chat_model: str
    visual_model: str
    voice_plan: VoicePlan
    video_gen: VideoGenerator | None = None

    @property
    def same_vendor_qa(self) -> bool:
        """True if the narrator and transcriber are from the same vendor family
        (e.g. GeminiNarrator + GeminiTranscriber), indicating correlated failure risk.
        """
        return isinstance(self.narrator, GeminiNarrator) and isinstance(self.transcriber, GeminiTranscriber)


def make_providers(cfg: Config | None = None, chaos: ChaosRegistry | None = None) -> Providers:
    """Real where generation actually works, simulated where nothing real is
    available — chat always uses the real path since it's confirmed live. Narration
    prefers NVIDIA, falls back to Gemini (real audio, same-model-family verification
    trade-off — see narrate.GeminiNarrator), then simulated (task #24, 2026-08-01).

    Both real and simulated visual/narration providers are given a real, LOCAL
    ``ParquetSink`` (not the single-use ``ObjectStorageSink`` — a bare ``ParquetSink``
    is safely reusable across calls, confirmed in test_telemetry.py by looping
    ``run_step`` three times against one instance). Without this, every pipeline run
    would produce genuine Genblaze manifests that are immediately discarded, and
    ``TelemetryStore`` would only ever see fixture data, never real activity — closing
    that gap is what makes the dashboard reflect the app's actual runs.
    """
    from genblaze_core import ParquetSink

    cfg = cfg or get_config()
    disabled = chaos.snapshot() if chaos else []
    sink = ParquetSink(str(cfg.data_dir / "telemetry"))

    # OfflineChatCompleter, NOT MockChatCompleter(["{}"]) — the latter returns the
    # literal string "{}" for every call, including the scene-split call, which
    # crashes at the very first pipeline stage with zero credentials. Confirmed
    # live in a fresh Docker container with no .env mounted (exactly what a judge
    # cloning this repo hits first): every story creation failed immediately with
    # "expected a 'scenes' key, got: {}" and never produced a single scene.
    from polyglo.chat import FallbackChatCompleter, GeminiChatCompleter, OpenRouterChatCompleter

    _gemini_chat = GeminiChatCompleter(cfg.gemini_api_key) if cfg.has_gemini else None
    _openrouter_chat = OpenRouterChatCompleter(cfg.openrouter_api_key) if cfg.has_openrouter else None
    _offline_chat = OfflineChatCompleter()

    if cfg.quality_mode == "pro" and cfg.has_openrouter:
        # Pro mode: OpenRouter (Claude) → Gemini → Offline.
        # We deliberately skip NVIDIA here — NVIDIA NIM doesn't host Anthropic models,
        # so passing claude slugs to it always 404s and wastes time.
        _tiers: list[ChatCompleter] = [_openrouter_chat]  # type: ignore[list-item]
        if _gemini_chat:
            _tiers.append(_gemini_chat)
        _tiers.append(_offline_chat)
        chat: ChatCompleter = FallbackChatCompleter(*_tiers)
    elif cfg.has_nvidia:
        # Free tier with NVIDIA: NVIDIA → OpenRouter → Gemini → Offline
        _tiers = [NvidiaChatCompleter()]
        if _openrouter_chat:
            _tiers.append(_openrouter_chat)
        if _gemini_chat:
            _tiers.append(_gemini_chat)
        _tiers.append(_offline_chat)
        chat = FallbackChatCompleter(*_tiers)
    elif _openrouter_chat:
        _tiers = [_openrouter_chat]
        if _gemini_chat:
            _tiers.append(_gemini_chat)
        _tiers.append(_offline_chat)
        chat = FallbackChatCompleter(*_tiers) if len(_tiers) > 1 else _openrouter_chat
    elif _gemini_chat:
        chat = FallbackChatCompleter(_gemini_chat, _offline_chat)
    else:
        chat = _offline_chat

    # Image and audio are gated independently (task #22 follow-up): direct raw-HTTP
    # testing confirmed `flux.1-dev` genuinely works (200 OK, real JPEG) while the
    # originally-configured primary `flux.1-schnell` times out — image generation was
    # never actually broken, only misconfigured. Audio is confirmed dead at the HTTP
    # level (all bundled + plausible current-catalog TTS slugs 404), so it stays
    # simulated until a working model is found.
    #
    # OpenRouter/Seedream is opt-in ONLY (prefer_openrouter_images, default false) —
    # it exists to fix a real character-consistency bug (Seedream supports real
    # image-to-image reference conditioning; NVIDIA's provider has no such parameter,
    # so cross-scene consistency there depends entirely on a repeated text
    # description). NVIDIA stays the default even when an OpenRouter key is present,
    # so setting OPENROUTER_API_KEY alone never changes existing behavior.
    # OpenRouter is genuinely pay-per-use (unlike NVIDIA's free tier), so both its
    # providers below share ONE daily call budget, same pattern as gemini_budget —
    # narration and image generation both draw from it.
    openrouter_budget = (
        GeminiBudget(cfg.openrouter.daily_call_cap, cfg.openrouter_budget_path, label="OpenRouter")
        if cfg.has_openrouter else None
    )

    visual_model = "black-forest-labs/flux.1-dev"
    if cfg.quality_mode == "pro" and cfg.has_openrouter:
        visuals: VisualGenerator = OpenRouterVisualGenerator(
            cfg.openrouter_api_key, sink=sink, budget=openrouter_budget, model="bytedance-seed/seedream-4.5"
        )
        visual_model = "bytedance-seed/seedream-4.5"
    elif cfg.has_openrouter and cfg.prefer_openrouter_images:
        visuals = OpenRouterVisualGenerator(
            cfg.openrouter_api_key, sink=sink, budget=openrouter_budget,
        )
        visual_model = "bytedance-seed/seedream-4.5"
    elif cfg.has_image_generation:
        nvidia_visuals = NvidiaVisualGenerator(sink=sink)
        visuals = FallbackVisualGenerator(
            primary=nvidia_visuals,
            secondary=(OpenRouterVisualGenerator(cfg.openrouter_api_key, sink=sink,
                                                 budget=openrouter_budget)
                      if cfg.has_openrouter else None),
            secondary_model="bytedance-seed/seedream-4.5",
        )
    else:
        visuals = SimulatedVisualGenerator(fail_models=disabled, sink=sink)

    gemini_budget = (
        GeminiBudget(cfg.gemini.daily_call_cap, cfg.gemini_budget_path, label="Gemini")
        if cfg.has_gemini else None
    )

    if cfg.quality_mode == "pro" and cfg.has_openrouter:
        narrator: Narrator = OpenRouterNarrator(cfg.openrouter_api_key, sink=sink, budget=openrouter_budget, model="mistralai/voxtral-mini-tts-2603")
    elif cfg.has_audio_generation:
        narrator = NvidiaNarrator(sink=sink)
    elif cfg.has_openrouter:
        narrator = OpenRouterNarrator(cfg.openrouter_api_key, sink=sink, budget=openrouter_budget)
    elif cfg.has_gemini:
        narrator = GeminiNarrator(cfg.gemini_api_key, budget=gemini_budget, sink=sink)
    else:
        narrator = SimulatedNarrator(fail_models=disabled, sink=sink)

    transcriber: Transcriber | None = None
    if cfg.has_gemini:
        transcriber = GeminiTranscriber(cfg.gemini_api_key, budget=gemini_budget)

    video_gen: VideoGenerator
    if cfg.fal_api_key:
        video_gen = FalVideoGenerator(cfg.fal_api_key)
    elif cfg.replicate_api_key:
        video_gen = ReplicateVideoGenerator(cfg.replicate_api_key)
    elif cfg.has_openrouter:
        video_gen = OpenRouterVideoGenerator(cfg.openrouter_api_key)
    else:
        video_gen = SimulatedVideoGenerator()

    # chat_model is passed to the active ChatCompleter's complete() call. Key notes:
    # - NvidiaChatCompleter ignores the model arg (uses its own internal default).
    # - GeminiChatCompleter ignores it too (uses gemini-2.0-flash internally).
    # - OpenRouterChatCompleter uses it verbatim.
    # - "openrouter/free" is a meta-router: picks the best available free model
    #   automatically. Confirmed 200 OK. Immune to individual model deprecations.
    if cfg.quality_mode == "pro" and cfg.has_openrouter:
        chat_model = "nvidia/nemotron-3-super-120b-a12b:free"   # 120B, confirmed 200 OK
    else:
        chat_model = "openrouter/free"                          # meta-router, always resolves

    return Providers(
        chat=chat, visuals=visuals, narrator=narrator, transcriber=transcriber,
        video_gen=video_gen,
        chat_model=chat_model,
        visual_model=visual_model,
        voice_plan=VoicePlan(primary="voice-a", alternates=["voice-b"],
                             escalation="voice-strong"),
    )


@dataclass
class PipelineOutcome:
    story: Story
    bundles: list[Bundle]
    dedup: DedupStats
    quarantined: int


def run_story_pipeline(
    story: Story,
    source_text: str,
    n_scenes: int,
    target_locales: list[str],
    conn: sqlite3.Connection,
    blob_store: BlobStore,
    telemetry: TelemetryStore,
    providers: Providers,
    *,
    on_progress: ProgressCallback | None = None,
) -> PipelineOutcome:
    """Run the full pipeline for one story and persist every result.

    Order matters and is deliberate: scenes and their images are generated and
    persisted BEFORE any locale-specific work starts, so the image hash exists to be
    shared before the first bundle references it — never generated per-locale.
    """

    def emit(stage: str, detail: str, **kw: Any) -> None:
        if on_progress:
            on_progress(ProgressEvent(stage=stage, story_id=story.story_id, detail=detail, **kw))

    from polyglo.authoring import AuthoringError, grade_source_text, split_story

    # Task #25: correct spelling/grammar and genuinely re-level the FULL source text
    # for story.cefr before splitting into scenes — split_story already asks for
    # CEFR-appropriate scene text, but only ever saw whatever raw input arrived; this
    # makes the correction a visible, inspectable step (both texts are persisted) and
    # gives split_story cleaner input to work from. Non-fatal by design: a grading
    # failure falls back to the original text rather than aborting the whole story.
    emit("authoring", "correcting and leveling source text")
    try:
        corrected_text = grade_source_text(
            source_text, story.cefr, providers.chat, model=providers.chat_model,
        )
    except AuthoringError as exc:
        emit("authoring", f"source text grading failed, using original text: {exc}")
        corrected_text = source_text

    story.original_source_text = source_text
    story.corrected_source_text = corrected_text
    # Persisted immediately, before split_story runs — if splitting then fails (a
    # real, observed case: the model occasionally doesn't honor the requested scene
    # count), the graded text must still be visible/inspectable rather than lost
    # along with the exception. Mirrors the existing "shell record so GET works
    # immediately" save at story creation time.
    dbm.save_story(conn, story)
    conn.commit()

    emit("authoring", f"splitting story into {n_scenes} scenes")
    scenes: list[Scene] = split_story(
        story, corrected_text, n_scenes, providers.chat, model=providers.chat_model,
    )
    story.scenes = scenes
    dbm.save_story(conn, story)
    conn.commit()

    emit("visuals", f"generating {len(scenes)} scene image(s) — once, shared by every locale")
    images = generate_story_visuals(scenes, providers.visuals, model=providers.visual_model)
    for scene in scenes:
        result = images[scene.ordinal]
        emit("visuals", f"scene {scene.ordinal + 1} of {len(scenes)} image ready", ordinal=scene.ordinal)
        put = blob_store.put_bytes(result.image)
        scene.image_sha256 = put.sha256
        dbm.save_scene(conn, scene)
    conn.commit()

    gate = QAGate(config=get_config().qa, transcriber=providers.transcriber)
    quarantined = 0
    bundles: list[Bundle] = []

    for locale in target_locales:
        image_refs: list[str] = []
        audio_refs: list[str] = []

        for scene in scenes:
            emit("localize", "translating", locale=locale, ordinal=scene.ordinal)
            try:
                loc_result = localize_scene(
                    scene, locale, story.cefr, providers.chat, model=providers.chat_model,
                )
            except LocalizationError as exc:
                emit("localize", f"translation failed: {exc}", locale=locale,
                     ordinal=scene.ordinal)
                continue

            ls = to_localized_scene(scene, locale, loc_result)
            if not loc_result.accepted:
                emit("localize", f"text gate rejected: {loc_result.gate.reason}",
                     locale=locale, ordinal=scene.ordinal)

            emit("narrate", "narrating + verifying", locale=locale, ordinal=scene.ordinal)
            gate_result: GateResult = gate.run(ls, providers.narrator, providers.voice_plan)
            gate_result.apply_to(ls)
            # gate.run() computes audio_sha256 from real bytes but never uploads them
            # (a real, pre-existing gap found live 2026-08-01 — every "Listen to it"
            # audio link 404'd, silently masked by the reader's own onerror fallback,
            # which looks identical to the *intentional* simulated-audio case). Same
            # pattern as the image upload just above: content-addressed, so the hash
            # `apply_to()` already set is exactly what this upload reproduces.
            if gate_result.audio:
                audio_put = blob_store.put_bytes(gate_result.audio)
                ls.audio_sha256 = audio_put.sha256
            dbm.save_localized(conn, ls)

            telemetry.write_gate_result(story.story_id, scene.ordinal, locale, gate_result)

            emit("qa", gate_result.summary(), locale=locale, ordinal=scene.ordinal,
                 data={"status": gate_result.status.value, "wer": gate_result.wer,
                      "attempts": gate_result.attempt_count})

            # Only a genuinely QUARANTINED segment is excluded from the bundle.
            # `QAStatus.is_good` (PASS/RETRIED only) is the wrong check here — it
            # would also exclude UNVERIFIED, which has real, generated content that
            # was simply never graded (no transcriber configured). Confirmed live
            # against the actual dev server: every scene came back UNVERIFIED
            # (Gemini/NVIDIA ASR wiring is still task #17-continuation) and every
            # bundle ended up with zero refs despite images and audio genuinely
            # existing in storage — silently discarding real content for no reason.
            if gate_result.status is QAStatus.QUARANTINED:
                quarantined += 1
                continue

            if scene.image_sha256:
                image_refs.append(scene.image_sha256)
            if ls.audio_sha256:
                audio_refs.append(ls.audio_sha256)

        bundle = Bundle(
            story_id=story.story_id, locale=locale,
            manifest_uri=f"local://{story.story_id}/{locale}",
            canonical_hash="",
            image_refs=sorted(set(image_refs)),
            audio_refs=audio_refs,
        )
        dbm.save_bundle(conn, bundle)
        bundles.append(bundle)
        emit("bundle", f"bundle assembled: {len(bundle.image_refs)} image ref(s), "
                       f"{len(bundle.audio_refs)} audio ref(s)", locale=locale)

    conn.commit()

    # Best-effort: snapshot the whole SQLite file to B2 so the story/scene INDEX
    # survives a platform reset (e.g. Render wipes local disk on every redeploy) —
    # the blobs themselves already survive fine in B2 regardless, confirmed live
    # 2026-08-01 (see docs/SESSION-LOG.md), but without this the index pointing to
    # them would be gone. A backup failure must not fail the story the user is
    # actually waiting on — the index staying stale until the next successful run
    # is a much smaller problem than losing an otherwise-successful pipeline result.
    try:
        dbm.backup_db_to_b2(get_config().db_path)
    except Exception as exc:
        emit("warning", f"db backup to B2 failed (non-fatal): {exc}")

    # Same treatment for the Parquet analytics lake, and for the same reason: it was
    # local-disk-only, so every redeploy/OOM-restart wiped the real numbers the
    # dashboard exists to show. Non-fatal for the same reason as the index backup —
    # losing one run's telemetry beats failing a story the user is waiting on.
    try:
        telemetry.snapshot_to_b2()
    except Exception as exc:
        emit("warning", f"telemetry snapshot to B2 failed (non-fatal): {exc}")

    dedup = dbm.dedup_stats(conn, story.story_id)
    emit("done", dedup.summary())

    return PipelineOutcome(story=story, bundles=bundles, dedup=dedup, quarantined=quarantined)
