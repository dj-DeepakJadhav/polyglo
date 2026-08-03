"""Pre-narration text gate.

Runs on translated text *before* it reaches TTS. Catching a bad translation here costs
a fraction of a chat call; discovering it after narration costs a TTS call, an ASR call,
and a retry — and the QA gate would blame the audio for a text defect.

Three failure modes, in descending order of how often LLM translation actually hits them:

1. **Untranslated echo** — the model returns the source text verbatim. Trivially
   detectable and by far the most common.
2. **Wrong script** — Latin characters where Devanagari or Japanese was expected. Usually
   means romanisation or a silent fallback to English.
3. **Wrong language, same script** — Spanish output where Portuguese was requested. The
   hardest of the three, and the one this module is weakest at.

Scope, stated plainly: this is a **coarse detector for gross failures**, not a
general-purpose language identifier. Script detection is near-certain; Latin-script
discrimination uses weighted function words and diacritic evidence, which is reliable for
sentence-length text and unreliable for fragments. Spanish and Portuguese overlap heavily
and are the most likely confusion.

Because a false reject wastes a regeneration and a false accept merely defers the problem
to the QA gate that follows it, this module is deliberately biased toward accepting when
uncertain. Short inputs are never rejected on language grounds.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from polyglo.qa.normalize import normalize
from polyglo.qa.numerals import language_of

__all__ = ["TextGateResult", "check_text", "detect_language", "dominant_script"]

#: Minimum token count before language scoring is trusted at all.
MIN_TOKENS_FOR_LANGUAGE_ID = 4

#: Function words. Short, high-frequency, and far more discriminative than content
#: words on sentence-length input. Weight 2 marks a term that is strongly distinctive
#: of that language against its nearest neighbours.
STOPWORDS: dict[str, dict[str, int]] = {
    "en": {"the": 2, "and": 1, "is": 1, "of": 2, "to": 1, "in": 1, "that": 1,
           "it": 1, "for": 1, "with": 1, "was": 2, "this": 1, "you": 1, "are": 1,
           "have": 1, "not": 1, "they": 1, "from": 1, "she": 1, "his": 1},
    "es": {"el": 1, "la": 1, "los": 2, "las": 1, "de": 1, "que": 1, "y": 1,
           "en": 1, "un": 1, "una": 1, "es": 1, "por": 1, "con": 2, "se": 1,
           "para": 1, "del": 2, "pero": 2, "más": 1, "muy": 2, "está": 2,
           "porque": 2, "hacia": 2},
    "fr": {"le": 1, "les": 2, "des": 2, "du": 2, "et": 1, "est": 1, "une": 1,
           "qui": 2, "dans": 2, "pour": 1, "pas": 2, "sur": 1, "il": 1,
           "elle": 1, "aux": 2, "avec": 2, "mais": 2, "plus": 1, "ne": 1,
           "cest": 2, "être": 2},
    "de": {"der": 2, "die": 2, "das": 2, "und": 2, "ist": 1, "den": 2, "von": 2,
           "zu": 1, "mit": 2, "sich": 2, "auf": 2, "für": 2, "ein": 1,
           "eine": 1, "auch": 2, "nicht": 2, "werden": 2, "ich": 2, "dass": 2},
    "it": {"il": 1, "lo": 1, "gli": 2, "di": 1, "che": 1, "una": 1, "per": 1,
           "con": 1, "non": 1, "del": 1, "della": 2, "sono": 2, "come": 1,
           "anche": 2, "perché": 2, "degli": 2, "nel": 2, "questo": 2},
    "pt": {"os": 1, "as": 1, "de": 1, "que": 1, "do": 2, "da": 2, "em": 1,
           "uma": 1, "para": 1, "com": 1, "não": 2, "por": 1, "na": 2, "no": 1,
           "mais": 1, "mas": 1, "dos": 2, "das": 2, "você": 2, "então": 2,
           "ele": 1, "foi": 1},
}

#: Characters that are strong positive evidence for one language over its neighbours.
DIACRITIC_HINTS: dict[str, str] = {
    "es": "ñ¿¡",
    "pt": "ãõ",
    "fr": "çœè",
    "de": "ßäöü",
    "it": "àìòù",
}

#: Unicode ranges, by the script each locale is expected to be written in.
_SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "latin": ((0x0041, 0x024F),),
    "devanagari": ((0x0900, 0x097F),),
    "japanese": ((0x3040, 0x30FF), (0x4E00, 0x9FFF), (0xFF66, 0xFF9F)),
}

EXPECTED_SCRIPT: dict[str, str] = {
    "en": "latin", "es": "latin", "fr": "latin", "de": "latin",
    "it": "latin", "pt": "latin", "hi": "devanagari", "ja": "japanese",
}

_WORD = re.compile(r"\w+", re.UNICODE)


# ---------------------------------------------------------------------------
# Script detection — near-certain, unlike language detection
# ---------------------------------------------------------------------------


def _script_of(ch: str) -> str | None:
    cp = ord(ch)
    for script, ranges in _SCRIPT_RANGES.items():
        if any(lo <= cp <= hi for lo, hi in ranges):
            return script
    return None


def dominant_script(text: str) -> str | None:
    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        s = _script_of(ch)
        if s:
            counts[s] = counts.get(s, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


# ---------------------------------------------------------------------------
# Language detection — coarse, biased toward "don't know"
# ---------------------------------------------------------------------------


def detect_language(text: str) -> tuple[str | None, float]:
    """Best-guess language and a 0-1 confidence.

    Returns ``(None, 0.0)`` when the text is too short or the evidence too thin.
    Callers must treat low confidence as "unknown", never as "wrong".
    """
    script = dominant_script(text)
    if script == "devanagari":
        return "hi", 0.95
    if script == "japanese":
        return "ja", 0.95

    folded = normalize(text, "en-US", expand_numbers=False, strip_diacritics=False)
    tokens = _WORD.findall(folded)
    if len(tokens) < MIN_TOKENS_FOR_LANGUAGE_ID:
        return None, 0.0

    scores: dict[str, float] = {}
    for lang, words in STOPWORDS.items():
        hit = sum(words.get(t, 0) for t in tokens)
        bonus = sum(2 for ch in DIACRITIC_HINTS.get(lang, "") if ch in folded)
        scores[lang] = hit + bonus

    total = sum(scores.values())
    if total == 0:
        return None, 0.0

    best = max(scores, key=lambda k: scores[k])
    confidence = scores[best] / total
    return best, round(confidence, 3)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass
class TextGateResult:
    ok: bool
    reason: str | None = None
    detected: str | None = None
    confidence: float = 0.0
    checks: dict[str, bool] = field(default_factory=dict)

    def summary(self) -> str:
        return "text ok" if self.ok else f"text rejected: {self.reason}"


def check_text(
    translated: str,
    target_locale: str,
    source_text: str | None = None,
    *,
    min_confidence: float = 0.45,
) -> TextGateResult:
    """Validate a translation before it costs a TTS call.

    ``min_confidence`` is the bar for *rejecting* on language grounds. Set it high:
    a false reject burns a regeneration, while a false accept only defers the problem
    to the QA gate, which is a much cheaper mistake.
    """
    target = language_of(target_locale)
    checks: dict[str, bool] = {}

    # 1. Empty or whitespace-only.
    if not translated or not translated.strip():
        return TextGateResult(False, "empty translation", checks={"non_empty": False})
    checks["non_empty"] = True

    # 2. Untranslated echo — the most common LLM translation failure.
    if source_text:
        same = normalize(translated, target_locale) == normalize(source_text, target_locale)
        checks["not_source_echo"] = not same
        if same:
            return TextGateResult(
                False, "translation is identical to the source text",
                checks=checks,
            )

    # 3. Script mismatch. Near-certain evidence, so this one is allowed to reject
    #    regardless of length.
    expected = EXPECTED_SCRIPT.get(target)
    actual = dominant_script(translated)
    if expected and actual and actual != expected:
        checks["correct_script"] = False
        return TextGateResult(
            False,
            f"expected {expected} script for {target_locale}, found {actual}",
            detected=actual, confidence=0.95, checks=checks,
        )
    checks["correct_script"] = True

    # 4. Wrong language, same script. Weakest check, so it defers when unsure.
    detected, conf = detect_language(translated)
    checks["language_match"] = True
    if detected and detected != target and conf >= min_confidence:
        checks["language_match"] = False
        return TextGateResult(
            False,
            f"expected {target}, detected {detected} (confidence {conf:.0%})",
            detected=detected, confidence=conf, checks=checks,
        )

    return TextGateResult(True, None, detected=detected, confidence=conf, checks=checks)


def contains_source_leakage(translated: str, source_text: str, *, min_run: int = 4) -> bool:
    """True if a run of >= ``min_run`` consecutive source words survives untranslated.

    Catches partial translation — a model that renders most of a sentence and leaves a
    clause in the source language. Kept separate from :func:`check_text` because short
    shared runs are legitimate between related languages, so this is advisory rather
    than a hard reject.
    """
    src = normalize(source_text, "en-US").split()
    tgt = normalize(translated, "en-US").split()
    if len(src) < min_run or len(tgt) < min_run:
        return False
    tgt_runs = {" ".join(tgt[i:i + min_run]) for i in range(len(tgt) - min_run + 1)}
    return any(
        " ".join(src[i:i + min_run]) in tgt_runs
        for i in range(len(src) - min_run + 1)
    )
