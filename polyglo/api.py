"""FastAPI app: the API surface from docs/02 §9.

Runs correctly with **zero credentials** — story creation, the pipeline, the QA gate,
the dashboard, and verify-on-upload all work against Simulated/Mock providers.
`Config.banner()` is surfaced in every relevant response so degraded mode is visible,
never silent (docs/02 §11).

Pipeline runs execute in a background thread per request (FastAPI's `BackgroundTasks`
runs sync callables in a threadpool) so the create-story call returns immediately;
progress is polled by the SSE endpoint from an in-memory per-story event log. This is
a single-process, single-story-at-a-time-per-thread design — correct for a hackathon
demo, not meant to scale past it.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from polyglo import db as dbm
from polyglo.chaos import ChaosRegistry
from polyglo.config import get_config
from polyglo.logging_config import configure_logging, get_logger
from polyglo.models import DEFAULT_LOCALES, Story
from polyglo.orchestrator import ProgressEvent, make_providers, run_story_pipeline
from polyglo.pipeline import manifest_report
from polyglo.qa.budget import BudgetExceeded, DailyCallBudget
from polyglo.ratelimit import RateLimiter, client_ip
from polyglo.store import make_store
from polyglo.telemetry import TelemetryStore, restore_telemetry_from_b2

__all__ = ["app", "get_progress_log"]

# ---------------------------------------------------------------------------
# App-level singletons — constructed once, shared across requests.
# ---------------------------------------------------------------------------

configure_logging()
_log = get_logger(__name__)

app = FastAPI(title="Polyglo", version="0.1.0")

_cfg = get_config()

# Restore the SQLite index from B2 before anything else touches it — must run
# before the first dbm.session() call. A no-op unless this is a genuinely fresh
# environment (no local db file yet) with B2 configured and a snapshot present;
# never overwrites a real local dev database. See db.restore_db_from_b2's own
# docstring and orchestrator.run_story_pipeline's matching backup_db_to_b2 call.
dbm.restore_db_from_b2(_cfg.db_path, _cfg)

_store = make_store(_cfg)
_telemetry = TelemetryStore(_cfg.data_dir / "telemetry")

# Same deal as the SQLite restore above, for the Parquet analytics lake. Render wipes
# local disk on redeploy and on the OOM restarts we've actually had, which zeroed the
# dashboard every time; it then seeded fixtures over the empty lake and reported them
# as live. Both halves of that are fixed — this restores the real history, and the
# dashboard routes now show an honest empty state when there genuinely is none.
# No-op unless B2 is configured, a snapshot exists, and the local lake is empty.
restore_telemetry_from_b2(_telemetry.base_dir, _cfg)
_chaos = ChaosRegistry()

_progress_lock = threading.Lock()
_progress: dict[str, list[ProgressEvent]] = {}

# Rate limiters — module-level singletons. Story creation is the real cost driver
# (a full pipeline run: chat + image + narration + ASR, all real once credentials
# are configured) — deliberately the tightest limit here. Chaos toggling costs
# nothing externally but is still shared demo state worth protecting from casual
# abuse.
_story_creation_limiter = RateLimiter(max_requests=5, window_seconds=600)
_chaos_toggle_limiter = RateLimiter(max_requests=30, window_seconds=60)

# Real production incident (2026-08-02): the video-export route (real ffmpeg
# subprocess encodes) shipped with NO rate limit at all -- unlike story creation
# and chaos toggling, which already had one. Shortly after, the Render instance
# hit its memory limit and was force-restarted. video.py's own concurrency
# semaphore (_MAX_CONCURRENT_ENCODES) now bounds simultaneous encodes to 1, but a
# per-IP rate limit here is still worth having independently -- it stops a single
# client from queuing up many encode requests back-to-back even serialized,
# which would still mean sustained memory/CPU pressure for a long stretch.
_video_export_limiter = RateLimiter(max_requests=3, window_seconds=300)

# Aggregate governor, complementary to the per-IP limiter above: bounds the TOTAL
# number of real story-creation requests across every distinct visitor combined,
# per day. Without this, many different IPs each staying within their own 5/10min
# allowance could still add up to unbounded total daily spend. Disk-persisted
# (same DailyCallBudget primitive as GeminiBudget) so it survives a process
# restart within the same day.
_global_story_budget = DailyCallBudget(
    _cfg.global_.daily_story_cap, _cfg.global_story_budget_path, label="Global daily story",
)


def _require_admin_key(request: Request) -> None:
    """Optional lockdown gate. A no-op unless ``POLYGLO_ADMIN_KEY`` is set, so the
    app stays open by default — which is the point for a public demo.

    **Header only, deliberately.** An earlier version of this also accepted
    ``?admin_key=...`` as a query parameter; that was removed because query strings
    leak into places a credential must never reach — web-server and proxy access
    logs, browser history, and the ``Referer`` header of any outbound link on the
    page. A header carries the same value without any of that exposure.

    Compared with ``secrets.compare_digest`` rather than ``!=`` so the check doesn't
    leak key length/prefix information through response timing.

    **Operational warning**: setting this on a public deployment blocks story
    creation for everyone without the key, including anyone evaluating the live app.
    It exists to lock the instance down deliberately (e.g. after judging closes),
    not as something to enable while the URL is meant to be usable.
    """
    admin_key = _cfg.admin_key
    if not admin_key:
        return
    provided = request.headers.get("X-Admin-Key") or ""
    if not secrets.compare_digest(provided, admin_key):
        _log.warning("admin key auth failed for client_ip=%s", client_ip(request))
        raise HTTPException(401, "Invalid or missing X-Admin-Key header")


def _require_story_creation_slot(request: Request) -> None:
    """A plain module-level function, not a bound method/closure, deliberately:
    it looks up `_story_creation_limiter` as a module global on every call, so
    tests that monkeypatch that name to a fresh RateLimiter (see test fixtures)
    actually take effect. A closure created once at route-registration time
    (e.g. `_story_creation_limiter.check`) would keep referencing the ORIGINAL
    instance forever, silently ignoring any later monkeypatch — confirmed while
    writing this, not a theoretical concern.

    web.py's HTML form route imports and reuses this exact function (not a
    separate limiter instance) specifically so both routes share one limit per
    client — a caller can't dodge the cap by switching between the JSON and
    HTML endpoints.
    """
    _require_admin_key(request)
    ip = client_ip(request)
    try:
        _story_creation_limiter.check(ip)
    except HTTPException:
        _log.warning("story creation rate-limited for client_ip=%s", ip)
        raise
    try:
        _global_story_budget.spend(1)
    except BudgetExceeded as exc:
        _log.warning("story creation blocked: global daily budget exhausted (%s)", exc)
        raise HTTPException(429, str(exc)) from exc


def _require_chaos_toggle_slot(request: Request) -> None:
    _require_admin_key(request)
    ip = client_ip(request)
    try:
        _chaos_toggle_limiter.check(ip)
    except HTTPException:
        _log.warning("chaos toggle rate-limited for client_ip=%s", ip)
        raise


def _require_video_export_slot(request: Request) -> None:
    ip = client_ip(request)
    try:
        _video_export_limiter.check(ip)
    except HTTPException:
        _log.warning("video export rate-limited for client_ip=%s", ip)
        raise


def get_progress_log(story_id: str) -> list[ProgressEvent]:
    with _progress_lock:
        return list(_progress.get(story_id, []))


def _record_progress(event: ProgressEvent) -> None:
    with _progress_lock:
        _progress.setdefault(event.story_id, []).append(event)


def _event_to_dict(e: ProgressEvent) -> dict[str, Any]:
    return {
        "stage": e.stage, "detail": e.detail,
        "locale": e.locale, "ordinal": e.ordinal, "data": e.data,
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateStoryRequest(BaseModel):
    title: str
    source_text: str
    cefr: str = "B1"
    # 3, not 5, deliberately: every (scene x locale) segment costs one ASR call, and
    # Gemini's free tier allows only 20 per DAY. At the previous 5 scenes x 4 default
    # locales = 20, the very first visitor consumed the entire day's verification
    # quota and everyone after them saw unverified segments. 3 x 4 = 12 leaves real
    # headroom while keeping the locale fan-out (and so the dedup ratio) identical --
    # dedup scales with locale count, not scene count, so this costs nothing on the
    # metric the demo actually leads with.
    n_scenes: int = Field(default=3, ge=1, le=20)
    locales: list[str] = Field(default_factory=lambda: list(DEFAULT_LOCALES))


class ChaosToggleRequest(BaseModel):
    model: str


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@app.get("/api/status")
def status() -> dict[str, Any]:
    """Surfaces degraded mode explicitly — never silent (docs/02 §11)."""
    return {
        "banner": _cfg.banner(),
        "has_b2": _cfg.has_b2,
        "has_nvidia": _cfg.has_nvidia,
        "has_generation": _cfg.has_generation,
        "has_image_generation": _cfg.has_image_generation,
        "has_audio_generation": _cfg.has_audio_generation,
        "has_gemini": _cfg.has_gemini,
        "mock_mode": _cfg.mock_mode,
        "gemini_calls_used_today": None,  # wired once a real Gemini call site exists
        "chaos_disabled_models": _chaos.snapshot(),
    }


# ---------------------------------------------------------------------------
# Stories
# ---------------------------------------------------------------------------


@app.post("/api/stories", status_code=202, dependencies=[Depends(_require_story_creation_slot)])
def create_story(req: CreateStoryRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    story = Story.create(req.title, cefr=req.cefr, source_locale="en-US")

    with dbm.session() as conn:
        dbm.save_story(conn, story)   # shell record so GET works immediately

    _log.info("story creation started story_id=%s n_scenes=%d locales=%s",
             story.story_id, req.n_scenes, req.locales)

    def _run() -> None:
        providers = make_providers(_cfg, _chaos)
        with dbm.session() as conn:
            try:
                run_story_pipeline(
                    story, req.source_text, req.n_scenes, req.locales,
                    conn, _store, _telemetry, providers,
                    on_progress=_record_progress,
                )
                _log.info("story creation completed story_id=%s", story.story_id)
            except Exception as exc:  # the pipeline must not crash the worker thread
                _log.error("story pipeline failed story_id=%s: %s", story.story_id, exc)
                _record_progress(ProgressEvent(
                    stage="error", story_id=story.story_id, detail=str(exc),
                ))

    background_tasks.add_task(_run)
    return {"story_id": story.story_id, "status": "started", "banner": _cfg.banner()}


@app.get("/api/stories/{story_id}")
def get_story(story_id: str) -> dict[str, Any]:
    with dbm.session() as conn:
        story = dbm.get_story(conn, story_id)
        if story is None:
            raise HTTPException(404, f"story {story_id!r} not found")
        localized = dbm.get_localized(conn, story_id)
        bundles = dbm.get_bundles(conn, story_id)
        qa = dbm.qa_summary(conn, story_id)
        dedup = dbm.dedup_stats(conn, story_id)

    matrix: dict[str, dict[int, dict[str, Any]]] = {}
    for ls in localized:
        matrix.setdefault(ls.locale, {})[ls.ordinal] = {
            "qa_status": ls.qa_status.value,
            "wer": ls.wer,
            "attempts": ls.attempts,
            "voice_model": ls.voice_model,
        }

    return {
        "story_id": story.story_id,
        "title": story.title,
        "cefr": story.cefr,
        "created_at": story.created_at,
        "original_source_text": story.original_source_text,
        "corrected_source_text": story.corrected_source_text,
        "scenes": [
            {"ordinal": s.ordinal, "text": s.source_text, "image_sha256": s.image_sha256}
            for s in story.scenes
        ],
        "locale_matrix": matrix,
        "qa_summary": qa,
        "bundles": [
            {"locale": b.locale, "image_ref_count": len(b.image_refs),
             "audio_ref_count": len(b.audio_refs)}
            for b in bundles
        ],
        "dedup": {"total_refs": dedup.total_refs, "unique_blobs": dedup.unique_blobs,
                  "dedup_ratio": dedup.dedup_ratio},
    }


@app.get("/api/stories")
def list_stories() -> dict[str, Any]:
    with dbm.session() as conn:
        stories = dbm.list_stories(conn)
    return {"stories": [{"story_id": s.story_id, "title": s.title, "cefr": s.cefr,
                        "created_at": s.created_at, "scene_count": len(s.scenes)}
                       for s in stories]}


# ---------------------------------------------------------------------------
# SSE progress
# ---------------------------------------------------------------------------


@app.get("/api/stories/{story_id}/events")
async def story_events(request: Request, story_id: str) -> StreamingResponse:
    """Server-Sent Events progress stream. Terminates on a 'done'/'error' stage, on
    client disconnect, or after ~20 seconds of no new events (a safety cutoff, not an
    expected outcome).

    Two bugs found the hard way, in this order:

    1. No `request.is_disconnected()` check at all — a client that opens the stream
       and walks away left this generator running server-side for the full idle
       budget regardless, since nothing here ever learned the client was gone.
    2. Added the check above, and it did NOT fix the observed slowdown: Starlette's
       synchronous `TestClient` does not reliably propagate ASGI disconnect events
       promptly (a known limitation, not a bug in this code), so the check is
       correct but insufficient for tests. Measured directly: with a 600-tick
       (~5 minute) idle budget, `test_api.py` took **312 seconds** to complete.
    3. The actual fix is the idle budget itself: **5 minutes was simply the wrong
       number**, independent of tests. A real pipeline emits its first progress
       event (`authoring: splitting story...`) within moments of the POST
       returning; total silence for anywhere near 5 minutes means something is
       already badly wrong (a crashed background thread that didn't even log an
       error event), and no real user would wait that long regardless. Cut to
       ~20 seconds — generous for real usage, and it bounds the worst case
       (disconnect detection failing, as it does in tests) to something sane
       rather than papering over a bad default with a detection mechanism that
       isn't guaranteed to fire.

    Keep the `is_disconnected()` check anyway — a real browser navigating away
    (as opposed to TestClient) does trigger it reliably against a real ASGI server,
    so it still saves real server resources in production even though it wasn't
    the fix for the test-speed problem.
    """

    async def generate():
        sent = 0
        idle_ticks = 0
        max_idle = 40   # ~20s at the 0.5s poll below — see docstring for why 5 min was wrong

        while True:
            if await request.is_disconnected():
                return

            with _progress_lock:
                events = list(_progress.get(story_id, []))
            new = events[sent:]
            sent = len(events)

            if new:
                idle_ticks = 0
            for e in new:
                yield f"data: {json.dumps(_event_to_dict(e))}\n\n"
                if e.stage in ("done", "error"):
                    return

            idle_ticks += 1
            if idle_ticks > max_idle:
                yield 'data: {"stage": "timeout", "detail": "no events received"}\n\n'
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(generate(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------


@app.get("/api/bundles/{story_id}/{locale}")
def get_bundle(story_id: str, locale: str) -> dict[str, Any]:
    with dbm.session() as conn:
        bundles = dbm.get_bundles(conn, story_id)
    bundle = next((b for b in bundles if b.locale == locale), None)
    if bundle is None:
        raise HTTPException(404, f"no bundle for story {story_id!r} locale {locale!r}")
    return {
        "story_id": bundle.story_id, "locale": bundle.locale,
        "manifest_uri": bundle.manifest_uri, "canonical_hash": bundle.canonical_hash,
        "image_refs": bundle.image_refs, "audio_refs": bundle.audio_refs,
        "created_at": bundle.created_at,
    }


# ---------------------------------------------------------------------------
# Verify — the provenance loop, closed in ~4 seconds of demo time
# ---------------------------------------------------------------------------


@app.post("/api/verify")
async def verify_manifest_upload(file: UploadFile = File(...)) -> JSONResponse:
    """Accepts a manifest JSON sidecar (docs/02 §7's guaranteed fallback — always
    written, unlike embedding which depends on the media handler and codec). Parses
    it as a genuine Genblaze ``Manifest`` and runs the same ``manifest_report()`` used
    internally, so the UI's verify widget and the pipeline's own checks agree by
    construction rather than by parallel implementation."""
    from genblaze_core.models import Manifest, parse_manifest
    from genblaze_core.models.manifest import UnsupportedSchemaVersionError

    raw = await file.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse(
            {"verified": False, "detail": "not valid JSON — expected a manifest sidecar"},
            status_code=200,
        )

    try:
        manifest: Manifest = parse_manifest(data)
    except UnsupportedSchemaVersionError as exc:
        return JSONResponse(
            {"verified": False, "detail": f"unsupported manifest schema version: {exc}"},
            status_code=200,
        )
    except Exception as exc:
        return JSONResponse(
            {"verified": False, "detail": f"not a valid manifest: {type(exc).__name__}: {exc}"},
            status_code=200,
        )

    return JSONResponse(manifest_report(manifest), status_code=200)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    """Real telemetry only, with ``source`` stating plainly whether any exists.

    This used to seed ``seed_fixture_telemetry`` when ``total_refs == 0`` so the
    dashboard "must never render empty" (tasks #21/#22, when real generation was
    blocked). That rationale expired once generation went real, and the mechanism was
    worse than an empty dashboard: the seeder *writes fixture Parquet into the real
    telemetry lake*, so the next call saw ``total_refs != 0`` and reported
    ``source: "live"`` over ``story_id="fixture-story"`` rows. Real runs append to the
    same tables, so the contamination was permanent and unfilterable.

    Empty now genuinely means "no runs recorded yet" — and real telemetry survives
    restarts via the B2 snapshot in ``telemetry.snapshot_telemetry_to_b2``.
    """
    dedup = _telemetry.dedup_stats()
    return {
        "source": "live" if dedup["total_refs"] > 0 else "empty (no runs recorded yet)",
        "banner": _cfg.banner(),
        "dedup": dedup,
        "qa_effectiveness": _telemetry.qa_effectiveness(),
        "qa_retry_evidence": _telemetry.qa_retry_evidence(),
        "cost_latency_by_model": _telemetry.cost_latency_by_model(),
    }


# ---------------------------------------------------------------------------
# Chaos toggle — the failover demo
# ---------------------------------------------------------------------------


@app.post("/api/chaos/{model}/disable", dependencies=[Depends(_require_chaos_toggle_slot)])
def chaos_disable(model: str) -> dict[str, Any]:
    """Force `model` to fail on its next call. The next pipeline run's QA gate will
    engage its alternate-voice/escalation ladder for real, on camera, with no
    dependency on a live outage or live credentials to demonstrate recovery."""
    _chaos.disable(model)
    return {"disabled": _chaos.snapshot()}


@app.post("/api/chaos/{model}/enable", dependencies=[Depends(_require_chaos_toggle_slot)])
def chaos_enable(model: str) -> dict[str, Any]:
    _chaos.enable(model)
    return {"disabled": _chaos.snapshot()}


@app.post("/api/chaos/reset", dependencies=[Depends(_require_chaos_toggle_slot)])
def chaos_reset() -> dict[str, Any]:
    _chaos.reset()
    return {"disabled": _chaos.snapshot()}
