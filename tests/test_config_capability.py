"""Tests for the has_nvidia vs has_image_generation/has_audio_generation distinction.

This gap was real: conflating "credentials present" with "this modality actually
works" would have made the app try (and fail) real image/audio generation the moment
a valid NVIDIA key existed. Task #22's follow-up investigation found the two
modalities are NOT in the same state: raw-HTTP testing confirmed image generation
genuinely works via `flux.1-dev` (the originally-configured primary model,
`flux.1-schnell`, is what was actually dead), while audio remains confirmed dead
(all bundled + plausible current-catalog TTS slugs 404). Hence two independent flags,
not one combined one.
"""

from __future__ import annotations

import pytest

from polyglo.config import B2Config, Config, GeminiConfig, QAConfig


def make_config(
    nvidia_key: str = "",
    image_broken: str | None = None,
    audio_broken: str | None = None,
    tmp_path=None,
) -> Config:
    import os

    if image_broken is not None:
        os.environ["NVIDIA_IMAGE_BROKEN"] = image_broken
    else:
        os.environ.pop("NVIDIA_IMAGE_BROKEN", None)

    if audio_broken is not None:
        os.environ["NVIDIA_AUDIO_BROKEN"] = audio_broken
    else:
        os.environ.pop("NVIDIA_AUDIO_BROKEN", None)

    return Config(
        b2=B2Config("", "", "b", ""), qa=QAConfig(), gemini=GeminiConfig(),
        nvidia_api_key=nvidia_key, gemini_api_key="", openrouter_api_key="",
        data_dir=tmp_path, db_path=tmp_path / "p.db",
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("NVIDIA_IMAGE_BROKEN", raising=False)
    monkeypatch.delenv("NVIDIA_AUDIO_BROKEN", raising=False)
    yield
    monkeypatch.delenv("NVIDIA_IMAGE_BROKEN", raising=False)
    monkeypatch.delenv("NVIDIA_AUDIO_BROKEN", raising=False)


def test_defaults_reflect_current_confirmed_state(tmp_path):
    """The actual current state: a valid key exists, chat AND image generation work,
    audio does not. Defaults must reflect this without any env override."""
    cfg = make_config(nvidia_key="nvapi-real-key-here", tmp_path=tmp_path)
    assert cfg.has_nvidia is True
    assert cfg.nvidia_image_broken is False
    assert cfg.nvidia_audio_broken is True
    assert cfg.has_image_generation is True
    assert cfg.has_audio_generation is False
    assert cfg.has_generation is True  # true because image works, even though audio doesn't


def test_no_key_means_no_generation_regardless_of_broken_flags(tmp_path):
    cfg = make_config(nvidia_key="", image_broken="false", audio_broken="false", tmp_path=tmp_path)
    assert cfg.has_nvidia is False
    assert cfg.has_image_generation is False
    assert cfg.has_audio_generation is False
    assert cfg.has_generation is False


def test_key_present_and_audio_explicitly_marked_fixed_enables_audio_generation(tmp_path):
    """Once a working NVIDIA TTS model is found, flipping this one flag is what
    re-enables real audio generation — no code change required."""
    cfg = make_config(nvidia_key="nvapi-real-key-here", audio_broken="false", tmp_path=tmp_path)
    assert cfg.has_audio_generation is True


def test_image_can_be_marked_broken_independently_of_audio(tmp_path):
    """The two flags are genuinely independent — a future image regression must not
    require touching the audio flag, and vice versa."""
    cfg = make_config(nvidia_key="k", image_broken="true", audio_broken="true", tmp_path=tmp_path)
    assert cfg.has_image_generation is False
    assert cfg.has_audio_generation is False
    assert cfg.has_generation is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "Yes"])
def test_image_broken_flag_accepts_common_truthy_spellings(value, tmp_path):
    cfg = make_config(nvidia_key="k", image_broken=value, tmp_path=tmp_path)
    assert cfg.nvidia_image_broken is True


@pytest.mark.parametrize("value", ["false", "0", "no", "FALSE"])
def test_image_broken_flag_accepts_common_falsy_spellings(value, tmp_path):
    cfg = make_config(nvidia_key="k", image_broken=value, tmp_path=tmp_path)
    assert cfg.nvidia_image_broken is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "Yes"])
def test_audio_broken_flag_accepts_common_truthy_spellings(value, tmp_path):
    cfg = make_config(nvidia_key="k", audio_broken=value, tmp_path=tmp_path)
    assert cfg.nvidia_audio_broken is True


@pytest.mark.parametrize("value", ["false", "0", "no", "FALSE"])
def test_audio_broken_flag_accepts_common_falsy_spellings(value, tmp_path):
    cfg = make_config(nvidia_key="k", audio_broken=value, tmp_path=tmp_path)
    assert cfg.nvidia_audio_broken is False


def test_has_openrouter_reflects_key_presence(tmp_path):
    from polyglo.config import Config as _Config
    cfg_no_key = make_config(tmp_path=tmp_path)
    assert cfg_no_key.has_openrouter is False

    cfg_with_key = _Config(
        b2=cfg_no_key.b2, qa=cfg_no_key.qa, gemini=cfg_no_key.gemini,
        nvidia_api_key="", gemini_api_key="", openrouter_api_key="sk-or-real",
        data_dir=tmp_path, db_path=tmp_path / "p.db",
    )
    assert cfg_with_key.has_openrouter is True


def test_prefer_openrouter_images_defaults_false_and_is_independent_of_the_key(tmp_path, monkeypatch):
    """The flag defaults false regardless of whether a key is present — setting
    OPENROUTER_API_KEY alone must never change which image generator is active."""
    monkeypatch.delenv("OPENROUTER_PREFER_IMAGES", raising=False)
    cfg = make_config(nvidia_key="k", tmp_path=tmp_path)
    assert cfg.prefer_openrouter_images is False

    monkeypatch.setenv("OPENROUTER_PREFER_IMAGES", "true")
    cfg = make_config(nvidia_key="k", tmp_path=tmp_path)
    assert cfg.prefer_openrouter_images is True
    monkeypatch.delenv("OPENROUTER_PREFER_IMAGES", raising=False)
