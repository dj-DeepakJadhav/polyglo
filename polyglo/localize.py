"""Translation fan-out: one scene -> N locales, CEFR-preserving.

Runs the pre-narration text gate (docs/02 §6.5 / F10) inline: a rejected translation is
retried with the specific defect named in the corrective prompt before it ever reaches
TTS, which is what makes the gate worth having — catching a bad translation here costs
a fraction of a chat call, not a TTS call plus an ASR call plus a QA retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polyglo.chat import ChatCompleter, ChatError
from polyglo.models import LocalizedScene, Scene, locale_name
from polyglo.qa.text_gate import TextGateResult, check_text

__all__ = ["LocalizationError", "LocalizationResult", "TRANSLATE_PROMPT",
           "translate_scene", "localize_scene", "localize_all_locales"]


class LocalizationError(RuntimeError):
    """Raised only on outright chat failure — a rejected-but-exhausted translation
    is returned to the caller, not raised, since a human reviewer may still want it."""


TRANSLATE_PROMPT = """Translate the following text into {locale_name} ({locale_code}).

Preserve the CEFR {cefr} reading level. Use natural, fluent native phrasing suitable for warm audio storytelling. Do not perform stiff or literal word-for-word translation.

Text:
{text}

Return ONLY the natural translation. No explanation, no quotation marks, no source text."""


@dataclass
class LocalizationResult:
    text: str
    gate: TextGateResult
    attempts: int
    accepted: bool = field(init=False)

    def __post_init__(self) -> None:
        self.accepted = self.gate.ok


def translate_scene(
    scene: Scene,
    target_locale: str,
    cefr: str,
    completer: ChatCompleter,
    *,
    model: str,
) -> str:
    """One translation call, no gating. Building block for :func:`localize_scene`."""
    prompt = TRANSLATE_PROMPT.format(
        locale_name=locale_name(target_locale),
        locale_code=target_locale,
        cefr=cefr,
        text=scene.source_text,
    )
    try:
        return completer.complete(prompt, model=model).strip()
    except Exception as exc:
        raise LocalizationError(f"translation failed: {type(exc).__name__}: {exc}") from exc


def localize_scene(
    scene: Scene,
    target_locale: str,
    cefr: str,
    completer: ChatCompleter,
    *,
    model: str,
    max_attempts: int = 2,
) -> LocalizationResult:
    """Translate, gate, and retry with the specific defect named if rejected.

    Returns the best attempt even if every attempt was rejected — a caller (or human
    reviewer) may still find a rejected-but-plausible translation useful, and silently
    discarding it would hide diagnostic information the gate already produced.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    best_text = ""
    best_gate = check_text("", target_locale)   # empty -> rejected, safe initial worst-case
    attempts = 0

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        if attempt == 1:
            text = translate_scene(scene, target_locale, cefr, completer, model=model)
        else:
            corrective = (
                f"{TRANSLATE_PROMPT.format(locale_name=locale_name(target_locale), locale_code=target_locale, cefr=cefr, text=scene.source_text)}\n\n"
                f"Your previous attempt was rejected: {best_gate.reason}. "
                f"Produce a corrected translation that fixes this specific problem."
            )
            try:
                text = completer.complete(corrective, model=model).strip()
            except Exception as exc:
                raise LocalizationError(
                    f"translation retry failed: {type(exc).__name__}: {exc}"
                ) from exc

        gate = check_text(text, target_locale, source_text=scene.source_text)

        if gate.ok:
            return LocalizationResult(text=text, gate=gate, attempts=attempt)

        # Keep the least-bad attempt so far — "least bad" approximated by simply
        # preferring the most recent rejection, since check_text has no partial score.
        best_text, best_gate = text, gate

    return LocalizationResult(text=best_text, gate=best_gate, attempts=attempts)


def localize_all_locales(
    scene: Scene,
    target_locales: list[str],
    cefr: str,
    completer: ChatCompleter,
    *,
    model: str,
    max_attempts: int = 2,
) -> dict[str, LocalizationResult | LocalizationError]:
    """Fan a single scene out to every requested locale.

    One locale's chat outage must not abort the other nineteen — a batch of 20 locale
    calls where the German endpoint is briefly down should not lose the Spanish,
    French, and eighteen other results that already succeeded. A failure is caught per
    locale and stored as the :class:`LocalizationError` itself; callers distinguish the
    two cases with ``isinstance(v, LocalizationResult)``.

    Sequential rather than concurrent for now — ``ChatCompleter`` doesn't expose an
    async surface, and the real bottleneck (per docs/03) is generation credits, not
    translation wall-clock. Revisit with ``asyncio.gather``/`abatch_run()` if profiling
    says otherwise.
    """
    results: dict[str, LocalizationResult | LocalizationError] = {}
    for locale in target_locales:
        try:
            results[locale] = localize_scene(
                scene, locale, cefr, completer, model=model, max_attempts=max_attempts
            )
        except LocalizationError as exc:
            results[locale] = exc
    return results


def to_localized_scene(scene: Scene, locale: str, result: LocalizationResult) -> LocalizedScene:
    """Adapt a :class:`LocalizationResult` into the persisted domain model."""
    return LocalizedScene(
        story_id=scene.story_id,
        ordinal=scene.ordinal,
        locale=locale,
        text=result.text,
    )
