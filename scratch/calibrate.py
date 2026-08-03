"""WER Threshold Calibration Utility.

Benchmarks Word Error Rate (WER) scoring against reference texts with various
synthetic mutations (exact match, single word drop, substitution, numeral expansion)
to evaluate the current QA gate thresholds (pass <= 0.10, retry <= 0.25, quarantine > 0.25).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

from polyglo.config import get_config
from polyglo.qa.wer import score


TEST_CASES = [
    {
        "name": "Exact match",
        "ref": "The quick brown fox jumps over the lazy dog",
        "hyp": "The quick brown fox jumps over the lazy dog",
        "expected_verdict": "pass",
    },
    {
        "name": "Single word insertion (10 words total)",
        "ref": "The quick brown fox jumps over the lazy dog today",
        "hyp": "The quick brown fox jumps over the very lazy dog today",
        "expected_verdict": "pass",
    },
    {
        "name": "Single word deletion (10 words total)",
        "ref": "The quick brown fox jumps over the lazy dog today",
        "hyp": "The quick brown fox jumps over lazy dog today",
        "expected_verdict": "retry",
    },
    {
        "name": "Two word substitutions (10 words total)",
        "ref": "The quick brown fox jumps over the lazy dog today",
        "hyp": "The fast brown fox leaps over the lazy dog today",
        "expected_verdict": "retry",
    },
    {
        "name": "Severe truncation (5 of 10 words missing)",
        "ref": "The quick brown fox jumps over the lazy dog today",
        "hyp": "The quick brown fox jumps",
        "expected_verdict": "quarantine",
    },
    {
        "name": "Numeral expansion ('3' vs 'three')",
        "ref": "There are 3 umbrellas in the garden",
        "hyp": "There are three umbrellas in the garden",
        "expected_verdict": "pass",
    },
]


def run_calibration() -> None:
    cfg = get_config().qa
    print("=" * 60)
    print("POLYGLO WER QA GATE THRESHOLD CALIBRATION")
    print(f"Configured Thresholds: Pass <= {cfg.wer_pass:.2f} | Retry <= {cfg.wer_retry:.2f} | Max Attempts = {cfg.max_attempts}")
    print("=" * 60)

    passed_tests = 0
    for case in TEST_CASES:
        res = score(case["ref"], case["hyp"], locale="en-US")
        wer = res.wer
        if wer <= cfg.wer_pass:
            verdict = "pass"
        elif wer <= cfg.wer_retry:
            verdict = "retry"
        else:
            verdict = "quarantine"

        status_icon = "OK" if verdict == case["expected_verdict"] else "X"
        if verdict == case["expected_verdict"]:
            passed_tests += 1

        print(f"\n[{status_icon}] Test Case: {case['name']}")
        print(f"    Reference:  {case['ref']!r}")
        print(f"    Hypothesis: {case['hyp']!r}")
        print(f"    WER Score:  {wer:.3f} (S: {res.substitutions}, D: {res.deletions}, I: {res.insertions}, N: {res.ref_len})")
        print(f"    Verdict:    {verdict.upper()} (Expected: {case['expected_verdict'].upper()})")

    print("\n" + "=" * 60)
    print(f"Calibration Complete: {passed_tests}/{len(TEST_CASES)} cases aligned with expected verdicts.")
    print("=" * 60)


if __name__ == "__main__":
    run_calibration()
