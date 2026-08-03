"""Tests for the real narration adapter's plumbing.

The underlying NVIDIA audio models are confirmed dead against the live API
(docs/SESSION-LOG.md, task #22), so these tests verify run_step -> Asset -> bytes
wiring via a monkeypatched pipeline call, not the provider itself. Every other module
in the codebase drives QAGate with qa.gate.MockNarrator, which is the actual
zero-credential path everything else depends on.
"""

from __future__ import annotations

import pytest

from polyglo.narrate import (
    GEMINI_VOICE_NAMES,
    OPENROUTER_VOICE_NAMES,
    GeminiNarrator,
    NarrationError,
    NvidiaNarrator,
    OpenRouterNarrator,
)
from polyglo.pipeline import StepOutcome
from polyglo.qa.budget import BudgetExceeded, GeminiBudget


def test_narrator_reads_bytes_from_a_successful_outcome(monkeypatch, tmp_path):
    import polyglo.narrate as narrate_mod
    from genblaze_core.models import Asset

    written = b"RIFF fake wav bytes"
    written_path = tmp_path / "out.wav"
    written_path.write_bytes(written)

    def fake_run_step(provider, *, model, prompt, modality, timeout=None,
                      name=None, **kw):
        asset = Asset(asset_id="a1", url=written_path.as_uri(), media_type="audio/wav",
                      sha256="e" * 64, size_bytes=len(written))
        return StepOutcome(ok=True, run_id="r1", model_requested=model,
                          model_used=model, assets=[asset], latency_ms=17)

    monkeypatch.setattr(narrate_mod, "run_step", fake_run_step)

    narrator = NvidiaNarrator()
    result = narrator.narrate("hola mundo", "es-ES", "some-voice-model")

    assert result.audio == written
    assert result.sha256 == "e" * 64
    assert result.model == "some-voice-model"
    assert result.latency_ms == 17


def test_narrator_raises_narration_error_on_failed_outcome(monkeypatch):
    import polyglo.narrate as narrate_mod

    def fake_run_step(*a, **kw):
        return StepOutcome(ok=False, run_id="", model_requested="m", model_used="m",
                          error="upstream probe returned DEAD")

    monkeypatch.setattr(narrate_mod, "run_step", fake_run_step)
    with pytest.raises(NarrationError, match="DEAD"):
        NvidiaNarrator().narrate("hola", "es-ES", "m")


def test_narrator_raises_when_outcome_ok_but_no_asset(monkeypatch):
    import polyglo.narrate as narrate_mod

    def fake_run_step(*a, **kw):
        return StepOutcome(ok=True, run_id="r1", model_requested="m", model_used="m",
                          assets=[])

    monkeypatch.setattr(narrate_mod, "run_step", fake_run_step)
    with pytest.raises(NarrationError, match="no asset"):
        NvidiaNarrator().narrate("hola", "es-ES", "m")


def test_narrator_conforms_to_the_narrator_protocol():
    from polyglo.qa.gate import Narrator
    assert isinstance(NvidiaNarrator(), Narrator)


# ---------------------------------------------------------------------------
# GeminiNarrator — client injected, no real network call. Real spike (WER 0.0,
# exact match) recorded in docs/SESSION-LOG.md 2026-07-31/2026-08-01, not repeated
# on every test run — this deliberately mirrors test_gemini_transcriber.py's
# fake-client pattern.
# ---------------------------------------------------------------------------


class _FakeInlineData:
    def __init__(self, data: bytes, mime_type: str = "audio/L16;codec=pcm;rate=24000"):
        self.data = data
        self.mime_type = mime_type


class _FakePart:
    def __init__(self, data: bytes):
        self.inline_data = _FakeInlineData(data)


class _FakeContent:
    def __init__(self, data: bytes):
        self.parts = [_FakePart(data)]


class _FakeCandidate:
    def __init__(self, data: bytes):
        self.content = _FakeContent(data)


class _FakeTTSResponse:
    def __init__(self, data: bytes | None):
        self.candidates = [_FakeCandidate(data)] if data is not None else []


