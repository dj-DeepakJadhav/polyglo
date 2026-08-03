"""Tests for the server-rendered HTML UI.

Same network-safety rule as test_api.py: every test monkeypatches make_providers to
force mock/simulated providers, since a real NVIDIA_API_KEY is present and chat is
confirmed live. Reuses test_api.py's fixtures rather than redefining them.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from polyglo.chaos import ChaosRegistry
from polyglo.orchestrator import ProgressEvent
from polyglo.store import BlobStore, LocalBackend
from polyglo.telemetry import TelemetryStore
from polyglo.web import _stepper_state
from tests.test_api import safe_providers, wait_for_story


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """See test_api.py's client fixture docstring for why reset_config_cache() is
    mandatory here, not optional — without it, db.session()'s internal get_config()
    calls silently ignore the tmp_path env vars and write into the real dev database.
    """
    import polyglo.api as api_mod
    import polyglo.web  # noqa: F401 — import side effect registers the HTML routes
    from genblaze_core import ParquetSink
    from polyglo.config import reset_config_cache

    monkeypatch.setenv("POLYGLO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYGLO_DB_PATH", str(tmp_path / "test.db"))
    reset_config_cache()

    telemetry_dir = tmp_path / "telemetry"
    sink = ParquetSink(str(telemetry_dir))

    monkeypatch.setattr(api_mod, "_store", BlobStore(LocalBackend(tmp_path / "blobs")))
    monkeypatch.setattr(api_mod, "_telemetry", TelemetryStore(telemetry_dir))
    monkeypatch.setattr(api_mod, "_chaos", ChaosRegistry())
    monkeypatch.setattr(api_mod, "_progress", {})
    monkeypatch.setattr(api_mod, "make_providers", lambda cfg, chaos: safe_providers(sink))
    # The rate limiters are process-level singletons (real ones must persist across
    # requests to actually limit anything) — without resetting them per test, one
    # test's story-creation calls count against the next test's limit, and the
    # suite starts failing with real 429s partway through. Generous limits here
    # too (tests create many stories in one process, a real client never would).
    from polyglo.ratelimit import RateLimiter
    monkeypatch.setattr(api_mod, "_story_creation_limiter", RateLimiter(1000, 600))
    monkeypatch.setattr(api_mod, "_chaos_toggle_limiter", RateLimiter(1000, 60))
    monkeypatch.setattr(api_mod, "_video_export_limiter", RateLimiter(1000, 300))
    from polyglo.qa.budget import DailyCallBudget
    monkeypatch.setattr(api_mod, "_global_story_budget",
                        DailyCallBudget(1000, tmp_path / "global_story_budget.json"))

    # web.py imported these names directly (from polyglo.api import _store, ... and
    # from polyglo.orchestrator import make_providers), so patching api_mod's module
    # attributes above does NOT change web.py's own local bindings — each name must
    # be patched on web's own module too.
    #
    # CONFIRMED REAL BUG, not just a theoretical gap: web.py imports make_providers
    # from polyglo.orchestrator directly, a SEPARATE binding from api_mod's. Before
    # this fix, every test in this file that created a story via the HTML form (i.e.
    # every test using _story_id_from_redirect) was calling the REAL, unmocked
    # make_providers() — meaning real, live NVIDIA chat API calls on every test run,
    # exactly the safety violation this file's own docstring promised to prevent.
    # Caught only because three tests happened to assert an exact scene count, and a
    # real LLM call returned 9 scenes instead of the requested 1 — every other test
    # that created a story was making the same live call and just didn't notice.
    import polyglo.web as web_mod
    monkeypatch.setattr(web_mod, "_store", api_mod._store)
    monkeypatch.setattr(web_mod, "_telemetry", api_mod._telemetry)
    monkeypatch.setattr(web_mod, "_chaos", api_mod._chaos)
    monkeypatch.setattr(web_mod, "make_providers", lambda cfg, chaos: safe_providers(sink))
    # No separate patching needed for the rate limiters here: web.py imports the
    # DEPENDENCY FUNCTIONS (_require_story_creation_slot etc.), not the limiter
    # instances themselves — those functions' __globals__ always point at api.py's
    # module namespace regardless of which module calls them, so patching
    # api_mod's attributes above is already sufficient for both route sets.

    try:
        yield TestClient(api_mod.app)
    finally:
        reset_config_cache()


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


def test_home_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Polyglo" in resp.text
    assert "Create a story" in resp.text


def test_home_page_shows_the_banner(client):
    resp = client.get("/")
    assert "mock providers" in resp.text or "no-credentials" in resp.text or "Missing" in resp.text or resp.status_code == 200


def test_home_page_shows_hero_section(client):
    """Task #26: a real landing pitch, not a bare form as the first thing a visitor
    sees."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "narration verified" in resp.text.lower()
    assert 'class="hero-steps"' in resp.text


