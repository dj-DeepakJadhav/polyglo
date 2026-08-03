"""Text normalisation for WER comparison.

The QA gate diffs the text we sent to TTS against what ASR heard back. Those two
strings are never byte-identical even when the audio is perfect, because ASR has its
own spelling conventions. Normalisation removes the differences that say nothing about
audio quality, and keeps the ones that do.

What we deliberately fold away:

- **Digits vs words** — "3" vs "tres". See :mod:`polyglo.qa.numerals`.
- **Latin diacritics** — "dieciseis" vs "dieciséis". ASR accent conventions are
  orthographic noise; we are measuring intelligibility, not spelling.
- **Punctuation, case, whitespace** — no bearing on whether the audio was correct.
- **Hyphens** — "quatre-vingt-dix" and "quatre vingt dix" must tokenise identically,
  so hyphens become spaces.
- **Apostrophes** — "l'eau" vs "l’eau" vs "leau". Straight vs curly quotes are a
  pure ASR-convention mismatch; dropping them kills the whole class.

Two decisions worth flagging because they diverge from ``docs/02`` §6.2:

1. That spec said to *keep* intra-word hyphens and apostrophes. Keeping hyphens is
   actively harmful: it makes "quatre-vingt-dix" one token where ASR may emit three,
   turning one orthographic difference into three word errors. Splitting is more
   robust and is what we do.
2. Diacritic stripping is **Latin-script only**. Devanagari vowel signs are also
   Unicode category ``Mn``, and stripping them would destroy Hindi text rather than
   normalise it. This is a real bug that a naive ``NFD + remove Mn`` implementation
   would ship silently, so it has its own test.
"""

from __future__ import annotations

import re
import unicodedata

from polyglo.qa.numerals import language_of, number_to_words

__all__ = [
    "normalize",
    "tokenize",
    "expand_numerals",
    "strip_latin_diacritics",
    "is_space_delimited",
]

#: Scripts that do not put spaces between words. Word-level WER is meaningless for
#: these, so :func:`tokenize` falls back to characters.
NON_SPACE_DELIMITED = {"ja", "zh", "th", "lo", "my", "km"}

_DIGITS = re.compile(r"\d+")
_APOSTROPHES = re.compile(r"['‘’ʼ]")
_SEPARATORS = re.compile(r"[-‐-―_/]+")
_NON_WORD = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def is_space_delimited(locale: str) -> bool:
    return language_of(locale) not in NON_SPACE_DELIMITED


# ---------------------------------------------------------------------------
# Diacritics
# ---------------------------------------------------------------------------


def _is_latin(ch: str) -> bool:
    try:
        return "LATIN" in unicodedata.name(ch)
    except ValueError:
        return False


def strip_latin_diacritics(text: str) -> str:
    """Remove combining marks, but only those attached to a Latin base character.

    Applying this indiscriminately would strip Devanagari matras and Japanese
    dakuten, mangling the text instead of normalising it.
    """
    out: list[str] = []
    base_is_latin = False
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch):
            if not base_is_latin:
                out.append(ch)          # keep — belongs to a non-Latin base
            continue
        base_is_latin = _is_latin(ch)
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


# ---------------------------------------------------------------------------
# Numerals
# ---------------------------------------------------------------------------


def expand_numerals(text: str, locale: str) -> str:
    """Replace digit runs with their spelled-out form for ``locale``.

    Digits are left untouched when the locale has no expander, or when the value is
    outside its supported range (see :data:`polyglo.qa.numerals.COVERAGE`). Leaving
    them is a known false-failure risk; emitting a wrong word would be worse, because
    it fails *and* misleads the diff panel.

    Thousands separators are not handled — "1,000" and "1.000" are genuinely ambiguous
    across locales, and guessing would corrupt decimals.
    """

    def repl(m: re.Match[str]) -> str:
        words = number_to_words(int(m.group()), locale)
        return words if words is not None else m.group()

    return _DIGITS.sub(repl, text)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalize(
    text: str,
    locale: str = "en-US",
    *,
    expand_numbers: bool = True,
    strip_diacritics: bool = True,
) -> str:
    """Canonicalise ``text`` for comparison. Apply to BOTH sides of a WER diff."""
    if not text:
        return ""

    s = unicodedata.normalize("NFKC", text)

    # Before casefolding: expanders emit natural-cased words, folded just below.
    if expand_numbers:
        s = expand_numerals(s, locale)

    s = s.casefold()

    if strip_diacritics:
        s = strip_latin_diacritics(s)

    s = _APOSTROPHES.sub("", s)     # l'eau -> leau   (drop, don't split)
    s = _SEPARATORS.sub(" ", s)     # quatre-vingt-dix -> quatre vingt dix
    s = _NON_WORD.sub(" ", s)       # remaining punctuation
    s = _WHITESPACE.sub(" ", s)

    return s.strip()


def tokenize(text: str, locale: str = "en-US") -> list[str]:
    """Split normalised text into comparison units.

    Words for space-delimited languages; characters for Japanese and friends, where
    word-level WER would otherwise compare two arbitrary segmentations and report
    nonsense.
    """
    if not text:
        return []
    if is_space_delimited(locale):
        return text.split()
    return [ch for ch in text if not ch.isspace()]
