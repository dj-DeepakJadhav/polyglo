# 07 — Devpost submission copy (ready to paste)

Filled in from the real, as-shipped system — not the template placeholders in
`05-SUBMISSION-KIT.md` §4. Replace the two bracketed URLs before submitting; every
other number and claim here is from an actual run or test, cited inline.

---

## The problem

Producing comprehensible-input language content in 20 languages means 20× the
translation work and, worse, 20× the QA burden — and that QA step is still humans
listening to every generated audio file against a transcript. It's the reason small
teams ship two languages, not twenty.

## What Polyglo does

One source story fans out to N locales. Scene visuals are generated **once** and
shared across every locale by content hash — never regenerated per language.
Narration is generated per locale — **real audio, via Google Gemini's TTS model** —
then verified automatically: the pipeline transcribes the generated audio back with
a *different* model than the one that generated it and diffs the transcript against
the source text. Word Error Rate above threshold means the audio is wrong; the
pipeline retries on a fallback voice, escalates to a stronger model, and finally
quarantines the segment for human review only if every attempt fails. This isn't
theoretical — a real run produced a real 3-word phrase transcribed with one word
dropped, scored a real WER of 0.33, retried on two different real voices, and was
correctly quarantined when none cleared threshold. Every step — generation and
verification — is a real Genblaze `Pipeline` run with a hash-verified manifest,
persisted to Backblaze B2. A live dashboard control (a "chaos" toggle) lets anyone —
judge included — force a model to fail on demand and watch the fallback chain
recover in real time, not just in a slide.

## Why it's different

Cross-modal verification (TTS → ASR → diff) is an established technique from speech
evaluation research (PCTS — percentage of completely correct transcribed sentences).
What's missing from every localization pipeline we looked at is putting it inside
production as an actual **blocking gate**, with a real retry/escalate/quarantine state
machine behind it — not a one-off QA script run by hand after the fact.

## How we use Backblaze B2

- **Content-addressed blob store** (`polyglo/store.py`) — every image and audio asset
  is keyed by its own SHA-256 hash. Writing identical bytes twice is a no-op.
- **Measured result, not an assertion**: a real run (4 scenes × 4 locales, real NVIDIA
  image generation via `flux.1-dev`) produced **32 asset references → 20 unique
  blobs — 37.5% deduplicated**, entirely from the 4 scene images being shared instead
  of duplicated per locale. The ratio only grows with locale count, since images stay
  fixed at `scenes` while references grow at `scenes × locales`.
- **Real, verified upload** — `B2Backend` talks to B2 over the S3-compatible API via
  `boto3`, confirmed against a live bucket.
- **Genblaze's own Parquet telemetry lake** (`runs`/`steps`/`assets` tables from
  `ParquetSink`, plus our own `qa` table) persisted alongside every real pipeline run,
  queried live with DuckDB for the dashboard's dedup ratio, retry evidence, and
  per-model latency figures.
- **Bucket configured with Object Lock + SSE-B2 encryption** — the capability for
  cryptographically immutable, approved bundles is wired in (`ObjectLockConfig` is a
  one-line `ObjectStorageSink` argument).

## How we use Genblaze

- Every generation step — scene splitting, translation, image generation, narration —
  runs through a real `Pipeline().step().run()` call, producing a genuine,
  hash-verified `Manifest`.
- **`fallback_models=[...]`** chains are wired into the visual provider, and a live
  "chaos" toggle (`POST /api/chaos/{model}/disable`) forces a model to fail on demand
  so the fallback recovery is demonstrable on camera, not just theoretical.
- **`ObjectStorageSink` + `S3StorageBackend.for_backblaze()`** persists real pipeline
  runs to B2; a reusable `ParquetSink` writes the telemetry lake the dashboard reads.
- **Manifest verification** (`manifest.verify()`) is exposed directly in the UI: drop a
  manifest JSON sidecar into the `/verify` page and get a pass/fail on its hash and
  every output asset's declared checksum.
- Real NVIDIA image generation (`black-forest-labs/flux.1-dev`) confirmed live — 200
  OK, verified JPEG output, ~7.5–8.2s per call.

## AI providers and models used

| Stage | Provider | Model |
|---|---|---|
| Scene splitting / translation | NVIDIA NIM | `meta/llama-3.1-8b-instruct` |
| Image generation | NVIDIA NIM | `black-forest-labs/flux.1-dev` |
| Narration (TTS) | Google Gemini API | `gemini-2.5-flash-preview-tts` |
| ASR verification | Google Gemini API | `gemini-2.5-flash` |
| Storage | Backblaze B2 | S3-compatible API via `boto3` |

## Honest limitations

- **The gate verifies the TTS round trip, not translation accuracy.** This is the
  most important boundary to state plainly. We transcribe the generated audio and
  diff it against *the translated text we fed to TTS* — so we prove the audio says
  the words it was given. We do **not** prove those words are a correct translation.
  A perfect WER score is fully consistent with fluently narrating a bad translation.
  Caught: dropped/truncated words, silence, mispronunciation that changes words,
  wrong-language output and untranslated source leakage (both via `text_gate.py`).
  Not caught: a fluent but semantically wrong translation. So the precise claim is
  that we made **the TTS-verification half** of localization QA a pipeline stage —
  the half that is real, currently manual, and expensive. Translation-accuracy
  scoring (back-translation + semantic similarity, or an LLM-as-judge rubric) is the
  next stage, and we're not claiming it.
- **WER also doesn't catch** unnatural prosody or cultural inappropriateness. This
  shrinks the human review queue; it doesn't eliminate it.
- **The narrator and the verifier are the same vendor (both Google Gemini).** Our own
  design principle is "the verifier must not be the generator" — a same-family pairing
  means a correlated failure could in principle pass undetected. We shipped it anyway
  because real, imperfectly-independent narration is a strictly better product state
  than no real narration at all, and we say so here rather than glossing over it.
- **NVIDIA's own TTS model (Magpie) is out of scope for a concrete reason, not a
  vague one**: it ships as a self-hosted GPU microservice — you deploy the container
  yourself via NGC onto your own hardware — not a hosted API call the way image and
  chat generation are. Every plausible hosted-endpoint slug returned 404 because no
  hosted endpoint for this model family exists; fixing it means provisioning GPU
  infrastructure, not a config change.
- **Gemini's free-tier TTS model caps at 3 requests per minute** — a hard, Google-side
  quota separate from our own daily call budget. A story with several narration
  segments generated within the same minute will hit it. The gate detects this
  specifically and quarantines fast instead of burning retries on doomed attempts, but
  it's a real pacing constraint on how much real narration one run produces quickly.
- This is a factory/orchestration layer, not a learner-facing product — the consumer
  app that would sit on top of these bundles is out of scope.

---

## Links to paste in

- Live demo: **https://polyglo.onrender.com/**
- 3-minute demo video: `[TODO — paste YouTube/Vimeo URL after recording]`
- GitHub repository: **https://github.com/dj-DeepakJadhav/polyglo** (public, confirmed)
