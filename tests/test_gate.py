"""Tests for the cross-modal QA gate.

The scenarios that matter for the demo, all covered here:

- clean pass on attempt 1
- fail then recover on an alternate voice (the 40 seconds that sell the video)
- fail everything and quarantine, keeping the *best* attempt for the reviewer
- provider outage mid-run, recovered by escalation
- no transcriber at all — degrade visibly to UNVERIFIED, never silently
"""

from __future__ import annotations

import pytest

from polyglo.config import QAConfig
from polyglo.models import LocalizedScene, QAStatus
from polyglo.qa.gate import (
    MockNarrator,
    MockTranscriber,
    Narrator,
    QAGate,
    Transcriber,
    VoicePlan,
)

PLAN = VoicePlan(primary="riva-a", alternates=["riva-b"], escalation="strong-model")


def make_scene(text: str = "El gato subió al tejado", locale: str = "es-ES"):
    return LocalizedScene(story_id="s1", ordinal=0, locale=locale, text=text)


def build(transcripts=None, fail_models=(), config=None):
    tr = MockTranscriber(transcripts)
    nar = MockNarrator(transcriber=tr, fail_models=fail_models)
    gate = QAGate(config=config or QAConfig(), transcriber=tr)
    return gate, nar, tr


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_mocks_satisfy_protocols():
    tr = MockTranscriber()
    assert isinstance(tr, Transcriber)
    assert isinstance(MockNarrator(tr), Narrator)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wer", "expected"),
    [(0.0, "pass"), (0.10, "pass"), (0.11, "retry"),
     (0.25, "retry"), (0.26, "escalate"), (1.0, "escalate")],
)
def test_classification_boundaries(wer, expected):
    assert QAGate().classify(wer) == expected


def test_thresholds_are_config_driven():
    """Defaults are an untested guess; calibration is task #18."""
    strict = QAGate(QAConfig(wer_pass=0.0, wer_retry=0.05))
    assert strict.classify(0.01) == "retry"
    assert strict.classify(0.5) == "escalate"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_perfect_audio_passes_first_attempt():
    gate, nar, _ = build()                       # None => transcriber echoes the text
    res = gate.run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.PASS
    assert res.attempt_count == 1
    assert res.wer == 0.0
    assert res.voice_model == "riva-a"
    assert res.recovered is False


def test_normalisation_differences_still_pass():
    """ASR spelling conventions must not trip the gate."""
    gate, nar, _ = build(["tengo tres libros"])
    res = gate.run(make_scene("Tengo 3 libros."), nar, PLAN)
    assert res.status is QAStatus.PASS
    assert res.wer == 0.0


# ---------------------------------------------------------------------------
# Retry and recovery — the demo's centrepiece
# ---------------------------------------------------------------------------


def test_bad_audio_retries_on_alternate_voice_then_passes():
    gate, nar, _ = build(["el gato subió al techo", None])
    res = gate.run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.RETRIED
    assert res.recovered is True
    assert res.attempt_count == 2
    assert res.voice_model == "riva-b"           # switched voice
    assert res.wer == 0.0

    assert res.attempts[0].status == "retry"
    assert res.attempts[0].wer > 0
    assert [c[2] for c in nar.calls] == ["riva-a", "riva-b"]


def test_severe_failure_skips_straight_to_escalation():
    """WER past the retry band should not waste an attempt on a sibling voice."""
    gate, nar, _ = build(["completely different words entirely here", None])
    res = gate.run(make_scene(), nar, PLAN)

    assert res.attempts[0].status == "escalate"
    assert res.voice_model == "strong-model"
    assert res.status is QAStatus.RETRIED


def test_every_attempt_is_recorded_even_when_it_passed():
    """The retry history is the artifact — a gate that only logs the winner
    demonstrates nothing."""
    gate, nar, _ = build(["el gato subió al techo", None])
    res = gate.run(make_scene(), nar, PLAN)

    assert len(res.attempts) == 2
    assert res.attempts[0].transcript == "el gato subió al techo"
    assert res.attempts[0].voice_model == "riva-a"
    assert all(a.latency_ms >= 0 for a in res.attempts)


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


