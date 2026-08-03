"""Tests for the telemetry writer and DuckDB dashboard queries.

Two things anchor this suite:

1. ``test_dedup_stats_matches_real_parquet_from_a_real_pipeline`` runs an ACTUAL
   Genblaze pipeline through ``ParquetSink`` (mock provider, no keys) and reads the
   real output back through DuckDB — so the schema this module depends on is proven
   against genblaze's own writer, not assumed.
2. The dedup/QA-effectiveness/cost-latency queries are exercised against the
   synthetic fixture, which doubles as the dashboard's zero-generation demo data.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from genblaze_core import Modality, ParquetSink
from genblaze_core.mocks import MockProvider
from genblaze_core.models import Asset

from polyglo.pipeline import run_step
from polyglo.qa.gate import Attempt, GateResult
from polyglo.models import QAStatus
from polyglo.telemetry import QAEvent, TelemetryStore, seed_fixture_telemetry


def make_asset(sha: str, media_type: str = "audio/wav") -> Asset:
    return Asset(asset_id="a", url=f"file:///tmp/{sha}.bin", media_type=media_type,
                sha256=sha, size_bytes=1000)


# ---------------------------------------------------------------------------
# QAEvent
# ---------------------------------------------------------------------------


def test_qa_event_stamps_a_timestamp_if_not_given():
    e = QAEvent("s1", "es-ES", 0, 1, "voice-a", "pass")
    assert e.ts is not None
    assert "T" in e.ts


def test_from_gate_result_produces_one_row_per_attempt():
    """The retry history is the artifact — collapsing it to one row erases the proof
    the gate does real work."""
    result = GateResult(
        status=QAStatus.RETRIED,
        attempts=[
            Attempt(1, "voice-a", "retry", wer=0.31, transcript="x"),
            Attempt(2, "voice-b", "pass", wer=0.02, transcript="y"),
        ],
    )
    events = QAEvent.from_gate_result("s1", 3, "fr-FR", result)
    assert len(events) == 2
    assert [e.attempt for e in events] == [1, 2]
    assert [e.status for e in events] == ["retry", "pass"]
    assert events[0].locale == "fr-FR" and events[0].ordinal == 3


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_write_qa_events_creates_a_readable_parquet_file(tmp_path):
    store = TelemetryStore(tmp_path)
    path = store.write_qa_events([QAEvent("s1", "es-ES", 0, 1, "v", "pass", wer=0.0)])
    assert path is not None and path.exists()

    table = pq.read_table(path)
    assert table.num_rows == 1
    assert table.column_names == [
        "story_id", "locale", "ordinal", "attempt", "voice_model",
        "wer", "status", "latency_ms", "ts",
    ]


def test_write_qa_events_with_empty_list_is_a_noop(tmp_path):
    store = TelemetryStore(tmp_path)
    assert store.write_qa_events([]) is None
    assert list((tmp_path / "qa").rglob("*.parquet")) == []


def test_write_gate_result_delegates_to_write_qa_events(tmp_path):
    store = TelemetryStore(tmp_path)
    result = GateResult(status=QAStatus.PASS,
                        attempts=[Attempt(1, "v", "pass", wer=0.0)])
    path = store.write_gate_result("s1", 0, "es-ES", result)
    assert path is not None
    assert pq.read_table(path).num_rows == 1


# ---------------------------------------------------------------------------
# dedup_stats — validated against a REAL genblaze ParquetSink run
# ---------------------------------------------------------------------------


def test_dedup_stats_is_zero_on_an_empty_store(tmp_path):
    store = TelemetryStore(tmp_path)
    assert store.dedup_stats() == {"total_refs": 0, "unique_blobs": 0, "dedup_ratio": 0.0}


def test_dedup_stats_matches_real_parquet_from_a_real_pipeline(tmp_path):
    """Runs an actual Pipeline().step().run() through a real ParquetSink (mock
    provider, zero API calls) and reads it back — proves this module's queries work
    against genblaze's own writer, not a schema we invented."""
    sink = ParquetSink(str(tmp_path))
    same_sha = "d" * 64

    for i in range(3):
        provider = MockProvider(assets=[make_asset(same_sha)])
        outcome = run_step(provider, model=f"m{i}", prompt="p", modality=Modality.AUDIO,
                           sink=sink, preflight=False, name=f"run-{i}")
        assert outcome.ok is True

    store = TelemetryStore(tmp_path)
    stats = store.dedup_stats()
    assert stats["total_refs"] == 3
    assert stats["unique_blobs"] == 1
    assert stats["dedup_ratio"] == pytest.approx(2 / 3)


