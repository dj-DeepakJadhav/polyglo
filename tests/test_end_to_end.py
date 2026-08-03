"""The capstone end-to-end test: one story, four locales, both failure modes.

Everything here is already proven piecemeal in test_orchestrator.py — one focused
assertion per test, which is the right shape for pinpointing a regression. This file
exists for a different reason: it's the single narrative that proves the *whole system
coheres* as one story, entirely on mock/simulated providers, with zero API keys and
zero real generation cost. If you read only one test file to understand whether
Polyglo actually works end to end, read this one.

The story: 3 scenes, 4 locales (the project's actual DEFAULT_LOCALES). One specific
scene/locale cell fails its first narration attempt and recovers on the second
(retry-and-recover — the 40 seconds the demo video leans on hardest). A different
cell fails every attempt and gets quarantined. Everything else passes cleanly on the
first try. All of it is asserted together: the dedup invariant, the QA outcomes, the
bundle contents, the telemetry, and the retry evidence query.
"""

from __future__ import annotations

import json

import pytest

from polyglo import db as dbm
from polyglo.chat import ChatCompleter
from polyglo.models import DEFAULT_LOCALES, QAStatus, Story
from polyglo.narrate import SimulatedNarrator
from polyglo.orchestrator import Providers, run_story_pipeline
from polyglo.qa.gate import Transcriber, VoicePlan
from polyglo.store import BlobStore, LocalBackend
from polyglo.telemetry import TelemetryStore
from polyglo.visuals import SimulatedVisualGenerator

N_SCENES = 3
LOCALES = list(DEFAULT_LOCALES)          # the project's real default locale set
assert len(LOCALES) == 4, "this test is written around exactly 4 default locales"

# The two engineered failure points, chosen from the real locale/scene space.
RETRY_LOCALE, RETRY_ORDINAL = LOCALES[0], 1     # fails once, recovers on attempt 2
QUARANTINE_LOCALE, QUARANTINE_ORDINAL = LOCALES[1], 2   # fails every attempt


def _scenes_json(n: int) -> str:
    return json.dumps({
        "style_guide": "a small orange tabby cat, flat children's-book watercolor style",
        "scenes": [
            {"text": f"Scene {i} narrative text here.", "visual_prompt": f"illustration of scene {i}"}
            for i in range(n)
        ]
    })


class NarrativeChatCompleter:
    """Splits the story once, then produces a distinct, per-scene plausible
    translation for every locale — never a fixed string, so scenes never
    accidentally collide and dedupe against each other (see docs/SESSION-LOG.md's
    "translation-collapse" entries for why a fixed string is the wrong choice here).
    """

    def complete(self, prompt: str, *, model: str) -> str:
        import re

        # Dispatch on prompt content, not call order — task #25 inserted a grading
        # call before the split call.
        if "Correct any spelling and grammar errors" in prompt:
            return json.dumps({"corrected_text": "a graded source story"})
        if "Split this story into" in prompt:
            return _scenes_json(N_SCENES)
        match = re.search(r"Scene (\d+) narrative", prompt)
        idx = match.group(1) if match else "x"
        return f"scene {idx} translated with enough distinct words to be its own text"


def _near_miss(text: str) -> str:
    """Swap two words so WER lands in the (0.10, 0.25] RETRY band, not past 0.25
    (which would ESCALATE straight past a retry — the classify() thresholds are
    docs/02 §6.3's, see qa/gate.py). Verified directly: 2 substitutions over the
    12-token source text scores WER 0.167.

    Deliberately no hyphens in the substitutes — normalize.py splits hyphenated
    words into multiple tokens (needed for real languages like French), which
    would silently inflate this to 3+ word errors instead of the intended 2 and
    push it past ESCALATE. Caught by computing the WER directly rather than
    guessing at what "a small corruption" would score.
    """
    words = text.split()
    if len(words) >= 2:
        words[1] = "wrongnumber"
        words[-1] = "wrongword"
    return " ".join(words)


class ScriptedFailureTranscriber:
    """Decodes the real text out of SimulatedNarrator's payload (a deterministic
    encoding of model|locale|text, not real audio) and returns it verbatim — UNLESS
    the (locale, ordinal) pair is scripted to fail, in which case it returns a
    corrupted transcript for a bounded number of attempts before self-correcting.

    ``fail_until`` maps (locale, ordinal) -> (attempts_to_fail, "retry" | "quarantine").
    "retry" corruption is a near-miss (lands in the RETRY WER band); "quarantine"
    corruption is unrelated garbage (lands past ESCALATE, and persists past
    max_attempts so the segment never recovers). This is what turns "always passes"
    into an engineered retry-and-recover plus an engineered quarantine, without
    needing a real ASR call.
    """

    def __init__(self, fail_until: dict[tuple[str, int], tuple[int, str]]):
        self.fail_until = dict(fail_until)
        self._attempt_counts: dict[tuple[str, int], int] = {}

    def transcribe(self, audio: bytes, locale: str) -> str:
        parts = audio.decode().split("|", 3)
        text = parts[3] if len(parts) == 4 else ""

        match = None
        for i in range(N_SCENES):
            if f"scene {i} translated" in text:
                match = (locale, i)
                break

        if match and match in self.fail_until:
            count = self._attempt_counts.get(match, 0) + 1
            self._attempt_counts[match] = count
            attempts_to_fail, mode = self.fail_until[match]
            if count <= attempts_to_fail:
                if mode == "retry":
                    return _near_miss(text)
                return "totally unrelated garbled nonsense that will not match anything"

        return text


