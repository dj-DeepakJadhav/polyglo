"""Tests for the FastAPI app.

Safety note, read before adding more tests here: a real NVIDIA_API_KEY is present in
this environment and chat calls are confirmed live (docs/SESSION-LOG.md). Every test
in this file monkeypatches ``polyglo.api.make_providers`` to force mock/simulated
providers regardless of what's in ``.env`` — this is not optional decoration, it is
what stops the test suite from making real network calls and spending real credits or
Gemini budget every time it runs.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from polyglo.chaos import ChaosRegistry
from polyglo.narrate import SimulatedNarrator
from polyglo.orchestrator import Providers
from polyglo.qa.gate import VoicePlan
from polyglo.store import BlobStore, LocalBackend
from polyglo.telemetry import TelemetryStore
from polyglo.visuals import SimulatedVisualGenerator


def scenes_json(n: int) -> str:
    return json.dumps({
        "style_guide": "a small orange tabby cat, flat children's-book watercolor style",
        "scenes": [
            {"text": f"Scene {i} happens.", "visual_prompt": f"illustration {i}"}
            for i in range(n)
        ]
    })


class FlexibleSplitCompleter:
    """A chat double that respects whatever n_scenes the request actually asked for.

    A first draft here hardcoded n_scenes=2 regardless of what each test's POST body
    requested. Several tests request n_scenes=1, so authoring.split_story's own
    count-mismatch check correctly rejected the 2-scene payload every time — the
    pipeline errored at the authoring stage, no bundle was ever produced, and
    wait_for_story() burned its full timeout on every affected test. Several of
    those stacking up is what pushed a full-file run past a 120s ceiling; it read
    as a hang but was actually N slow, deterministic failures. Fixed by parsing the
    real scene count out of the prompt (authoring.py's SPLIT_PROMPT always says
    "Split this story into {n} scenes") instead of guessing it.
    """

    def complete(self, prompt: str, *, model: str) -> str:
        # Dispatch on prompt content, not call order — task #25 inserted a grading
        # call before the split call, so "first call" no longer means "split call".
        if "Correct any spelling and grammar errors" in prompt:
            return json.dumps({"corrected_text": "a graded source story"})
        if "Split this story into" in prompt:
            match = re.search(r"Split this story into (\d+) scenes", prompt)
            n = int(match.group(1)) if match else 1
            return scenes_json(n)
        # A FIXED translation string here would make every scene in a locale
        # translate to byte-identical text, and SimulatedNarrator hashes on that
        # text — so their audio would genuinely (and correctly) dedupe down to one
        # blob. That's real content-addressing behaviour, not a bug, but it broke
        # tests asserting one audio ref per scene. Keying the response off the
        # scene index embedded in the prompt (localize.py's TRANSLATE_PROMPT
        # includes the source scene text) keeps each scene's translation distinct.
        match = re.search(r"Scene (\d+) happens", prompt)
        idx = match.group(1) if match else "0"
        return f"la escena {idx} sucede aqui con muchas palabras diferentes"


class DecodingTranscriber:
    """Decodes the original text back out of SimulatedNarrator's payload, which is a
    deterministic encoding of ``model|locale|text`` rather than real audio. Lets the
    QA gate see a "perfect" transcript for whatever distinct text each scene actually
    got translated to, without hardcoding a matching script in advance."""

    def transcribe(self, audio: bytes, locale: str) -> str:
        parts = audio.decode().split("|", 3)
        return parts[3] if len(parts) == 4 else ""


def safe_providers(sink=None) -> Providers:
    """Providers guaranteed not to touch the network, and correct for any n_scenes.

    ``sink`` mirrors what the real ``orchestrator.make_providers()`` does — pass a
    real ``ParquetSink`` here so test-driven pipeline runs populate genblaze's own
    assets/steps/runs tables, not just our own qa table. Without it, the dashboard's
    "has anything real happened" check (which reads the assets table) stays empty
    forever no matter how many pipeline runs a test triggers — that gap is exactly
    what test_dashboard_reflects_real_runs_once_they_exist caught.
    """
    return Providers(
        chat=FlexibleSplitCompleter(),
        visuals=SimulatedVisualGenerator(sink=sink),
        narrator=SimulatedNarrator(sink=sink),
        transcriber=DecodingTranscriber(),
        chat_model="mock-chat", visual_model="mock-image",
        voice_plan=VoicePlan(primary="voice-a", alternates=["voice-b"]),
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Isolated app: temp DB, temp blob store, temp telemetry, fresh chaos state,
    and network-safe providers — every request in a test is fully sandboxed.

    ``reset_config_cache()`` is not optional here. ``get_config()`` is
    ``@lru_cache``'d, and ``db.session()`` calls it fresh (with no explicit path)
    on every request rather than using api.py's cached ``_cfg`` singleton. Setting
    ``POLYGLO_DB_PATH`` via ``monkeypatch.setenv`` alone does nothing until the
    cache is invalidated — without this call, every "isolated" test here was
    silently writing story records into the real dev database at
    ``./data/polyglo.db`` instead of ``tmp_path``. Confirmed after the fact: 48
    test-created stories had accumulated in the real file before this was caught.
    Reset again on teardown so the next thing to call get_config() (another test
    without this fixture, or anything else in-process) doesn't inherit a config
    still pointed at an already-deleted tmp_path.
    """
    import polyglo.api as api_mod
    from polyglo.config import reset_config_cache

    monkeypatch.setenv("POLYGLO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYGLO_DB_PATH", str(tmp_path / "test.db"))
    reset_config_cache()

    from genblaze_core import ParquetSink

    telemetry_dir = tmp_path / "telemetry"
    sink = ParquetSink(str(telemetry_dir))

    monkeypatch.setattr(api_mod, "_store", BlobStore(LocalBackend(tmp_path / "blobs")))
    monkeypatch.setattr(api_mod, "_telemetry", TelemetryStore(telemetry_dir))
    monkeypatch.setattr(api_mod, "_chaos", ChaosRegistry())
    monkeypatch.setattr(api_mod, "_progress", {})
    monkeypatch.setattr(api_mod, "make_providers", lambda cfg, chaos: safe_providers(sink))
    # Rate limiters are process-level singletons — reset per test the same way
    # _chaos/_progress are, or one test's calls count against the next test's
    # limit and the suite starts failing with real 429s partway through.
    from polyglo.ratelimit import RateLimiter
    monkeypatch.setattr(api_mod, "_story_creation_limiter", RateLimiter(1000, 600))
    monkeypatch.setattr(api_mod, "_chaos_toggle_limiter", RateLimiter(1000, 60))
    # Same reasoning, same fix, for the global daily story budget — it's built
    # once at import time from whatever _cfg existed then, pointed at the REAL
    # data dir's global_story_budget.json if not overridden here.
    from polyglo.qa.budget import DailyCallBudget
    monkeypatch.setattr(api_mod, "_global_story_budget",
                        DailyCallBudget(1000, tmp_path / "global_story_budget.json"))

    try:
        yield TestClient(api_mod.app)
    finally:
        # monkeypatch reverts the env vars automatically, but the CACHED Config
        # object needs an explicit reset too, or the next get_config() call (in
        # another test, or elsewhere in-process) inherits this test's tmp_path.
        reset_config_cache()


