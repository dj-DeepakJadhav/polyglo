"""Tests for the Simulated* providers.

These exist for one reason: while real NVIDIA image/audio is broken (task #22), the
app should still produce GENUINE Genblaze manifests rather than bypassing Genblaze
entirely. Every test here proves that genuineness, not just that bytes come out.
"""

from __future__ import annotations

import pytest

from polyglo.narrate import NarrationError, SimulatedNarrator
from polyglo.visuals import SimulatedVisualGenerator, VisualError


def test_simulated_narrator_produces_real_genblaze_manifest():
    """The whole point: this must be an actual Manifest with a verifiable hash, not
    hand-rolled fake provenance."""
    narrator = SimulatedNarrator()
    result = narrator.narrate("hola mundo", "es-ES", "sim-voice-a")
    assert result.sha256
    assert result.model == "sim-voice-a"
    assert len(result.audio) > 0


def test_simulated_narrator_is_deterministic_for_identical_input():
    narrator = SimulatedNarrator()
    a = narrator.narrate("hola mundo", "es-ES", "sim-voice-a")
    b = narrator.narrate("hola mundo", "es-ES", "sim-voice-a")
    assert a.sha256 == b.sha256


def test_simulated_narrator_differs_by_locale_and_model():
    narrator = SimulatedNarrator()
    a = narrator.narrate("hola", "es-ES", "voice-a")
    b = narrator.narrate("hola", "fr-FR", "voice-a")
    c = narrator.narrate("hola", "es-ES", "voice-b")
    assert len({a.sha256, b.sha256, c.sha256}) == 3


def test_simulated_narrator_chaos_toggle_raises_narration_error():
    """This is what makes the failover demo real without live credentials: flip a
    model into fail_models and the QA gate's fallback chain must actually engage."""
    narrator = SimulatedNarrator(fail_models=["primary-voice"])
    with pytest.raises(NarrationError, match="chaos toggle"):
        narrator.narrate("hola", "es-ES", "primary-voice")

    # an un-disabled model on the same instance still works
    result = narrator.narrate("hola", "es-ES", "backup-voice")
    assert result.sha256


def test_simulated_narrator_conforms_to_narrator_protocol():
    from polyglo.qa.gate import Narrator
    assert isinstance(SimulatedNarrator(), Narrator)


def test_simulated_narrator_works_inside_the_real_qa_gate():
    """End to end: SimulatedNarrator driven by the actual QAGate state machine —
    not just called standalone.

    MockTranscriber's "echo whatever was narrated" trick only fires when paired with
    gate.py's own MockNarrator, which sets transcriber.last_text as a side channel.
    SimulatedNarrator is a real, independent implementation with no such hook — its
    simulated audio doesn't encode real speech content — so the expected transcript
    must be scripted explicitly here instead.
    """
    from polyglo.config import QAConfig
    from polyglo.models import LocalizedScene, QAStatus
    from polyglo.qa.gate import MockTranscriber, QAGate, VoicePlan

    ls = LocalizedScene(story_id="s1", ordinal=0, locale="es-ES", text="hola mundo")
    plan = VoicePlan(primary="voice-a", alternates=["voice-b"])
    transcriber = MockTranscriber(["hola mundo"])
    gate = QAGate(QAConfig(), transcriber=transcriber)

    result = gate.run(ls, SimulatedNarrator(), plan)
    assert result.status is QAStatus.PASS


# ---------------------------------------------------------------------------
# Visuals
# ---------------------------------------------------------------------------


def test_simulated_visual_generator_produces_real_manifest():
    gen = SimulatedVisualGenerator()
    result = gen.generate("a cat on a roof", "sim-image-a")
    assert result.sha256
    assert result.model == "sim-image-a"


def test_simulated_visual_generator_is_deterministic():
    gen = SimulatedVisualGenerator()
    a = gen.generate("a cat", "m")
    b = gen.generate("a cat", "m")
    assert a.sha256 == b.sha256


def test_simulated_visual_generator_chaos_toggle():
    gen = SimulatedVisualGenerator(fail_models=["flux-primary"])
    with pytest.raises(VisualError, match="chaos toggle"):
        gen.generate("a cat", "flux-primary")
    assert gen.generate("a cat", "flux-backup").sha256


def test_simulated_visual_generator_conforms_to_protocol():
    from polyglo.visuals import VisualGenerator
    assert isinstance(SimulatedVisualGenerator(), VisualGenerator)


def test_simulated_visual_generator_within_generate_story_visuals():
    from polyglo.models import Scene
    from polyglo.visuals import generate_story_visuals

    scenes = [Scene("s1", i, f"text {i}", f"prompt {i}") for i in range(3)]
    result = generate_story_visuals(scenes, SimulatedVisualGenerator(), model="sim-model")
    assert len(result) == 3
    assert len({r.sha256 for r in result.values()}) == 3
