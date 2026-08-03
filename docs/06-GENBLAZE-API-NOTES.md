# 06 — Genblaze API Notes `[VERIFIED]`

Introspected from the installed source: `genblaze-core 0.3.8`, `genblaze-s3 0.3.6`,
`genblaze-nvidia 0.3.3`. This supersedes the `[VERIFY]` tags in `docs/02`.

---

## 1. Resolved: `Pipeline.run()` returns `PipelineResult`, not a tuple

```python
Pipeline.run(*, sink=None, fail_fast=True, raise_on_failure=None, timeout=None,
             max_retries=None, on_progress=None, progress=None,
             pipeline_timeout=None, on_step_complete=None, on_retry=None
             ) -> PipelineResult
```

The Backblaze README examples showing `run, manifest = Pipeline(...).run(...)` are
**wrong** — or at least not the 0.3.8 signature. Always a single `PipelineResult`.
Task #10's wrapper normalizes this so the rest of the codebase is insulated.

### ⚠️ Gotcha: `raise_on_failure` default flips in 0.4.0

`None` (the current default) emits a `DeprecationWarning` and behaves like `False`.
**Always pass it explicitly** or the behaviour changes under us on an upstream bump.

### ⚠️ Gotcha: `ObjectStorageSink` is single-use

From the docstring:

> *"Sinks with run-scoped resources (e.g. ObjectStorageSink) are closed automatically
> when the run finishes (their `close()` fires in a `finally` block), so such a sink is
> **spent afterward — construct a fresh one per run**."*

**`docs/02` §4 is wrong** — it shows one module-level `storage` sink reused across every
call. That would break on the second run. Build the sink inside a factory function and
call it per run.

```python
def make_sink() -> ObjectStorageSink:      # fresh EVERY run
    return ObjectStorageSink(
        S3StorageBackend.for_backblaze(cfg.b2.bucket,
                                       key_id=cfg.b2.key_id,
                                       app_key=cfg.b2.app_key),
        key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
        parquet_sink=ParquetSink("telemetry/"),
    )
```

---

## 2. Full signatures

```python
Pipeline(name=None, tenant_id=None, *, project_id=None, chain=False,
         structured_log=False, max_concurrency=None, moderation=None,
         tracer=None, preflight=True)

Pipeline.step(provider, *, model, prompt=None, modality=Modality.IMAGE,
              step_type=StepType.GENERATE, fallback_models=None,
              input_from=None, external_inputs=None, expected_duration_sec=None,
              metadata=None, prompt_visibility=PromptVisibility.PUBLIC,
              params=None, **extra_params) -> Pipeline

ObjectStorageSink(backend, *, prefix='genblaze',
                  key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,   # already the default
                  parquet_sink=None, max_upload_workers=4,
                  manifest_lock=None, pipelined_transfer=False,
                  eager_transfer=False, asset_url_policy=URLPolicy.AUTO, ...)

ParquetSink(base_dir, *, policy=None)   # writes runs/ steps/ assets/

S3StorageBackend.for_backblaze(bucket=None, *, region=None, key_id=None,
                               app_key=None, public_url_base=None,
                               auto_lifecycle=False, preflight=True)
```

`Modality` has **four** members: `IMAGE`, `VIDEO`, `AUDIO`, **`TEXT`**.

### Opportunity: route translation through Pipeline as a `TEXT` step

`docs/02` §5.1/5.3 planned to call the bare `chat()` helper for scene splitting and
translation, which keeps those stages **outside** Genblaze's manifest and telemetry.
Using `modality=Modality.TEXT` with `NvidiaChatProvider` puts them inside — every stage
then appears in the manifest, the Parquet tables, and the cost rollups.

That is a direct, cheap win on the **"Use of Genblaze"** criterion. Do it.

---

## 3. Two B2 features are natively supported — use both

### Object Lock (immutability)

```python
from genblaze_core.storage import ObjectLockConfig, ObjectLockMode
ObjectStorageSink(..., manifest_lock=ObjectLockConfig(...))
```

Manifests can be written under B2 Object Lock — immutable retention, unbypassable even
by the root account. **"Approved bundles are cryptographically immutable"** is a strong,
storage-native claim for the judges, and it costs one constructor argument.

### Lifecycle rules

`S3StorageBackend.for_backblaze(..., auto_lifecycle=True)` — automatic lifecycle
management. Pair with the "prune intermediates, keep finals and manifests" cost story.

