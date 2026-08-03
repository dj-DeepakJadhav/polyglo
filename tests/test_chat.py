"""Tests for the chat abstraction and JSON repair-retry."""

from __future__ import annotations

import json

import pytest

from polyglo.authoring import GRADE_PROMPT, SPLIT_PROMPT
from polyglo.chat import ChatError, MockChatCompleter, OfflineChatCompleter, complete_json
from polyglo.localize import TRANSLATE_PROMPT


def test_valid_json_parses_first_try():
    completer = MockChatCompleter(['{"a": 1, "b": "two"}'])
    result = complete_json(completer, "prompt", model="m")
    assert result == {"a": 1, "b": "two"}
    assert completer._calls == 1


def test_json_wrapped_in_code_fence_is_extracted():
    completer = MockChatCompleter(['Here you go:\n```json\n{"a": 1}\n```'])
    assert complete_json(completer, "p", model="m") == {"a": 1}


def test_json_wrapped_in_prose_is_extracted():
    completer = MockChatCompleter(['Sure! Here is the JSON: {"a": 1} — hope that helps!'])
    assert complete_json(completer, "p", model="m") == {"a": 1}


def test_malformed_then_fixed_uses_repair_retry():
    completer = MockChatCompleter(['{"a": 1,}', '{"a": 1}'])   # trailing comma, then valid
    result = complete_json(completer, "p", model="m", max_attempts=3)
    assert result == {"a": 1}
    assert completer._calls == 2


def test_repair_prompt_names_the_actual_parse_error():
    completer = MockChatCompleter(['not json at all', '{"ok": true}'])
    complete_json(completer, "original prompt", model="m")
    assert "original prompt" in completer.prompts[1]
    assert "could not be parsed" in completer.prompts[1]


def test_exhausting_attempts_raises_chat_error():
    completer = MockChatCompleter(['nope', 'still nope', 'nope again'])
    with pytest.raises(ChatError, match="after 3 attempts"):
        complete_json(completer, "p", model="m", max_attempts=3)


def test_underlying_exception_is_wrapped_as_chat_error():
    completer = MockChatCompleter(fail_with=RuntimeError("rate limited"))
    with pytest.raises(ChatError, match="rate limited"):
        complete_json(completer, "p", model="m")


def test_max_attempts_must_be_positive():
    with pytest.raises(ValueError):
        complete_json(MockChatCompleter(), "p", model="m", max_attempts=0)


def test_mock_records_every_prompt_sent():
    completer = MockChatCompleter(['bad', '{"x": 1}'])
    complete_json(completer, "p", model="m")
    assert len(completer.prompts) == 2


# ---------------------------------------------------------------------------
# OfflineChatCompleter — the actual zero-credential production path. Never had
# direct unit tests before (only exercised indirectly through the full offline
# pipeline in test_orchestrator_offline.py) — added here to cover each of the
# three real prompt shapes it must recognise on its own.
# ---------------------------------------------------------------------------


def test_offline_completer_recognises_split_prompt_and_returns_valid_json():
    prompt = SPLIT_PROMPT.format(n=3, cefr="A2", story="A fox runs.")
    raw = OfflineChatCompleter().complete(prompt, model="m")
    data = json.loads(raw)

    assert "style_guide" in data
    assert len(data["scenes"]) == 3
    for scene in data["scenes"]:
        assert scene["text"].strip()
        assert scene["visual_prompt"].strip()


def test_offline_completer_split_prompt_honours_the_requested_scene_count():
    for n in (1, 2, 5):
        prompt = SPLIT_PROMPT.format(n=n, cefr="B1", story="text")
        data = json.loads(OfflineChatCompleter().complete(prompt, model="m"))
        assert len(data["scenes"]) == n


def test_offline_completer_recognises_grade_prompt_as_identity_passthrough():
    """No honest 'corrected' text can be fabricated without a real chat provider —
    the offline placeholder must return the input unchanged, not invent a fix."""
    prompt = GRADE_PROMPT.format(cefr="A1", story="a kat sits down")
    raw = OfflineChatCompleter().complete(prompt, model="m")
    data = json.loads(raw)
    assert data == {"corrected_text": "a kat sits down"}


def test_offline_completer_recognises_translate_prompt():
    prompt = TRANSLATE_PROMPT.format(
        locale_name="Spanish", locale_code="es-ES", cefr="B1", text="The cat sits.",
    )
    result = OfflineChatCompleter().complete(prompt, model="m")
    assert "Spanish" in result
    assert "cat sits" in result.lower() or "the cat sits" in result.lower()


def test_offline_completer_prompt_shapes_do_not_cross_match():
    """The three regexes must stay mutually exclusive — a grading prompt must never
    be mistaken for a split prompt or vice versa."""
    grade_prompt = GRADE_PROMPT.format(cefr="A1", story="text")
    split_prompt = SPLIT_PROMPT.format(n=2, cefr="A1", story="text")

    grade_result = json.loads(OfflineChatCompleter().complete(grade_prompt, model="m"))
    split_result = json.loads(OfflineChatCompleter().complete(split_prompt, model="m"))

    assert "corrected_text" in grade_result and "scenes" not in grade_result
    assert "scenes" in split_result and "corrected_text" not in split_result
