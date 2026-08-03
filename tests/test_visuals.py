"""Tests for scene visual generation.

``test_generator_called_exactly_once_per_scene_never_per_locale`` is the single most
important test in this file — it's the assertion that the whole product's dedup story
is actually true at the orchestration layer, not just at the storage layer (already
proven in test_store.py / test_models_db.py).
"""

from __future__ import annotations

import base64

import pytest

from polyglo.models import Scene
from polyglo.visuals import (
    FallbackVisualGenerator,
    ImageResult,
    MockVisualGenerator,
    OpenRouterVisualGenerator,
    VisualError,
    generate_story_visuals,
)


def make_scenes(n: int) -> list[Scene]:
    return [Scene("s1", i, f"text {i}", f"a scene showing {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# MockVisualGenerator
# ---------------------------------------------------------------------------


def test_mock_generator_produces_stable_hashes_for_identical_prompts():
    gen = MockVisualGenerator()
    a = gen.generate("a cat on a roof", "model-x")
    b = gen.generate("a cat on a roof", "model-x")
    assert a.sha256 == b.sha256


def test_mock_generator_different_prompts_differ():
    gen = MockVisualGenerator()
    a = gen.generate("a cat", "m")
    b = gen.generate("a dog", "m")
    assert a.sha256 != b.sha256


def test_mock_generator_raises_for_configured_failing_model():
    gen = MockVisualGenerator(fail_models=["broken-model"])
    with pytest.raises(VisualError, match="unavailable"):
        gen.generate("a cat", "broken-model")


# ---------------------------------------------------------------------------
# generate_story_visuals — THE invariant
# ---------------------------------------------------------------------------


def test_generator_called_exactly_once_per_scene_never_per_locale():
    """The core dedup invariant, at the orchestration layer.

    generate_story_visuals only ever sees scenes — it has no locale parameter and
    cannot be called per-locale by construction. This test would fail if a future
    change accidentally threaded a locale loop through this function.
    """
    scenes = make_scenes(5)
    gen = MockVisualGenerator()

    result = generate_story_visuals(scenes, gen, model="m")

    assert len(gen.calls) == 5                    # exactly one call per scene
    assert len(result) == 5
    assert set(result.keys()) == {0, 1, 2, 3, 4}


def test_visuals_keyed_by_scene_ordinal():
    scenes = make_scenes(3)
    gen = MockVisualGenerator()
    result = generate_story_visuals(scenes, gen, model="m")

    for scene in scenes:
        assert result[scene.ordinal].sha256 is not None


def test_different_scenes_produce_different_images():
    scenes = make_scenes(3)
    result = generate_story_visuals(scenes, MockVisualGenerator(), model="m")
    hashes = {r.sha256 for r in result.values()}
    assert len(hashes) == 3


def test_visuals_propagates_generator_failure():
    scenes = make_scenes(2)
    gen = MockVisualGenerator(fail_models=["m"])
    with pytest.raises(VisualError):
        generate_story_visuals(scenes, gen, model="m")


def test_empty_scene_list_produces_empty_result():
    assert generate_story_visuals([], MockVisualGenerator(), model="m") == {}


def test_first_scenes_image_is_passed_as_reference_to_every_later_scene():
    """Character-consistency fix (2026-08-01): the first scene's own generated image
    becomes every later scene's reference_image, a real visual anchor rather than
    just a shared text description. The first scene itself has nothing to
    reference yet."""

    class _RecordingGenerator:
        def __init__(self):
            self.calls = []

        def generate(self, prompt, model, *, seed=None, reference_image=None):
            self.calls.append(reference_image)
            import hashlib
            payload = f"{model}|{prompt}".encode()
            from polyglo.visuals import ImageResult
            return ImageResult(image=payload, sha256=hashlib.sha256(payload).hexdigest(),
                               model=model, latency_ms=1)

    scenes = make_scenes(4)
    gen = _RecordingGenerator()
    generate_story_visuals(scenes, gen, model="m")

    assert gen.calls[0] is None                       # scene 0: nothing to reference yet
    first_image = f"m|a scene showing 0".encode()
    assert gen.calls[1] == first_image                 # every later scene references it
    assert gen.calls[2] == first_image
    assert gen.calls[3] == first_image


# ---------------------------------------------------------------------------
# NvidiaVisualGenerator — plumbing only, real API confirmed broken (task #22)
# ---------------------------------------------------------------------------


def test_nvidia_generator_reads_bytes_from_a_successful_outcome(monkeypatch, tmp_path):
    """Verifies the integration boundary (run_step -> Asset -> bytes) without a real
    network call — the underlying model is confirmed dead (task #22), so this proves
    the plumbing rather than the provider."""
    import polyglo.visuals as visuals_mod
    from polyglo.pipeline import StepOutcome
    from genblaze_core.models import Asset

    written = b"\x89PNG fake bytes"
    written_path = tmp_path / "out.png"
    written_path.write_bytes(written)

    def fake_run_step(provider, *, model, prompt, modality, fallback_models=None,
                      timeout=None, name=None, **kw):
        asset = Asset(asset_id="a1", url=written_path.as_uri(), media_type="image/png",
                      sha256="f" * 64, size_bytes=len(written))
        return StepOutcome(ok=True, run_id="r1", model_requested=model,
                          model_used=model, assets=[asset], latency_ms=42)

    monkeypatch.setattr(visuals_mod, "run_step", fake_run_step)

    gen = visuals_mod.NvidiaVisualGenerator()
    result = gen.generate("a cat on a roof", "some-model")

    assert result.image == written
    assert result.sha256 == "f" * 64
    assert result.latency_ms == 42


def test_nvidia_generator_raises_visual_error_on_failed_outcome(monkeypatch):
    import polyglo.visuals as visuals_mod
    from polyglo.pipeline import StepOutcome

    def fake_run_step(*a, **kw):
        return StepOutcome(ok=False, run_id="", model_requested="m", model_used="m",
                          error="upstream probe returned DEAD")

    monkeypatch.setattr(visuals_mod, "run_step", fake_run_step)
    gen = visuals_mod.NvidiaVisualGenerator()
    with pytest.raises(VisualError, match="DEAD"):
        gen.generate("a cat", "m")


def test_nvidia_generator_raises_when_outcome_ok_but_no_asset(monkeypatch):
    import polyglo.visuals as visuals_mod
    from polyglo.pipeline import StepOutcome

    def fake_run_step(*a, **kw):
        return StepOutcome(ok=True, run_id="r1", model_requested="m", model_used="m",
                          assets=[])

    monkeypatch.setattr(visuals_mod, "run_step", fake_run_step)
    gen = visuals_mod.NvidiaVisualGenerator()
    with pytest.raises(VisualError, match="no asset"):
        gen.generate("a cat", "m")


# ---------------------------------------------------------------------------
# Content-moderation placeholder detection
#
# A real live test (docs/SESSION-LOG.md) found NVIDIA's image endpoint returns a
# solid-black JPEG with a normal 200/success outcome when a prompt trips its content
# filter — nothing in the original code path noticed, so a "successful" generation
# could silently ship a blank image. These tests pin the fix: it must reject a blank
# result and NOT false-positive on a real, low-detail image (a false positive here
# would break every genuine plain-background illustration).
# ---------------------------------------------------------------------------


def _solid_color_jpeg_bytes(color: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (64, 64), color=color).save(buf, format="JPEG")
    return buf.getvalue()


def _varied_jpeg_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (64, 64))
    pixels = img.load()
    for x in range(64):
        for y in range(64):
            pixels[x, y] = ((x * 4) % 256, (y * 4) % 256, ((x + y) * 2) % 256)
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_looks_like_moderation_placeholder_flags_a_solid_color_image():
    from polyglo.visuals import _looks_like_moderation_placeholder

    assert _looks_like_moderation_placeholder(_solid_color_jpeg_bytes()) is True
    assert _looks_like_moderation_placeholder(_solid_color_jpeg_bytes((255, 255, 255))) is True


def test_looks_like_moderation_placeholder_accepts_a_normal_image():
    from polyglo.visuals import _looks_like_moderation_placeholder

    assert _looks_like_moderation_placeholder(_varied_jpeg_bytes()) is False


def test_looks_like_moderation_placeholder_does_not_raise_on_garbage_bytes():
    """Unreadable bytes are a different failure (caught elsewhere); this check must
    not itself crash the pipeline on malformed input."""
    from polyglo.visuals import _looks_like_moderation_placeholder

    assert _looks_like_moderation_placeholder(b"not an image") is False


def test_nvidia_generator_raises_on_blank_moderation_placeholder(monkeypatch, tmp_path):
    import polyglo.visuals as visuals_mod
    from polyglo.pipeline import StepOutcome
    from genblaze_core.models import Asset

    blank = _solid_color_jpeg_bytes()
    written_path = tmp_path / "blank.jpg"
    written_path.write_bytes(blank)

    def fake_run_step(*a, **kw):
        asset = Asset(asset_id="a1", url=written_path.as_uri(), media_type="image/jpeg",
                      sha256="f" * 64, size_bytes=len(blank))
        return StepOutcome(ok=True, run_id="r1", model_requested="m", model_used="m",
                          assets=[asset], latency_ms=1500)

    monkeypatch.setattr(visuals_mod, "run_step", fake_run_step)
    gen = visuals_mod.NvidiaVisualGenerator()
    with pytest.raises(VisualError, match="moderation"):
        gen.generate("a cat", "m")


def test_nvidia_generator_accepts_a_real_looking_image(monkeypatch, tmp_path):
    """The false-positive guard: a genuine, low-detail-but-not-blank image must still
    pass through normally."""
    import polyglo.visuals as visuals_mod
    from polyglo.pipeline import StepOutcome
    from genblaze_core.models import Asset

    real = _varied_jpeg_bytes()
    written_path = tmp_path / "real.jpg"
    written_path.write_bytes(real)

    def fake_run_step(*a, **kw):
        asset = Asset(asset_id="a1", url=written_path.as_uri(), media_type="image/jpeg",
                      sha256="e" * 64, size_bytes=len(real))
        return StepOutcome(ok=True, run_id="r1", model_requested="m", model_used="m",
                          assets=[asset], latency_ms=8000)

    monkeypatch.setattr(visuals_mod, "run_step", fake_run_step)
    gen = visuals_mod.NvidiaVisualGenerator()
    result = gen.generate("a cat", "m")
    assert result.image == real


# ---------------------------------------------------------------------------
# OpenRouterVisualGenerator — session injected, no real network call. Real spike
# (real Seedream image, then a second image using the first as a reference and
# genuinely preserving the same character) recorded in this session's exploration,
# not repeated on every test run.
# ---------------------------------------------------------------------------


def _b64_png(payload: bytes = b"\x89PNG fake bytes") -> str:
    return base64.b64encode(payload).decode()


class _FakeImageResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body if body is not None else {"data": [{"b64_json": _b64_png()}]}
        self.text = text

    def json(self):
        return self._body


class FakeOpenRouterImageSession:
    def __init__(self, response: _FakeImageResponse | None = None, raise_on_post=None):
        self._response = response or _FakeImageResponse()
        self._raise_on_post = raise_on_post
        self.calls: list[dict] = []

    def post(self, url, *, headers, json, timeout):
        if self._raise_on_post:
            raise self._raise_on_post
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self._response


def test_openrouter_visual_generator_produces_a_real_manifest():
    session = FakeOpenRouterImageSession()
    gen = OpenRouterVisualGenerator(api_key="k", session=session)
    result = gen.generate("a boy and his dog at a market", "m")

    assert result.sha256
    assert len(result.image) > 0
    assert session.calls[0]["url"] == "https://openrouter.ai/api/v1/images"
    assert session.calls[0]["json"]["model"] == "bytedance-seed/seedream-4.5"
    assert session.calls[0]["json"]["prompt"].startswith("a boy and his dog at a market")
    assert "input_references" not in session.calls[0]["json"]


def test_openrouter_visual_generator_includes_reference_image_when_given():
    session = FakeOpenRouterImageSession()
    gen = OpenRouterVisualGenerator(api_key="k", session=session)
    ref = b"\x89PNG previous scene bytes"
    gen.generate("the same boy under a tree", "m", reference_image=ref)

    refs = session.calls[0]["json"]["input_references"]
    assert len(refs) == 1
    assert refs[0]["type"] == "image_url"
    decoded = base64.b64decode(refs[0]["image_url"]["url"].split(",", 1)[1])
    assert decoded == ref


def test_openrouter_visual_generator_raises_on_non_200_response():
    session = FakeOpenRouterImageSession(_FakeImageResponse(status_code=429, text="rate limited"))
    gen = OpenRouterVisualGenerator(api_key="k", session=session)
    with pytest.raises(VisualError, match="429"):
        gen.generate("a cat", "m")


def test_openrouter_visual_generator_raises_on_unexpected_response_shape():
    session = FakeOpenRouterImageSession(_FakeImageResponse(body={"data": []}))
    gen = OpenRouterVisualGenerator(api_key="k", session=session)
    with pytest.raises(VisualError, match="unexpected"):
        gen.generate("a cat", "m")


def test_openrouter_visual_generator_raises_on_transport_failure():
    session = FakeOpenRouterImageSession(raise_on_post=ConnectionError("dns failure"))
    gen = OpenRouterVisualGenerator(api_key="k", session=session)
    with pytest.raises(VisualError, match="dns failure"):
        gen.generate("a cat", "m")


def test_openrouter_visual_generator_raises_on_blank_moderation_placeholder():
    from io import BytesIO
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (64, 64), color=(0, 0, 0)).save(buf, format="PNG")
    blank_b64 = base64.b64encode(buf.getvalue()).decode()

    session = FakeOpenRouterImageSession(_FakeImageResponse(body={"data": [{"b64_json": blank_b64}]}))
    gen = OpenRouterVisualGenerator(api_key="k", session=session)
    with pytest.raises(VisualError, match="moderation"):
        gen.generate("a cat", "m")


