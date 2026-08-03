"""Tests for WER, PCTS and alignment.

The alignment tests matter as much as the score tests: the diff panel is what makes a
failed segment legible to a human, and a wrong backtrace produces a diff that looks
like nonsense even when the number is right.
"""

from __future__ import annotations

import pytest

from polyglo.qa.wer import Op, WERResult, pcts, score, wer_tokens


def toks(s: str) -> list[str]:
    return s.split()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_identical_sequences_score_zero():
    r = wer_tokens(toks("the cat sat on the mat"), toks("the cat sat on the mat"))
    assert r.wer == 0.0
    assert r.exact is True
    assert r.hits == 6
    assert r.errors == 0


def test_single_substitution():
    r = wer_tokens(toks("the cat sat"), toks("the dog sat"))
    assert r.substitutions == 1
    assert r.deletions == r.insertions == 0
    assert r.wer == pytest.approx(1 / 3)


def test_single_deletion():
    r = wer_tokens(toks("the cat sat"), toks("the sat"))
    assert r.deletions == 1
    assert r.wer == pytest.approx(1 / 3)


def test_single_insertion():
    r = wer_tokens(toks("the cat sat"), toks("the big cat sat"))
    assert r.insertions == 1
    assert r.wer == pytest.approx(1 / 3)


def test_wer_can_exceed_one_with_many_insertions():
    """Standard WER is uncapped. A runaway TTS appending garbage should score worse
    than one that merely got every word wrong."""
    r = wer_tokens(toks("hello"), toks("hello and then a lot more words appeared"))
    assert r.wer == pytest.approx(7.0)   # 7 insertions over a 1-word reference
    assert r.hits == 1
    assert r.insertions == 7


def test_completely_wrong_scores_one():
    r = wer_tokens(toks("a b c"), toks("x y z"))
    assert r.wer == 1.0
    assert r.substitutions == 3


def test_accuracy_complements_hits():
    r = wer_tokens(toks("one two three four"), toks("one two three five"))
    assert r.hits == 3
    assert r.accuracy == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


def test_both_empty_scores_zero():
    r = wer_tokens([], [])
    assert r.wer == 0.0
    assert r.exact is True


def test_empty_reference_with_output_scores_one():
    """No denominator available; report 1.0 rather than dividing by zero."""
    r = wer_tokens([], toks("unexpected speech"))
    assert r.wer == 1.0
    assert r.insertions == 2


def test_empty_hypothesis_is_all_deletions():
    """Silent audio — the failure mode the gate most needs to catch."""
    r = wer_tokens(toks("the cat sat"), [])
    assert r.wer == 1.0
    assert r.deletions == 3
    assert r.hits == 0


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


def test_alignment_is_in_reading_order():
    r = wer_tokens(toks("the cat sat"), toks("the dog sat"))
    assert [a.op for a in r.alignment] == [Op.EQUAL, Op.SUB, Op.EQUAL]
    assert r.alignment[1].ref == "cat"
    assert r.alignment[1].hyp == "dog"


def test_alignment_covers_every_token():
    ref, hyp = toks("a b c d"), toks("a x c d e")
    r = wer_tokens(ref, hyp)
    assert [a.ref for a in r.alignment if a.ref] == ref
    assert [a.hyp for a in r.alignment if a.hyp] == hyp


def test_alignment_op_counts_match_totals():
    r = wer_tokens(toks("one two three four five"), toks("one three four six five extra"))
    counts = {op: sum(1 for a in r.alignment if a.op is op) for op in Op}
    assert counts[Op.EQUAL] == r.hits
    assert counts[Op.SUB] == r.substitutions
    assert counts[Op.DEL] == r.deletions
    assert counts[Op.INS] == r.insertions


def test_ties_prefer_substitution_over_delete_plus_insert():
    """A changed word should render as one sub, not a delete beside an insert —
    otherwise the diff panel shows two unrelated-looking errors."""
    r = wer_tokens(toks("hola mundo"), toks("hola planeta"))
    assert r.substitutions == 1
    assert r.deletions == 0 and r.insertions == 0


def test_diff_pairs_shape():
    r = wer_tokens(toks("a b"), toks("a c"))
    assert r.diff_pairs() == [("equal", "a", "a"), ("sub", "b", "c")]


def test_summary_is_readable():
    r = wer_tokens(toks("one two three four"), toks("one two three"))
    assert "1D" in r.summary()
    assert "over 4 words" in r.summary()


# ---------------------------------------------------------------------------
# score() — normalisation is applied to both sides
# ---------------------------------------------------------------------------


def test_score_folds_case_and_punctuation():
    r = score("Hello, world!", "hello world", "en-US")
    assert r.wer == 0.0


def test_score_folds_digits_against_words():
    """The reason numeral expansion exists: identical audio, different orthography."""
    r = score("Tengo 3 libros", "tengo tres libros", "es-ES")
    assert r.wer == 0.0
    assert r.exact is True


def test_score_folds_accents():
    r = score("dieciséis años", "dieciseis anos", "es-ES")
    assert r.wer == 0.0


def test_score_catches_a_real_mispronunciation():
    """A genuine error must survive normalisation — folding must not hide defects."""
    r = score("Tengo tres libros", "tengo tres libras", "es-ES")
    assert r.substitutions == 1
    assert r.wer == pytest.approx(1 / 3)


def test_score_catches_truncated_audio():
    """TTS dropping the tail of a sentence is a common silent failure."""
    r = score(
        "El gato subió al tejado por la noche",
        "el gato subió al tejado",
        "es-ES",
    )
    assert r.deletions == 3
    assert r.wer > 0.3


def test_score_catches_wrong_language_drift():
    r = score("Le chat dort", "the cat sleeps", "fr-FR")
    assert r.wer == 1.0


def test_score_on_japanese_uses_character_tokens():
    r = score("犬が走る", "犬が歩く", "ja-JP")
    assert r.ref_len == 4          # characters, not words
    assert r.substitutions == 2


def test_short_sentences_are_high_variance():
    """One wrong word in five is already past the 0.10 pass threshold — documents
    why thresholds must be calibrated rather than reasoned about (task #18)."""
    r = score("the cat sat on it", "the cat sat on him", "en-US")
    assert r.wer == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# PCTS
# ---------------------------------------------------------------------------


def test_pcts_counts_only_exact_segments():
    results = [
        score("hello world", "hello world", "en-US"),      # exact
        score("hello world", "hello word", "en-US"),       # near miss, still not exact
        score("good day", "good day", "en-US"),            # exact
        score("good day", "", "en-US"),                    # silent
    ]
    assert pcts(results) == pytest.approx(0.5)


def test_pcts_of_empty_set_is_zero():
    assert pcts([]) == 0.0


def test_pcts_is_harsher_than_mean_wer():
    """Every segment slightly wrong: mean WER looks fine, PCTS is zero. This is why
    both are reported."""
    results = [score("one two three four five", f"one two three four {w}", "en-US")
               for w in ("six", "seven", "eight")]
    mean_wer = sum(r.wer for r in results) / len(results)
    assert mean_wer == pytest.approx(0.2)
    assert pcts(results) == 0.0