def test_persistent_failure_quarantines():
    gate, nar, _ = build(["nothing like the source text at all"])
    res = gate.run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.QUARANTINED
    assert res.attempt_count == 3                # max_attempts
    assert res.status.is_good is False


def test_quarantine_keeps_the_best_attempt_not_the_last():
    """A reviewer needs the closest the pipeline got, not whatever it tried last."""
    gate, nar, _ = build([
        "totally wrong output here entirely",     # worst
        "el gato subió al tejado alto",           # closest
        "wrong again completely different",       # worst
    ])
    res = gate.run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.QUARANTINED
    assert res.transcript == "el gato subió al tejado alto"
    assert res.wer == min(a.wer for a in res.attempts if a.wer is not None)


def test_max_attempts_is_configurable():
    gate, nar, _ = build(["wrong entirely different text"], config=QAConfig(max_attempts=1))
    res = gate.run(make_scene(), nar, PLAN)
    assert res.attempt_count == 1
    assert res.status is QAStatus.QUARANTINED


def test_silent_audio_is_caught():
    """Empty transcript = TTS produced nothing. A common silent failure."""
    gate, nar, _ = build([""])
    res = gate.run(make_scene(), nar, PLAN)
    assert res.status is QAStatus.QUARANTINED
    assert res.wer == 1.0


# ---------------------------------------------------------------------------
# Provider failures
# ---------------------------------------------------------------------------


def test_provider_outage_is_recorded_and_escalated():
    gate, nar, _ = build([None], fail_models=["riva-a"])
    res = gate.run(make_scene(), nar, PLAN)

    assert res.attempts[0].status == "error"
    assert "unavailable" in res.attempts[0].error
    assert res.status is QAStatus.RETRIED
    assert res.voice_model == "strong-model"     # an error forces escalation


def test_total_provider_outage_quarantines_without_crashing():
    gate, nar, _ = build([None], fail_models=["riva-a", "riva-b", "strong-model"])
    res = gate.run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.QUARANTINED
    assert all(a.status == "error" for a in res.attempts)
    assert res.wer is None


def test_transcriber_failure_does_not_crash_the_run():
    tr = MockTranscriber(fail_with=RuntimeError("ASR quota exceeded"))
    nar = MockNarrator(transcriber=tr)
    res = QAGate(transcriber=tr).run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.QUARANTINED
    assert "ASR quota exceeded" in res.attempts[0].error


def test_rate_limited_provider_fails_fast_instead_of_burning_the_whole_ladder():
    """Found live 2026-08-01: Gemini's free-tier TTS enforces a 3-req/minute cap,
    separate from and tighter than this project's own daily GeminiBudget. Once hit,
    every remaining attempt in the ladder fails identically (retrying on a different
    voice cannot fix a quota window) — the gate must stop immediately rather than
    quarantine real content after 3 doomed attempts that never even reach the
    network."""
    tr = MockTranscriber(fail_with=RuntimeError(
        "ClientError: 429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests"
    ))
    nar = MockNarrator(transcriber=tr)
    res = QAGate(transcriber=tr).run(make_scene(), nar, PLAN)

    assert len(res.attempts) == 1                 # stopped after the first 429, not 3
    assert "RESOURCE_EXHAUSTED" in res.attempts[0].error