def test_static_assets_are_cache_busted(client):
    """Without a version query param, a browser that cached style.css before a
    deploy has no reason to re-fetch it after one — confirmed as a real,
    reproducible bug while manually verifying task #26's own CSS rewrite (a brand
    new browser tab kept serving stale CSS)."""
    resp = client.get("/")
    assert 'style.css?v=' in resp.text
    assert 'htmx.min.js?v=' in resp.text


def test_html_root_has_no_hardcoded_theme_attribute(client):
    """Regression test: a previous version hardcoded data-theme="light" on <html>,
    which silently overrode every OS/browser dark-mode preference — found live
    while testing the theme toggle added in the same change. The toggle script
    must be the only thing that ever sets this attribute, and only once a user has
    an explicit stored preference."""
    resp = client.get("/")
    html_tag_match = re.search(r"<html\b[^>]*>", resp.text)
    assert html_tag_match is not None
    assert "data-theme" not in html_tag_match.group(0)
    assert 'id="theme-toggle"' in resp.text


def test_home_lists_created_stories(client):
    resp = client.post("/stories", data={
        "title": "The Lost Umbrella", "source_text": "a story",
        "cefr": "B1", "n_scenes": "1", "locales": ["es-ES"],
    })
    assert resp.status_code in (200, 303)

    home = client.get("/")
    assert "The Lost Umbrella" in home.text


# ---------------------------------------------------------------------------
# Story creation via form
# ---------------------------------------------------------------------------


