"""Tests for make_providers()'s transcriber AND narrator selection logic.

Updated 2026-08-01 (task #24): GeminiNarrator now steps in as a real narrator
whenever NVIDIA audio is unavailable and Gemini is configured — so GeminiTranscriber
now activates whenever Gemini is configured at all, not only when NVIDIA audio
specifically works. Real narrated audio exists either way (NVIDIA or Gemini itself);
only the fully-simulated case (neither available) has nothing genuine to verify.
"""

from __future__ import annotations

import pytest

from polyglo.config import B2Config, Config, GeminiConfig, QAConfig
from polyglo.narrate import GeminiNarrator, NvidiaNarrator, OpenRouterNarrator, SimulatedNarrator
from polyglo.orchestrator import make_providers
from polyglo.qa.gemini_transcriber import GeminiTranscriber
from polyglo.visuals import FallbackVisualGenerator, NvidiaVisualGenerator, OpenRouterVisualGenerator


def make_config(*, nvidia_key: str, gemini_key: str, image_broken: bool,
                audio_broken: bool, tmp_path, openrouter_key: str = "",
                prefer_openrouter_images: bool = False) -> Config:
    import os

    # has_image_generation / has_audio_generation / prefer_openrouter_images read
    # these env vars directly (see Config.nvidia_image_broken / nvidia_audio_broken /
    # prefer_openrouter_images) rather than as constructor fields, so they must be
    # set here rather than passed into Config(...) below.
    os.environ["NVIDIA_IMAGE_BROKEN"] = "true" if image_broken else "false"
    os.environ["NVIDIA_AUDIO_BROKEN"] = "true" if audio_broken else "false"
    os.environ["OPENROUTER_PREFER_IMAGES"] = "true" if prefer_openrouter_images else "false"
    return Config(
        b2=B2Config("", "", "b", ""), qa=QAConfig(), gemini=GeminiConfig(),
        nvidia_api_key=nvidia_key, gemini_api_key=gemini_key,
        openrouter_api_key=openrouter_key,
        data_dir=tmp_path, db_path=tmp_path / "p.db",
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("NVIDIA_IMAGE_BROKEN", raising=False)
    monkeypatch.delenv("NVIDIA_AUDIO_BROKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_PREFER_IMAGES", raising=False)
    yield
    monkeypatch.delenv("NVIDIA_IMAGE_BROKEN", raising=False)
    monkeypatch.delenv("NVIDIA_AUDIO_BROKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_PREFER_IMAGES", raising=False)


def test_no_transcriber_when_neither_nvidia_nor_gemini_configured(tmp_path):
    cfg = make_config(nvidia_key="", gemini_key="", image_broken=True,
                      audio_broken=True, tmp_path=tmp_path)
    providers = make_providers(cfg, chaos=None)
    assert providers.transcriber is None
    assert isinstance(providers.narrator, SimulatedNarrator)


def test_gemini_narrator_and_transcriber_activate_when_nvidia_audio_still_broken(tmp_path):
    """The confirmed current production state (task #24): a real Gemini key present,
    real NVIDIA image generation working, NVIDIA audio still dead — GeminiNarrator
    now steps in as a real narrator, and GeminiTranscriber verifies it. Different
    from the pre-#24 behavior (transcriber stayed None here) — real, if
    same-model-family, narration is now preferred over staying fully simulated."""
    cfg = make_config(nvidia_key="nvapi-real", gemini_key="real-gemini-key",
                      image_broken=False, audio_broken=True, tmp_path=tmp_path)
    assert cfg.has_gemini is True
    assert cfg.has_image_generation is True
    assert cfg.has_audio_generation is False   # audio broken flag wins even with a valid key

    providers = make_providers(cfg, chaos=None)
    assert isinstance(providers.narrator, GeminiNarrator)
    assert isinstance(providers.transcriber, GeminiTranscriber)


def test_narrator_and_transcriber_share_one_gemini_budget_instance(tmp_path):
    """Narration and verification must draw from the SAME daily cap, not two
    independent trackers that would silently double the effective budget."""
    cfg = make_config(nvidia_key="nvapi-real", gemini_key="real-gemini-key",
                      image_broken=False, audio_broken=True, tmp_path=tmp_path)
    providers = make_providers(cfg, chaos=None)
    assert providers.narrator._budget is providers.transcriber._budget


def test_simulated_narrator_and_no_transcriber_when_no_gemini_key(tmp_path):
    cfg = make_config(nvidia_key="nvapi-real", gemini_key="",
                      image_broken=False, audio_broken=True, tmp_path=tmp_path)
    assert cfg.has_audio_generation is False
    assert cfg.has_gemini is False
    providers = make_providers(cfg, chaos=None)
    assert isinstance(providers.narrator, SimulatedNarrator)
    assert providers.transcriber is None


def test_nvidia_narrator_preferred_over_gemini_when_nvidia_audio_works(tmp_path):
    """The moment a working NVIDIA TTS model is found, NVIDIA is still preferred —
    it's the cross-model-family pairing GeminiTranscriber was designed to verify,
    stronger evidence than Gemini verifying itself."""
    cfg = make_config(nvidia_key="nvapi-real", gemini_key="real-gemini-key",
                      image_broken=False, audio_broken=False, tmp_path=tmp_path)
    assert cfg.has_audio_generation is True
    assert cfg.has_gemini is True

    providers = make_providers(cfg, chaos=None)
    assert isinstance(providers.narrator, NvidiaNarrator)
    assert isinstance(providers.transcriber, GeminiTranscriber)


# ---------------------------------------------------------------------------
# OpenRouter narrator/visual gating — both opt-in, neither ever displaces an
# existing setup unless explicitly configured to.
# ---------------------------------------------------------------------------


def test_openrouter_narrator_preferred_over_gemini_when_both_configured(tmp_path):
    """The whole point of adding OpenRouter as a narrator option: it's a genuinely
    independent vendor from GeminiTranscriber (the verifier), fixing the
    same-model-family trade-off GeminiNarrator+GeminiTranscriber otherwise carries.
    GeminiTranscriber still verifies either way."""
    cfg = make_config(nvidia_key="", gemini_key="real-gemini-key",
                      image_broken=True, audio_broken=True,
                      openrouter_key="sk-or-real", tmp_path=tmp_path)
    assert cfg.has_audio_generation is False
    assert cfg.has_openrouter is True
    assert cfg.has_gemini is True

    providers = make_providers(cfg, chaos=None)
    assert isinstance(providers.narrator, OpenRouterNarrator)
    assert isinstance(providers.transcriber, GeminiTranscriber)


def test_nvidia_narrator_still_preferred_over_openrouter_when_nvidia_audio_works(tmp_path):
    cfg = make_config(nvidia_key="nvapi-real", gemini_key="real-gemini-key",
                      image_broken=False, audio_broken=False,
                      openrouter_key="sk-or-real", tmp_path=tmp_path)
    providers = make_providers(cfg, chaos=None)
    assert isinstance(providers.narrator, NvidiaNarrator)


def test_openrouter_narrator_without_gemini_gets_no_transcriber(tmp_path):
    """Real narration from OpenRouter, but no Gemini key at all means nothing
    verifies it — same UNVERIFIED-but-honest degradation as any other real-narrator-
    without-a-verifier case."""
    cfg = make_config(nvidia_key="", gemini_key="", image_broken=True, audio_broken=True,
                      openrouter_key="sk-or-real", tmp_path=tmp_path)
    providers = make_providers(cfg, chaos=None)
    assert isinstance(providers.narrator, OpenRouterNarrator)
    assert providers.transcriber is None


def test_openrouter_key_alone_does_not_change_the_default_image_provider(tmp_path):
    """OPENROUTER_API_KEY being set must never, by itself, change which image
    generator is used — prefer_openrouter_images must be explicitly true. This is
    the "optional, never kills the existing setup" guarantee. NVIDIA is still the
    PRIMARY generator either way (wrapped in FallbackVisualGenerator so a real
    NVIDIA failure can route to OpenRouter instead of the confirmed-dead
    flux.1-schnell fallback — see test below for that case specifically)."""
    cfg = make_config(nvidia_key="nvapi-real", gemini_key="", image_broken=False,
                      audio_broken=True, openrouter_key="sk-or-real", tmp_path=tmp_path)
    assert cfg.has_openrouter is True
    assert cfg.prefer_openrouter_images is False

    providers = make_providers(cfg, chaos=None)
    assert isinstance(providers.visuals, FallbackVisualGenerator)
    assert isinstance(providers.visuals.primary, NvidiaVisualGenerator)
    assert isinstance(providers.visuals.secondary, OpenRouterVisualGenerator)
    assert providers.visual_model == "black-forest-labs/flux.1-dev"


def test_prefer_openrouter_images_switches_the_visual_generator(tmp_path):
    """The explicit opt-in: this is the actual character-consistency fix (real
    image-to-image reference conditioning) becoming active."""
    cfg = make_config(nvidia_key="nvapi-real", gemini_key="", image_broken=False,
                      audio_broken=True, openrouter_key="sk-or-real",
                      prefer_openrouter_images=True, tmp_path=tmp_path)

    providers = make_providers(cfg, chaos=None)
    assert isinstance(providers.visuals, OpenRouterVisualGenerator)
    assert providers.visual_model == "bytedance-seed/seedream-4.5"


def test_prefer_openrouter_images_with_no_key_falls_back_to_nvidia(tmp_path):
    """The flag alone can't activate anything without a real key — has_openrouter
    must also be true. No OpenRouter key also means no fallback secondary."""
    cfg = make_config(nvidia_key="nvapi-real", gemini_key="", image_broken=False,
                      audio_broken=True, openrouter_key="",
                      prefer_openrouter_images=True, tmp_path=tmp_path)

    providers = make_providers(cfg, chaos=None)
    assert isinstance(providers.visuals, FallbackVisualGenerator)
    assert isinstance(providers.visuals.primary, NvidiaVisualGenerator)
    assert providers.visuals.secondary is None


def test_nvidia_image_failure_falls_back_to_openrouter_when_configured(tmp_path):
    """The actual point of this change: a real NVIDIA failure must route to a
    genuinely different, working provider instead of the confirmed-dead
    flux.1-schnell fallback that used to sit here."""
    from polyglo.visuals import VisualError

    cfg = make_config(nvidia_key="nvapi-real", gemini_key="", image_broken=False,
                      audio_broken=True, openrouter_key="sk-or-real", tmp_path=tmp_path)
    providers = make_providers(cfg, chaos=None)

    class _AlwaysFailsGenerator:
        def generate(self, prompt, model, *, seed=None, reference_image=None):
            raise VisualError("simulated NVIDIA outage")

    providers.visuals.primary = _AlwaysFailsGenerator()
    calls = []
    real_secondary_generate = providers.visuals.secondary.generate
    def _recording_generate(prompt, model, **kw):
        calls.append(model)
        raise VisualError("simulated OpenRouter failure too, just checking it was tried")
    providers.visuals.secondary.generate = _recording_generate

    with pytest.raises(VisualError, match="simulated NVIDIA outage"):
        providers.visuals.generate("a prompt", "black-forest-labs/flux.1-dev")
    assert calls == ["bytedance-seed/seedream-4.5"]  # the fallback really was tried


def test_openrouter_narrator_and_visuals_share_one_openrouter_budget_instance(tmp_path):
    """OpenRouter is genuinely pay-per-use — narration and image generation must
    draw from the SAME daily cap, not two independent trackers that would
    silently double the effective budget (same reasoning as the Gemini
    narrator+transcriber sharing test above)."""
    cfg = make_config(nvidia_key="", gemini_key="", image_broken=True, audio_broken=True,
                      openrouter_key="sk-or-real", prefer_openrouter_images=True,
                      tmp_path=tmp_path)
    providers = make_providers(cfg, chaos=None)
    assert providers.narrator._budget is providers.visuals._budget
    assert providers.narrator._budget is not None