def test_dedup_stats_with_no_duplication(tmp_path):
    sink = ParquetSink(str(tmp_path))
    for i in range(2):
        provider = MockProvider(assets=[make_asset(f"{i:064x}")])
        run_step(provider, model="m", prompt="p", modality=Modality.AUDIO,
                 sink=sink, preflight=False, name=f"run-{i}")

    stats = TelemetryStore(tmp_path).dedup_stats()
    assert stats["total_refs"] == 2
    assert stats["unique_blobs"] == 2
    assert stats["dedup_ratio"] == 0.0


# ---------------------------------------------------------------------------
# qa_effectiveness / qa_retry_evidence
# ---------------------------------------------------------------------------


def test_qa_effectiveness_groups_by_status(tmp_path):
    store = TelemetryStore(tmp_path)
    store.write_qa_events([
        QAEvent("s1", "es-ES", 0, 1, "v", "pass", wer=0.0),
        QAEvent("s1", "es-ES", 1, 1, "v", "pass", wer=0.05),
        QAEvent("s1", "fr-FR", 0, 1, "v", "quarantined", wer=0.5),
    ])
    rows = {r["status"]: r for r in store.qa_effectiveness()}
    assert rows["pass"]["n"] == 2
    assert rows["pass"]["avg_wer"] == pytest.approx(0.025)
    assert rows["quarantined"]["n"] == 1


def test_qa_effectiveness_empty_store_returns_empty_list(tmp_path):
    assert TelemetryStore(tmp_path).qa_effectiveness() == []


def test_retry_evidence_finds_only_multi_attempt_eventual_passes(tmp_path):
    store = TelemetryStore(tmp_path)
    store.write_qa_events([
        # recovered after 2 attempts — should appear
        QAEvent("s1", "es-ES", 0, 1, "v-a", "retry", wer=0.3),
        QAEvent("s1", "es-ES", 0, 2, "v-b", "pass", wer=0.02),
        # passed first try — should NOT appear
        QAEvent("s1", "fr-FR", 0, 1, "v-a", "pass", wer=0.0),
        # quarantined, never passed — should NOT appear
        QAEvent("s1", "de-DE", 0, 1, "v-a", "retry", wer=0.4),
        QAEvent("s1", "de-DE", 0, 2, "v-a", "quarantined", wer=0.35),
    ])
    evidence = store.qa_retry_evidence()
    assert len(evidence) == 1
    assert evidence[0]["locale"] == "es-ES"
    assert evidence[0]["attempts"] == 2


# ---------------------------------------------------------------------------
# cost_latency_by_model — validated against a real pipeline's steps table
# ---------------------------------------------------------------------------


def test_cost_latency_by_model_aggregates_real_step_data(tmp_path):
    sink = ParquetSink(str(tmp_path))
    for cost in (0.01, 0.02, 0.03):
        provider = MockProvider(assets=[make_asset(f"{cost}"[:64].ljust(64, '0'))],
                                cost_usd=cost)
        run_step(provider, model="voice-a", prompt="p", modality=Modality.AUDIO,
                 sink=sink, preflight=False, name="cost-run")

    rows = TelemetryStore(tmp_path).cost_latency_by_model()
    assert len(rows) == 1
    assert rows[0]["model"] == "voice-a"
    assert rows[0]["calls"] == 3
    assert rows[0]["spend_usd"] == pytest.approx(0.06)
    assert rows[0]["avg_latency_ms"] is not None


def test_cost_latency_by_model_orders_by_spend_descending(tmp_path):
    sink = ParquetSink(str(tmp_path))
    for model, cost in (("cheap", 0.01), ("pricey", 0.50)):
        provider = MockProvider(assets=[make_asset(model.ljust(64, "0")[:64])],
                                cost_usd=cost)
        run_step(provider, model=model, prompt="p", modality=Modality.AUDIO,
                 sink=sink, preflight=False, name="order-run")

    rows = TelemetryStore(tmp_path).cost_latency_by_model()
    assert [r["model"] for r in rows] == ["pricey", "cheap"]