def wait_for_story(client: TestClient, story_id: str, timeout: float = 10.0) -> dict:
    """Poll GET /api/stories/{id} until the background pipeline has produced bundles,
    or fail with a clear timeout rather than hanging."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/stories/{story_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["bundles"]:
            return data
        time.sleep(0.1)
    pytest.fail(f"story {story_id} did not complete within {timeout}s")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_exposes_the_banner(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "banner" in body
    assert isinstance(body["chaos_disabled_models"], list)


# ---------------------------------------------------------------------------
# Story lifecycle
# ---------------------------------------------------------------------------


def test_create_story_returns_202_and_a_story_id(client):
    resp = client.post("/api/stories", json={
        "title": "The Lost Umbrella", "source_text": "a story", "n_scenes": 2,
        "locales": ["es-ES"],
    })
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "started"
    assert body["story_id"]


def test_admin_key_unset_leaves_the_app_open(client, monkeypatch):
    """The default, and the one that matters for a public demo: with no
    POLYGLO_ADMIN_KEY configured the gate is a no-op and anyone can create a
    story. Setting the key on a live instance locks out everyone without it —
    including anyone evaluating the app — so 'open unless deliberately locked'
    has to be the default behaviour, not an accident."""
    import polyglo.api as api_mod

    assert api_mod._cfg.admin_key == ""
    payload = {"title": "Story", "source_text": "text", "n_scenes": 1, "locales": ["es-ES"]}
    assert client.post("/api/stories", json=payload).status_code == 202


def test_admin_key_set_rejects_requests_without_the_header(client, monkeypatch):
    import dataclasses

    import polyglo.api as api_mod

    monkeypatch.setattr(api_mod, "_cfg",
                        dataclasses.replace(api_mod._cfg, admin_key="s3cret"))

    payload = {"title": "Story", "source_text": "text", "n_scenes": 1, "locales": ["es-ES"]}
    assert client.post("/api/stories", json=payload).status_code == 401
    assert client.post("/api/stories", json=payload,
                       headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert client.post("/api/stories", json=payload,
                       headers={"X-Admin-Key": "s3cret"}).status_code == 202


def test_admin_key_is_not_accepted_via_query_string(client, monkeypatch):
    """Regression guard: an earlier version also accepted ?admin_key=..., which
    leaks a credential into access logs, browser history, and Referer headers.
    Header only — a query-string key must NOT authenticate."""
    import dataclasses

    import polyglo.api as api_mod

    monkeypatch.setattr(api_mod, "_cfg",
                        dataclasses.replace(api_mod._cfg, admin_key="s3cret"))

    payload = {"title": "Story", "source_text": "text", "n_scenes": 1, "locales": ["es-ES"]}
    resp = client.post("/api/stories?admin_key=s3cret", json=payload)
    assert resp.status_code == 401


def test_create_story_is_rate_limited_per_client(client, monkeypatch):
    """The real protection this exists for: the app is a single public instance
    with real credentials behind it — without this, one caller could trigger
    unlimited real pipeline runs. Uses a tiny limit so the test doesn't need to
    fire 6 real requests to prove the point."""
    import polyglo.api as api_mod
    from polyglo.ratelimit import RateLimiter
    monkeypatch.setattr(api_mod, "_story_creation_limiter", RateLimiter(2, 600))

    payload = {"title": "Story", "source_text": "text", "n_scenes": 1, "locales": ["es-ES"]}
    assert client.post("/api/stories", json=payload).status_code == 202
    assert client.post("/api/stories", json=payload).status_code == 202
    third = client.post("/api/stories", json=payload)
    assert third.status_code == 429
    assert "Retry-After" in third.headers


def test_create_story_is_blocked_by_the_global_daily_budget_even_across_different_ips(client, monkeypatch, tmp_path):
    """The complementary aggregate cap: unlike the per-IP limiter, this one is NOT
    dodgeable by switching client IPs — it bounds the total across everyone."""
    import polyglo.api as api_mod
    from polyglo.qa.budget import DailyCallBudget
    monkeypatch.setattr(api_mod, "_global_story_budget",
                        DailyCallBudget(2, tmp_path / "test_global_budget.json"))

    payload = {"title": "Story", "source_text": "text", "n_scenes": 1, "locales": ["es-ES"]}
    assert client.post("/api/stories", json=payload,
                       headers={"x-forwarded-for": "1.1.1.1"}).status_code == 202
    assert client.post("/api/stories", json=payload,
                       headers={"x-forwarded-for": "2.2.2.2"}).status_code == 202
    # Global budget (2) is now exhausted -- a THIRD, previously-unseen IP still gets blocked.
    third = client.post("/api/stories", json=payload, headers={"x-forwarded-for": "3.3.3.3"})
    assert third.status_code == 429


def test_create_story_rate_limit_is_independent_per_client_ip(client, monkeypatch):
    import polyglo.api as api_mod
    from polyglo.ratelimit import RateLimiter
    monkeypatch.setattr(api_mod, "_story_creation_limiter", RateLimiter(1, 600))

    payload = {"title": "Story", "source_text": "text", "n_scenes": 1, "locales": ["es-ES"]}
    assert client.post("/api/stories", json=payload,
                       headers={"x-forwarded-for": "1.1.1.1"}).status_code == 202
    # A different client IP gets its own, untouched slot.
    assert client.post("/api/stories", json=payload,
                       headers={"x-forwarded-for": "2.2.2.2"}).status_code == 202
    # But the first IP is now out of slots.
    assert client.post("/api/stories", json=payload,
                       headers={"x-forwarded-for": "1.1.1.1"}).status_code == 429


def test_get_story_404_for_unknown_id(client):
    resp = client.get("/api/stories/does-not-exist")
    assert resp.status_code == 404


def test_get_story_returns_shell_immediately_before_pipeline_completes(client):
    resp = client.post("/api/stories", json={
        "title": "Story", "source_text": "text", "n_scenes": 2, "locales": ["es-ES"],
    })
    story_id = resp.json()["story_id"]

    get_resp = client.get(f"/api/stories/{story_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["story_id"] == story_id


def test_full_story_pipeline_completes_and_produces_bundles(client):
    resp = client.post("/api/stories", json={
        "title": "The Lost Umbrella", "source_text": "a long story",
        "n_scenes": 2, "locales": ["es-ES"],
    })
    story_id = resp.json()["story_id"]

    data = wait_for_story(client, story_id)
    assert len(data["scenes"]) == 2
    assert all(s["image_sha256"] for s in data["scenes"])
    assert len(data["bundles"]) == 1
    assert data["bundles"][0]["locale"] == "es-ES"
    assert data["dedup"]["total_refs"] > 0


def test_list_stories_includes_created_story(client):
    resp = client.post("/api/stories", json={
        "title": "Listable Story", "source_text": "text", "n_scenes": 1,
        "locales": ["es-ES"],
    })
    story_id = resp.json()["story_id"]
    wait_for_story(client, story_id)

    listing = client.get("/api/stories").json()["stories"]
    assert any(s["story_id"] == story_id for s in listing)


def test_n_scenes_is_bounded(client):
    resp = client.post("/api/stories", json={
        "title": "Too Many", "source_text": "text", "n_scenes": 999, "locales": ["es-ES"],
    })
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def test_events_stream_reaches_a_done_stage(client):
    resp = client.post("/api/stories", json={
        "title": "Story", "source_text": "text", "n_scenes": 1, "locales": ["es-ES"],
    })
    story_id = resp.json()["story_id"]

    seen_stages = []
    with client.stream("GET", f"/api/stories/{story_id}/events") as stream:
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[len("data: "):])
            seen_stages.append(payload["stage"])
            if payload["stage"] in ("done", "error", "timeout"):
                break

    assert "done" in seen_stages


def test_events_stream_opens_cleanly_for_an_id_with_no_events_yet(client):
    """No events posted for this id — the endpoint must still open a valid stream
    rather than erroring, even though nothing will ever arrive on it. The real
    ~5-minute idle cutoff inside the generator isn't exercised here (that would
    make the test itself slow); this only proves the connection doesn't error."""
    with client.stream("GET", "/api/stories/ghost/events") as stream:
        assert stream.status_code == 200


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------