def test_verifier_quota_exhaustion_reports_unverified_not_quarantined():
    """The semantic distinction that matters most in this file: QUARANTINED means
    "we checked this and it failed"; UNVERIFIED means "we could not check it".

    Reporting a verifier outage as QUARANTINED misrepresents both the content AND
    the QA gate. Not hypothetical — Gemini's free tier allows only 20 ASR requests
    per DAY, so a public deployment genuinely runs out mid-day; before this fix
    every subsequent segment was labelled a quality failure and had its real,
    playable narration discarded, which also silently broke video export."""
    tr = MockTranscriber(fail_with=RuntimeError(
        "ClientError: 429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests"
    ))
    nar = MockNarrator(transcriber=tr)
    res = QAGate(transcriber=tr).run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.UNVERIFIED
    assert res.wer is None                        # genuinely never scored
    assert res.audio, "real narration must survive a verifier outage"
    assert res.audio_sha256


def test_narrator_failure_still_quarantines_with_no_audio():
    """The other side of the split: if NARRATION itself fails there is no audio at
    all, so UNVERIFIED would be a lie — that stays QUARANTINED."""
    tr = MockTranscriber(fail_with=RuntimeError("429 RESOURCE_EXHAUSTED"))
    nar = MockNarrator(transcriber=tr, fail_models=["riva-a", "riva-b", "strong-model"])
    res = QAGate(transcriber=tr).run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.QUARANTINED
    assert res.audio is None


def test_a_real_quality_failure_still_quarantines_even_with_audio_present():
    """Guards the new UNVERIFIED branch from being too broad: audio existing is not
    sufficient — the gate must have genuinely failed to VERIFY, not verified-and-
    rejected. A segment that was really scored and really failed stays QUARANTINED."""
    gate, nar, _ = build(["completely different words that will not match at all"],
                         config=QAConfig(max_attempts=1))
    res = gate.run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.QUARANTINED
    assert res.wer is not None                    # it really was scored


def test_non_rate_limit_errors_still_exhaust_the_full_retry_ladder():
    """Guards the fail-fast branch against being too broad: a real transient/quality
    failure (not a 429) must still get the full retry ladder, unchanged from before."""
    tr = MockTranscriber(fail_with=RuntimeError("connection reset by peer"))
    nar = MockNarrator(transcriber=tr)
    res = QAGate(transcriber=tr).run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.QUARANTINED
    assert len(res.attempts) == 3                 # full ladder, same as before this fix


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_no_transcriber_degrades_to_unverified_not_pending():
    """Audio still gets produced, but is explicitly marked ungraded.

    PENDING would be indistinguishable from 'not started' — an invisible failure,
    which docs/02 §11 rules out.
    """
    nar = MockNarrator()
    res = QAGate(transcriber=None).run(make_scene(), nar, PLAN)

    assert res.status is QAStatus.UNVERIFIED
    assert res.audio_sha256 is not None
    assert res.wer is None
    assert "no transcriber" in res.summary()


# ---------------------------------------------------------------------------
# Writing results back
# ---------------------------------------------------------------------------


def test_apply_to_updates_the_scene():
    gate, nar, _ = build(["el gato subió al techo", None])
    ls = make_scene()
    gate.run(ls, nar, PLAN).apply_to(ls)

    assert ls.qa_status is QAStatus.RETRIED
    assert ls.attempts == 2
    assert ls.voice_model == "riva-b"
    assert ls.audio_sha256 is not None
    assert ls.wer == 0.0


def test_summary_is_readable():
    gate, nar, _ = build()
    assert "pass after 1 attempt(s)" in gate.run(make_scene(), nar, PLAN).summary()


# ---------------------------------------------------------------------------
# Voice ladder
# ---------------------------------------------------------------------------


def test_voice_plan_ladder():
    p = VoicePlan(primary="a", alternates=["b", "c"], escalation="big")
    assert p.model_for(1, escalate=False) == "a"
    assert p.model_for(2, escalate=False) == "b"
    assert p.model_for(3, escalate=False) == "c"
    assert p.model_for(2, escalate=True) == "big"
    assert p.model_for(9, escalate=False) == "big"      # past the alternates


def test_voice_plan_without_escalation_falls_back_to_primary():
    p = VoicePlan(primary="only")
    assert p.model_for(5, escalate=True) == "only"
