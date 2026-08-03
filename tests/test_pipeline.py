"""Tests for the Genblaze pipeline wrapper.

Uses Genblaze's own ``MockProvider`` — a real ``Pipeline().step().run()`` executes,
just against a fake provider. This is the same pattern the FastAPI app uses in
no-credentials mode, so these tests double as proof that path works.
"""

from __future__ import annotations

import pytest

from genblaze_core import Modality
from genblaze_core.mocks import MockProvider
from genblaze_core.models import Asset, ProviderErrorCode

from polyglo.pipeline import make_sink, manifest_report, run_step, verify_manifest


def make_asset(sha: str = "b" * 64) -> Asset:
    return Asset(
        asset_id="a1",
        url="file:///tmp/a1.wav",
        media_type="audio/wav",
        sha256=sha,
        size_bytes=1234,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_step_succeeds_with_mock_provider():
    provider = MockProvider(name="mock-tts", assets=[make_asset()], cost_usd=0.002)
    outcome = run_step(
        provider, model="mock-voice-a", prompt="hola mundo",
        modality=Modality.AUDIO, timeout=30, preflight=False,
    )

    assert outcome.ok is True
    assert outcome.model_used == "mock-voice-a"
    assert outcome.model_requested == "mock-voice-a"
    assert outcome.fell_back is False
    assert outcome.cost_usd == pytest.approx(0.002)
    assert len(outcome.assets) == 1
    assert outcome.canonical_hash is not None
    assert outcome.run_id


def test_manifest_is_built_with_zero_api_calls():
    """The provenance layer must work before any key exists."""
    provider = MockProvider(assets=[make_asset()])
    outcome = run_step(provider, model="m", prompt="p", modality=Modality.AUDIO,
                       preflight=False)
    assert outcome.manifest is not None
    assert verify_manifest(outcome.manifest) is True


def test_manifest_report_distinguishes_failure_modes():
    provider = MockProvider(assets=[make_asset()])
    outcome = run_step(provider, model="m", prompt="p", modality=Modality.AUDIO,
                       preflight=False)
    report = manifest_report(outcome.manifest)
    assert report["verified"] is True
    assert report["hash_ok"] is True
    assert report["assets_missing_sha256"] == []
    assert report["detail"] == "verified"


def test_manifest_report_flags_assets_missing_sha256():
    bad_asset = Asset(asset_id="a2", url="file:///tmp/a2.wav",
                      media_type="audio/wav", sha256=None, size_bytes=99)
    provider = MockProvider(assets=[bad_asset])
    outcome = run_step(provider, model="m", prompt="p", modality=Modality.AUDIO,
                       preflight=False)
    report = manifest_report(outcome.manifest)
    assert report["verified"] is False
    assert len(report["assets_missing_sha256"]) >= 1


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_run_step_reports_provider_failure_without_raising():
    provider = MockProvider(should_fail=True, error_code=ProviderErrorCode.UNKNOWN,
                            error_message="synthetic outage")
    outcome = run_step(provider, model="flaky", prompt="p", modality=Modality.AUDIO,
                       preflight=False)
    assert outcome.ok is False
    assert outcome.error is not None


def test_run_step_never_raises_on_a_malformed_provider():
    """A pipeline wrapper that can throw defeats the point of wrapping it.

    Genblaze itself rejects a non-BaseProvider at .step() time with a TypeError —
    which happens BEFORE .run(). That call must be inside the wrapper's try/except
    too, or this exact mistake would crash the caller instead of returning a
    failed StepOutcome. (Caught a real bug here: .step() was originally outside
    the try block.)
    """

    class NotAProvider:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    outcome = run_step(NotAProvider(), model="x", prompt="p", modality=Modality.AUDIO,
                       preflight=False)
    assert outcome.ok is False
    assert "BaseProvider" in outcome.error


# ---------------------------------------------------------------------------
# fell_back — the evidence behind the failover demo
# ---------------------------------------------------------------------------


def test_fell_back_is_false_when_primary_succeeds():
    provider = MockProvider(assets=[make_asset()])
    outcome = run_step(provider, model="primary", prompt="p", modality=Modality.AUDIO,
                       preflight=False)
    assert outcome.fell_back is False


def test_summary_reports_cost_and_asset_count():
    provider = MockProvider(assets=[make_asset(), make_asset("c" * 64)], cost_usd=0.01)
    outcome = run_step(provider, model="m", prompt="p", modality=Modality.AUDIO,
                       preflight=False)
    s = outcome.summary()
    assert "2 asset(s)" in s
    assert "$0.0100" in s


def test_failed_summary_includes_error():
    provider = MockProvider(should_fail=True, error_message="rate limited")
    outcome = run_step(provider, model="m", prompt="p", modality=Modality.AUDIO,
                       preflight=False)
    assert "FAILED" in outcome.summary()


# ---------------------------------------------------------------------------
# Sink construction
# ---------------------------------------------------------------------------


def test_make_sink_returns_none_without_b2_credentials(tmp_path):
    from polyglo.config import Config, B2Config, QAConfig, GeminiConfig

    cfg = Config(
        b2=B2Config("", "", "b", ""), qa=QAConfig(), gemini=GeminiConfig(),
        nvidia_api_key="", gemini_api_key="", openrouter_api_key="",
        data_dir=tmp_path, db_path=tmp_path / "p.db",
    )
    assert make_sink(cfg) is None


def test_pipeline_runs_without_a_sink():
    """No B2 configured must not prevent execution — only persistence."""
    provider = MockProvider(assets=[make_asset()])
    outcome = run_step(provider, model="m", prompt="p", modality=Modality.AUDIO,
                       sink=None, preflight=False)
    assert outcome.ok is True
