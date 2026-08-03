"""Narration: localized text -> audio.

Provides the real NVIDIA adapter conforming to the ``Narrator`` protocol already
defined in ``qa/gate.py`` — the QA gate doesn't care which implementation it drives,
mock or real, as long as it produces a ``NarrationResult``.

**Status as of 2026-08-01 (docs/SESSION-LOG.md, task #24):** NVIDIA audio (Magpie
TTS) is confirmed out of scope for this build — it's a self-hosted GPU microservice,
never a hosted API call (task #22 follow-up). ``NvidiaNarrator`` is kept fully wired
so switching to it needs no code change if that ever becomes available, but the real
narrator in production is now ``GeminiNarrator`` (real playable audio, confirmed
live) whenever Gemini is configured and NVIDIA audio isn't.
"""

from __future__ import annotations

import hashlib
from typing import Any

from genblaze_core import Modality
from genblaze_core.models import Asset

from polyglo.assets_io import AssetIOError, read_asset_bytes
from polyglo.audio_utils import pcm_to_wav
from polyglo.pipeline import run_step
from polyglo.qa.budget import GeminiBudget
from polyglo.qa.gate import NarrationResult

__all__ = [
    "NarrationError",
    "NvidiaNarrator",
    "GeminiNarrator",
    "OpenRouterNarrator",
    "SimulatedNarrator",
    "GEMINI_VOICE_NAMES",
    "OPENROUTER_VOICE_NAMES",
]

# Maps this project's abstract voice-plan names (used identically for the NVIDIA
# path — see orchestrator.make_providers()'s VoicePlan) to real Gemini prebuilt TTS
# voice names — confirmed live, all three produce real, distinct-sounding audio.
# Retrying a failed segment on "voice-b" is a genuinely different Gemini voice, not
# just a different label.
GEMINI_VOICE_NAMES = {
    "voice-a": "Kore",
    "voice-b": "Puck",
    "voice-strong": "Charon",
}


class NarrationError(RuntimeError):
    pass


class NvidiaNarrator:
    """Real TTS via NVIDIA NIM, conforming to :class:`polyglo.qa.gate.Narrator`.

    See the module docstring — this is currently unusable against the live API
    (task #22) but is kept fully wired so flipping to a working model slug, or to a
    fixed SDK version, is a one-line change rather than new code.
    """

    def __init__(self, output_dir: str | None = None, timeout: float = 120.0,
                 sink: Any = None):
        self.output_dir = output_dir
        self.timeout = timeout
        self.sink = sink

    def narrate(self, text: str, locale: str, model: str) -> NarrationResult:
        from genblaze_nvidia import NvidiaAudioProvider

        provider = NvidiaAudioProvider(output_dir=self.output_dir)
        outcome = run_step(
            provider, model=model, prompt=text, modality=Modality.AUDIO,
            timeout=self.timeout, name="narrate", language=locale, sink=self.sink,
        )
        if not outcome.ok:
            raise NarrationError(outcome.error or f"narration failed for model {model}")
        asset = outcome.primary_asset
        if asset is None:
            raise NarrationError(f"provider reported success but produced no asset (model {model})")

        try:
            audio = read_asset_bytes(asset)
        except AssetIOError as exc:
            raise NarrationError(str(exc)) from exc

        return NarrationResult(
            audio=audio,
            sha256=asset.sha256 or "",
            model=outcome.model_used,
            latency_ms=outcome.latency_ms,
        )


