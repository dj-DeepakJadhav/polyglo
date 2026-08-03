"""Word Error Rate, PCTS, and word-level alignment.

WER is the QA gate's decision variable, and the alignment is what the UI renders in the
diff panel. Both come from one Levenshtein pass with backtrace — computing the score
without the alignment would mean a judge sees "WER 0.31" and no explanation of why.

    WER = (substitutions + deletions + insertions) / len(reference)

Two properties worth knowing before reading a number:

- **WER is not capped at 1.0.** A hypothesis longer than the reference can exceed it
  through insertions. That is standard and intentional; a runaway TTS that appends
  garbage *should* score worse than one that merely got every word wrong.
- **Reference length is the denominator**, so short sentences are high-variance. One
  wrong word in a five-word sentence is WER 0.20 — already past our pass threshold.
  This is why thresholds need calibration against real samples (task #18) rather than
  being reasoned about in the abstract.

PCTS (percentage of completely correct transcribed sentences) is the corpus-level
companion metric from the TTS-evaluation literature: the fraction of segments that came
back *exactly* right. It is harsher than mean WER and much harder to game.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from polyglo.qa.normalize import normalize, tokenize

__all__ = ["Op", "AlignmentOp", "WERResult", "score", "wer_tokens", "pcts"]


class Op(str, Enum):
    EQUAL = "equal"
    SUB = "sub"
    INS = "ins"     # present in hypothesis, absent from reference
    DEL = "del"     # present in reference, absent from hypothesis


@dataclass(frozen=True)
class AlignmentOp:
    op: Op
    ref: str | None
    hyp: str | None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.op.value}({self.ref!r}->{self.hyp!r})"


@dataclass
class WERResult:
    wer: float
    hits: int
    substitutions: int
    deletions: int
    insertions: int
    ref_len: int
    hyp_len: int
    alignment: list[AlignmentOp] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def exact(self) -> bool:
        """Contributes to PCTS."""
        return self.errors == 0

    @property
    def accuracy(self) -> float:
        return self.hits / self.ref_len if self.ref_len else 0.0

    def summary(self) -> str:
        return (
            f"WER {self.wer:.1%} ({self.substitutions}S {self.deletions}D "
            f"{self.insertions}I over {self.ref_len} words)"
        )

    def diff_pairs(self) -> list[tuple[str, str, str]]:
        """(op, reference_word, heard_word) triples for the UI diff panel."""
        return [(a.op.value, a.ref or "", a.hyp or "") for a in self.alignment]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def wer_tokens(reference: list[str], hypothesis: list[str]) -> WERResult:
    """Levenshtein over token lists, with backtrace.

    Substitution, insertion and deletion all cost 1 — the standard WER weighting.
    """
    n, m = len(reference), len(hypothesis)

    # Degenerate cases. An empty reference has no denominator; we report 0.0 when the
    # hypothesis is also empty and 1.0 otherwise, rather than dividing by zero or
    # returning inf, so the gate has a usable number either way.
    if n == 0:
        return WERResult(
            wer=0.0 if m == 0 else 1.0,
            hits=0, substitutions=0, deletions=0, insertions=m,
            ref_len=0, hyp_len=m,
            alignment=[AlignmentOp(Op.INS, None, h) for h in hypothesis],
        )

    # d[i][j] = edit distance between reference[:i] and hypothesis[:j]
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j

    for i in range(1, n + 1):
        ref_i = reference[i - 1]
        row, prev = d[i], d[i - 1]
        for j in range(1, m + 1):
            if ref_i == hypothesis[j - 1]:
                row[j] = prev[j - 1]
            else:
                row[j] = 1 + min(prev[j - 1], prev[j], row[j - 1])

    # Backtrace. Ties are resolved diagonal-first so aligned words stay paired in the
    # diff panel instead of being split into a delete plus an insert.
    alignment: list[AlignmentOp] = []
    hits = subs = dels = ins = 0
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and reference[i - 1] == hypothesis[j - 1] and d[i][j] == d[i - 1][j - 1]:
            alignment.append(AlignmentOp(Op.EQUAL, reference[i - 1], hypothesis[j - 1]))
            hits += 1
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + 1:
            alignment.append(AlignmentOp(Op.SUB, reference[i - 1], hypothesis[j - 1]))
            subs += 1
            i, j = i - 1, j - 1
        elif i > 0 and d[i][j] == d[i - 1][j] + 1:
            alignment.append(AlignmentOp(Op.DEL, reference[i - 1], None))
            dels += 1
            i -= 1
        else:
            alignment.append(AlignmentOp(Op.INS, None, hypothesis[j - 1]))
            ins += 1
            j -= 1

    alignment.reverse()
    return WERResult(
        wer=(subs + dels + ins) / n,
        hits=hits, substitutions=subs, deletions=dels, insertions=ins,
        ref_len=n, hyp_len=m,
        alignment=alignment,
    )


def score(expected: str, transcript: str, locale: str = "en-US") -> WERResult:
    """Normalise both sides, then score.

    Both strings go through the *same* normalisation — that is the whole contract.
    Scoring raw strings would measure ASR spelling conventions rather than audio quality.
    """
    ref = tokenize(normalize(expected, locale), locale)
    hyp = tokenize(normalize(transcript, locale), locale)
    return wer_tokens(ref, hyp)


def pcts(results: list[WERResult]) -> float:
    """Fraction of segments transcribed exactly right."""
    if not results:
        return 0.0
    return sum(1 for r in results if r.exact) / len(results)