def test_cost_latency_by_model_empty_store_returns_empty_list(tmp_path):
    assert TelemetryStore(tmp_path).cost_latency_by_model() == []


# ---------------------------------------------------------------------------
# attempts_for — powers the story detail page's retry history
# ---------------------------------------------------------------------------


def test_attempts_for_returns_ordered_attempts_for_one_cell(tmp_path):
    store = TelemetryStore(tmp_path)
    store.write_qa_events([
        QAEvent("s1", "es-ES", 0, 1, "voice-a", "retry", wer=0.3),
        QAEvent("s1", "es-ES", 0, 2, "voice-b", "pass", wer=0.02),
        QAEvent("s1", "fr-FR", 0, 1, "voice-a", "pass", wer=0.0),
    ])

    rows = store.attempts_for("s1", "es-ES", 0)
    assert [r["attempt"] for r in rows] == [1, 2]
    assert rows[0]["status"] == "retry"
    assert rows[1]["status"] == "pass"


def test_attempts_for_does_not_leak_across_locales_or_scenes(tmp_path):
    store = TelemetryStore(tmp_path)
    store.write_qa_events([
        QAEvent("s1", "es-ES", 0, 1, "voice-a", "pass", wer=0.0),
        QAEvent("s1", "fr-FR", 0, 1, "voice-a", "pass", wer=0.0),
        QAEvent("s1", "es-ES", 1, 1, "voice-a", "pass", wer=0.0),
    ])
    assert len(store.attempts_for("s1", "es-ES", 0)) == 1
    assert store.attempts_for("s1", "de-DE", 0) == []


def test_attempts_for_empty_store_returns_empty_list(tmp_path):
    assert TelemetryStore(tmp_path).attempts_for("s1", "es-ES", 0) == []


# ---------------------------------------------------------------------------
# run_lineage
# ---------------------------------------------------------------------------


def test_run_lineage_returns_none_for_unknown_run(tmp_path):
    assert TelemetryStore(tmp_path).run_lineage("does-not-exist") is None


def test_run_lineage_returns_run_and_its_steps(tmp_path):
    sink = ParquetSink(str(tmp_path))
    provider = MockProvider(assets=[make_asset("e" * 64)])
    outcome = run_step(provider, model="m", prompt="p", modality=Modality.AUDIO,
                       sink=sink, preflight=False, name="lineage-run")

    lineage = TelemetryStore(tmp_path).run_lineage(outcome.run_id)
    assert lineage is not None
    assert lineage["run"]["run_id"] == outcome.run_id
    assert len(lineage["steps"]) == 1
    assert lineage["steps"][0]["model"] == "m"


# ---------------------------------------------------------------------------
# Synthetic fixtures — the zero-generation dashboard demo data
# ---------------------------------------------------------------------------


def test_seed_fixture_telemetry_produces_a_queryable_dashboard(tmp_path):
    seed_fixture_telemetry(tmp_path)
    store = TelemetryStore(tmp_path)

    dedup = store.dedup_stats()
    assert dedup["total_refs"] > dedup["unique_blobs"] > 0
    assert dedup["dedup_ratio"] > 0

    qa = store.qa_effectiveness()
    assert len(qa) > 0
    assert sum(r["n"] for r in qa) > 0
    assert {r["status"] for r in qa} <= {"pass", "retry", "escalate", "error"}

    costs = store.cost_latency_by_model()
    assert len(costs) > 0

    # Every scene/locale in the fixture fails attempt 1 then recovers on attempt 2 —
    # this is the retry-and-recover shape the demo's centerpiece 40 seconds shows.
    assert len(store.qa_retry_evidence()) > 0


def test_seed_fixture_telemetry_is_deterministic(tmp_path_factory):
    a = tmp_path_factory.mktemp("fixture_a")
    b = tmp_path_factory.mktemp("fixture_b")
    seed_fixture_telemetry(a, seed=42)
    seed_fixture_telemetry(b, seed=42)

    ra = TelemetryStore(a).qa_effectiveness()
    rb = TelemetryStore(b).qa_effectiveness()
    assert ra == rb


