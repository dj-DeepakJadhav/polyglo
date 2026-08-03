"""Tests for the shared PCM->WAV wrapping used by both GeminiTranscriber (ASR) and
GeminiNarrator (TTS) — this is the fix that made the original TTS->ASR spike's round
trip an exact-match (docs/SESSION-LOG.md, 2026-07-31)."""

from __future__ import annotations

import struct

from polyglo.audio_utils import pcm_to_wav


def test_pcm_to_wav_produces_a_valid_riff_header():
    pcm = b"\x00\x01" * 100
    wav = pcm_to_wav(pcm, sample_rate=24000, channels=1, sample_width=2)

    assert wav[0:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert wav[12:16] == b"fmt "
    assert wav[36:40] == b"data"

    riff_size = struct.unpack("<I", wav[4:8])[0]
    assert riff_size == 36 + len(pcm)

    data_size = struct.unpack("<I", wav[40:44])[0]
    assert data_size == len(pcm)
    assert wav[44:] == pcm


def test_pcm_to_wav_header_encodes_sample_rate_and_channels():
    wav = pcm_to_wav(b"\x00" * 10, sample_rate=24000, channels=1, sample_width=2)
    channels = struct.unpack("<H", wav[22:24])[0]
    sample_rate = struct.unpack("<I", wav[24:28])[0]
    bits_per_sample = struct.unpack("<H", wav[34:36])[0]
    assert channels == 1
    assert sample_rate == 24000
    assert bits_per_sample == 16