class FakeGeminiTTSClient:
    def __init__(self, pcm: bytes = b"\x01\x02" * 100, raise_on_generate=None):
        self._pcm = pcm
        self._raise_on_generate = raise_on_generate
        self.calls: list[dict] = []

        outer = self

        class _Models:
            def generate_content(self, *, model, contents, config):
                if outer._raise_on_generate:
                    raise outer._raise_on_generate
                voice_name = (
                    config.speech_config.voice_config.prebuilt_voice_config.voice_name
                )
                outer.calls.append({"model": model, "contents": contents, "voice": voice_name})
                return _FakeTTSResponse(outer._pcm)

        self.models = _Models()


def test_gemini_narrator_produces_a_real_manifest():
    client = FakeGeminiTTSClient()
    narrator = GeminiNarrator(client=client)
    result = narrator.narrate("hola mundo", "es-ES", "voice-a")

    assert result.sha256
    assert len(result.audio) > 0
    assert result.audio[:4] == b"RIFF"  # PCM must be WAV-wrapped


def test_gemini_narrator_maps_voice_plan_names_to_real_gemini_voices():
    client = FakeGeminiTTSClient()
    narrator = GeminiNarrator(client=client)

    for label, expected_voice in GEMINI_VOICE_NAMES.items():
        narrator.narrate("hola", "es-ES", label)
    assert [c["voice"] for c in client.calls] == list(GEMINI_VOICE_NAMES.values())


def test_gemini_narrator_defaults_to_a_voice_for_an_unrecognised_model_name():
    client = FakeGeminiTTSClient()
    narrator = GeminiNarrator(client=client)
    narrator.narrate("hola", "es-ES", "some-unrelated-model-string")
    assert client.calls[0]["voice"] == "Kore"


def test_gemini_narrator_raises_on_generate_failure():
    client = FakeGeminiTTSClient(raise_on_generate=RuntimeError("quota exceeded"))
    narrator = GeminiNarrator(client=client)
    with pytest.raises(NarrationError, match="quota exceeded"):
        narrator.narrate("hola", "es-ES", "voice-a")


def test_gemini_narrator_raises_on_empty_response():
    client = FakeGeminiTTSClient(pcm=None)
    narrator = GeminiNarrator(client=client)
    with pytest.raises(NarrationError, match="unexpected"):
        narrator.narrate("hola", "es-ES", "voice-a")


def test_gemini_narrator_is_deterministic_for_identical_fake_audio():
    """Same underlying PCM bytes -> same WAV bytes -> same hash, mirroring the
    real dedup guarantee (identical audio should never re-upload)."""
    client = FakeGeminiTTSClient(pcm=b"\x05\x06" * 50)
    narrator = GeminiNarrator(client=client)
    a = narrator.narrate("hola", "es-ES", "voice-a")
    b = narrator.narrate("hola", "es-ES", "voice-a")
    assert a.sha256 == b.sha256


def test_gemini_narrator_spends_one_budget_unit_per_call(tmp_path):
    budget = GeminiBudget(5, tmp_path / "budget.json")
    client = FakeGeminiTTSClient()
    narrator = GeminiNarrator(client=client, budget=budget)

    narrator.narrate("hola", "es-ES", "voice-a")
    assert budget.remaining() == 4


def test_gemini_narrator_raises_before_any_network_call_once_budget_exhausted(tmp_path):
    budget = GeminiBudget(0, tmp_path / "budget.json")
    client = FakeGeminiTTSClient()
    narrator = GeminiNarrator(client=client, budget=budget)

    with pytest.raises(BudgetExceeded):
        narrator.narrate("hola", "es-ES", "voice-a")
    assert client.calls == []


def test_gemini_narrator_conforms_to_the_narrator_protocol():
    from polyglo.qa.gate import Narrator
    assert isinstance(GeminiNarrator(client=FakeGeminiTTSClient()), Narrator)


# ---------------------------------------------------------------------------
# OpenRouterNarrator — session injected, no real network call. Real spike (real
# Spanish and Hindi audio, both real 200s) recorded in this session's exploration,
# not repeated on every test run — mirrors GeminiNarrator's fake-client pattern.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"ID3fake-mp3-bytes",
                 text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text or content.decode("latin-1")