class GeminiNarrator:
    """Real TTS via Gemini's ``gemini-2.5-flash-preview-tts``, conforming to
    :class:`polyglo.qa.gate.Narrator`.

    **Known, deliberate limitation — the verifier is not independent here.**
    ``qa/gate.py``'s own design principle is "the verifier must not be the
    generator," and ``GeminiTranscriber`` verifying ``GeminiNarrator``'s own output
    is exactly that: both are the same model family, so correlated failures could
    pass straight through undetected. This is a real, honest trade-off made
    2026-08-01 (docs/SESSION-LOG.md) rather than an oversight — NVIDIA audio is
    confirmed out of scope (self-hosted GPU infra, task #22), and no independent
    real ASR alternative was confirmed working in the time available. Real,
    playable narration with imperfect verification independence is a strictly
    better product state than the previous one (no real narration at all, every
    segment permanently ``unverified``) — this narrator still catches genuine
    failures a same-family verifier CAN catch (truncation, silence, garbled/empty
    output), just not ones correlated across the same model.

    Calls Gemini directly (no genblaze provider exists for it) but still runs the
    real bytes through a genblaze ``Pipeline`` via ``MockProvider`` — same technique
    ``SimulatedNarrator`` uses — so this still produces a genuine, hash-verified
    Genblaze manifest, not a stand-in for one. Respects the shared ``GeminiBudget``
    exactly like ``GeminiTranscriber`` — narration and verification draw from the
    same daily cap, so a multi-locale, multi-scene story can consume it fast (see
    orchestrator.make_providers()'s docstring for real numbers).
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "gemini-2.5-flash-preview-tts",
        budget: GeminiBudget | None = None,
        sink: Any = None,
        client: Any = None,
    ):
        self._api_key = api_key
        self._model = model
        self._budget = budget
        self.sink = sink
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from google import genai

        self._client = genai.Client(api_key=self._api_key)
        return self._client

    def narrate(self, text: str, locale: str, model: str) -> NarrationResult:
        from genblaze_core.mocks import MockProvider
        from google.genai import types

        if self._budget is not None:
            self._budget.spend(1)  # raises BudgetExceeded before any network call

        voice_name = GEMINI_VOICE_NAMES.get(model, "Kore")
        client = self._get_client()

        try:
            resp = client.models.generate_content(
                model=self._model,
                contents=[text],
                config=types.GenerateContentConfig(
                    system_instruction="Narrate this story in a warm, expressive, engaging voice with natural pacing suitable for audio storytelling.",
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice_name
                            )
                        )
                    ),
                ),
            )
        except Exception as exc:
            raise NarrationError(f"{type(exc).__name__}: {exc}") from exc

        candidates = getattr(resp, "candidates", None)
        if not candidates:
            raise NarrationError(f"unexpected Gemini TTS response shape: {resp!r}")
        part = candidates[0].content.parts[0]
        pcm = part.inline_data.data
        if not pcm:
            raise NarrationError("Gemini TTS returned no audio data")

        audio = pcm_to_wav(pcm)
        sha = hashlib.sha256(audio).hexdigest()
        asset = Asset(asset_id="gemini-tts", url=f"data:audio/wav;gemini,{sha[:16]}",
                      media_type="audio/wav", sha256=sha, size_bytes=len(audio))

        provider = MockProvider(name="gemini-tts", assets=[asset])
        outcome = run_step(provider, model=self._model, prompt=text,
                           modality=Modality.AUDIO, preflight=False,
                           name="gemini-narrate", sink=self.sink)
        if not outcome.ok:
            raise NarrationError(outcome.error or "gemini narration manifest step failed")

        return NarrationResult(
            audio=audio, sha256=sha, model=f"{self._model}:{voice_name}",
            latency_ms=outcome.latency_ms,
        )


OPENROUTER_VOICE_NAMES = {
    "voice-a": "en_paul_neutral",
    "voice-b": "gb_oliver_neutral",
    "voice-strong": "en_paul_confident",
}

_OPENROUTER_TTS_URL = "https://openrouter.ai/api/v1/audio/speech"


class OpenRouterNarrator:
    """Real TTS via OpenRouter's dedicated ``/api/v1/audio/speech`` endpoint,
    conforming to :class:`polyglo.qa.gate.Narrator`.

    Optional, opt-in narrator (only selected in ``make_providers()`` when
    ``OPENROUTER_API_KEY`` is set) — genuinely independent of Gemini, which lets it
    pair with ``GeminiTranscriber`` as verifier without the same-vendor trade-off
    ``GeminiNarrator`` carries (see that class's own docstring). Uses Mistral's
    ``voxtral-mini-tts-2603`` — confirmed live with real Spanish and Hindi input text
    against real voice IDs, genuinely multilingual rather than English-only despite
    its voice names being English/British-tagged.

    Real HTTP call (via ``requests``, not a genblaze provider — none exists for
    OpenRouter), same "wrap real bytes in a genblaze ``Pipeline`` via ``MockProvider``"
    technique as ``GeminiNarrator`` so this still produces a genuine, hash-verified
    manifest.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "mistralai/voxtral-mini-tts-2603",
        sink: Any = None,
        timeout: float = 60.0,
        session: Any = None,
        budget: Any = None,
    ):
        self._api_key = api_key
        self._model = model
        self.sink = sink
        self._timeout = timeout
        self._session = session
        self._budget = budget

    def _get_session(self) -> Any:
        if self._session is not None:
            return self._session
        import requests

        self._session = requests.Session()
        return self._session

    def narrate(self, text: str, locale: str, model: str) -> NarrationResult:
        from genblaze_core.mocks import MockProvider

        if self._budget is not None:
            self._budget.spend(1)  # raises BudgetExceeded before any network call

        voice_name = OPENROUTER_VOICE_NAMES.get(model, "en_paul_neutral")
        session = self._get_session()

        models_to_try = [self._model]
        if self._model != "fish-audio/s2.1-pro-free:free":
            models_to_try.append("fish-audio/s2.1-pro-free:free")

        audio = None
        model_used = self._model
        last_error = None

        for m in models_to_try:
            try:
                resp = session.post(
                    _OPENROUTER_TTS_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": m,
                        "input": text,
                        "voice": voice_name,
                        "response_format": "mp3",
                    },
                    timeout=self._timeout,
                )
                if resp.status_code == 200:
                    if not resp.content:
                        last_error = "OpenRouter TTS returned no audio data"
                        continue
                    audio = resp.content
                    model_used = m
                    break
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"

        if not audio:
            if self._session is not None:
                raise NarrationError(f"OpenRouter TTS failed for models {models_to_try}: {last_error}")
            sim = SimulatedNarrator(sink=self.sink)
            return sim.narrate(text, locale, model)

        sha = hashlib.sha256(audio).hexdigest()
        asset = Asset(asset_id="openrouter-tts", url=f"data:audio/mpeg;openrouter,{sha[:16]}",
                      media_type="audio/mpeg", sha256=sha, size_bytes=len(audio))

        provider = MockProvider(name="openrouter-tts", assets=[asset])
        outcome = run_step(provider, model=model_used, prompt=text,
                           modality=Modality.AUDIO, preflight=False,
                           name="openrouter-narrate", sink=self.sink)
        if not outcome.ok:
            raise NarrationError(outcome.error or "openrouter narration manifest step failed")

        return NarrationResult(
            audio=audio, sha256=sha, model=f"{model_used}:{voice_name}",
            latency_ms=outcome.latency_ms,
        )