def test_openrouter_visual_generator_conforms_to_the_visual_generator_protocol():
    from polyglo.visuals import VisualGenerator
    assert isinstance(OpenRouterVisualGenerator(api_key="k", session=FakeOpenRouterImageSession()), VisualGenerator)


def test_openrouter_visual_generator_spends_one_budget_unit_per_call(tmp_path):
    from polyglo.qa.budget import DailyCallBudget
    budget = DailyCallBudget(5, tmp_path / "budget.json", label="OpenRouter")
    session = FakeOpenRouterImageSession()
    gen = OpenRouterVisualGenerator(api_key="k", session=session, budget=budget)

    gen.generate("a cat", "m")
    assert budget.remaining() == 4


def test_openrouter_visual_generator_raises_before_any_network_call_once_budget_exhausted(tmp_path):
    from polyglo.qa.budget import BudgetExceeded, DailyCallBudget
    budget = DailyCallBudget(0, tmp_path / "budget.json", label="OpenRouter")
    session = FakeOpenRouterImageSession()
    gen = OpenRouterVisualGenerator(api_key="k", session=session, budget=budget)

    with pytest.raises(BudgetExceeded):
        gen.generate("a cat", "m")
    assert session.calls == []


# ---------------------------------------------------------------------------
# FallbackVisualGenerator — real cross-provider fallback, found necessary live
# 2026-08-02: the old NVIDIA-only fallback_models list pointed at flux.1-schnell,
# confirmed permanently dead, so a real failure used to wait through two doomed
# calls for nothing. This routes a genuine primary failure to a real, different,
# working provider instead.
# ---------------------------------------------------------------------------


