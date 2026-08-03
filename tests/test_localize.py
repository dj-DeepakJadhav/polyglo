"""Tests for translation and the inline text gate.

The scenario that matters most: a bad translation gets caught, retried with the
specific defect named, and the retry's outcome is what the caller sees — never
silently upgraded to "ok" and never silently discarded on failure.
"""

from __future__ import annotations

import pytest

from polyglo.chat import MockChatCompleter
from polyglo.localize import (
    LocalizationError,
    LocalizationResult,
    localize_all_locales,
    localize_scene,
    to_localized_scene,
    translate_scene,
)
from polyglo.models import Scene


def scene(text: str = "The cat sat on the roof") -> Scene:
    return Scene(story_id="s1", ordinal=0, source_text=text, visual_prompt="p")


# ---------------------------------------------------------------------------
# translate_scene
# ---------------------------------------------------------------------------


def test_translate_scene_returns_stripped_text():
    completer = MockChatCompleter(["  el gato se sentó en el tejado  "])
    text = translate_scene(scene(), "es-ES", "B1", completer, model="m")
    assert text == "el gato se sentó en el tejado"


def test_translate_scene_preserves_cefr_in_prompt():
    completer = MockChatCompleter(["el gato"])
    translate_scene(scene(), "es-ES", "B1", completer, model="m")
    assert "B1" in completer.prompts[0]
    assert "Spanish (Spain)" in completer.prompts[0]


def test_translate_scene_wraps_failure():
    completer = MockChatCompleter(fail_with=RuntimeError("down"))
    with pytest.raises(LocalizationError, match="translation failed"):
        translate_scene(scene(), "es-ES", "B1", completer, model="m")


# ---------------------------------------------------------------------------
# localize_scene — the gate loop
# ---------------------------------------------------------------------------


def test_localize_scene_accepts_a_good_translation_first_try():
    completer = MockChatCompleter(["el gato se sentó en el tejado"])
    result = localize_scene(scene(), "es-ES", "B1", completer, model="m")

    assert result.accepted is True
    assert result.attempts == 1
    assert result.gate.ok is True


def test_localize_scene_retries_an_untranslated_echo():
    """The most common LLM translation failure: the model just returns the source."""
    src = "The cat sat on the roof"
    completer = MockChatCompleter([src, "el gato se sentó en el tejado"])
    result = localize_scene(scene(src), "es-ES", "B1", completer, model="m")

    assert result.accepted is True
    assert result.attempts == 2
    assert result.text == "el gato se sentó en el tejado"


def test_retry_prompt_names_the_specific_rejection_reason():
    src = "The cat sat on the roof"
    completer = MockChatCompleter([src, "el gato"])
    localize_scene(scene(src), "es-ES", "B1", completer, model="m")
    assert "identical to the source" in completer.prompts[1]


def test_localize_scene_gives_up_after_max_attempts_but_returns_the_last_attempt():
    """A caller or human reviewer may still want the closest attempt — never
    silently discard it."""
    src = "The cat sat on the roof"
    completer = MockChatCompleter([src, src, src])   # echoes every time
    result = localize_scene(scene(src), "es-ES", "B1", completer, model="m", max_attempts=3)

    assert result.accepted is False
    assert result.attempts == 3
    assert result.gate.ok is False
    assert result.text == src


def test_localize_scene_rejects_wrong_script_without_retry_budget_issues():
    completer = MockChatCompleter(["inu ga hashitte iru"])   # romanised, not Devanagari
    result = localize_scene(scene(), "hi-IN", "A1", completer, model="m", max_attempts=1)
    assert result.accepted is False
    assert "devanagari" in result.gate.reason


def test_max_attempts_must_be_positive():
    with pytest.raises(ValueError):
        localize_scene(scene(), "es-ES", "B1", MockChatCompleter(), model="m", max_attempts=0)


def test_localize_scene_wraps_retry_call_failure():
    """First call echoes (triggers a retry), second call raises inside complete()."""
    src = "The cat sat on the roof"

    class FlakyCompleter:
        def __init__(self):
            self.calls = 0
        def complete(self, prompt, *, model):
            self.calls += 1
            if self.calls == 1:
                return src
            raise RuntimeError("boom")

    with pytest.raises(LocalizationError, match="translation retry failed"):
        localize_scene(scene(src), "es-ES", "B1", FlakyCompleter(), model="m")


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


def test_localize_all_locales_covers_every_requested_locale():
    completer = MockChatCompleter(["el gato en el tejado"])
    results = localize_all_locales(
        scene(), ["es-ES", "fr-FR", "de-DE"], "B1", completer, model="m",
    )
    assert set(results.keys()) == {"es-ES", "fr-FR", "de-DE"}


def test_localize_all_locales_isolates_per_locale_failures():
    """One locale's chat outage must not lose the results already obtained for
    the others — a batch of 20 locales shouldn't fail as a unit because one is down."""

    class PerLocaleCompleter:
        def complete(self, prompt, *, model):
            if "German" in prompt:
                raise RuntimeError("outage")
            return "una traducción aceptable con muchas palabras diferentes"

    results = localize_all_locales(
        scene(), ["es-ES", "de-DE"], "B1", PerLocaleCompleter(), model="m",
    )
    assert isinstance(results["es-ES"], LocalizationResult)
    assert results["es-ES"].accepted is True
    assert isinstance(results["de-DE"], LocalizationError)
    assert "outage" in str(results["de-DE"])


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def test_to_localized_scene_carries_locale_and_text():
    completer = MockChatCompleter(["el gato en el tejado"])
    result = localize_scene(scene(), "es-ES", "B1", completer, model="m")
    ls = to_localized_scene(scene(), "es-ES", result)

    assert ls.locale == "es-ES"
    assert ls.text == result.text
    assert ls.ordinal == 0
    assert ls.audio_sha256 is None
