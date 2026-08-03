"""Thin wrapper over the Genblaze Pipeline API.

Everything that generates media goes through here, so the rest of the codebase never
touches Genblaze directly. Three concrete reasons, all discovered by introspection and
a mock-provider spike rather than from the docs:

1. **The published examples are wrong about the return type.** Backblaze's README shows
   ``run, manifest = Pipeline(...).run(...)``. It actually returns a single
   ``PipelineResult`` with ``.run`` and ``.manifest`` attributes — tuple-unpacking it
   raises. One wrapper means that surprise is fixed in one place.

2. **``ObjectStorageSink`` is single-use.** Its ``close()`` fires in a ``finally`` block
   when a run finishes, so the sink is spent afterward. A shared module-level sink works
   exactly once and then fails confusingly. :func:`make_sink` builds a fresh one per run.

3. **``raise_on_failure`` changes default in 0.4.0.** Leaving it unset emits a
   DeprecationWarning today and silently flips behaviour on an upstream bump. We always
   pass it explicitly.

The wrapper also flattens what the dashboard needs — cost, latency, retries, and whether
a fallback model was used — out of the nested run/step structure and into one flat
:class:`StepOutcome`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from genblaze_core import Modality, Pipeline
from genblaze_core.models import Asset, Manifest, Run

from polyglo.config import Config, get_config

__all__ = [
    "StepOutcome",
    "make_sink",
    "run_step",
    "verify_manifest",
    "manifest_report",
]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class StepOutcome:
    """Flattened result of one generation step."""

    ok: bool
    run_id: str
    model_requested: str
    model_used: str
    canonical_hash: str | None = None
    manifest: Manifest | None = None
    manifest_uri: str | None = None
    assets: list[Asset] = field(default_factory=list)
    cost_usd: float = 0.0
    latency_ms: int = 0
    retries: int = 0
    error: str | None = None

    @property
    def fell_back(self) -> bool:
        """True when a fallback model produced the output.

        This is the failover demo's evidence: the manifest records which model actually
        ran, so recovery is provable after the fact rather than only visible in logs.
        """
        return self.ok and self.model_used != self.model_requested

    @property
    def primary_asset(self) -> Asset | None:
        return self.assets[0] if self.assets else None

    def summary(self) -> str:
        if not self.ok:
            return f"FAILED {self.model_requested}: {self.error}"
        note = f" (fell back from {self.model_requested})" if self.fell_back else ""
        return (
            f"{self.model_used}{note} — {len(self.assets)} asset(s), "
            f"${self.cost_usd:.4f}, {self.latency_ms}ms"
        )


# ---------------------------------------------------------------------------
# Sink
# ---------------------------------------------------------------------------


def make_sink(cfg: Config | None = None, *, parquet_dir: str | None = "telemetry/"):
    """Build a **fresh** ObjectStorageSink. Never cache the result.

    Returns ``None`` when B2 credentials are absent, in which case runs still execute
    and still produce verifiable manifests — they just are not persisted to object
    storage. That is what keeps the whole pipeline exercisable with no keys.
    """
    cfg = cfg or get_config()
    if not cfg.has_b2:
        return None

    from genblaze_core.storage import KeyStrategy, ObjectStorageSink
    from genblaze_s3 import S3StorageBackend

    backend = S3StorageBackend.for_backblaze(
        cfg.b2.bucket,
        key_id=cfg.b2.key_id,
        app_key=cfg.b2.app_key,
    )

    kwargs: dict[str, Any] = {"key_strategy": KeyStrategy.CONTENT_ADDRESSABLE}
    if parquet_dir:
        from genblaze_core.storage import ParquetSink  # type: ignore[attr-defined]

        kwargs["parquet_sink"] = ParquetSink(parquet_dir)

    return ObjectStorageSink(backend, **kwargs)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _extract(result: Any, requested_model: str, elapsed_ms: int) -> StepOutcome:
    run: Run = result.run
    manifest: Manifest | None = getattr(result, "manifest", None)
    steps = list(getattr(run, "steps", []) or [])
    step = steps[0] if steps else None

    status = str(getattr(step, "status", "") or "").lower()
    ok = "succeed" in status or "complete" in status

    return StepOutcome(
        ok=ok,
        run_id=str(getattr(run, "run_id", "") or ""),
        model_requested=requested_model,
        model_used=str(getattr(step, "model", requested_model) or requested_model),
        canonical_hash=getattr(manifest, "canonical_hash", None),
        manifest=manifest,
        manifest_uri=getattr(manifest, "manifest_uri", None),
        assets=list(getattr(step, "assets", []) or []),
        cost_usd=float(getattr(step, "cost_usd", None) or 0.0),
        latency_ms=elapsed_ms,
        retries=int(getattr(step, "retries", 0) or 0),
        error=getattr(step, "error", None),
    )


def run_step(
    provider: Any,
    *,
    model: str,
    prompt: str,
    modality: Modality,
    fallback_models: list[str] | None = None,
    sink: Any = None,
    timeout: float = 300.0,
    name: str = "polyglo",
    metadata: dict[str, Any] | None = None,
    preflight: bool = True,
    **params: Any,
) -> StepOutcome:
    """Run a single-step pipeline and flatten the result.

    ``preflight=False`` silences Genblaze's "no family matched" warning for models it
    does not recognise — needed for mock providers, and harmless for real ones.
    """
    started = time.perf_counter()

    # Construction (Pipeline(), .step()) is inside the same try as .run(). Genblaze
    # validates the provider type at .step() time and raises TypeError immediately —
    # that must be caught here too, or "run_step never raises" would be false for the
    # single most common caller mistake (passing something that is not a BaseProvider).
    try:
        pipeline = Pipeline(name, preflight=preflight)
        pipeline.step(
            provider,
            model=model,
            prompt=prompt,
            modality=modality,
            fallback_models=fallback_models,
            metadata=metadata,
            **params,
        )
        result = pipeline.run(
            sink=sink,
            timeout=timeout,
            raise_on_failure=False,   # explicit: the default flips in 0.4.0
        )
    except Exception as exc:
        return StepOutcome(
            ok=False,
            run_id="",
            model_requested=model,
            model_used=model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )

    return _extract(result, model, int((time.perf_counter() - started) * 1000))


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def verify_manifest(manifest: Manifest) -> bool:
    """Strict verification: canonical hash **and** every output asset's sha256."""
    try:
        return bool(manifest.verify())
    except Exception:
        return False


def manifest_report(manifest: Manifest) -> dict[str, Any]:
    """Detail for the verify-on-upload widget.

    ``verify()`` alone collapses several distinct failures into one ``False``. The UI
    should distinguish a tampered payload (hash mismatch) from a manifest whose outputs
    simply never declared a sha256, so both are reported separately.
    """
    report: dict[str, Any] = {
        "canonical_hash": getattr(manifest, "canonical_hash", None),
        "schema_version": getattr(manifest, "schema_version", None),
        "manifest_uri": getattr(manifest, "manifest_uri", None),
    }

    try:
        report["hash_ok"] = bool(manifest.verify_hash())
    except Exception:
        report["hash_ok"] = report["canonical_hash"] == manifest.compute_hash()

    try:
        missing = list(manifest.output_asset_ids_missing_sha256() or [])
    except Exception:
        missing = []
    report["assets_missing_sha256"] = missing

    report["verified"] = verify_manifest(manifest)
    report["detail"] = (
        "verified"
        if report["verified"]
        else "hash mismatch — content does not match the manifest"
        if not report["hash_ok"]
        else f"{len(missing)} output(s) declare no sha256"
    )
    return report
