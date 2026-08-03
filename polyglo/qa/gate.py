"""The cross-modal QA gate.

Generate narration, transcribe it back with a *different* model, diff the transcript
against the text that produced it. If Word Error Rate exceeds threshold the audio is
wrong, and the pipeline retries on another voice, escalates to a stronger model, and
finally quarantines the segment for a human.

Two design points worth stating explicitly:

**The verifier must not be the generator.** Transcribing with the same model family that
synthesised the audio is grading homework with its own author — correlated failures pass
straight through. The ``Transcriber`` protocol exists so the verifier is injected and can
be a different vendor entirely.

**Every attempt is recorded, including the ones that passed.** The retry history *is* the
demonstration. A gate that only ever reports PASS on attempt 1 proves nothing to anyone
watching; the interesting artifact is a segment that failed at 0.31, retried on a
different voice, and came back at 0.04.

What this gate does not measure: naturalness, prosody, or cultural appropriateness. It
catches intelligibility failures — mispronunciation, truncation, silence, wrong-language
drift. It shrinks the human review queue; it does not empty it. ``docs/01`` commits to
saying so out loud rather than overclaiming.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from polyglo.config import QAConfig
from polyglo.models import LocalizedScene, QAStatus
from polyglo.qa.wer import WERResult, score

__all__ = [
    "Transcriber",
    "Narrator",
    "NarrationResult",
    "VoicePlan",
    "Attempt",
    "GateResult",
    "QAGate",
    "MockTranscriber",
    "MockNarrator",
]


# ---------------------------------------------------------------------------
# Collaborator protocols
# ---------------------------------------------------------------------------


@dataclass
class NarrationResult:
    audio: bytes
    sha256: str
    model: str
    latency_ms: int = 0


@runtime_checkable
class Narrator(Protocol):
    """Synthesises speech. Implemented by the NVIDIA Riva path and by mocks."""

    def narrate(self, text: str, locale: str, model: str) -> NarrationResult: ...


@runtime_checkable
class Transcriber(Protocol):
    """Transcribes audio back to text.

    Deliberately a separate protocol from ``Narrator`` so the two are always distinct
    objects — it makes "verifier != generator" a structural property rather than a
    convention someone can quietly break.
    """

    name: str

    def transcribe(self, audio: bytes, locale: str) -> str: ...


# ---------------------------------------------------------------------------
# Voice ladder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoicePlan:
    """Which model to try at each rung of the escalation ladder."""

    primary: str
    alternates: Sequence[str] = ()
    escalation: str | None = None

    def model_for(self, attempt: int, *, escalate: bool) -> str:
        """Attempt 1 is primary. Later attempts take an alternate, unless the previous
        error was severe enough to skip straight to the escalation model."""
        if attempt <= 1:
            return self.primary
        if escalate and self.escalation:
            return self.escalation
        idx = attempt - 2
        if idx < len(self.alternates):
            return self.alternates[idx]
        return self.escalation or self.primary


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    n: int
    voice_model: str
    status: str                       # pass | retry | escalate | error
    wer: float | None = None
    transcript: str | None = None
    audio_sha256: str | None = None
    latency_ms: int = 0
    error: str | None = None
    audio: bytes | None = None        # in-memory only — QAEvent.from_gate_result
                                       # extracts named fields explicitly, so this
                                       # never reaches telemetry/parquet

    @property
    def ok(self) -> bool:
        return self.status == "pass"


@dataclass
class GateResult:
    status: QAStatus
    attempts: list[Attempt] = field(default_factory=list)
    wer: float | None = None
    transcript: str | None = None
    audio_sha256: str | None = None
    voice_model: str | None = None
    detail: WERResult | None = None
    audio: bytes | None = None        # the real bytes behind audio_sha256 — the
                                       # caller (orchestrator.py) uploads this to the
                                       # blob store; nothing in this module does, so
                                       # a caller that forgets is the one gap left

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def recovered(self) -> bool:
        """Passed, but not on the first try — the interesting case for the demo."""
        return self.status is QAStatus.RETRIED

    def apply_to(self, ls: LocalizedScene) -> LocalizedScene:
        ls.qa_status = self.status
        ls.wer = self.wer
        ls.attempts = self.attempt_count
        ls.transcript = self.transcript
        ls.voice_model = self.voice_model
        if self.audio_sha256:
            ls.audio_sha256 = self.audio_sha256
        # NOTE: this sha256 is only really fetchable once the caller uploads
        # self.audio to the blob store (see GateResult.audio) — apply_to() alone
        # does not make the audio playable, only records the hash the upload will
        # produce (content-addressed, so it's already known ahead of the upload).
        return ls

    def summary(self) -> str:
        if self.status is QAStatus.UNVERIFIED:
            return "unverified (no transcriber available)"
        w = f"{self.wer:.1%}" if self.wer is not None else "n/a"
        return f"{self.status.value} after {self.attempt_count} attempt(s), WER {w}"


def _is_rate_limit(exc: Exception) -> bool:
    """True if ``exc`` (wrapped from a Narrator/Transcriber provider call) looks like
    a provider rate-limit rejection rather than a real quality/transport failure.

    Found live (2026-08-01, WER-calibration probing): Gemini's free-tier TTS model
    enforces a hard 3-requests-per-minute cap, *separate* from and much tighter than
    this project's own daily `GeminiBudget` cap. Once hit, every subsequent attempt in
    the same `run()` call fails identically within milliseconds (no real network round
    trip happens) — retrying on a different voice cannot help, since the ladder's whole
    premise is "a different voice might sound better," not "wait for the quota window
    to reset." Continuing to burn attempts here would falsely quarantine perfectly good
    content as a *quality* failure and waste real, budget-tracked API calls on attempts
    that cannot possibly succeed.
    """
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class QAGate:
    def __init__(
        self,
        config: QAConfig | None = None,
        transcriber: Transcriber | None = None,
    ):
        self.config = config or QAConfig()
        self.transcriber = transcriber

    # -- decision ---------------------------------------------------------

    def classify(self, wer: float) -> str:
        """pass | retry | escalate, from the configured thresholds.

        Thresholds are config-driven because the defaults are an untested guess.
        Calibration against real samples is task #18 — a gate that never fires is
        theatre, one that always fires burns the credit budget on retries.
        """
        if wer <= self.config.wer_pass:
            return "pass"
        if wer <= self.config.wer_retry:
            return "retry"
        return "escalate"

    # -- execution --------------------------------------------------------

    def run(
        self,
        ls: LocalizedScene,
        narrator: Narrator,
        plan: VoicePlan,
    ) -> GateResult:
        """Narrate, verify, and retry until the audio passes or the budget runs out."""
        if self.transcriber is None:
            # Graceful degradation: produce the audio, mark it explicitly ungraded.
            # Silent PENDING would be indistinguishable from "not started", which is
            # exactly the kind of invisible failure docs/02 §11 rules out.
            nar = narrator.narrate(ls.text, ls.locale, plan.primary)
            return GateResult(
                status=QAStatus.UNVERIFIED,
                attempts=[Attempt(1, plan.primary, "pass",
                                  audio_sha256=nar.sha256,
                                  latency_ms=nar.latency_ms,
                                  audio=nar.audio)],
                audio_sha256=nar.sha256,
                voice_model=plan.primary,
                audio=nar.audio,
            )

        attempts: list[Attempt] = []
        escalate_next = False
        verifier_unavailable = False

        for n in range(1, self.config.max_attempts + 1):
            model = plan.model_for(n, escalate=escalate_next)
            started = time.perf_counter()

            # Narration and verification are caught SEPARATELY, deliberately: they
            # are different failures with different correct outcomes. A narrator
            # failure means there is no audio at all. A *verifier* failure means we
            # have real, playable audio that simply wasn't graded — throwing that
            # away (as one combined except block did) both lost real content and
            # mislabeled it as a quality failure.
            try:
                nar = narrator.narrate(ls.text, ls.locale, model)
            except Exception as exc:
                attempts.append(
                    Attempt(
                        n=n, voice_model=model, status="error",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                if _is_rate_limit(exc):
                    # Every remaining attempt would hit the same quota window and
                    # fail identically — stop rather than burn the rest of the
                    # retry ladder (and real API budget) on doomed attempts.
                    break
                escalate_next = True
                continue

            try:
                transcript = self.transcriber.transcribe(nar.audio, ls.locale)
            except Exception as exc:
                attempts.append(
                    Attempt(
                        n=n, voice_model=model, status="error",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        error=f"{type(exc).__name__}: {exc}",
                        audio_sha256=nar.sha256, audio=nar.audio,
                    )
                )
                if _is_rate_limit(exc):
                    verifier_unavailable = True
                    break
                escalate_next = True
                continue

            detail = score(ls.text, transcript, ls.locale)
            verdict = self.classify(detail.wer)
            attempts.append(
                Attempt(
                    n=n, voice_model=model, status=verdict,
                    wer=detail.wer, transcript=transcript,
                    audio_sha256=nar.sha256,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    audio=nar.audio,
                )
            )

            if verdict == "pass":
                return GateResult(
                    status=QAStatus.PASS if n == 1 else QAStatus.RETRIED,
                    attempts=attempts,
                    wer=detail.wer,
                    transcript=transcript,
                    audio_sha256=nar.sha256,
                    voice_model=model,
                    detail=detail,
                    audio=nar.audio,
                )

            escalate_next = verdict == "escalate"

        # Budget exhausted. Keep the best attempt so the review queue shows a human
        # the closest the pipeline got, not just the last thing it tried.
        scored = [a for a in attempts if a.wer is not None]
        best = min(scored, key=lambda a: a.wer) if scored else None

        # Nothing was ever actually graded, and the reason was the VERIFIER being
        # unavailable (quota/rate limit) rather than bad audio. That is UNVERIFIED,
        # not QUARANTINED — the distinction is the difference between "we checked
        # this and it failed" and "we could not check this", and reporting the
        # latter as the former is a real misrepresentation of the content AND of
        # the QA gate itself. Matches the no-transcriber-configured branch above.
        #
        # This is not hypothetical: Gemini's free tier allows only 20 ASR requests
        # per DAY, so a public deployment genuinely runs out mid-day, and every
        # subsequent segment was being labelled a quality failure and having its
        # real, playable narration discarded.
        if best is None and verifier_unavailable:
            with_audio = next((a for a in reversed(attempts) if a.audio), None)
            if with_audio is not None:
                return GateResult(
                    status=QAStatus.UNVERIFIED,
                    attempts=attempts,
                    audio_sha256=with_audio.audio_sha256,
                    voice_model=with_audio.voice_model,
                    audio=with_audio.audio,
                )

        return GateResult(
            status=QAStatus.QUARANTINED,
            attempts=attempts,
            wer=best.wer if best else None,
            transcript=best.transcript if best else None,
            audio_sha256=best.audio_sha256 if best else None,
            voice_model=best.voice_model if best else None,
            audio=best.audio if best else None,
        )


# ---------------------------------------------------------------------------
# Test doubles
#
# These live in the package rather than the test suite because the FastAPI app uses
# them in no-credentials mode — the whole UI is demonstrable before a key exists.
# ---------------------------------------------------------------------------


class MockTranscriber:
    """Returns scripted transcripts, so gate behaviour is deterministic.

    ``transcripts`` is consumed one entry per call; the last entry repeats once
    exhausted. Pass ``None`` for "heard it perfectly".
    """

    name = "mock-transcriber"

    def __init__(self, transcripts: Sequence[str | None] | None = None,
                 fail_with: Exception | None = None):
        self._scripted = list(transcripts or [None])
        self._calls = 0
        self._fail_with = fail_with
        self.last_text: str | None = None

    def transcribe(self, audio: bytes, locale: str) -> str:
        if self._fail_with is not None:
            raise self._fail_with
        idx = min(self._calls, len(self._scripted) - 1)
        self._calls += 1
        scripted = self._scripted[idx]
        # None means "perfect": echo back whatever the narrator was given.
        return self.last_text if scripted is None else scripted


class MockNarrator:
    """Deterministic fake TTS. Audio bytes derive from the text so hashes are stable."""

    def __init__(self, transcriber: MockTranscriber | None = None,
                 fail_models: Sequence[str] = ()):
        self.transcriber = transcriber
        self.fail_models = set(fail_models)
        self.calls: list[tuple[str, str, str]] = []

    def narrate(self, text: str, locale: str, model: str) -> NarrationResult:
        import hashlib

        self.calls.append((text, locale, model))
        if model in self.fail_models:
            raise RuntimeError(f"provider {model} unavailable")
        if self.transcriber is not None:
            self.transcriber.last_text = text
        payload = f"{model}|{locale}|{text}".encode()
        return NarrationResult(
            audio=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            model=model,
            latency_ms=1,
        )