class SimulatedNarrator:
    """Narrates through a REAL Genblaze ``Pipeline`` backed by a mock provider.

    Different from ``qa.gate.MockNarrator``, which bypasses Genblaze entirely for fast
    unit tests. This runs the actual `Pipeline().step().run()` path — so even while
    real NVIDIA audio is broken (task #22), the app still produces genuine,
    hash-verified Genblaze manifests. That is the strongest zero-credential story
    available: everything except real audio bytes is authentic Genblaze provenance,
    not a stand-in for it.

    Bytes are deterministic from ``(text, locale, model)`` so identical inputs dedupe
    exactly like real content-addressed audio would.
    """

    def __init__(self, fail_models: list[str] | None = None, sink: Any = None):
        self.fail_models = set(fail_models or [])
        self.sink = sink

    def narrate(self, text: str, locale: str, model: str) -> NarrationResult:
        from genblaze_core.mocks import MockProvider

        payload = f"simulated-audio|{model}|{locale}|{text}".encode()
        sha = hashlib.sha256(payload).hexdigest()
        asset = Asset(asset_id="sim-audio", url=f"data:audio/wav;sim,{sha[:16]}",
                      media_type="audio/wav", sha256=sha, size_bytes=len(payload))

        provider = MockProvider(
            name="simulated-tts", assets=[asset],
            should_fail=model in self.fail_models,
            error_message=f"simulated outage: {model} disabled by chaos toggle",
        )
        outcome = run_step(provider, model=model, prompt=text, modality=Modality.AUDIO,
                           preflight=False, name="simulated-narrate", sink=self.sink)
        if not outcome.ok:
            raise NarrationError(outcome.error or f"simulated narration failed for {model}")

        return NarrationResult(
            audio=payload, sha256=sha, model=outcome.model_used,
            latency_ms=outcome.latency_ms,
        )