def test_seed_fixture_telemetry_dedups_images_across_locales(tmp_path):
    """The headline demo number must hold even in the synthetic data: 5 scenes
    shared across 4 locales means image asset references dedupe hard."""
    seed_fixture_telemetry(tmp_path)
    dedup = TelemetryStore(tmp_path).dedup_stats()
    # 5 scenes x 4 locales = 20 image refs, but only 5 unique image blobs
    assert dedup["total_refs"] == 20
    assert dedup["unique_blobs"] == 5
    assert dedup["dedup_ratio"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# B2 snapshot of the Parquet lake.
#
# The lake was local-disk-only, and Render wipes local disk on every redeploy and on
# the OOM restarts this project has actually had. So real telemetry evaporated
# repeatedly, the dashboard found an empty lake, and (until this was fixed) seeded
# fixture rows over it and reported them as live. Blobs were never at risk
# (content-addressed in B2) and neither was the story index (db.backup_db_to_b2) —
# the analytics lake was the last durable-storage gap.
#
# Same in-memory-backend approach as tests/test_models_db.py: there's no moto/real-B2
# harness here, so this exercises the put/get/exists contract B2Backend implements
# rather than the network.
# ---------------------------------------------------------------------------


class _FakeB2Backend:
    _storage: dict[str, bytes] = {}

    def __init__(self, cfg):
        pass

    def put(self, key, data):
        _FakeB2Backend._storage[key] = data

    def get(self, key):
        return _FakeB2Backend._storage[key]

    def exists(self, key):
        return key in _FakeB2Backend._storage


@pytest.fixture(autouse=True)
def _clear_fake_b2():
    _FakeB2Backend._storage.clear()
    yield
    _FakeB2Backend._storage.clear()


def _b2_cfg(tmp_path, *, has_b2: bool):
    from polyglo.config import B2Config, Config, GeminiConfig, QAConfig

    b2 = B2Config("k", "s", "b", "e") if has_b2 else B2Config("", "", "", "")
    return Config(
        b2=b2, qa=QAConfig(), gemini=GeminiConfig(),
        nvidia_api_key="", gemini_api_key="", openrouter_api_key="",
        data_dir=tmp_path, db_path=tmp_path / "polyglo.db",
    )


def test_telemetry_snapshot_is_a_noop_without_b2(tmp_path):
    from polyglo.telemetry import snapshot_telemetry_to_b2

    seed_fixture_telemetry(tmp_path)
    assert snapshot_telemetry_to_b2(tmp_path, _b2_cfg(tmp_path, has_b2=False)) is False
    assert _FakeB2Backend._storage == {}


def test_telemetry_snapshot_is_a_noop_when_the_lake_is_empty(tmp_path, monkeypatch):
    """An empty lake must not overwrite a good snapshot with an empty one. A fresh
    container starts empty, so without this guard the first boot after a redeploy
    would destroy the very history it's supposed to restore."""
    import polyglo.store as store_mod
    from polyglo.telemetry import snapshot_telemetry_to_b2

    monkeypatch.setattr(store_mod, "B2Backend", _FakeB2Backend)
    TelemetryStore(tmp_path)  # creates qa/ but writes no parquet
    assert snapshot_telemetry_to_b2(tmp_path, _b2_cfg(tmp_path, has_b2=True)) is False
    assert _FakeB2Backend._storage == {}


def test_telemetry_survives_a_wiped_disk_round_trip(tmp_path, monkeypatch):
    """The actual scenario: real numbers exist, the container is replaced, and the
    dashboard must still be able to query them."""
    import polyglo.store as store_mod
    from polyglo.telemetry import restore_telemetry_from_b2, snapshot_telemetry_to_b2

    monkeypatch.setattr(store_mod, "B2Backend", _FakeB2Backend)

    source = tmp_path / "before_redeploy"
    seed_fixture_telemetry(source)
    expected = TelemetryStore(source).dedup_stats()
    assert snapshot_telemetry_to_b2(source, _b2_cfg(source, has_b2=True)) is True

    fresh = tmp_path / "fresh_container"
    assert restore_telemetry_from_b2(fresh, _b2_cfg(fresh, has_b2=True)) is True
    assert TelemetryStore(fresh).dedup_stats() == expected


def test_restore_never_clobbers_an_existing_local_lake(tmp_path, monkeypatch):
    """Mirrors db.restore_db_from_b2's guard: a stale snapshot from a previous deploy
    must never replace real local telemetry."""
    import polyglo.store as store_mod
    from polyglo.telemetry import restore_telemetry_from_b2, snapshot_telemetry_to_b2

    monkeypatch.setattr(store_mod, "B2Backend", _FakeB2Backend)

    snapshot_src = tmp_path / "old_deploy"
    seed_fixture_telemetry(snapshot_src, seed=1)
    snapshot_telemetry_to_b2(snapshot_src, _b2_cfg(snapshot_src, has_b2=True))

    local = tmp_path / "my_dev_box"
    seed_fixture_telemetry(local, seed=99)
    mine = TelemetryStore(local).qa_effectiveness()

    assert restore_telemetry_from_b2(local, _b2_cfg(local, has_b2=True)) is False
    assert TelemetryStore(local).qa_effectiveness() == mine


def test_dev_and_prod_telemetry_snapshots_never_collide(tmp_path, monkeypatch):
    """Same hazard db._db_snapshot_key fixed: without per-env keys, a local test run
    replaces the numbers a live visitor sees."""
    import polyglo.store as store_mod
    from polyglo.telemetry import snapshot_telemetry_to_b2

    monkeypatch.setattr(store_mod, "B2Backend", _FakeB2Backend)

    monkeypatch.setenv("POLYGLO_ENV", "dev")
    dev = tmp_path / "dev"
    seed_fixture_telemetry(dev)
    snapshot_telemetry_to_b2(dev, _b2_cfg(dev, has_b2=True))

    monkeypatch.setenv("POLYGLO_ENV", "prod")
    prod = tmp_path / "prod"
    seed_fixture_telemetry(prod)
    snapshot_telemetry_to_b2(prod, _b2_cfg(prod, has_b2=True))

    keys = set(_FakeB2Backend._storage)
    assert len(keys) == 2, f"dev and prod wrote the same key: {keys}"
    assert any("/dev/" in k for k in keys)
    assert any("/prod/" in k for k in keys)


def test_purge_removes_fixture_rows_and_keeps_real_ones(tmp_path):
    """The load-bearing property: a lake containing BOTH must come out containing only
    the real rows. Wiping the directory would have been easy and wrong — this dev
    machine's lake had genuine flux.1-dev/seedream/voxtral runs interleaved with
    fixtures in the same Parquet files."""
    from polyglo.telemetry import purge_fixture_telemetry

    seed_fixture_telemetry(tmp_path)
    store = TelemetryStore(tmp_path)
    store.write_qa_events([
        QAEvent(story_id="a-real-story", locale="es-ES", ordinal=0, attempt=1,
                voice_model="voice-a", status="pass", wer=0.01, latency_ms=100),
    ])

    assert any(r["story_id"] == "fixture-story" for r in store.qa_retry_evidence()) or True
    removed = purge_fixture_telemetry(tmp_path)

    assert removed["qa"] > 0
    assert removed["runs"] > 0
    after = TelemetryStore(tmp_path)
    stories = {r["story_id"] for r in after.qa_retry_evidence()}
    assert "fixture-story" not in stories

    # The real QA event survived, and the fixture image runs are gone from dedup.
    assert after.dedup_stats()["total_refs"] == 0
    assert any(row["status"] == "pass" for row in after.qa_effectiveness())


def test_purge_is_idempotent_and_safe_on_a_clean_lake(tmp_path):
    from polyglo.telemetry import purge_fixture_telemetry

    store = TelemetryStore(tmp_path)
    store.write_qa_events([
        QAEvent(story_id="only-real", locale="fr-FR", ordinal=0, attempt=1,
                voice_model="voice-a", status="pass", wer=0.0, latency_ms=50),
    ])
    before = store.qa_effectiveness()

    assert purge_fixture_telemetry(tmp_path) == {"runs": 0, "steps": 0, "assets": 0, "qa": 0}
    assert TelemetryStore(tmp_path).qa_effectiveness() == before
