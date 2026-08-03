"""Story authoring: source text -> CEFR-graded scenes with visual prompts.

The first stage of the pipeline (docs/02 §5.1). Splits a source story into scenes,
each carrying graded source text and a visual prompt — locale-independent, because the
image generated from that prompt is shared by every locale downstream (the whole point
of the architecture; see `models.py` module docstring).

**Cross-scene visual consistency** (added after a real submission-review finding: a
5-scene story's illustrations showed a different-looking cat, a different setting, and
a different art style in nearly every scene — because each scene's ``visual_prompt``
was generated independently, with nothing anchoring the character or style across
calls). The fix is a single shared ``style_guide`` string, generated once per story
alongside the scenes, describing the recurring character(s) and a fixed art style —
prepended to every scene's own ``visual_prompt`` before it's stored on the ``Scene``.
Downstream code (``visuals.generate_story_visuals``) needs no awareness of this; it
just sees one already-combined ``visual_prompt`` per scene, same as before.
"""

from __future__ import annotations

from polyglo.chat import ChatCompleter, ChatError, complete_json
from polyglo.models import Scene, Story

__all__ = ["AuthoringError", "GRADE_PROMPT", "SPLIT_PROMPT", "grade_source_text", "split_story"]


class AuthoringError(RuntimeError):
    """Raised when scene splitting fails outright, or returns a malformed shape
    that JSON repair alone cannot fix (e.g. right JSON, wrong structure)."""


GRADE_PROMPT = """Correct any spelling and grammar errors in the following story, \
then rewrite it so its vocabulary and sentence structure genuinely match CEFR level \
{cefr} for a language learner — simplify vocabulary and shorten/restructure \
sentences as needed, not just fix typos. Preserve the original meaning, characters, \
and events exactly; do not add or remove plot content.

Story:
{story}

Return strict JSON, no prose, no markdown fencing:
{{"corrected_text": "..."}}"""


def grade_source_text(
    source_text: str,
    cefr: str,
    completer: ChatCompleter,
    *,
    model: str,
    max_attempts: int = 3,
) -> str:
    """Correct spelling/grammar and restructure ``source_text`` to genuinely match
    ``cefr``, returning the corrected story text (not yet split into scenes).

    Deliberately a separate, upstream pass from :func:`split_story` — that function
    already asks the model for CEFR-appropriate scene text, but only ever sees
    whatever raw input arrives; this exists so the correction/leveling of the FULL
    story is itself a visible, inspectable step (the caller can show "here's what
    you typed, here's what we leveled it to"), not an implicit side-effect buried
    inside scene splitting.

    Raises :class:`AuthoringError` on total failure or an empty/malformed result —
    callers should treat that as non-fatal and fall back to the original text
    (this pass makes the input better, it is not load-bearing for the pipeline to
    function at all).
    """
    prompt = GRADE_PROMPT.format(cefr=cefr, story=source_text)

    try:
        data = complete_json(completer, prompt, model=model, max_attempts=max_attempts)
    except ChatError as exc:
        raise AuthoringError(f"source text grading failed: {exc}") from exc

    if not isinstance(data, dict) or "corrected_text" not in data:
        raise AuthoringError(f"expected a 'corrected_text' key, got: {data!r}")

    corrected = data["corrected_text"]
    if not isinstance(corrected, str) or not corrected.strip():
        raise AuthoringError(f"'corrected_text' is empty or not a string: {corrected!r}")

    return corrected.strip()