Both were recommended in `docs/04` as differentiators. They turn out to be one-liners.

---

## 4. Manifest API

```python
Manifest.model_fields = ['canonical_hash', 'encryption_scheme', 'manifest_uri',
                         'run', 'schema_version', 'signature', 'transfer_failures']

Manifest.verify()                          # EXISTS — confirmed via hasattr
Manifest.compute_hash() -> str
Manifest.from_run(run: Run) -> Manifest    # staticmethod — build with ZERO API calls
Manifest.to_canonical_json() -> str
Manifest.to_embed_json() -> str
Manifest.output_asset_ids_missing_sha256()
```

`Manifest.from_run(run)` is the zero-credential path for task #10: construct a `Run`,
build a manifest, verify the hash — no provider, no key, no spend.

Also available: `parse_manifest`, `canonical_hash`, `canonical_json`, `is_valid_sha256`,
`strip_asset_url_credentials` in `genblaze_core.models.manifest`.

---

## 5. Media embedding — audio IS supported

Resolved via `get_handler(mime_type)`:

| MIME | Handler |
|---|---|
| `audio/mpeg` | `Mp3Handler` ✅ |
| `audio/wav` | `WavHandler` ✅ |
| `video/mp4` | `Mp4Handler` |
| `image/png` | `PngHandler` |
| **`audio/x-wav`** | **`None`** ⚠️ |

Handler submodules present: `mp3, wav, aac, flac, mp4, png, jpeg, webp, sidecar`.
Top-level `genblaze_core.media` exports only some of them — **use `get_handler(mime)`
or `SmartEmbedder`, not direct class imports.**

⚠️ `audio/x-wav` returns `None`. Normalize MIME types before calling `get_handler`, or
verify-on-upload will silently fail for some WAV files. `SidecarHandler` is the fallback
and always works.

---

## 6. Built-in mocks — this is the zero-credential unlock

`genblaze_core.mocks` and `genblaze_core.testing` ship real mock providers:

```python
MockProvider(*, name='mock', assets=None, latency=0.0, should_fail=False,
             error_code=ProviderErrorCode.UNKNOWN, error_message='...',
             cost_usd=None)
MockAudioProvider(**kwargs)
MockVideoProvider(**kwargs)
ProviderComplianceTests          # pytest base class for provider conformance
```

Two consequences, both significant:

1. **We do not need to hand-roll mock providers** for tasks #12 and #15. `assets` accepts
   a `Callable[[Step], list[Asset]]`, so mocks can return deterministic seeded output.
2. **`should_fail=True` + `error_code` makes the failover demo work with no keys at all.**
   The `/api/chaos` endpoint can flip a real provider to a failing `MockProvider` and the
   Genblaze `fallback_models` chain will visibly recover — on camera, for free. This is
   safer than depending on a live provider outage during the demo.

`cost_usd` on the mock also means the cost dashboard renders realistic figures before any
real spend exists.

---

## 7. NVIDIA providers

```python
NvidiaImageProvider(api_key=None, *, http_timeout=120.0, gen_base_url=None,
                    nvcf_status_url=None, nvcf_timeout=120.0, output_dir=None,
                    http_client=None, models=None, retry_policy=None, ...)
NvidiaAudioProvider(...)   # same shape
NvidiaVideoProvider(..., poll_interval=10.0)
NvidiaChatProvider(api_key=None, *, base_url=None, timeout=60.0, reasoning=None,
                   media_io_kwargs=None, mm_processor_kwargs=None, client=None, ...)
module-level: chat(), achat()
```

All accept `api_key=None` (falls back to env) and a `retry_policy`.

`NvidiaChatProvider` has `media_io_kwargs` / `mm_processor_kwargs`, consistent with
multimodal input — the ASR-inside-Genblaze path. Combined with
`Pipeline.step(external_inputs=[Asset(...)])` (which requires the provider to declare
`accepts_chain_input=True`), this is how audio would be fed in for transcription.
**Still needs a live key to confirm** — task #17.

---

## 8. Outstanding

| Item | Status |
|---|---|
| `PipelineResult` field list | Introspection returned `error_summary, failed_steps, save, succeeded_steps` — likely properties, not the full model. Confirm against a real run in task #10. |
| `NvidiaChatProvider` audio input | Needs a live key (task #17) |
| `ObjectLockConfig` constructor args | Not yet introspected; needed only when Object Lock is wired |
| NVIDIA credit cost per call | **The number that sets demo scope.** Task #17. |