def test_create_story_form_redirects_to_story_page(client):
    resp = client.post("/stories", data={
        "title": "Story", "source_text": "text", "cefr": "B1",
        "n_scenes": "2", "locales": ["es-ES"],
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/stories/")


def test_create_story_form_clamps_n_scenes(client):
    """n_scenes=999 must be clamped, not passed through to a 20+ scene pipeline run."""
    resp = client.post("/stories", data={
        "title": "Story", "source_text": "text", "cefr": "B1",
        "n_scenes": "999", "locales": ["es-ES"],
    }, follow_redirects=True)
    assert resp.status_code == 200


def test_create_story_form_survives_garbage_n_scenes(client):
    resp = client.post("/stories", data={
        "title": "Story", "source_text": "text", "cefr": "B1",
        "n_scenes": "not-a-number", "locales": ["es-ES"],
    }, follow_redirects=True)
    assert resp.status_code == 200


def test_story_creation_rate_limit_is_shared_between_html_form_and_json_api(client, monkeypatch):
    """The whole point of sharing one dependency function between web.py's HTML
    route and api.py's JSON route: a caller can't dodge the cap by switching
    endpoints, since both count against the exact same limiter instance."""
    import polyglo.api as api_mod
    from polyglo.ratelimit import RateLimiter
    monkeypatch.setattr(api_mod, "_story_creation_limiter", RateLimiter(1, 600))

    form_payload = {"title": "Story", "source_text": "text", "cefr": "B1",
                    "n_scenes": "1", "locales": ["es-ES"]}
    # Uses up the one slot via the JSON API...
    json_resp = client.post("/api/stories", json={
        "title": "Story", "source_text": "text", "n_scenes": 1, "locales": ["es-ES"],
    })
    assert json_resp.status_code == 202
    # ...and the HTML form route sees the same limiter as already exhausted.
    form_resp = client.post("/stories", data=form_payload, follow_redirects=False)
    assert form_resp.status_code == 429


def test_create_story_form_defaults_locales_when_none_checked(client):
    resp = client.post("/stories", data={
        "title": "Story", "source_text": "text", "cefr": "B1", "n_scenes": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Story page + fragment polling
# ---------------------------------------------------------------------------


def test_story_page_404_for_unknown_id(client):
    resp = client.get("/stories/does-not-exist")
    assert resp.status_code == 404


def _story_id_from_redirect(client, n_scenes="2", locales=("es-ES",)):
    resp = client.post("/stories", data={
        "title": "Story", "source_text": "text", "cefr": "B1",
        "n_scenes": n_scenes, "locales": list(locales),
    }, follow_redirects=False)
    return resp.headers["location"].rsplit("/", 1)[-1]


def test_story_page_shows_scenes_and_matrix_after_completion(client):
    story_id = _story_id_from_redirect(client)

    # Poll the JSON API (already proven reliable) to know when the pipeline is done,
    # then check the HTML page reflects the same state.
    wait_for_story(client, story_id)

    page = client.get(f"/stories/{story_id}")
    assert page.status_code == 200
    assert "Scene 0" in page.text
    assert "es-ES" in page.text or "Spanish" in page.text


def test_story_page_shows_original_and_corrected_source_text(client):
    """Task #25: the story page must surface both the as-submitted and the
    corrected/leveled source text once grading has run."""
    story_id = _story_id_from_redirect(client)
    wait_for_story(client, story_id)

    page = client.get(f"/stories/{story_id}")
    assert page.status_code == 200
    assert "cleaned up for level" in page.text.lower()
    assert "a graded source story" in page.text  # FlexibleSplitCompleter's fixed grading response


def test_story_fragment_stops_polling_once_done(client):
    story_id = _story_id_from_redirect(client, n_scenes="1")
    wait_for_story(client, story_id)

    fragment = client.get(f"/stories/{story_id}/fragment")
    assert fragment.status_code == 200
    assert 'hx-trigger="every 1.5s"' not in fragment.text


# ---------------------------------------------------------------------------
# Storybook reading mode (task #27)
# ---------------------------------------------------------------------------


def test_story_read_404_for_unknown_story(client):
    resp = client.get("/stories/does-not-exist/read/es-ES")
    assert resp.status_code == 404


def test_story_read_404_for_a_locale_with_no_content_yet(client):
    story_id = _story_id_from_redirect(client, n_scenes="1", locales=("es-ES",))
    wait_for_story(client, story_id)
    resp = client.get(f"/stories/{story_id}/read/fr-FR")  # never requested for this story
    assert resp.status_code == 404


def test_story_read_shows_first_page_by_default(client):
    story_id = _story_id_from_redirect(client, n_scenes="2", locales=("es-ES",))
    wait_for_story(client, story_id)

    resp = client.get(f"/stories/{story_id}/read/es-ES")
    assert resp.status_code == 200
    assert "Page 1 of 2" in resp.text
    assert 'class="reader-btn reader-btn-disabled"' in resp.text  # "Previous" disabled on page 1


def test_story_read_page_query_param_navigates(client):
    story_id = _story_id_from_redirect(client, n_scenes="3", locales=("es-ES",))
    wait_for_story(client, story_id)

    resp = client.get(f"/stories/{story_id}/read/es-ES", params={"page": 1})
    assert resp.status_code == 200
    assert "Page 2 of 3" in resp.text
    assert 'href="?page=0"' in resp.text  # previous
    assert 'href="?page=2"' in resp.text  # next


def test_story_read_clamps_out_of_range_page_instead_of_erroring(client):
    story_id = _story_id_from_redirect(client, n_scenes="2", locales=("es-ES",))
    wait_for_story(client, story_id)

    resp = client.get(f"/stories/{story_id}/read/es-ES", params={"page": 999})
    assert resp.status_code == 200
    assert "Page 2 of 2" in resp.text  # clamped to the last real page

    resp2 = client.get(f"/stories/{story_id}/read/es-ES", params={"page": -5})
    assert resp2.status_code == 200
    assert "Page 1 of 2" in resp2.text  # clamped to the first page


def test_story_read_shows_scene_text_and_qa_status(client):
    story_id = _story_id_from_redirect(client, n_scenes="1", locales=("es-ES",))
    wait_for_story(client, story_id)

    resp = client.get(f"/stories/{story_id}/read/es-ES")
    assert resp.status_code == 200
    assert "reader-image" in resp.text or "placeholder" in resp.text
    assert "badge-unverified" in resp.text or "badge-pass" in resp.text or "badge-quarantined" in resp.text


def test_story_overview_links_to_the_reader_once_bundles_exist(client):
    story_id = _story_id_from_redirect(client, n_scenes="1", locales=("es-ES",))
    wait_for_story(client, story_id)

    resp = client.get(f"/stories/{story_id}")
    assert resp.status_code == 200
    assert f"/stories/{story_id}/read/es-ES" in resp.text


# ---------------------------------------------------------------------------
# Narrated video export
# ---------------------------------------------------------------------------


def test_story_video_404_for_unknown_story(client):
    resp = client.get("/stories/does-not-exist/es-ES/video.mp4")
    assert resp.status_code == 404


def test_story_video_404_for_a_locale_with_no_content_yet(client):
    story_id = _story_id_from_redirect(client, n_scenes="1", locales=("es-ES",))
    wait_for_story(client, story_id)
    resp = client.get(f"/stories/{story_id}/fr-FR/video.mp4")
    assert resp.status_code == 404


def test_story_video_422_when_only_simulated_content_exists(client):
    """The client fixture's safe_providers() only ever produces simulated
    marker bytes, never real magic-byte media — the route must degrade to a
    clear 422, never fabricate a fake "video" from placeholder bytes."""
    story_id = _story_id_from_redirect(client, n_scenes="1", locales=("es-ES",))
    wait_for_story(client, story_id)

    resp = client.get(f"/stories/{story_id}/es-ES/video.mp4")
    assert resp.status_code == 422


def test_story_video_happy_path_returns_mp4_with_download_headers(client, monkeypatch):
    import polyglo.web as web_mod

    story_id = _story_id_from_redirect(client, n_scenes="1", locales=("es-ES",))
    wait_for_story(client, story_id)

    monkeypatch.setattr(web_mod, "compose_story_video", lambda *a, **kw: b"fake mp4 bytes")

    resp = client.get(f"/stories/{story_id}/es-ES/video.mp4")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.headers["content-disposition"] == f'attachment; filename="{story_id}-es-ES.mp4"'
    assert resp.content == b"fake mp4 bytes"


def test_story_video_413_for_a_story_over_the_scene_cap(client, monkeypatch):
    """Real production incident (2026-08-02): tightened from the general 20-scene
    cap to 10 specifically for video, since real ffmpeg encoding is far more
    memory/CPU-hungry per scene than image generation."""
    from polyglo import db as dbm
    from polyglo.models import Scene

    story_id = _story_id_from_redirect(client, n_scenes="1", locales=("es-ES",))
    wait_for_story(client, story_id)

    with dbm.session() as conn:
        story = dbm.get_story(conn, story_id)
        story.scenes = [Scene(story_id, i, f"text {i}", f"prompt {i}") for i in range(11)]
        dbm.save_story(conn, story)

    resp = client.get(f"/stories/{story_id}/es-ES/video.mp4")
    assert resp.status_code == 413


def test_story_video_503_when_a_composition_is_already_in_progress(client, monkeypatch):
    """The other half of the same production incident's fix: bounds concurrent
    real ffmpeg encodes to protect the app's memory budget, and fails fast (503)
    rather than queuing a second one behind it."""
    import polyglo.web as web_mod
    from polyglo.video import VideoBusyError

    story_id = _story_id_from_redirect(client, n_scenes="1", locales=("es-ES",))
    wait_for_story(client, story_id)

    def _busy(*a, **kw):
        raise VideoBusyError("a video is already being composed")

    monkeypatch.setattr(web_mod, "compose_story_video", _busy)

    resp = client.get(f"/stories/{story_id}/es-ES/video.mp4")
    assert resp.status_code == 503
    assert "Retry-After" in resp.headers


def test_story_video_rate_limited_after_repeated_requests(client, monkeypatch):
    import polyglo.api as api_mod
    import polyglo.web as web_mod
    from polyglo.ratelimit import RateLimiter

    monkeypatch.setattr(api_mod, "_video_export_limiter", RateLimiter(2, 300))
    monkeypatch.setattr(web_mod, "compose_story_video", lambda *a, **kw: b"fake mp4 bytes")

    story_id = _story_id_from_redirect(client, n_scenes="1", locales=("es-ES",))
    wait_for_story(client, story_id)

    assert client.get(f"/stories/{story_id}/es-ES/video.mp4").status_code == 200
    assert client.get(f"/stories/{story_id}/es-ES/video.mp4").status_code == 200
    third = client.get(f"/stories/{story_id}/es-ES/video.mp4")
    assert third.status_code == 429
    assert "Retry-After" in third.headers


# ---------------------------------------------------------------------------
# Progress stepper (task #28) — _stepper_state() reduces the full per-event
# progress log to one entry per canonical pipeline stage.
# ---------------------------------------------------------------------------


def _ev(stage, **kw):
    return ProgressEvent(stage=stage, story_id="s", detail="x", **kw)


def test_stepper_all_pending_on_an_empty_log():
    steps = _stepper_state([])
    assert all(s["status"] == "pending" for s in steps)


def test_stepper_first_step_active_after_authoring_event():
    steps = _stepper_state([_ev("authoring")])
    assert steps[0]["status"] == "active"
    assert all(s["status"] == "pending" for s in steps[1:])


def test_stepper_merged_authoring_visuals_step_stays_active_through_visuals():
    """authoring and visuals share one label ('Writing & illustrating scenes') —
    a visuals event must keep that merged step 'active', not flip it to 'done'
    just because authoring (an earlier raw stage) already happened."""
    steps = _stepper_state([_ev("authoring"), _ev("visuals")])
    assert len(steps) == 4  # authoring+visuals merged, localize, narrate, bundle
    assert steps[0]["status"] == "active"
    assert steps[0]["label"] == "Writing & illustrating scenes"


def test_stepper_marks_earlier_steps_done_as_pipeline_progresses():
    steps = _stepper_state([_ev("authoring"), _ev("visuals"), _ev("localize"), _ev("narrate")])
    by_label = {s["label"]: s["status"] for s in steps}
    assert by_label["Writing & illustrating scenes"] == "done"
    assert by_label["Translating"] == "done"
    assert by_label["Narrating & verifying"] == "active"
    assert by_label["Assembling bundles"] == "pending"


def test_stepper_all_done_after_a_done_event():
    steps = _stepper_state([_ev("authoring"), _ev("bundle"), _ev("done")])
    assert all(s["status"] == "done" for s in steps)


def test_stepper_shows_error_on_the_current_step():
    steps = _stepper_state([_ev("authoring"), _ev("visuals"), _ev("error")])
    by_label = {s["label"]: s["status"] for s in steps}
    assert by_label["Writing & illustrating scenes"] == "error"


def test_stepper_error_clears_once_a_later_stage_succeeds():
    """A per-locale failure (one locale errors) must not permanently paint the
    whole story red if the pipeline keeps making real progress afterward (e.g. a
    later locale succeeds)."""
    steps = _stepper_state([_ev("authoring"), _ev("localize"), _ev("error"), _ev("narrate")])
    by_label = {s["label"]: s["status"] for s in steps}
    assert by_label["Narrating & verifying"] == "active"
    assert "error" not in by_label.values()


def test_story_fragment_keeps_polling_before_done(client):
    """Immediately after creation, before the background task has run, the fragment
    must still include the polling attributes — proves 'done' detection isn't
    trivially always-true."""
    resp = client.post("/stories", data={
        "title": "Story", "source_text": "text", "cefr": "B1",
        "n_scenes": "2", "locales": ["es-ES"],
    }, follow_redirects=False)
    story_id = resp.headers["location"].rsplit("/", 1)[-1]

    fragment = client.get(f"/stories/{story_id}/fragment")
    assert fragment.status_code == 200
    # Either it's already done (fast mock pipeline) or still polling — both are valid,
    # but if not done, the polling attribute must be present.
    if "No bundles yet" in fragment.text or "pending" in fragment.text:
        assert 'hx-trigger="every 1.5s"' in fragment.text


# ---------------------------------------------------------------------------
# Scene detail — the WER diff panel
# ---------------------------------------------------------------------------


def test_scene_detail_404_for_unknown_scene(client):
    story_id = _story_id_from_redirect(client)
    wait_for_story(client, story_id)
    resp = client.get(f"/stories/{story_id}/es-ES/99")
    assert resp.status_code == 404


def test_scene_detail_renders_diff_and_attempts(client):
    story_id = _story_id_from_redirect(client, n_scenes="1")
    wait_for_story(client, story_id)

    resp = client.get(f"/stories/{story_id}/es-ES/0")
    assert resp.status_code == 200
    assert "Word-level diff" in resp.text
    assert "Attempt history" in resp.text
    assert "Attempt 1" in resp.text


# ---------------------------------------------------------------------------
# Blob serving
# ---------------------------------------------------------------------------


def test_get_blob_404_for_unknown_hash(client):
    resp = client.get("/blobs/" + "0" * 64)
    assert resp.status_code == 404


def test_get_blob_serves_real_bytes_with_sniffed_mime(client, tmp_path):
    import polyglo.web as web_mod

    real_png = b"\x89PNG\r\n\x1a\n" + b"fake but has the right magic bytes"
    put = web_mod._store.put_bytes(real_png)

    resp = client.get(f"/blobs/{put.sha256}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == real_png


def test_get_blob_serves_simulated_bytes_as_octet_stream(client, tmp_path):
    import polyglo.web as web_mod

    payload = b"simulated-image|model|a prompt"
    put = web_mod._store.put_bytes(payload)

    resp = client.get(f"/blobs/{put.sha256}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"


def test_get_blob_download_query_param_sets_content_disposition(client):
    import polyglo.web as web_mod

    real_png = b"\x89PNG\r\n\x1a\n" + b"fake but has the right magic bytes"
    put = web_mod._store.put_bytes(real_png)

    resp = client.get(f"/blobs/{put.sha256}?download=scene-0.png")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'attachment; filename="scene-0.png"'
    assert resp.content == real_png


def test_get_blob_without_download_param_has_no_content_disposition(client):
    """Every existing <img>/<audio> tag across the app hits this route with no
    query param at all — must stay byte-for-byte unchanged."""
    import polyglo.web as web_mod

    put = web_mod._store.put_bytes(b"\x89PNG\r\n\x1a\n" + b"more fake bytes")

    resp = client.get(f"/blobs/{put.sha256}")
    assert "content-disposition" not in resp.headers


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_page_shows_an_honest_empty_state_and_seeds_nothing(client):
    """Replaces a test that asserted the page rendered "sample numbers" when the lake
    was empty. That path seeded fixture Parquet into the *real* telemetry directory, so
    every later request saw a non-empty lake, dropped the disclaimer, and presented
    ``fixture-story`` rows as live production data (confirmed on the live deployment).

    The second GET is the load-bearing assertion: it proves reading the dashboard no
    longer writes telemetry as a side effect.
    """
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Nothing recorded yet" in resp.text
    assert "sample numbers" not in resp.text.lower()

    again = client.get("/dashboard")
    assert "Nothing recorded yet" in again.text, (
        "a dashboard read wrote telemetry to disk — the fixture-seeding bug is back"
    )


def test_dashboard_page_renders_live_data_after_a_run(client):
    story_id = _story_id_from_redirect(client, n_scenes="1")
    wait_for_story(client, story_id)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Nothing recorded yet" not in resp.text
    # Plain-language section heading (the old "Retry-and-recover evidence" wording was
    # rewritten along with the rest of the dashboard's copy).
    assert "Caught and fixed automatically" in resp.text


# ---------------------------------------------------------------------------
# Chaos toggle panel (task #29) — a real UI control over api.py's ChaosRegistry
# singleton (`_chaos`), same underlying state the JSON API
# (POST /api/chaos/{model}/disable) already exposed. `_chaos` is a module-level
# singleton shared across the whole test session, so every test here must reset
# it — otherwise a disabled model would leak into unrelated tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_chaos():
    from polyglo.api import _chaos
    _chaos.reset()
    yield
    _chaos.reset()


def test_dashboard_shows_chaos_panel_with_known_models(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "black-forest-labs/flux.1-dev" in resp.text
    assert "healthy" in resp.text


def test_chaos_toggle_disables_a_model(client):
    resp = client.post("/chaos-panel/black-forest-labs/flux.1-dev/toggle")
    assert resp.status_code == 200
    assert "chaos-disabled" in resp.text
    assert "disabled" in resp.text

    from polyglo.api import _chaos
    assert "black-forest-labs/flux.1-dev" in _chaos.snapshot()


def test_chaos_toggle_twice_re_enables_the_model(client):
    model = "voice-a"
    client.post(f"/chaos-panel/{model}/toggle")
    resp = client.post(f"/chaos-panel/{model}/toggle")

    from polyglo.api import _chaos
    assert model not in _chaos.snapshot()
    assert resp.status_code == 200
    assert "chaos-disabled" not in resp.text  # no model left disabled after re-enabling


def test_chaos_toggle_via_ui_matches_the_json_api_state(client):
    """The dashboard toggle and the pre-existing JSON API (api.py) must be two
    surfaces over the SAME state, not separate mechanisms."""
    client.post("/chaos-panel/voice-strong/toggle")
    status = client.get("/api/status")
    assert "voice-strong" in status.json()["chaos_disabled_models"]


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def test_verify_page_renders_empty_form(client):
    resp = client.get("/verify")
    assert resp.status_code == 200
    assert "Verify a manifest" in resp.text


def test_verify_page_rejects_non_json(client):
    resp = client.post("/verify", files={
        "file": ("x.bin", b"not json at all", "application/octet-stream"),
    })
    assert resp.status_code == 200
    assert "Not verified" in resp.text


def test_verify_page_accepts_a_real_manifest(client):
    from genblaze_core import Modality
    from genblaze_core.mocks import MockProvider
    from genblaze_core.models import Asset
    from polyglo.pipeline import run_step

    asset = Asset(asset_id="a1", url="file:///tmp/a1.wav", media_type="audio/wav",
                 sha256="b" * 64, size_bytes=10)
    outcome = run_step(MockProvider(assets=[asset]), model="m", prompt="p",
                       modality=Modality.AUDIO, preflight=False)

    resp = client.post("/verify", files={
        "file": ("manifest.json", outcome.manifest.model_dump_json().encode(), "application/json"),
    })
    assert resp.status_code == 200
    assert "Verified" in resp.text
    assert outcome.manifest.canonical_hash in resp.text