SPLIT_PROMPT = """Split this story into {n} scenes for a CEFR {cefr} language learner.

Story:
{story}

First, write a "style_guide" in exactly this form: "Recurring character: <species/\
object and 2-3 distinguishing visual traits — clothing or markings, not physical \
size or body descriptions>. Art style: <one fixed illustration style, e.g. flat \
children's-book watercolor, warm pastel palette>." This will be reused verbatim in \
every scene's image prompt, so it must fully identify the character by its \
distinguishing features — not just a name — since each scene is illustrated \
independently with no memory of the others. Keep it factual and plain (a reference \
sheet, not a physical description) — avoid adjectives like "small"/"tiny" paired \
with body-part descriptions (e.g. "tiny nose", "small ears"), which have been \
observed to trip the image model's content filter even for ordinary animal \
characters; describe markings, colors, and worn items instead.

Then, for each scene return:
- "text": the scene's narrative text (max 40 words, CEFR {cefr} vocabulary only)
- "visual_prompt": a concrete, culturally neutral illustration description of THIS \
scene's action/setting only (no text-in-image, no culture-specific dress/food/\
architecture unless the story requires it) — do not repeat the character/style \
description here, that comes from "style_guide"

Return strict JSON, no prose, no markdown fencing:
{{"style_guide": "...", "scenes": [{{"text": "...", "visual_prompt": "..."}}, ...]}}"""


def _validate_scene_payload(
    data: object, expected_n: int
) -> tuple[str, list[dict[str, str]]]:
    if not isinstance(data, dict) or "scenes" not in data:
        raise AuthoringError(f"expected a 'scenes' key, got: {data!r}")

    style_guide = data.get("style_guide")
    if not isinstance(style_guide, str) or not style_guide.strip():
        raise AuthoringError(f"missing or empty 'style_guide': {style_guide!r}")

    scenes = data["scenes"]
    if not isinstance(scenes, list) or not scenes:
        raise AuthoringError(f"'scenes' must be a non-empty list, got: {scenes!r}")

    for i, s in enumerate(scenes):
        if not isinstance(s, dict) or "text" not in s or "visual_prompt" not in s:
            raise AuthoringError(
                f"scene {i} missing 'text'/'visual_prompt': {s!r}"
            )
        if not str(s["text"]).strip():
            raise AuthoringError(f"scene {i} has empty text")
        if not str(s["visual_prompt"]).strip():
            raise AuthoringError(f"scene {i} has empty visual_prompt")

    # A model returning a different count than requested is not a JSON-repair
    # problem — accept what came back rather than silently padding or truncating,
    # but surface it so the caller can decide whether to retry.
    if len(scenes) != expected_n:
        raise AuthoringError(
            f"requested {expected_n} scenes, model returned {len(scenes)}"
        )

    return style_guide.strip(), scenes


def split_story(
    story: Story,
    source_text: str,
    n_scenes: int,
    completer: ChatCompleter,
    *,
    model: str,
    max_attempts: int = 3,
) -> list[Scene]:
    """Split ``source_text`` into ``n_scenes`` graded scenes.

    Raises :class:`AuthoringError` if the model cannot produce a valid, correctly
    shaped payload within ``max_attempts`` — including the JSON-repair attempts inside
    :func:`complete_json` itself, so total calls can exceed ``max_attempts`` by that
    inner budget. Deliberately does not retry a scene-count mismatch beyond what
    ``complete_json`` already attempts, since that failure isn't a formatting bug.
    """
    prompt = SPLIT_PROMPT.format(n=n_scenes, cefr=story.cefr, story=source_text)

    try:
        data = complete_json(completer, prompt, model=model, max_attempts=max_attempts)
    except ChatError as exc:
        raise AuthoringError(f"scene splitting failed: {exc}") from exc

    style_guide, scenes_data = _validate_scene_payload(data, n_scenes)

    # style_guide first: it fixes the character/style, the scene-specific prompt
    # then adds this scene's action/setting on top — both need to be present in
    # the same call to the image model, since each scene is generated independently
    # with no memory of the others (see module docstring).
    return [
        Scene(
            story_id=story.story_id,
            ordinal=i,
            source_text=str(s["text"]).strip(),
            visual_prompt=f"{style_guide} {str(s['visual_prompt']).strip()}",
        )
        for i, s in enumerate(scenes_data)
    ]
