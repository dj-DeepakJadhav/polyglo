"""Tests for the pre-narration text gate.

Bias check runs through all of these: the gate must reliably catch **gross** failures
(echo, wrong script) while staying quiet on ambiguous input. A false reject costs a
regeneration; a false accept only defers the problem to the QA gate. The tests encode
that asymmetry rather than demanding precision the detector cannot deliver.
"""

from __future__ import annotations

import pytest

from polyglo.qa.text_gate import (
    check_text,
    contains_source_leakage,
    detect_language,
    dominant_script,
)


# ---------------------------------------------------------------------------
# Script detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "script"),
    [
        ("The cat sat on the mat", "latin"),
        ("El gato subió al tejado", "latin"),
        ("मेरे पास किताबें हैं", "devanagari"),
        ("犬が走っている", "japanese"),
        ("ねこ", "japanese"),
    ],
)
def test_dominant_script(text, script):
    assert dominant_script(text) == script


def test_script_of_digits_and_punctuation_is_none():
    assert dominant_script("123 !!! ...") is None


def test_mixed_script_reports_the_majority():
    assert dominant_script("मेरे पास किताबें हैं और ok") == "devanagari"


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def test_non_latin_scripts_detect_with_high_confidence():
    lang, conf = detect_language("मेरे पास बहुत सारी किताबें हैं")
    assert lang == "hi" and conf > 0.9

    lang, conf = detect_language("犬が公園を走っている")
    assert lang == "ja" and conf > 0.9


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("the cat is sitting on the mat with a hat", "en"),
        ("el gato está en la casa con los libros", "es"),
        ("le chat est dans la maison avec les livres", "fr"),
        ("der Hund ist nicht in dem Haus und das Buch", "de"),
        ("il gatto è nella casa con gli amici", "it"),
    ],
)
def test_latin_languages_on_sentence_length_input(text, expected):
    lang, conf = detect_language(text)
    assert lang == expected
    assert conf > 0.3


def test_short_input_declines_to_guess():
    """Fragments carry too little evidence; guessing here causes false rejects."""
    lang, conf = detect_language("el gato")
    assert lang is None
    assert conf == 0.0


def test_evidence_free_input_declines_to_guess():
    lang, conf = detect_language("zzz qqq xxx www vvv")
    assert lang is None
    assert conf == 0.0


# ---------------------------------------------------------------------------
# The gate — rejections
# ---------------------------------------------------------------------------


def test_empty_translation_is_rejected():
    r = check_text("", "es-ES")
    assert r.ok is False
    assert "empty" in r.reason
    assert r.checks["non_empty"] is False


def test_whitespace_only_is_rejected():
    assert check_text("   \n\t ", "es-ES").ok is False


def test_untranslated_echo_is_rejected():
    """The single most common LLM translation failure: the model returns the input."""
    src = "The cat sat on the roof"
    r = check_text(src, "es-ES", source_text=src)
    assert r.ok is False
    assert "identical to the source" in r.reason
    assert r.checks["not_source_echo"] is False


def test_echo_detection_survives_punctuation_and_case_differences():
    r = check_text("the cat SAT on the roof!", "es-ES",
                   source_text="The cat sat on the roof.")
    assert r.ok is False
    assert "identical to the source" in r.reason


def test_latin_output_for_hindi_target_is_rejected():
    """Romanisation or a silent fallback to English."""
    r = check_text("mere paas kitaabein hain", "hi-IN")
    assert r.ok is False
    assert "devanagari" in r.reason and "latin" in r.reason
    assert r.checks["correct_script"] is False


def test_latin_output_for_japanese_target_is_rejected():
    r = check_text("inu ga hashitte iru", "ja-JP")
    assert r.ok is False
    assert "japanese" in r.reason


def test_wrong_language_same_script_is_rejected_when_confident():
    r = check_text("der Hund ist nicht in dem Haus und das Buch", "es-ES")
    assert r.ok is False
    assert r.detected == "de"
    assert "expected es" in r.reason


# ---------------------------------------------------------------------------
# The gate — acceptances (false rejects are the expensive mistake)
# ---------------------------------------------------------------------------


def test_correct_translation_passes():
    r = check_text(
        "el gato está en la casa con los libros",
        "es-ES",
        source_text="the cat is in the house with the books",
    )
    assert r.ok is True
    assert r.reason is None
    assert all(r.checks.values())


def test_correct_hindi_passes():
    assert check_text("मेरे पास बहुत सारी किताबें हैं", "hi-IN").ok is True


def test_correct_japanese_passes():
    assert check_text("犬が公園を走っている", "ja-JP").ok is True


def test_short_translation_is_not_rejected_on_language_grounds():
    """Too little evidence to judge — accept and let the QA gate decide."""
    r = check_text("El gato.", "es-ES", source_text="The cat.")
    assert r.ok is True


def test_low_confidence_does_not_reject():
    """Spanish and Portuguese overlap heavily; the gate must not fire on weak evidence."""
    r = check_text("para o que de que com a casa", "es-ES", min_confidence=0.9)
    assert r.ok is True


def test_min_confidence_is_tunable():
    text = "der Hund ist nicht in dem Haus und das Buch"
    assert check_text(text, "es-ES", min_confidence=0.01).ok is False
    assert check_text(text, "es-ES", min_confidence=0.999).ok is True


def test_unknown_target_locale_skips_script_check():
    """An unlisted locale must not be rejected for having no expected script."""
    assert check_text("jambo habari za asubuhi rafiki", "sw-KE").ok is True


def test_summary_wording():
    assert check_text("el gato está en la casa hoy", "es-ES").summary() == "text ok"
    assert "rejected" in check_text("", "es-ES").summary()


# ---------------------------------------------------------------------------
# Partial-translation leakage (advisory)
# ---------------------------------------------------------------------------


def test_leakage_detects_an_untranslated_clause():
    src = "The cat sat on the roof and watched the birds"
    tgt = "El gato se sentó and watched the birds"
    assert contains_source_leakage(tgt, src) is True


def test_no_leakage_on_a_clean_translation():
    assert contains_source_leakage(
        "el gato se sentó en el tejado",
        "the cat sat on the roof",
    ) is False


def test_leakage_ignores_short_inputs():
    assert contains_source_leakage("hola", "hi") is False
