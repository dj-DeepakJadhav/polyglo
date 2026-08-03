"""Tests for story splitting."""

from __future__ import annotations

import json

import pytest

from polyglo.authoring import AuthoringError, grade_source_text, split_story
from polyglo.chat import MockChatCompleter
from polyglo.models import Story


def make_story(cefr: str = "B1") -> Story:
    return Story.create("The Lost Umbrella", cefr=cefr, source_locale="en-US")


STYLE_GUIDE = "a small orange tabby cat, flat children's-book watercolor style"


def scenes_json(n: int, *, style_guide: str = STYLE_GUIDE) -> str:
    return json.dumps({
        "style_guide": style_guide,
        "scenes": [
            {"text": f"Scene {i} happens.", "visual_prompt": f"illustration {i}"}
            for i in range(n)
        ]
    })


def test_split_story_produces_correctly_ordered_scenes():
    completer = MockChatCompleter([scenes_json(3)])
    scenes = split_story(make_story(), "a long story...", 3, completer, model="m")

    assert [s.ordinal for s in scenes] == [0, 1, 2]
    assert all(s.story_id == scenes[0].story_id for s in scenes)
    assert scenes[0].source_text == "Scene 0 happens."
    assert scenes[0].visual_prompt == f"{STYLE_GUIDE} illustration 0"


def test_split_story_prepends_style_guide_to_every_scene():
    """The actual fix: every scene's stored visual_prompt carries the SAME shared
    style_guide, since each scene is illustrated independently with no memory of
    the others — this is what stops every scene looking like a different story."""
    completer = MockChatCompleter([scenes_json(3)])
    scenes = split_story(make_story(), "a long story...", 3, completer, model="m")

    for i, scene in enumerate(scenes):
        assert scene.visual_prompt == f"{STYLE_GUIDE} illustration {i}"


def test_split_story_rejects_missing_style_guide():
    bad = json.dumps({"scenes": [{"text": "t", "visual_prompt": "p"}]})
    completer = MockChatCompleter([bad])
    with pytest.raises(AuthoringError, match="style_guide"):
        split_story(make_story(), "story", 1, completer, model="m")


def test_split_story_rejects_blank_style_guide():
    bad = json.dumps({"style_guide": "   ", "scenes": [{"text": "t", "visual_prompt": "p"}]})
    completer = MockChatCompleter([bad])
    with pytest.raises(AuthoringError, match="style_guide"):
        split_story(make_story(), "story", 1, completer, model="m")


def test_split_story_carries_no_image_hash_yet():
    completer = MockChatCompleter([scenes_json(2)])
    scenes = split_story(make_story(), "story", 2, completer, model="m")
    assert all(s.image_sha256 is None for s in scenes)


def test_split_story_recovers_from_malformed_json():
    completer = MockChatCompleter(["not json", scenes_json(2)])
    scenes = split_story(make_story(), "story", 2, completer, model="m")
    assert len(scenes) == 2


def test_split_story_rejects_missing_scenes_key():
    completer = MockChatCompleter(['{"oops": []}'])
    with pytest.raises(AuthoringError, match="'scenes' key"):
        split_story(make_story(), "story", 2, completer, model="m")


def test_split_story_rejects_empty_scenes_list():
    completer = MockChatCompleter([json.dumps({"style_guide": STYLE_GUIDE, "scenes": []})])
    with pytest.raises(AuthoringError, match="non-empty list"):
        split_story(make_story(), "story", 2, completer, model="m")


def test_split_story_rejects_scene_missing_required_field():
    bad = json.dumps({
        "style_guide": STYLE_GUIDE,
        "scenes": [{"text": "only text, no prompt"}],
    })
    completer = MockChatCompleter([bad])
    with pytest.raises(AuthoringError, match="missing"):
        split_story(make_story(), "story", 1, completer, model="m")


def test_split_story_rejects_empty_text_field():
    bad = json.dumps({
        "style_guide": STYLE_GUIDE,
        "scenes": [{"text": "   ", "visual_prompt": "x"}],
    })
    completer = MockChatCompleter([bad])
    with pytest.raises(AuthoringError, match="empty text"):
        split_story(make_story(), "story", 1, completer, model="m")


def test_split_story_rejects_scene_count_mismatch():
    completer = MockChatCompleter([scenes_json(2)])
    with pytest.raises(AuthoringError, match="requested 5.*returned 2"):
        split_story(make_story(), "story", 5, completer, model="m")


def test_split_story_raises_authoring_error_on_total_chat_failure():
    completer = MockChatCompleter(fail_with=RuntimeError("outage"))
    with pytest.raises(AuthoringError, match="scene splitting failed"):
        split_story(make_story(), "story", 2, completer, model="m")


def test_prompt_includes_cefr_level_and_scene_count():
    completer = MockChatCompleter([scenes_json(4)])
    split_story(make_story(cefr="A2"), "my story text", 4, completer, model="m")
    sent = completer.prompts[0]
    assert "A2" in sent
    assert "4 scenes" in sent
    assert "my story text" in sent


# ---------------------------------------------------------------------------
# grade_source_text — task #25: correct/level the FULL source story before
# split_story ever sees it.
# ---------------------------------------------------------------------------


def test_grade_source_text_returns_the_corrected_text():
    completer = MockChatCompleter([json.dumps({"corrected_text": "A cat sits down."})])
    result = grade_source_text("a kat sits down", "A1", completer, model="m")
    assert result == "A cat sits down."


def test_grade_source_text_strips_whitespace():
    completer = MockChatCompleter([json.dumps({"corrected_text": "  Fixed text.  \n"})])
    result = grade_source_text("raw text", "B1", completer, model="m")
    assert result == "Fixed text."


def test_grade_source_text_rejects_missing_key():
    completer = MockChatCompleter([json.dumps({"oops": "x"})])
    with pytest.raises(AuthoringError, match="corrected_text"):
        grade_source_text("raw text", "B1", completer, model="m")


def test_grade_source_text_rejects_empty_result():
    completer = MockChatCompleter([json.dumps({"corrected_text": "   "})])
    with pytest.raises(AuthoringError, match="empty"):
        grade_source_text("raw text", "B1", completer, model="m")


def test_grade_source_text_raises_authoring_error_on_total_chat_failure():
    completer = MockChatCompleter(fail_with=RuntimeError("outage"))
    with pytest.raises(AuthoringError, match="grading failed"):
        grade_source_text("raw text", "B1", completer, model="m")


def test_grade_prompt_includes_cefr_level_and_source_text():
    completer = MockChatCompleter([json.dumps({"corrected_text": "fixed"})])
    grade_source_text("my raw story", "C1", completer, model="m")
    sent = completer.prompts[0]
    assert "C1" in sent
    assert "my raw story" in sent