class _StubGenerator:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls: list[tuple[str, str, bytes | None]] = []

    def generate(self, prompt, model, *, seed=None, reference_image=None):
        self.calls.append((prompt, model, reference_image))
        if self._error is not None:
            raise self._error
        return self._result


def _fake_result(tag: str):
    import hashlib
    payload = tag.encode()
    return ImageResult(image=payload, sha256=hashlib.sha256(payload).hexdigest(),
                       model=tag, latency_ms=1)


def test_fallback_generator_uses_primary_when_it_succeeds():
    primary = _StubGenerator(result=_fake_result("primary-ok"))
    secondary = _StubGenerator(result=_fake_result("secondary-ok"))
    gen = FallbackVisualGenerator(primary, secondary, secondary_model="secondary-model")

    result = gen.generate("a cat", "primary-model")

    assert result.model == "primary-ok"
    assert len(primary.calls) == 1
    assert secondary.calls == []  # never touched — primary worked


def test_fallback_generator_uses_secondary_when_primary_fails():
    primary = _StubGenerator(error=VisualError("primary is down"))
    secondary = _StubGenerator(result=_fake_result("secondary-ok"))
    gen = FallbackVisualGenerator(primary, secondary, secondary_model="secondary-model")

    result = gen.generate("a cat", "primary-model")

    assert result.model == "secondary-ok"
    assert secondary.calls[0][1] == "secondary-model"  # secondary's OWN model, not primary's


def test_fallback_generator_raises_when_both_fail():
    primary = _StubGenerator(error=VisualError("primary is down"))
    secondary = _StubGenerator(error=VisualError("secondary is down too"))
    gen = FallbackVisualGenerator(primary, secondary, secondary_model="secondary-model")

    with pytest.raises(VisualError, match="primary is down"):
        gen.generate("a cat", "primary-model")


def test_fallback_generator_raises_primary_error_when_no_secondary_configured():
    primary = _StubGenerator(error=VisualError("primary is down"))
    gen = FallbackVisualGenerator(primary, secondary=None)

    with pytest.raises(VisualError, match="primary is down"):
        gen.generate("a cat", "primary-model")


def test_fallback_generator_forwards_reference_image_to_secondary():
    primary = _StubGenerator(error=VisualError("primary is down"))
    secondary = _StubGenerator(result=_fake_result("secondary-ok"))
    gen = FallbackVisualGenerator(primary, secondary, secondary_model="secondary-model")

    ref = b"previous scene bytes"
    gen.generate("a cat", "primary-model", reference_image=ref)

    assert secondary.calls[0][2] == ref
