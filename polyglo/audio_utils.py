"""Shared audio byte-format helpers.

Extracted from ``qa/gemini_transcriber.py`` so ``narrate.py``'s ``GeminiNarrator`` can
reuse the same PCM->WAV wrapping without a `qa` package importing into `narrate`, or
vice versa — both are leaves depending on this, not on each other.
"""

from __future__ import annotations

import struct

__all__ = ["pcm_to_wav"]


def pcm_to_wav(pcm: bytes, *, sample_rate: int = 24000, channels: int = 1,
              sample_width: int = 2) -> bytes:
    """Wrap raw PCM in a minimal WAV header.

    Gemini's TTS models return raw L16/PCM (confirmed live: mime type
    ``audio/L16;codec=pcm;rate=24000``) — every downstream consumer (playback,
    ffmpeg, a second Gemini call for transcription) needs a real container format,
    not bare samples.
    """
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16, 1, channels,
        sample_rate, byte_rate, block_align, sample_width * 8, b"data", data_size,
    )
    return header + pcm