def test_get_bundle_404_when_missing(client):
    resp = client.get("/api/bundles/no-such-story/es-ES")
    assert resp.status_code == 404


def test_get_bundle_returns_refs_after_pipeline_completes(client):
    resp = client.post("/api/stories", json={
        "title": "Story", "source_text": "text", "n_scenes": 2, "locales": ["es-ES"],
    })
    story_id = resp.json()["story_id"]
    wait_for_story(client, story_id)

    bundle = client.get(f"/api/bundles/{story_id}/es-ES").json()
    assert bundle["locale"] == "es-ES"
    assert len(bundle["image_refs"]) == 2
    assert len(bundle["audio_refs"]) == 2


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def test_verify_rejects_non_json_upload(client):
    resp = client.post("/api/verify", files={"file": ("x.bin", b"\x00\x01not json", "application/octet-stream")})
    assert resp.status_code == 200
    assert resp.json()["verified"] is False
    assert "not valid JSON" in resp.json()["detail"]


def test_verify_rejects_json_that_is_not_a_manifest(client):
    resp = client.post("/api/verify", files={"file": ("x.json", b'{"not": "a manifest"}', "application/json")})
    assert resp.status_code == 200
    assert resp.json()["verified"] is False


def test_verify_accepts_a_real_manifest_and_reports_verified(client):
    """Builds an actual Genblaze manifest (mock provider, zero API calls — same
    pattern as test_pipeline.py) and round-trips it through the upload endpoint."""
    from genblaze_core import Modality
    from genblaze_core.mocks import MockProvider
    from genblaze_core.models import Asset
    from polyglo.pipeline import run_step

    asset = Asset(asset_id="a1", url="file:///tmp/a1.wav", media_type="audio/wav",
                 sha256="b" * 64, size_bytes=10)
    outcome = run_step(MockProvider(assets=[asset]), model="m", prompt="p",
                       modality=Modality.AUDIO, preflight=False)
    manifest_json = outcome.manifest.model_dump_json()

    resp = client.post("/api/verify", files={
        "file": ("manifest.json", manifest_json.encode(), "application/json"),
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["hash_ok"] is True


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_reports_empty_and_seeds_nothing_when_no_real_runs_exist(client):
    """Inverts an earlier test that asserted the dashboard seeded fixture telemetry
    when the lake was empty. That behavior was a real bug, not a feature: the seeder
    writes fixture Parquet into the *real* lake, so the next request found
    ``total_refs != 0``, reported ``source: "live"``, and served ``fixture-story``
    rows as production telemetry — confirmed on the live Render deployment. Real runs
    append to the same tables, so the contamination was permanent.

    Both halves are pinned here: the honest empty report, and — the part that actually
    mattered — that a second call still reports empty, proving nothing was written.
    """
    body = client.get("/api/dashboard").json()
    assert body["source"] == "empty (no runs recorded yet)"
    assert body["dedup"]["total_refs"] == 0
    assert body["qa_effectiveness"] == []

    again = client.get("/api/dashboard").json()
    assert again["source"] == "empty (no runs recorded yet)", (
        "a mere dashboard read wrote telemetry to disk — the fixture-seeding bug is back"
    )


def test_dashboard_reflects_real_runs_once_they_exist(client):
    resp = client.post("/api/stories", json={
        "title": "Story", "source_text": "text", "n_scenes": 1, "locales": ["es-ES"],
    })
    wait_for_story(client, resp.json()["story_id"])

    body = client.get("/api/dashboard").json()
    assert body["source"] == "live"


# ---------------------------------------------------------------------------
# Chaos toggle
# ---------------------------------------------------------------------------


def test_chaos_toggle_is_rate_limited_per_client(client, monkeypatch):
    import polyglo.api as api_mod
    from polyglo.ratelimit import RateLimiter
    monkeypatch.setattr(api_mod, "_chaos_toggle_limiter", RateLimiter(2, 60))

    assert client.post("/api/chaos/voice-a/disable").status_code == 200
    assert client.post("/api/chaos/voice-a/enable").status_code == 200
    assert client.post("/api/chaos/voice-a/disable").status_code == 429


def test_chaos_disable_and_enable_roundtrip(client):
    resp = client.post("/api/chaos/voice-a/disable")
    assert resp.status_code == 200
    assert "voice-a" in resp.json()["disabled"]

    resp = client.post("/api/chaos/voice-a/enable")
    assert "voice-a" not in resp.json()["disabled"]


def test_chaos_reset_clears_all(client):
    client.post("/api/chaos/voice-a/disable")
    client.post("/api/chaos/voice-b/disable")
    resp = client.post("/api/chaos/reset")
    assert resp.json()["disabled"] == []


def test_chaos_toggle_is_visible_in_status(client):
    client.post("/api/chaos/voice-a/disable")
    body = client.get("/api/status").json()
    assert "voice-a" in body["chaos_disabled_models"]