class FakeOpenRouterSession:
    def __init__(self, response: _FakeResponse | None = None, raise_on_post=None):
        self._response = response or _FakeResponse()
        self._raise_on_post = raise_on_post
        self.calls: list[dict] = []

    def post(self, url, *, headers, json, timeout):
        if self._raise_on_post:
            raise self._raise_on_post
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self._response


def test_openrouter_narrator_produces_a_real_manifest():
    session = FakeOpenRouterSession()
    narrator = OpenRouterNarrator(api_key="k", session=session)
    result = narrator.narrate("hola mundo", "es-ES", "voice-a")

    assert result.sha256
    assert len(result.audio) > 0
    assert session.calls[0]["url"] == "https://openrouter.ai/api/v1/audio/speech"
    assert session.calls[0]["json"]["model"] == "mistralai/voxtral-mini-tts-2603"
    assert session.calls[0]["json"]["response_format"] == "mp3"


def test_openrouter_narrator_maps_voice_plan_names_to_real_voxtral_voices():
    session = FakeOpenRouterSession()
    narrator = OpenRouterNarrator(api_key="k", session=session)

    for label in OPENROUTER_VOICE_NAMES:
        narrator.narrate("hola", "es-ES", label)
    assert [c["json"]["voice"] for c in session.calls] == list(OPENROUTER_VOICE_NAMES.values())


def test_openrouter_narrator_defaults_to_a_voice_for_an_unrecognised_model_name():
    session = FakeOpenRouterSession()
    narrator = OpenRouterNarrator(api_key="k", session=session)
    narrator.narrate("hola", "es-ES", "some-unrelated-model-string")
    assert session.calls[0]["json"]["voice"] == "en_paul_neutral"


def test_openrouter_narrator_raises_on_non_200_response():
    session = FakeOpenRouterSession(_FakeResponse(status_code=429, text="rate limited"))
    narrator = OpenRouterNarrator(api_key="k", session=session)
    with pytest.raises(NarrationError, match="429"):
        narrator.narrate("hola", "es-ES", "voice-a")


def test_openrouter_narrator_raises_on_empty_audio():
    session = FakeOpenRouterSession(_FakeResponse(content=b""))
    narrator = OpenRouterNarrator(api_key="k", session=session)
    with pytest.raises(NarrationError, match="no audio"):
        narrator.narrate("hola", "es-ES", "voice-a")


def test_openrouter_narrator_raises_on_transport_failure():
    session = FakeOpenRouterSession(raise_on_post=ConnectionError("dns failure"))
    narrator = OpenRouterNarrator(api_key="k", session=session)
    with pytest.raises(NarrationError, match="dns failure"):
        narrator.narrate("hola", "es-ES", "voice-a")


def test_openrouter_narrator_is_deterministic_for_identical_fake_audio():
    session = FakeOpenRouterSession(_FakeResponse(content=b"ID3identical-bytes"))
    narrator = OpenRouterNarrator(api_key="k", session=session)
    a = narrator.narrate("hola", "es-ES", "voice-a")
    b = narrator.narrate("hola", "es-ES", "voice-a")
    assert a.sha256 == b.sha256


def test_openrouter_narrator_conforms_to_the_narrator_protocol():
    from polyglo.qa.gate import Narrator
    assert isinstance(OpenRouterNarrator(api_key="k", session=FakeOpenRouterSession()), Narrator)


def test_openrouter_narrator_spends_one_budget_unit_per_call(tmp_path):
    from polyglo.qa.budget import DailyCallBudget
    budget = DailyCallBudget(5, tmp_path / "budget.json", label="OpenRouter")
    session = FakeOpenRouterSession()
    narrator = OpenRouterNarrator(api_key="k", session=session, budget=budget)

    narrator.narrate("hola", "es-ES", "voice-a")
    assert budget.remaining() == 4


def test_openrouter_narrator_raises_before_any_network_call_once_budget_exhausted(tmp_path):
    from polyglo.qa.budget import BudgetExceeded, DailyCallBudget
    budget = DailyCallBudget(0, tmp_path / "budget.json", label="OpenRouter")
    session = FakeOpenRouterSession()
    narrator = OpenRouterNarrator(api_key="k", session=session, budget=budget)

    with pytest.raises(BudgetExceeded):
        narrator.narrate("hola", "es-ES", "voice-a")
    assert session.calls == []
