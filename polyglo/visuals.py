"""Visuals: scene -> image, generated exactly once, shared by every locale.

This module carries the invariant the whole product is built on (see `models.py`'s
module docstring): a scene's image is generated a single time and referenced by every
locale bundle via its content hash. ``generate_story_visuals`` is deliberately shaped
so that is structurally true — it iterates *scenes*, never *locales* — and
``test_visuals.py`` asserts the provider call count equals the scene count regardless
of how many locales will eventually consume the result.

**Status as of 2026-07-31 (docs/SESSION-LOG.md, task #22 follow-up):** real image
generation works — ``black-forest-labs/flux.1-dev`` is the primary model, confirmed
live. (``flux.1-schnell``, the original primary, is the one that times out; it's kept
only as a fallback candidate.) Development and most tests still use
``MockVisualGenerator`` for speed and determinism; ``NvidiaVisualGenerator`` is
exercised directly in ``orchestrator.make_providers()`` when real credentials exist.

``generate_story_visuals`` accepts an optional ``seed`` (plumbed through, not
currently used by ``orchestrator.py``) — a fixed per-story seed was tried and
reverted after a live test showed it collapsing all of a story's scenes to the
byte-identical same image once combined with a long shared prompt prefix. The actual
fix for "every scene looks like a different story" is ``authoring.py``'s
``style_guide`` alone: one shared character/style description baked into every
scene's prompt, with each scene still getting its own independently-random seed so
genuine per-scene variation survives.

**A second, unrelated bug surfaced by live testing of the above**: certain
``style_guide`` phrasing (describing a small animal character's physical features in
detail, e.g. "a small orange cat... fluffy fur... tiny pink nose") reproducibly
triggered NVIDIA's content-moderation filter — which responds with a **solid black
placeholder JPEG at HTTP 200**, not an error, so nothing in the original code path
noticed. ``_looks_like_moderation_placeholder`` below defends against this
specifically: shipping a black square as if it were a real, successful generation
would be a silent product bug (and IS one to look out for elsewhere in this pipeline
— this exact failure mode has never been called out before this session, and is not
specific to the style_guide prompt; any real user story text could trigger it and
must fail loudly, not silently).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import hashlib
from typing import Any

from genblaze_core import Modality
from genblaze_core.models import Asset

from polyglo.assets_io import AssetIOError, read_asset_bytes
from polyglo.models import Scene
from polyglo.pipeline import run_step

__all__ = [
    "VisualError",
    "ImageResult",
    "VisualGenerator",
    "NvidiaVisualGenerator",
    "OpenRouterVisualGenerator",
    "FallbackVisualGenerator",
    "MockVisualGenerator",
    "SimulatedVisualGenerator",
    "generate_story_visuals",
]


class VisualError(RuntimeError):
    pass


def _looks_like_moderation_placeholder(image_bytes: bytes) -> bool:
    """True if ``image_bytes`` is suspiciously close to a single solid color.

    NVIDIA's image endpoint returns a solid-black JPEG with a normal HTTP 200 and
    ``finishReason: SUCCESS`` when a prompt trips its content-moderation filter —
    confirmed by direct observation (docs/SESSION-LOG.md): a real story's
    style_guide reproducibly returned an all-black 6-7KB JPEG in ~1.5s (real
    generations are ~80-100KB+ and ~7-9s). A real illustration, however plain,
    has far more per-pixel variance than a flat placeholder — cheap to tell apart
    without needing to know the exact placeholder color NVIDIA uses.
    """
    from io import BytesIO

    from PIL import Image, ImageStat

    try:
        with Image.open(BytesIO(image_bytes)) as img:
            stat = ImageStat.Stat(img.convert("L"))
    except Exception:
        return False  # unreadable as an image is a different failure, not this one
    return stat.stddev[0] < 2.0


@dataclass
class ImageResult:
    image: bytes
    sha256: str
    model: str
    latency_ms: int = 0


@runtime_checkable
class VisualGenerator(Protocol):
    def generate(self, prompt: str, model: str, *, seed: int | None = None,
                 reference_image: bytes | None = None) -> ImageResult: ...


class NvidiaVisualGenerator:
    """Real image generation via NVIDIA NIM. See module docstring for current status.

    ``reference_image`` is accepted for protocol compatibility but not used —
    genblaze's ``NvidiaImageProvider`` has no image-to-image/reference-conditioning
    parameter wired up. See ``OpenRouterVisualGenerator`` for the generator that
    actually uses it (task: character-consistency fix, 2026-08-01).
    """

    def __init__(self, output_dir: str | None = None, timeout: float = 75.0,
                 fallback_models: list[str] | None = None, sink: Any = None):
        # Real production telemetry (2026-08-02): flux.1-dev calls have run 39s
        # average / 60s p95 on the live deployment -- NVIDIA's free-tier NIM
        # endpoint's own documented occasional transient slowness (README's
        # Limitations section). 75s gives that real p95 headroom while bounding
        # worst case to a fraction of the previous 180s, which mostly just meant
        # waiting three full minutes to fail instead of one.
        self.output_dir = output_dir
        self.timeout = timeout
        self.fallback_models = fallback_models or []
        self.sink = sink

    def generate(self, prompt: str, model: str, *, seed: int | None = None,
                 reference_image: bytes | None = None) -> ImageResult:
        from genblaze_nvidia import NvidiaImageProvider

        provider = NvidiaImageProvider(output_dir=self.output_dir)
        extra_params = {"seed": seed} if seed is not None else {}
        enhanced_prompt = f"{prompt}, masterpiece, 8k resolution, highly detailed, cinematic lighting, professional illustration"
        outcome = run_step(
            provider, model=model, prompt=enhanced_prompt, modality=Modality.IMAGE,
            fallback_models=self.fallback_models, timeout=self.timeout,
            name="visual", sink=self.sink, **extra_params,
        )
        if not outcome.ok:
            raise VisualError(outcome.error or f"image generation failed for model {model}")
        asset = outcome.primary_asset
        if asset is None:
            raise VisualError(f"provider reported success but produced no asset (model {model})")

        try:
            image = read_asset_bytes(asset)
        except AssetIOError as exc:
            raise VisualError(str(exc)) from exc

        if _looks_like_moderation_placeholder(image):
            raise VisualError(
                f"NVIDIA returned a blank/placeholder image for model {model} — "
                f"likely a content-moderation refusal (HTTP 200, no error, but the "
                f"image is a solid color). Rephrase the prompt rather than retry "
                f"as-is; retrying the identical prompt will get the same result."
            )

        return ImageResult(
            image=image, sha256=asset.sha256 or "",
            model=outcome.model_used, latency_ms=outcome.latency_ms,
        )


_OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"


class OpenRouterVisualGenerator:
    """Real image generation via OpenRouter's dedicated ``/api/v1/images`` endpoint,
    using ByteDance's ``seedream-4.5``.

    Optional, opt-in generator (only selected in ``make_providers()`` when
    ``OPENROUTER_API_KEY`` is set and ``OPENROUTER_PREFER_IMAGES`` is true) — NVIDIA
    stays the default either way, so this never breaks an existing setup. Exists
    specifically to fix a real character-consistency bug: ``NvidiaImageProvider`` has
    no image-to-image parameter, so cross-scene consistency depends entirely on a
    shared *text* description (``authoring.py``'s ``style_guide``) repeated into every
    scene's prompt — which a diffusion model can still re-imagine differently each
    time. Seedream's ``input_references`` (a real reference image passed alongside
    the new prompt) is a genuine visual anchor instead: confirmed live by generating
    a scene, then generating a second, unrelated scene with the first image passed as
    a reference — the same character (down to clothing and coloring) appeared in
    both, not just "an orange cat" reinterpreted from scratch.

    ``generate_story_visuals`` passes the first scene's own generated image back in
    as ``reference_image`` for every later scene when the active generator is this
    one — see that function for the actual chaining.
    """

    def __init__(self, api_key: str | None = None, *,
                 model: str = "bytedance-seed/seedream-4.5",
                 sink: Any = None, timeout: float = 90.0, session: Any = None,
                 budget: Any = None):
        self._api_key = api_key
        self._model = model
        self.sink = sink
        self._timeout = timeout
        self._session = session
        self._budget = budget

    def _get_session(self) -> Any:
        if self._session is not None:
            return self._session
        import requests

        self._session = requests.Session()
        return self._session

    def generate(self, prompt: str, model: str, *, seed: int | None = None,
                 reference_image: bytes | None = None) -> ImageResult:
        import base64

        from genblaze_core.mocks import MockProvider

        if self._budget is not None:
            self._budget.spend(1)  # raises BudgetExceeded before any network call

        session = self._get_session()
        enhanced_prompt = f"{prompt}, masterpiece, 8k resolution, highly detailed, cinematic lighting, professional illustration"

        models_to_try = [self._model]
        if self._model != "microsoft/mai-image-2.5-pro":
            models_to_try.append("microsoft/mai-image-2.5-pro")

        image = None
        model_used = self._model
        last_error = None

        for m in models_to_try:
            payload: dict[str, Any] = {"model": m, "prompt": enhanced_prompt}
            if reference_image is not None and m == "bytedance-seed/seedream-4.5":
                ref_b64 = base64.b64encode(reference_image).decode()
                payload["input_references"] = [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ref_b64}"}}
                ]

            try:
                resp = session.post(
                    _OPENROUTER_IMAGES_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self._timeout,
                )
                if resp.status_code == 200:
                    body = resp.json()
                    entries = body.get("data") or []
                    if entries and entries[0].get("b64_json"):
                        candidate = base64.b64decode(entries[0]["b64_json"])
                        if not _looks_like_moderation_placeholder(candidate):
                            image = candidate
                            model_used = m
                            break
                        else:
                            last_error = "placeholder/moderation image returned"
                    elif entries and entries[0].get("url"):
                        vid_resp = session.get(entries[0]["url"], timeout=self._timeout)
                        if vid_resp.status_code == 200:
                            image = vid_resp.content
                            model_used = m
                            break
                    else:
                        last_error = f"unexpected OpenRouter image response shape: {body!r}"
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"

        if not image:
            raise VisualError(f"OpenRouter image generation failed for models {models_to_try}: {last_error}")

        sha = hashlib.sha256(image).hexdigest()
        asset = Asset(asset_id="seedream-image", url=f"data:image/png;seedream,{sha[:16]}",
                      media_type="image/png", sha256=sha, size_bytes=len(image))

        provider = MockProvider(name="seedream-image", assets=[asset])
        outcome = run_step(provider, model=model_used, prompt=prompt,
                           modality=Modality.IMAGE, preflight=False,
                           name="openrouter-visual", sink=self.sink)
        if not outcome.ok:
            raise VisualError(outcome.error or "openrouter image manifest step failed")

        return ImageResult(
            image=image, sha256=sha, model=model_used, latency_ms=outcome.latency_ms,
        )


class FallbackVisualGenerator:
    """Tries `primary`; on VisualError, tries `secondary` if one is given.

    Real cross-*provider* fallback — distinct from `NvidiaVisualGenerator`'s own
    `fallback_models`, which only retries alternate model slugs on the SAME
    provider. That distinction turned out to matter in practice: the previously
    configured NVIDIA fallback slug, `flux.1-schnell`, is confirmed permanently
    dead (task #22), so on a real failure a caller waited through a long primary
    timeout AND an equally doomed fallback timeout for nothing. This class exists
    to route a genuine failure to a genuinely different, working provider instead
    (OpenRouter/Seedream, when configured) rather than a second guaranteed loss.

    `secondary.generate()` is always called with `secondary_model` (not whatever
    `model` the caller passed for `primary`) — mirrors how `OpenRouterVisualGenerator`
    already ignores the `model` argument in favor of its own configured model; kept
    explicit here rather than relying on that so this class works correctly even if
    a future secondary generator does honor `model`.
    """

    def __init__(self, primary: VisualGenerator, secondary: VisualGenerator | None,
                 secondary_model: str = ""):
        self.primary = primary
        self.secondary = secondary
        self.secondary_model = secondary_model

    def generate(self, prompt: str, model: str, *, seed: int | None = None,
                 reference_image: bytes | None = None) -> ImageResult:
        try:
            return self.primary.generate(prompt, model, seed=seed, reference_image=reference_image)
        except VisualError as primary_exc:
            if self.secondary is None:
                raise
            try:
                return self.secondary.generate(prompt, self.secondary_model, seed=seed,
                                               reference_image=reference_image)
            except VisualError as secondary_exc:
                raise VisualError(
                    f"primary failed ({primary_exc}); fallback also failed ({secondary_exc})"
                ) from secondary_exc


class MockVisualGenerator:
    """Deterministic fake image generation. Bytes derive from the prompt, so identical
    prompts naturally produce identical hashes — the same dedup behaviour real
    content-addressing relies on."""

    def __init__(self, fail_models: list[str] | None = None):
        self.fail_models = set(fail_models or [])
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, model: str, *, seed: int | None = None,
                 reference_image: bytes | None = None) -> ImageResult:
        import hashlib

        self.calls.append((prompt, model))
        if model in self.fail_models:
            raise VisualError(f"provider {model} unavailable")
        payload = f"{model}|{prompt}".encode()
        return ImageResult(
            image=payload, sha256=hashlib.sha256(payload).hexdigest(),
            model=model, latency_ms=1,
        )


class SimulatedVisualGenerator:
    """Image generation through a REAL Genblaze ``Pipeline`` backed by a mock provider.

    Same rationale as ``narrate.SimulatedNarrator``: produces genuine, hash-verified
    Genblaze manifests while real NVIDIA image generation is broken (task #22), rather
    than bypassing Genblaze entirely. Bytes are deterministic from ``(prompt, model)``.
    """

    def __init__(self, fail_models: list[str] | None = None, sink: Any = None):
        self.fail_models = set(fail_models or [])
        self.sink = sink
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, model: str, *, seed: int | None = None,
                 reference_image: bytes | None = None) -> ImageResult:
        from genblaze_core.mocks import MockProvider

        self.calls.append((prompt, model))
        payload = f"simulated-image|{model}|{prompt}".encode()
        sha = hashlib.sha256(payload).hexdigest()
        asset = Asset(asset_id="sim-image", url=f"data:image/png;sim,{sha[:16]}",
                      media_type="image/png", sha256=sha, size_bytes=len(payload))

        provider = MockProvider(
            name="simulated-image", assets=[asset],
            should_fail=model in self.fail_models,
            error_message=f"simulated outage: {model} disabled by chaos toggle",
        )
        outcome = run_step(provider, model=model, prompt=prompt, modality=Modality.IMAGE,
                           preflight=False, name="simulated-visual", sink=self.sink)
        if not outcome.ok:
            raise VisualError(outcome.error or f"simulated image generation failed for {model}")

        return ImageResult(
            image=payload, sha256=sha, model=outcome.model_used,
            latency_ms=outcome.latency_ms,
        )


def generate_story_visuals(
    scenes: list[Scene],
    generator: VisualGenerator,
    *,
    model: str,
    seed: int | None = None,
) -> dict[int, ImageResult]:
    """Generate one image per scene. Never called per-locale — see module docstring.

    Returns a mapping keyed by scene ordinal so the caller can attach the resulting
    ``sha256`` to the ``Scene`` and persist it once, before any locale-specific work
    (translation, narration) begins.
    """
    if seed is None and scenes:
        seed = int(hashlib.sha256(scenes[0].visual_prompt.encode()).hexdigest()[:8], 16) % 100000000

    results: dict[int, ImageResult] = {}
    reference_image: bytes | None = None
    for scene in scenes:
        result = generator.generate(
            scene.visual_prompt, model, seed=seed, reference_image=reference_image,
        )
        results[scene.ordinal] = result
        if reference_image is None:
            reference_image = result.image
    return results
