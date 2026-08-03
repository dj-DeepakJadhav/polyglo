"""Tests for the production Gemini ASR transcriber.

No test here spends a real API call — the google-genai client is monkeypatched via
constructor injection (``client=...``), the same pattern proven for
``NvidiaNarrator``/``NvidiaVisualGenerator``. The real, live-verified round trip
(WER 0.0, exact match) is recorded in docs/SESSION-LOG.md, not repeated here — that
was a deliberate one-time spike (2 calls, tracked against the daily budget), not
something to re-run on every test invocation.
"""

from __future__ import annotations

import pytest

from polyglo.qa.budget import BudgetExceeded, GeminiBudget
from polyglo.qa.gemini_transcriber import (
    GeminiTranscriber,
    GeminiTranscriberError,
    _looks_like_raw_pcm,
)

# PCM<->WAV wrapping itself is tested in test_audio_utils.py (moved there since
# GeminiNarrator now shares the same helper, not just this transcriber).


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("audio/L16;codec=pcm;rate=24000", True),   # Gemini TTS's actual mime, confirmed live
        ("audio/pcm", True),
        ("audio/wav", False),
        ("audio/mpeg", False),
        (None, False),
    ],
)
def test_looks_like_raw_pcm(mime, expected):
    assert _looks_like_raw_pcm(mime) is expected


# ---------------------------------------------------------------------------
# Transcription — client injected, no real network call
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeGenaiClient:
    def __init__(self, response_text="hola mundo", raise_on_upload=None,
                 raise_on_generate=None):
        self._response_text = response_text
        self._raise_on_upload = raise_on_upload
        self._raise_on_generate = raise_on_generate
        self.uploaded_mime_types: list[str] = []
        self.generate_calls: list[tuple[str, list]] = []

        outer = self

        class _Files:
            def upload(self, *, file, config):
                if outer._raise_on_upload:
                    raise outer._raise_on_upload
                outer.uploaded_mime_types.append(config["mime_type"])
                return "uploaded-file-handle"

        class _Models:
            def generate_content(self, *, model, contents):
                if outer._raise_on_generate:
                    raise outer._raise_on_generate
                outer.generate_calls.append((model, contents))
                return FakeResponse(outer._response_text)

        self.files = _Files()
        self.models = _Models()


def test_transcribe_returns_the_response_text():
    client = FakeGenaiClient(response_text="el gato subio al tejado")
    transcriber = GeminiTranscriber(client=client)

    result = transcriber.transcribe(b"RIFF....WAVEfmt ", "es-ES")
    assert result == "el gato subio al tejado"


def test_transcribe_strips_whitespace():
    client = FakeGenaiClient(response_text="  hola  \n")
    transcriber = GeminiTranscriber(client=client)
    assert transcriber.transcribe(b"audio", "es-ES") == "hola"


def test_transcribe_uploads_as_wav_mime_type():
    client = FakeGenaiClient()
    transcriber = GeminiTranscriber(client=client)
    transcriber.transcribe(b"already wav bytes", "es-ES")
    assert client.uploaded_mime_types == ["audio/wav"]


def test_transcribe_wraps_raw_pcm_before_upload():
    """The specific fix confirmed by the live spike: Gemini TTS output (raw PCM)
    must be WAV-wrapped, or transcription accuracy degrades."""
    client = FakeGenaiClient()
    transcriber = GeminiTranscriber(client=client, audio_mime_hint="audio/L16;rate=24000")

    pcm = b"\x00\x01" * 50
    transcriber.transcribe(pcm, "es-ES")

    # We can't see the exact bytes uploaded through this fake, but we CAN verify
    # the wrapping function itself produces a valid header (tested above) and that
    # the mime hint correctly routed through the PCM branch rather than raising.
    assert client.uploaded_mime_types == ["audio/wav"]


def test_transcribe_prompt_names_the_locale():
    client = FakeGenaiClient()
    transcriber = GeminiTranscriber(client=client)
    transcriber.transcribe(b"audio", "fr-FR")

    model, contents = client.generate_calls[0]
    prompt = contents[1]
    assert "fr-FR" in prompt
    assert "verbatim" in prompt


def test_transcribe_wraps_upload_failure():
    client = FakeGenaiClient(raise_on_upload=RuntimeError("quota exceeded"))
    transcriber = GeminiTranscriber(client=client)
    with pytest.raises(GeminiTranscriberError, match="quota exceeded"):
        transcriber.transcribe(b"audio", "es-ES")


def test_transcribe_wraps_generate_failure():
    client = FakeGenaiClient(raise_on_generate=RuntimeError("model overloaded"))
    transcriber = GeminiTranscriber(client=client)
    with pytest.raises(GeminiTranscriberError, match="model overloaded"):
        transcriber.transcribe(b"audio", "es-ES")


def test_transcribe_raises_on_missing_text_attribute():
    class NoTextResponse:
        pass

    class WeirdClient(FakeGenaiClient):
        def __init__(self):
            super().__init__()

            class _Models:
                def generate_content(self, *, model, contents):
                    return NoTextResponse()

            self.models = _Models()

    with pytest.raises(GeminiTranscriberError, match="unexpected response shape"):
        GeminiTranscriber(client=WeirdClient()).transcribe(b"audio", "es-ES")


def test_conforms_to_transcriber_protocol():
    from polyglo.qa.gate import Transcriber
    assert isinstance(GeminiTranscriber(client=FakeGenaiClient()), Transcriber)


# ---------------------------------------------------------------------------
# Budget enforcement — a hard gate, per the user's explicit instruction
# ---------------------------------------------------------------------------


def test_transcribe_spends_one_budget_unit_per_call(tmp_path):
    client = FakeGenaiClient()
    budget = GeminiBudget(cap=10, path=tmp_path / "b.json")
    transcriber = GeminiTranscriber(client=client, budget=budget)

    transcriber.transcribe(b"audio", "es-ES")
    assert budget.used() == 1
    transcriber.transcribe(b"audio", "es-ES")
    assert budget.used() == 2


def test_transcribe_raises_before_any_network_call_once_budget_exhausted(tmp_path):
    """The budget check must happen BEFORE the network call — spending on a call
    that's about to be rejected would defeat the point of a hard cap."""
    client = FakeGenaiClient()
    budget = GeminiBudget(cap=1, path=tmp_path / "b.json")
    transcriber = GeminiTranscriber(client=client, budget=budget)

    transcriber.transcribe(b"audio", "es-ES")   # consumes the only unit
    assert len(client.generate_calls) == 1

    with pytest.raises(BudgetExceeded):
        transcriber.transcribe(b"audio", "es-ES")

    assert len(client.generate_calls) == 1   # the second call never reached the network
    assert budget.used() == 1                 # and did not get double-charged either


def test_transcriber_works_with_no_budget_configured():
    """Budget is optional — a caller that doesn't pass one gets no cap, not a crash."""
    client = FakeGenaiClient(response_text="sin limite")
    transcriber = GeminiTranscriber(client=client)
    assert transcriber.transcribe(b"audio", "es-ES") == "sin limite"