@pytest.fixture()
def conn(tmp_path):
    c = dbm.connect(tmp_path / "e2e.db")
    dbm.init_db(c)
    yield c
    c.close()


@pytest.fixture()
def blob_store(tmp_path):
    return BlobStore(LocalBackend(tmp_path / "blobs"))


@pytest.fixture()
def telemetry(tmp_path):
    return TelemetryStore(tmp_path / "telemetry")


@pytest.fixture()
def providers() -> Providers:
    transcriber: Transcriber = ScriptedFailureTranscriber({
        (RETRY_LOCALE, RETRY_ORDINAL): (1, "retry"),          # near-miss once, then correct
        (QUARANTINE_LOCALE, QUARANTINE_ORDINAL): (99, "quarantine"),  # never recovers
    })
    chat: ChatCompleter = NarrativeChatCompleter()
    return Providers(
        chat=chat,
        visuals=SimulatedVisualGenerator(),
        narrator=SimulatedNarrator(),
        transcriber=transcriber,
        chat_model="mock-chat", visual_model="mock-image",
        voice_plan=VoicePlan(primary="voice-a", alternates=["voice-b"],
                             escalation="voice-strong"),
    )


def test_full_story_across_four_locales_with_a_retry_and_a_quarantine(
    conn, blob_store, telemetry, providers,
):
    story = Story.create("The Lost Umbrella", cefr="B1")

    outcome = run_story_pipeline(
        story, "source story text", N_SCENES, LOCALES,
        conn, blob_store, telemetry, providers,
    )

    # ---- shape of the run --------------------------------------------------
    assert len(outcome.bundles) == 4
    assert {b.locale for b in outcome.bundles} == set(LOCALES)

    # ---- the dedup invariant: images generated ONCE, shared by every locale ----
    # 3 scenes x 4 locales = 12 image references, but only 3 unique images.
    assert outcome.dedup.total_refs > outcome.dedup.unique_blobs
    saved = dbm.get_story(conn, story.story_id)
    unique_image_hashes = {s.image_sha256 for s in saved.scenes}
    assert len(unique_image_hashes) == N_SCENES

    # ---- the engineered retry-and-recover ----------------------------------
    localized = dbm.get_localized(conn, story.story_id, RETRY_LOCALE)
    retry_scene = next(ls for ls in localized if ls.ordinal == RETRY_ORDINAL)
    assert retry_scene.qa_status is QAStatus.RETRIED
    assert retry_scene.attempts == 2
    assert retry_scene.audio_sha256 is not None   # it DID eventually produce audio

    retry_attempts = telemetry.attempts_for(story.story_id, RETRY_LOCALE, RETRY_ORDINAL)
    assert [a["status"] for a in retry_attempts] == ["retry", "pass"]
    assert retry_attempts[0]["voice_model"] != retry_attempts[1]["voice_model"], (
        "recovery must actually have switched voices, not retried the same one"
    )

    # ---- the engineered quarantine ------------------------------------------
    localized_q = dbm.get_localized(conn, story.story_id, QUARANTINE_LOCALE)
    quarantined_scene = next(ls for ls in localized_q if ls.ordinal == QUARANTINE_ORDINAL)
    assert quarantined_scene.qa_status is QAStatus.QUARANTINED
    assert quarantined_scene.attempts == 3        # exhausted the default max_attempts
    assert outcome.quarantined >= 1

    # The quarantined scene's audio must be EXCLUDED from its locale's bundle —
    # a genuine failure, unlike UNVERIFIED, really shouldn't ship.
    q_bundle = next(b for b in outcome.bundles if b.locale == QUARANTINE_LOCALE)
    assert quarantined_scene.audio_sha256 not in q_bundle.audio_refs

    # ---- everything else passed cleanly on the first attempt -----------------
    clean_cells = [
        (locale, ls)
        for locale in LOCALES
        for ls in dbm.get_localized(conn, story.story_id, locale)
        if (locale, ls.ordinal) not in {(RETRY_LOCALE, RETRY_ORDINAL),
                                        (QUARANTINE_LOCALE, QUARANTINE_ORDINAL)}
    ]
    assert len(clean_cells) == N_SCENES * len(LOCALES) - 2
    assert all(ls.qa_status is QAStatus.PASS and ls.attempts == 1 for _, ls in clean_cells)

    # ---- telemetry reflects the whole story, not just one cell ---------------
    retry_evidence = telemetry.qa_retry_evidence()
    assert any(
        r["locale"] == RETRY_LOCALE and r["ordinal"] == RETRY_ORDINAL and r["attempts"] == 2
        for r in retry_evidence
    )
    qa_eff = {row["status"]: row["n"] for row in telemetry.qa_effectiveness()}
    assert qa_eff.get("pass", 0) >= N_SCENES * len(LOCALES) - 2   # the clean cells
    assert qa_eff.get("retry", 0) >= 1     # at least the engineered retry's first attempt

    # ---- provenance: every stored image is exactly what its hash claims -------
    for sha in unique_image_hashes:
        assert blob_store.verify(sha) is True
