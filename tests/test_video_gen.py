"""Unit tests for AI Video Generation (Image-to-Video) providers."""

from __future__ import annotations

from polyglo.config import get_config
from polyglo.orchestrator import make_providers
from polyglo.providers.video_gen import (
    FalVideoGenerator,
    OpenRouterVideoGenerator,
    ReplicateVideoGenerator,
    SimulatedVideoGenerator,
)


def test_simulated_video_generator(tmp_path) -> None:
    # 1x1 red PNG magic bytes for simple testing
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    vigen = SimulatedVideoGenerator(duration_sec=1.0)
    video_bytes = vigen.animate_scene("A sunny garden", png_bytes)

    assert isinstance(video_bytes, bytes)
    assert len(video_bytes) > 0
    # MP4 files start with ftyp header
    assert b"ftyp" in video_bytes[:32]


def test_fal_and_replicate_generator_fallbacks() -> None:
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    # Without keys, all fall back gracefully to simulated animation
    fal_gen = FalVideoGenerator(api_key="")
    v1 = fal_gen.animate_scene("A test prompt", png_bytes)
    assert b"ftyp" in v1[:32]

    rep_gen = ReplicateVideoGenerator(api_key="")
    v2 = rep_gen.animate_scene("A test prompt", png_bytes)
    assert b"ftyp" in v2[:32]

    or_gen = OpenRouterVideoGenerator(api_key="")
    v3 = or_gen.animate_scene("A test prompt", png_bytes)
    assert b"ftyp" in v3[:32]


def test_make_providers_includes_video_gen() -> None:
    providers = make_providers(get_config())
    assert providers.video_gen is not None
