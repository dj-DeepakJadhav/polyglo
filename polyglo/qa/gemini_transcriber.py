"""Real ASR verifier via Gemini's native audio understanding.

Confirmed live (docs/SESSION-LOG.md, 2026-07-31): a full Gemini TTS -> Gemini ASR
round trip on real generated audio scored **WER 0.0, exact match**. This module is the
production implementation of the `Transcriber` protocol proven by that spike.

Deliberately a different model family from the narrator (NVIDIA Riva, once task #22
resolves) — see `qa/gate.py`'s own docstring on why the verifier must not be the
generator. Gemini verifying NVIDIA-narrated audio is the intended real pairing; this
class doesn't care who generated the audio, only that it can transcribe it.

Respects the user's explicit daily call-budget instruction (`qa/budget.py`) as a hard
gate, not advisory — `transcribe()` raises `BudgetExceeded` rather than silently
spending past the cap.
"""

from __future__ import annotations

import io
from typing import Any

from polyglo.audio_utils import pcm_to_wav
from polyglo.qa.budget import GeminiBudget

__all__ = ["GeminiTranscriberError", "GeminiTranscriber"]


class GeminiTranscriberError(RuntimeError):
    pass


def _looks_like_raw_pcm(mime_type: str | None) -> bool:
    if not mime_type:
        return False
    lowered = mime_type.lower()
    return "pcm" in lowered or "l16" in lowered


class GeminiTranscriber:
    """Transcribes audio via Gemini's audio understanding. Implements `Transcriber`.

    ``audio_mime_hint`` tells the transcriber whether the bytes it receives are raw
    PCM (needs WAV-wrapping, as Gemini's own TTS output does) or an already-containered
    format (WAV/MP3, as a real narration provider would produce) — set per-instance
    rather than sniffed, since sniffing raw PCM reliably from bytes alone is not
    possible (it has no magic number).
    """

    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "gemini-2.5-flash",
        budget: GeminiBudget | None = None,
        audio_mime_hint: str = "audio/wav",
        client: Any = None,
    ):
        self._api_key = api_key
        self._model = model
        self._budget = budget
        self._audio_mime_hint = audio_mime_hint
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from google import genai

        self._client = genai.Client(api_key=self._api_key)
        return self._client

    def transcribe(self, audio: bytes, locale: str) -> str:
        if self._budget is not None:
            self._budget.spend(1)   # raises BudgetExceeded before any network call

        client = self._get_client()

        payload = (
            pcm_to_wav(audio) if _looks_like_raw_pcm(self._audio_mime_hint) else audio
        )

        try:
            uploaded = client.files.upload(
                file=io.BytesIO(payload),
                config={"mime_type": "audio/wav"},
            )
            response = client.models.generate_content(
                model=self._model,
                contents=[
                    uploaded,
                    f"Transcribe this {locale} audio verbatim. Output only the "
                    f"transcript, no commentary, no translation.",
                ],
            )
        except Exception as exc:
            raise GeminiTranscriberError(f"{type(exc).__name__}: {exc}") from exc

        text = getattr(response, "text", None)
        if text is None:
            raise GeminiTranscriberError(f"unexpected response shape: {response!r}")
        return text.strip()
