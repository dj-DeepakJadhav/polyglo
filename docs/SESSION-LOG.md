# Session Log

Append-only record of findings from autonomous build sessions. Newest entries at the bottom.

---

## 2026-07-31 — Task #1: Environment verification

**Result: PASS. No fallback interpreter needed.**

### Interpreters on this machine

| Version | Path | Notes |
|---|---|---|
| 3.10 | `C:\Installed\Python310\python.exe` | Available fallback |
| 3.12 | — | **Orphaned registry entry, not actually installed** |
| 3.14.5 | `C:\Installed\Python\python.exe` | System default, **in use** |

The `py` launcher is not installed; interpreters were located via
`HKCU:\SOFTWARE\Python\PythonCore`. Use full paths, not `py -3.x`.

### Virtualenv

```
.venv\Scripts\python.exe   →  Python 3.14.5
```

### Installed versions `[VERIFIED]`

| Package | Version |
|---|---|
| genblaze | 0.4.5 |
| genblaze-core | 0.3.8 |
| genblaze-s3 | 0.3.6 |
| genblaze-nvidia | 0.3.3 |
| boto3 / botocore | 1.43.60 |
| pydantic | 2.13.4 |
| pillow | 11.3.0 |

Python 3.14 wheel availability was the main risk and it is **not** a problem —
`cp314` wheels exist for both `pillow` and `pydantic-core`.

### Import smoke test — all pass

```python
import genblaze, genblaze_core, genblaze_s3, genblaze_nvidia   # OK
from genblaze_core import Pipeline, Modality                    # OK
```

### Finding: `Modality` has four members, not three

```python
Modality: ['IMAGE', 'VIDEO', 'AUDIO', 'TEXT']
```

`docs/02` only referenced IMAGE / VIDEO / AUDIO. **`Modality.TEXT` exists**, which may
be the correct modality for the chat/translation stages rather than bypassing Pipeline
with the bare `chat()` helper. Worth checking in task #3 — routing translation through
Pipeline as a TEXT step would put the translation stage inside Genblaze's manifest and
telemetry, which strengthens the "Use of Genblaze" story.

### Note on `genblaze` version pinning

The umbrella `genblaze` 0.4.5 pins `genblaze-core<0.4,>=0.3.8` and `genblaze-s3<0.4,>=0.3.6`.
Pin exact versions in `pyproject.toml` so an upstream release mid-hackathon cannot break
the build.

---

## 2026-07-31 — Tasks #2, #3: Scaffold and API introspection

Scaffold committed (`bd4837a`); full API findings in
[06-GENBLAZE-API-NOTES.md](06-GENBLAZE-API-NOTES.md) (`f1b01ef`).

Whole stack installs clean on 3.14 — duckdb 1.5.5, pyarrow 25.0.0, fastapi 0.141.1,
pytest 9.1.1. No dependency risk remains.

**Three findings that changed the design** (details in doc 06):

1. `ObjectStorageSink` is **single-use** — self-closes after each run. `docs/02` §4 showed
   a shared module-level sink, which would have failed on the second run. Use a factory.
2. Genblaze ships `MockProvider(should_fail=..., cost_usd=...)` — the **failover demo and
   cost dashboard work with zero keys**, and that is more reliable on camera than hoping a
   live provider misbehaves.
3. Object Lock (`manifest_lock=`) and lifecycle (`auto_lifecycle=True`) are one-liners.

---

## 2026-07-31 — Task #4: Domain model and SQLite index

`polyglo/models.py`, `polyglo/db.py`, `tests/test_models_db.py` — **20 tests pass.**

Design notes:

- **SQLite is a rebuildable cache, never source of truth.** `rebuild_from_b2()` exists and
  deliberately raises `NotImplementedError` pointing at task #17, so the recovery contract
  is explicit rather than aspirational.
- `bundle_refs` is the table that makes the dedup claim *measurable*: `COUNT(*)` vs
  `COUNT(DISTINCT sha256)`. Without it the headline number would be asserted, not computed.
- `QAStatus.RETRIED.is_good == True` — passed on retry is still shippable. Kept distinct
  from `PASS` because the retry history is what proves the gate does real work.
- Added `QAStatus.UNVERIFIED` (not in `docs/02`) for the degraded path where audio exists
  but no ASR was available. Silent `PENDING` would have been indistinguishable from
  "not started".

**Bug caught by a test:** `slugify("El Niño's Café!")` produced `el-nino-s-cafe` because
apostrophes were treated as separators. Fixed in the implementation (strip apostrophes
before splitting), not by relaxing the test.

**Guard test:** `test_scene_image_hash_is_not_clobbered_by_later_upsert` — the pipeline
saves scenes before generating visuals, so a naive upsert would erase image hashes on any
later metadata write. `COALESCE` in the upsert prevents it.

---

## 2026-07-31 — Task #5: Content-addressed blob store

`polyglo/store.py`, `tests/test_store.py` — **39 tests pass total.**

- `LocalBackend` / `B2Backend` behind one `StorageBackend` protocol. Every test runs on
  `LocalBackend`: no credentials, no network.
- Layout `blobs/<sha[0:2]>/<sha[2:4]>/<sha256>` — fans out so no directory or key prefix
  gets hot.
- **Chose boto3 directly over reusing Genblaze's `S3StorageBackend`.** Genblaze owns its
  asset/manifest keyspace under the sink; this store owns the bundle/blob keyspace.
  Separating them means neither can surprise the other with a key-layout change, and
  boto3 is already a hard dependency of `genblaze-s3`.
- Writes go to a temp file then move, so a crash mid-write cannot leave truncated bytes
  sitting at a hash that claims to describe them. Test: `test_partial_write_leaves_no_blob`.
- `store.verify(sha)` re-hashes and compares — content addressing makes corruption
  detectable for free, because the key *is* the checksum.

`test_locale_fanout_stores_image_once` encodes the headline demo scenario: 20 puts of the
same image → 1 write, 19 dedup hits, 95% hit rate.

---

## 2026-07-31 — B2 bucket provisioned (user)

| Setting | Value |
|---|---|
| Bucket | `polyglo` |
| Bucket ID | `27ac93f1e302ba5b90f80011` |
| Endpoint | `s3.eu-central-003.backblazeb2.com` |
| Type | **Private** — app proxies blobs server-side, so no presigned-URL expiry during judging |
| Object Lock | **Enabled** (one-way door at creation; capability only, **no default retention rule**) |
| Encryption | Enabled (SSE-B2) |

`.env` written with `B2_KEY_ID`, `B2_BUCKET`, `B2_ENDPOINT`. Confirmed gitignored
(`git check-ignore` passes; only `.env.example` is tracked).

**Still outstanding:** `B2_APP_KEY` (B2 shows the secret once, at creation),
`NVIDIA_API_KEY`, `GEMINI_API_KEY`. Task #17 stays blocked until at least the B2 secret
and the NVIDIA key are present.

---

## 2026-07-31 — Task #6: Normalisation and numeral expansion

`polyglo/qa/normalize.py`, `polyglo/qa/numerals.py`, `tests/test_normalize.py` —
**117 tests pass total.**

Numeral expansion covers en/es/fr/de/it/pt/ja at 0–9999 with the awkward cases handled:
French 70/80/90 arithmetic and positional trailing -s, German unit-before-ten compounds,
Spanish 16–29 single words and cien/ciento, Italian vowel elision before uno/otto.

### Two decisions that diverge from `docs/02` §6.2

1. **Hyphens become spaces**, contrary to the spec's "keep intra-word hyphens".
   Keeping them makes `quatre-vingt-dix` one token where ASR may emit three — turning a
   single orthographic difference into three word errors. Splitting is strictly more robust.
2. **Apostrophes are dropped, not split.** `l'eau` / `l’eau` / `leau` all fold together,
   killing the straight-vs-curly-quote mismatch class outright.

### Bug avoided: diacritic stripping must be Latin-only

Devanagari vowel signs are Unicode category `Mn`, exactly like Latin accents. A naive
`NFD + drop Mn` would turn `किताबें` into `कतबन` and make every Hindi WER score noise.
`strip_latin_diacritics` only removes marks whose base character is Latin. Guarded by
`test_devanagari_matras_survive_diacritic_stripping` and a Japanese dakuten test.

### Japanese needs character-level tokenisation

Japanese has no word spaces, so word-level WER would compare two arbitrary segmentations
and report nonsense. `tokenize()` falls back to characters for `ja/zh/th/lo/my/km`.

### Declared limitations rather than hidden ones

- **Hindi 21–99 unsupported** — each is individually irregular, and a wrong word is worse
  than an unexpanded digit because it fails the gate *and* lies in the diff panel. The
  expander returns `None`, digits are left intact, and `COVERAGE` says so. Follow-up task
  created; practical mitigation is to avoid digits in Hindi seed content.
- **Thousands separators not handled** — `1,000` vs `1.000` is genuinely ambiguous across
  locales and guessing would corrupt decimals.
- **German variant forms** — `hundert`/`einhundert` and `tausend`/`eintausend` are both
  valid. We emit the explicit `ein-` forms on the reasoning that TTS engines reading
  numerals aloud tend to be explicit, and the ASR transcript is what we must match. This
  is a genuine coin-flip until real Riva output is observed; revisit in task #18.

---

## 2026-07-31 — Task #7: WER, PCTS and alignment

`polyglo/qa/wer.py`, `tests/test_wer.py` — **144 tests pass total.**

One Levenshtein pass with backtrace yields both the score and the word-level alignment.
Computing the score alone would leave the diff panel with nothing to render — a judge
would see "WER 0.31" and no explanation.

- **WER is deliberately uncapped.** A hypothesis longer than the reference exceeds 1.0
  through insertions. A runaway TTS appending garbage *should* score worse than one that
  merely got every word wrong.
- **Backtrace resolves ties diagonal-first**, so a changed word renders as one
  substitution rather than a delete beside an insert. Without that the diff panel shows
  two unrelated-looking errors for a single mistake.
- **Empty reference** returns 0.0 when the hypothesis is also empty, 1.0 otherwise —
  a usable number for the gate instead of a division by zero.
- **PCTS** (fraction of exactly-correct segments) is reported alongside mean WER because
  it is much harsher and far harder to game. `test_pcts_is_harsher_than_mean_wer` pins
  the case where mean WER looks acceptable at 0.20 while PCTS is 0.

Short sentences are high variance — one wrong word in five is WER 0.20, already past the
0.10 pass threshold. Pinned in a test as evidence that thresholds need real calibration
rather than armchair reasoning.

**Test bug, not code bug:** my first `test_wer_can_exceed_one_with_many_insertions`
miscounted tokens (8 hypothesis words against a 1-word reference is 1 hit + 7 insertions).
Implementation was correct; the expectation was fixed.

---

## 2026-07-31 — Task #8: The QA gate

`polyglo/qa/gate.py`, `tests/test_gate.py` — **169 tests pass total.** All 25 gate tests
passed on first run.

Structure: `Narrator` and `Transcriber` are **separate protocols**, so "the verifier is
not the generator" is a structural property rather than a convention someone can quietly
break. Transcribing with the model family that synthesised the audio would be grading
homework with its own author, and correlated failures would pass straight through.

Escalation ladder: primary voice → alternate voice → stronger model family → quarantine.
A WER past the retry band (or a provider error) **skips the sibling voice** and goes
straight to escalation rather than wasting an attempt.

Three behaviours worth noting:

1. **Every attempt is recorded, including successful ones.** The retry history is the
   demo artifact. A gate that only logs its winner proves nothing on camera.
2. **Quarantine keeps the *best* attempt, not the last.** A reviewer needs the closest the
   pipeline got. `test_quarantine_keeps_the_best_attempt_not_the_last` pins it.
3. **No transcriber → `UNVERIFIED`, never silent `PENDING`.** Audio is still produced and
   explicitly marked ungraded. `PENDING` would be indistinguishable from "not started" —
   the invisible-failure mode `docs/02` §11 rules out.

`MockTranscriber` / `MockNarrator` live in the package rather than the test suite,
because the FastAPI app uses them in no-credentials mode — the entire UI, including a
live retry-and-recover demo, is exercisable before any API key exists.

---

## 2026-07-31 — Task #9: Pre-narration text gate

`polyglo/qa/text_gate.py`, `tests/test_text_gate.py` — **202 tests pass total.**
All 33 gate tests passed on first run.

Runs on translated text *before* TTS. Catching a bad translation here costs a fraction of
a chat call; catching it after narration costs a TTS call, an ASR call and a retry — and
the QA gate would blame the audio for what is actually a text defect.

Three checks, ordered by how often LLM translation actually fails that way:

1. **Untranslated echo** — model returns the source verbatim. Most common by far, and
   trivially detectable through the shared normaliser (so it survives punctuation and
   case differences).
2. **Wrong script** — Latin where Devanagari or Japanese was expected, i.e. romanisation
   or a silent fallback to English. Near-certain evidence, so this rejects regardless of
   input length.
3. **Wrong language, same script** — weakest check, and it defers when unsure.

### The design decision that matters: asymmetric bias

A false reject burns a regeneration. A false accept only defers the problem to the QA
gate, which is a much cheaper mistake. So the gate is deliberately biased toward
accepting:

- inputs under 4 tokens are **never** rejected on language grounds
- language rejection needs confidence ≥ 0.45 (tunable per call)
- unlisted target locales skip the script check rather than failing it

Detection is script-based (near-certain) plus weighted function words and diacritic
evidence for Latin scripts. Stated plainly in the module docstring: this is a coarse
detector for gross failures, **not** a general-purpose language identifier. Spanish and
Portuguese overlap heavily and are the most likely confusion — pinned by
`test_low_confidence_does_not_reject`.

`contains_source_leakage()` is kept **separate and advisory** rather than wired into the
hard reject, because short shared word runs are legitimate between related languages.

---

## 2026-07-31 — Task #10: Genblaze pipeline wrapper

`polyglo/pipeline.py`, `tests/test_pipeline.py` — **232 tests pass total.**

Wraps `Pipeline().step().run()` so the rest of the codebase never touches Genblaze
directly, fixing the three surprises found during introspection (task #3): single-use
sinks (`make_sink()` builds fresh, never cached), the `raise_on_failure` default flip
in 0.4.0 (always passed explicitly), and the tuple-vs-`PipelineResult` mismatch against
the published README examples.

**Real bug caught by a test, not by inspection:** the first draft put `pipeline.step()`
*outside* the wrapper's `try/except`, which only wrapped `.run()`. Genblaze validates the
provider type at `.step()` time and raises `TypeError` immediately for anything that
isn't a `BaseProvider` — so the single most common caller mistake (passing a bare
function or a wrong object) would have crashed straight through a wrapper whose entire
purpose is "never raises." `test_run_step_never_raises_on_a_malformed_provider` caught
it; fixed by moving construction inside the same `try` as execution.

`manifest_report()` deliberately separates hash mismatch (tampering) from assets missing
a `sha256` (a different failure a plain `verify()` collapses into one `False`) — the
verify-on-upload UI needs to say which one happened.

All tests run against Genblaze's own `MockProvider` — a real `Pipeline().step().run()`
executes, just against a fake provider, so this doubles as proof the zero-credential path
genuinely works end to end, not merely that our code compiles.

---

## 2026-07-31 — User provisioned credentials; Session 0 partially run (task #17, still blocked)

`.env` populated with `B2_KEY_ID`, `B2_BUCKET`, `B2_ENDPOINT`, `NVIDIA_API_KEY`,
`GEMINI_API_KEY`. Installed missing extras: `genblaze-nvidia[chat]` (needs the `openai`
package, not pulled by the base install) and `google-genai`.

**Results — a genuinely mixed picture, not a clean pass:**

| Check | Result | Evidence |
|---|---|---|
| B2 auth | ❌ **FAIL** | `SignatureDoesNotMatch`. `B2_APP_KEY` is byte-identical to `B2_KEY_ID` (both 25 chars) — the keyID was pasted into both fields. B2 shows the real `applicationKey` only once, at key creation. **Needs the user to create a new key and copy the actual secret.** |
| NVIDIA chat | ✅ PASS | Live call to `meta/llama-3.1-8b-instruct` returned correctly. Confirmed via `/v1/models`: 102 chat/LLM models available on this account. |
| NVIDIA audio (TTS) | ❌ **FAIL** | All three bundled model slugs (`nvidia/magpie-tts-multilingual`, `nvidia/fugatto`, `nvidia/maxine-voice-font`) — genblaze's own `validate_model(refresh=True)` reports `NOT_FOUND` / "upstream probe returned DEAD". **`genblaze-nvidia` 0.3.3's audio model registry is stale against NVIDIA's current catalog.** This is the SDK's own probe, not a network fluke. |
| NVIDIA image | ❌ **FAIL** | `black-forest-labs/flux.1-schnell` — genblaze's family-based check says `OK_PROVISIONAL` (unverifiable, not a real probe), but two independent live attempts (240s timeout, then a 90s attempt with an explicit 45s `http_timeout`/`nvcf_timeout` override) both hung to a transport timeout. Consistent failure, not a one-off. |
| Gemini | ✅ PASS | `client.models.list()` succeeded. Catalog includes `gemini-2.5-flash-preview-tts` and `gemini-2.5-pro-preview-tts` — a viable narration alternative if the NVIDIA audio path stays dead. |

**Net effect:** chat/translation generation is provably working. **Image and audio
generation — the two modalities the demo needs most — are currently non-functional via
NVIDIA in this SDK version**, and B2 storage cannot be tested until the app key is fixed.
This is reported plainly rather than smoothed over, because it changes near-term
priorities: task #17 stays **in_progress, not completed**, pending (a) a corrected
`B2_APP_KEY`, and (b) a decision on the image/audio path — retry with different models,
investigate whether NVIDIA image generation needs async polling this SDK isn't doing
correctly, or fall back to Gemini for one or both modalities.

**Two follow-up tasks created** rather than guessed past: task #21 (fix B2 credentials —
user action) and task #22 (resolve NVIDIA image/audio generation — needs investigation,
possibly a genblaze-nvidia version bump or different provider).

### GeminiBudget — explicit daily call cap

The user asked directly to keep Gemini usage capped on a daily basis, not just by cost.
Added `polyglo/qa/budget.py` (`GeminiBudget`, `tests/test_budget.py`, 13 tests) — a
**hard gate**, not a warning: `spend()` raises `BudgetExceeded` once the cap is hit, and
the counter persists to `data/gemini_budget.json` so a process restart mid-day does not
grant a fresh budget. Default cap 50 calls/day, configurable via `GEMINI_DAILY_CALL_CAP`.
Wired into `Config` as `GeminiConfig`. Not yet wired into an actual Gemini call site
(there isn't one yet — ASR integration is task #12/#17-continuation) but the gate exists
before any Gemini spend happens, per the instruction.

---

## 2026-07-31 — Task #11: Telemetry writer and DuckDB queries

`polyglo/telemetry.py`, `tests/test_telemetry.py` — **251 tests pass total.**

### Ground-truthed the schema before writing a line of SQL

Rather than guess at what Genblaze's `ParquetSink` produces, ran an actual
`Pipeline().step().run()` (mock provider, zero API calls) against a local `ParquetSink`
and read the real Parquet files back. `ParquetSink` turned out to be a standalone
`BaseSink` — it does **not** require `ObjectStorageSink` or B2, which is what made this
possible with no credentials at all. Confirmed schema, hive-partitioned as
`<table>/dt=<date>/tenant_id=<id>/modality=<m>/provider=<p>/<run_id>.parquet`:

- `runs`: run_id, parent_run_id, tenant_id, project_id, name, status, step_count,
  canonical_hash, created_at
- `steps`: run_id, step_id, provider, model, step_type, modality, status, prompt, seed,
  params_json, asset_count, retries, cost_usd, error, error_code, started_at,
  completed_at — **no `latency_ms` column**, only the two timestamps
- `assets`: run_id, step_id, asset_id, url, media_type, sha256, size_bytes, plus nullable
  video/audio metadata fields

`docs/02` §8 assumed a `latency_ms` field on steps that doesn't exist; the real queries
compute it via `date_diff('millisecond', started_at, completed_at)` in SQL instead.

### Two real bugs, both caught by tests, neither by inspection

1. **`.df()` requires pandas/numpy**, which aren't installed. Switched `_query()` to
   `duckdb.sql(...).fetchall()` + column zip — no new dependency.
2. **Vocabulary collision in the fixture generator.** `Attempt.status` (the real,
   per-attempt verdict from `gate.py`) is `pass`/`retry`/`escalate`/`error`. The scene-level
   `QAStatus` (`pass`/`retried`/`quarantined`/`unverified`) is a *different* vocabulary
   that lives on `LocalizedScene` in SQLite. `seed_fixture_telemetry` initially wrote the
   scene-level vocabulary into the per-attempt table, which silently broke
   `qa_retry_evidence()` — its `ever_passed = max(status = 'pass')` check never matched
   because nothing was ever literally `'pass'` on a recovered attempt. Fixed by making the
   fixture emit exactly the vocabulary `QAEvent.from_gate_result` actually produces in
   production.
3. **`ORDER BY n DESC` had no tiebreaker.** Two equal-count status groups returned in
   an order DuckDB does not guarantee, so two runs of `seed_fixture_telemetry(seed=42)`
   compared unequal despite identical input — caught by
   `test_seed_fixture_telemetry_is_deterministic`. Fixed with `ORDER BY n DESC, status`.

### Design notes

- `qa_retry_evidence()` is arguably the most important query in the whole telemetry
  layer: segments that failed at least once and eventually passed. That is the literal
  proof the QA gate does real work, and it is what the demo's centerpiece 40 seconds
  shows on screen.
- `dedup_stats()` on the Parquet lake mirrors `polyglo.db.dedup_stats` on SQLite — the
  two independent implementations should always agree; drift between them would mean
  the sink and the local index have gone out of sync.
- `seed_fixture_telemetry()` is deterministic via a hand-rolled LCG rather than
  `random` (which is disallowed for reproducibility reasons elsewhere in this build) —
  and it directly serves the current situation: real image/audio generation is blocked
  (tasks #21/#22), so this is what makes the dashboard demonstrable regardless.

---

## 2026-07-31 — Task #12: Authoring, localization, narration, visuals

`polyglo/chat.py`, `authoring.py`, `localize.py`, `assets_io.py`, `narrate.py`,
`visuals.py`, plus five test files — **302 tests pass total.**

Every generation-facing module follows the `Narrator`/`Transcriber` split already
established in `gate.py`: a protocol, a real NVIDIA-backed implementation, and a mock.
Since NVIDIA image and audio are confirmed broken (task #22), the real adapters
(`NvidiaVisualGenerator`, `NvidiaNarrator`) are fully wired and tested against a
**monkeypatched `run_step`** — proving the plumbing (`run_step` → `Asset` → bytes)
rather than the provider, which is honest about what can and can't be verified right
now. Chat is the one modality confirmed live, so `authoring.py`/`localize.py` are
exercised for real against `NvidiaChatCompleter`'s shape via `MockChatCompleter`.

### `chat.py` — shared JSON repair-retry

`complete_json()` extracts JSON from code fences or prose, and on a parse failure sends
a **corrective prompt naming the actual parse error** back to the model, not just "try
again." Shared by scene splitting and (indirectly) nothing else yet, but written once
rather than duplicated between the two call sites that need it.

### `assets_io.py` — new, small, load-bearing

NVIDIA providers write to `output_dir` and return a `file://` URI (confirmed by
introspection). `read_asset_bytes()` is the one place that URI becomes bytes — handles
the Windows drive-letter leading-slash quirk (`file:///C:/foo` parses with an extra `/`
before `C:`) and raises clearly for any other scheme rather than guessing. Small module,
but every real adapter depends on it, so it has its own focused test file.

### Two real bugs, caught before they were real bugs

1. **`localize_all_locales` was not actually isolating failures.** First draft was a
   plain dict comprehension — one locale's chat outage would have raised and lost every
   other locale's already-successful translation in the same batch. Caught while writing
   `test_localize_all_locales_isolates_per_locale_failures` (the test's own first draft
   asserted the *old*, wrong behavior — fixed the implementation, not the test, since a
   20-locale batch failing as a unit because one endpoint hiccupped is a real production
   bug). Now returns `dict[str, LocalizationResult | LocalizationError]` per locale.
2. Minor: a leftover unused import/name collision in a hand-written test
   (`LocalizationResult` used but not imported) — caught by the test suite itself on
   first run, fixed immediately.

### Design notes

- **`localize_scene` never silently discards a rejected translation.** After exhausting
  `max_attempts`, it returns the last attempt with `accepted=False` rather than raising
  or returning nothing — a human reviewer or the app's quarantine queue may still want
  the closest attempt, mirroring the same principle already established in the QA gate
  (`quarantine keeps the best attempt, not the last`).
- **`generate_story_visuals` structurally cannot be called per-locale** — it takes only
  a list of scenes, no locale parameter. `test_generator_called_exactly_once_per_scene_
  never_per_locale` is the single most important test in the new set: it's the proof,
  at the orchestration layer, that the product's core dedup claim is actually true —
  not just true at the storage layer (already proven in `test_store.py`).
- Chat completions (`authoring`/`localize`) deliberately call `genblaze_nvidia.chat()`
  directly rather than routing through `Pipeline`/`run_step` — they're a control-plane
  concern (structuring text), not a generation step whose output needs a manifest. Only
  `visuals.py`/`narrate.py`, which produce the actual media assets, go through the
  Pipeline/manifest path.

---

## 2026-07-31 — Task #13: FastAPI app with routes, SSE, and chaos toggle

`polyglo/chaos.py`, `orchestrator.py`, `api.py`, plus five test files
(`test_config_capability.py`, `test_simulated_providers.py`, `test_orchestrator.py`,
`test_api.py`) — **371 tests pass total.** All seven documented API routes (docs/02 §9)
implemented and tested.

### The capability gap this task started by closing

Config only distinguished "NVIDIA key present" from nothing — it had no way to say
"the key is valid but this specific modality is known broken." Added
`Config.nvidia_image_audio_broken` (env-driven, defaults `true` per task #22's findings)
and `Config.has_generation = has_nvidia and not nvidia_image_audio_broken`. Without this,
the moment `.env` got a real NVIDIA key, the app would have started trying (and failing)
real image/audio generation on every request, despite #22 already proving both are dead.
12 tests pin this distinction, including the actual current-state case: a valid key
present, chat confirmed live, and `has_generation` still correctly `False`.

### `Simulated*` providers — the strongest available zero-credential story

Rather than have the orchestrator bypass Genblaze entirely when generation is blocked,
`SimulatedNarrator`/`SimulatedVisualGenerator` (in `narrate.py`/`visuals.py`) route
through a **real** `Pipeline().step().run()` backed by `genblaze_core.mocks.MockProvider`.
Every manifest the app produces in demo mode is a genuine, hash-verified Genblaze
artifact — not hand-rolled fake provenance standing in for it. Both accept `fail_models`
for the chaos toggle and a `sink` for real telemetry persistence.

### `orchestrator.py` ties all five stages together

`run_story_pipeline()`: authoring → visuals (once per scene) → per-locale
(localize → narrate → QA gate) → bundle assembly, with a `ProgressCallback` for SSE and
full persistence via `db.py` + `store.py` + `telemetry.py`. `make_providers()` is the one
place "real vs simulated" gets decided, and it constructs a single reusable `ParquetSink`
(confirmed safe to reuse across calls — only `ObjectStorageSink` is single-use, per
task #10) so real pipeline activity actually reaches the telemetry lake instead of
producing manifests that are immediately discarded.

### `api.py` — all seven routes, running with zero credentials

`POST /api/stories` kicks off the pipeline via `BackgroundTasks` (sync callables run in
Starlette's threadpool) and returns immediately; SSE polls an in-memory per-story event
log. `/api/verify` parses the **manifest JSON sidecar** (docs/02 §7's guaranteed
fallback — embedding depends on the media handler/codec and isn't wired into the
pipeline yet) as a real `Manifest` and runs the same `manifest_report()` the pipeline
itself uses, so the UI's verify widget and internal checks agree by construction.
`/api/dashboard` falls back to `seed_fixture_telemetry()` when no real run has happened
yet — the dashboard must never render empty just because generation is currently
blocked. `/api/chaos/{model}/disable` is the failover demo: force a model to fail, and
the QA gate's alternate-voice/escalation ladder engages for real, on camera, with no
dependency on a live outage.

### Five real bugs, all caught by tests before they'd have surfaced in a demo

1. **`localize_all_locales` batch-abort** (surfaced again transitively via orchestrator
   tests, already fixed in task #12) — confirmed still correct here.
2. **`SimulatedVisualGenerator` had no call tracking**, unlike `MockVisualGenerator` —
   added, since the orchestration-layer dedup test depends on it.
3. **Translation-collapse test artifact, twice.** `MockChatCompleter`/simple test
   completers repeat their last scripted response forever. When every scene in a locale
   gets byte-identical translated text, `SimulatedNarrator` correctly (and harmlessly)
   dedupes their audio to one blob — real content-addressing working exactly as
   designed, but it broke tests asserting one audio ref per scene. Hit once in
   `test_orchestrator.py`, then again independently in `test_api.py`. Fixed both by
   keying test translations off the scene index embedded in the prompt instead of using
   a fixed string.
4. **A slow-motion false "hang."** The API test file appeared to hang past a 120s
   ceiling. Root cause: several tests posted `n_scenes: 1` while the test fixture's mock
   chat completer hardcoded a 2-scene split response, so `authoring.split_story`'s own
   count-mismatch check correctly rejected it every time — the pipeline errored, no
   bundle was ever produced, and `wait_for_story()`'s polling loop burned its full
   timeout on each affected test. Several of those stacking up read as a hang but were
   actually N slow, deterministic failures. Fixed by parsing the real requested count
   out of the prompt (`"Split this story into {n} scenes"`) instead of guessing it.
   Separately learned: piping through `Select-Object -Last N` buffers until the whole
   command finishes, so it was also hiding the interim progress that would have made
   this obvious sooner — redirecting straight to a file and reading that instead is
   what actually let this get diagnosed.
5. **`UnsupportedSchemaVersionError` import path.** Present in
   `genblaze_core.models.manifest` but not re-exported at the top-level
   `genblaze_core.models` package — task #3's introspection had recorded it as a
   `models.manifest` export, but I imported from the wrong level anyway. Three test
   failures, one-line fix.
6. **Dashboard fixture-seed/read path inconsistency** — a real design bug, not just a
   test artifact. `dashboard()` reseeded fixtures at a path recomputed from `_cfg.data_dir`
   while reading from `_telemetry`'s own (independently constructed) path. In production
   both derive from the same config and never diverge, which is exactly why this kind of
   bug hides until something — a test monkeypatching one but not the other — forces the
   two paths apart. Fixed by seeding at `_telemetry.base_dir` directly, the single source
   of truth, rather than recomputing a path that's supposed to match it.

Also caught, in the test fixtures themselves rather than the app: `safe_providers()`
initially built `Simulated*` providers with no `sink`, so test-driven pipeline runs never
populated genblaze's own `assets` table — meaning `test_dashboard_reflects_real_runs_
once_they_exist` could never see a run as "live" no matter how many stories the test
created. Fixed by mirroring production's `make_providers()`: construct one real
`ParquetSink` per test and thread it through both providers.

---

## 2026-07-31 — Task #21 RESOLVED: B2 credentials fixed by the user

`B2_APP_KEY` now holds a distinct 31-char value (previously duplicated `B2_KEY_ID`'s
25-char value). Verified with a real `list_objects_v2` call against bucket `polyglo` at
`s3.eu-central-003.backblazeb2.com` — **auth succeeds, `KeyCount: 0`** (empty bucket, as
expected). Task #17 (Session 0) can now proceed on the B2 half; NVIDIA image/audio
remains blocked pending task #22.


---

## 2026-07-31 — Task #14: UI templates and dashboard

`polyglo/web.py`, six Jinja2 templates, `polyglo/static/{style.css,htmx.min.js}` —
**392 tests pass total.** Validated twice: once via the full test suite, once by
actually running the app (`.claude/launch.json` + Browser preview) and clicking
through it live — which is what surfaced everything below.

### Vendored htmx locally rather than trust a CDN + fabricated SRI hash

First draft used a CDN `<script>` tag with a guessed `integrity` attribute — that would
have failed the browser's Subresource Integrity check and silently broken every
interactive page. Downloaded the real `htmx.min.js` (confirmed real JS by content, not
an error page) into `polyglo/static/` instead. No CDN dependency at all now, which also
means the demo doesn't depend on network access to a third party during judging.

### Starlette's `TemplateResponse` signature is version-specific

The installed Starlette (1.3.1) requires `TemplateResponse(request, name, context)`,
not the older `TemplateResponse(name, context_with_request_key)`. Checked the installed
signature before writing any routes rather than guessing from memory or older
tutorials — saved seven call sites from being wrong on the first run.

### Simulated media renders gracefully via the browser's own decode failure

Scene images/audio are `<img>`/`<audio src="/blobs/{sha256}">` with an `onerror` handler
swapping in a placeholder. No server-side format sniffing needed for the common case —
the browser's own failure to decode non-image bytes as an image is the signal.
Confirmed live: real magic-byte detection (`_sniff()`) correctly serves
`image/png`/`audio/wav` when real bytes exist, and the placeholder correctly appeared
for simulated payloads during the live click-through.

### Live click-through surfaced two real bugs neither test suite had caught

**1. Dev-database pollution, root cause finally found.** `get_config()` is
`@lru_cache`'d; `db.session()` calls it fresh (no explicit path) on every request
rather than using the module-level `_cfg` singleton. `monkeypatch.setenv(...)` alone
does nothing until the cache is invalidated — confirmed 48 test-created stories had
silently accumulated in the real dev database at `./data/polyglo.db` before this was
caught. Fixed by adding `reset_config_cache()` immediately after setting env vars (and
again on teardown) in every fixture that touches `POLYGLO_DATA_DIR`/`POLYGLO_DB_PATH`.
Cleared the polluted files — both gitignored, nothing was ever committed, but real
dev-state pollution is worth fixing properly rather than living with. Re-ran the full
suite afterward: exactly one story in the dev database, matching the one live-browser
test story created during manual validation — confirms the fix holds.

**2. A genuine safety bug: `test_web.py` was making real, live NVIDIA API calls.**
`web.py` imports `make_providers` directly from `polyglo.orchestrator` — a separate
name binding from `api.py`'s own import of the same function. The `test_web.py` fixture
patched `api_mod.make_providers` (correct for `test_api.py`, whose routes live in that
module) but never touched `web_mod.make_providers`, so every test that created a story
through the HTML form was calling the real, unmocked function. Confirmed live: chat is
genuinely wired to NVIDIA, so `create_story_form()`'s background task made an actual
API call every time a test posted to `/stories`. Caught only because three tests
happened to assert an exact scene count, and a real LLM call returned 9 scenes instead
of the requested 1 (`AuthoringError: requested 1 scenes, model returned 9`) — every
other story-creation test in the file was making the same live call and simply never
noticed. Fixed by patching `web_mod.make_providers` too. Documented prominently in both
the file docstring and the fixture itself so the next HTML-route test file doesn't
repeat it — this class of bug (a module importing its own separate binding of a shared
name) is exactly what made the earlier `_store`/`_telemetry`/`_chaos` fix necessary too.

**3. A real orchestrator bug, also found live, not by any test:** `QAStatus.is_good`
(PASS/RETRIED only) was the condition gating whether a segment's content made it into
the bundle. With no transcriber configured, every segment correctly comes back
`UNVERIFIED` — but `is_good` treats `UNVERIFIED` exactly like `QUARANTINED`, so every
bundle ended up with zero refs despite real images and audio existing in storage.
Confirmed live: created a story through the actual running app, watched two real scene
images render, and found the bundle table showing "0 image refs, 0 audio refs" for a
pipeline that had completed successfully. Fixed by changing the exclusion condition to
`gate_result.status is QAStatus.QUARANTINED` specifically — `is_good`'s semantics
elsewhere (QA-effectiveness reporting) are unchanged and remain correct.

### Real B2 upload confirmed end-to-end for the first time

The same live browser test that surfaced the bugs above also proved the positive case:
with corrected credentials (task #21), scene image bytes were genuinely uploaded to the
real `polyglo` bucket. Verified directly against B2 afterward: `KeyCount: 2`, two blobs
totaling 241 bytes — left in place as real, harmless proof rather than cleaned up,
unlike the SQLite pollution which was a pure testing artifact.

### Design notes

- `_matrix_fragment.html` is shared between the full story page (initial render) and
  the htmx polling endpoint (`GET /stories/{id}/fragment`) — one template, two entry
  points, one source of truth for what the matrix looks like. Polling
  (`hx-trigger="every 1.5s"`) is conditional on `not done`, confirmed live to stop once
  the pipeline actually finishes rather than polling forever.
- The scene/locale detail page recomputes the WER diff at render time via
  `qa.wer.score(ls.text, ls.transcript, locale)` — the same function the gate itself
  used — rather than persisting a serialized diff, so the UI and the gate's own
  decision can never silently drift apart.
- `TelemetryStore.attempts_for(story_id, locale, ordinal)` is new — one scene/locale
  cell's full retry history, ordered by attempt. This is what the detail page renders
  as proof the gate did real work on that specific segment, not just an aggregate.

---

## 2026-07-31 — Task #15: Capstone end-to-end test; a real session-long slowdown finally root-caused

`tests/test_end_to_end.py` — **370 tests pass total, full suite in 32.5s** (down from
an observed 5+ minutes before the fix below).

### The end-to-end test itself

One story, the project's real 4 default locales, 3 scenes, entirely on mock/simulated
providers. Engineers both failure modes in the same run — a scene/locale cell that
fails its first narration attempt and recovers on the second (retry-and-recover), and a
different cell that fails every attempt and gets quarantined — then asserts the dedup
invariant, QA outcomes, bundle contents, telemetry, and retry evidence all together as
one coherent narrative, distinct in purpose from `test_orchestrator.py`'s many
single-assertion tests.

**Caught while writing it:** a corrupted transcript built from hyphenated substitute
words ("totally-different-word") scored WER 0.417 instead of the intended ~0.17,
because `normalize()` deliberately splits hyphenated text into multiple tokens (needed
for real languages) — turning 2 intended word errors into 5. Computed the WER directly
via `qa.wer.score()` rather than estimating a second time, and used single-word,
non-hyphenated substitutes instead.

### The real story of this task: a session-long "mysterious slowdown," finally explained

While confirming this task's test suite, a full run took over 5 minutes with zero
interim output — alarming enough that its root cause mattered more than the test itself.

**What it looked like, and what it wasn't:** two false leads, ruled out in order:
1. *Thread-pool contention from many background-task-spawning tests* — ruled out by
   running the suspect tests in total isolation; they failed identically alone.
2. *PowerShell/pytest output buffering* — real, but a red herring for the actual delay.
   Piping pytest's output through `Select-Object -Last N` or `Tee-Object` causes Python
   to detect a non-TTY and fully block-buffer, so checking an "interim" output file
   during a run shows nothing regardless of real progress — worth knowing for future
   diagnosis, but not why the suite was actually slow.

**What it actually was:** `GET /api/stories/{id}/events` (the SSE progress endpoint)
polls in a loop with `await asyncio.sleep(0.5)` and no check for whether the client
is still listening. `test_events_stream_opens_cleanly_for_an_id_with_no_events_yet`
deliberately opens the stream and exits without waiting for completion — legitimate
behaviour for what it's testing — but the *server-side* generator has no way to know
that and kept polling regardless, for the full idle budget, **originally hard-coded to
~5 minutes (600 ticks)**. That's not a test bug; the generator would do the exact same
thing against a real browser tab a user closed early.

**First fix attempt was insufficient, and measuring proved it:** added
`await request.is_disconnected()` to the loop — correct in general, but Starlette's
synchronous `TestClient` does not reliably propagate ASGI disconnect events, a known
limitation rather than a bug in this code. Timed `test_api.py` directly before
concluding the fix worked: **312.05 seconds**, unchanged from before — proving the
disconnect check alone did not fix it, rather than assuming it had.

**The actual fix: the idle budget itself was simply wrong.** A real pipeline emits its
first progress event within moments of the POST returning; near-total silence for
anywhere close to 5 minutes means something is already badly broken (a crashed
background thread that didn't even log an error event), and no real user would wait
that long regardless of tests. Cut `max_idle` from 600 ticks (~5 min) to 40 ticks
(~20s) — a product correctness fix, not a testing workaround. Re-timed the same file
directly: **24.23 seconds.** Full suite: **32.53 seconds.** Kept the
`is_disconnected()` check in place — it still saves real resources against a real
ASGI server even though it wasn't what fixed the measured test slowdown.

**Process notes for next time:**
- When "it's taking a long time" with no visible cause, measure directly
  (`[Diagnostics.Stopwatch]`) rather than reasoning about what *should* be true. The
  first fix attempt looked obviously correct and still wasn't sufficient — only a
  before/after timing comparison proved that.
- A cleanup step mid-session mattered: stray orphaned `python.exe` processes from
  earlier killed/interrupted background bash tasks were still resident (confirmed via
  `tasklist`), including a dev server no longer needed. `TaskStop` on a `local_bash`
  task appears to stop the shell wrapper without necessarily killing spawned child
  processes on Windows — worth a `tasklist`/`taskkill` sanity check when a supposedly
  independent test run behaves oddly.

---

## 2026-07-31 — Task #16: Dockerfile, verified via real Docker build/run, found two production bugs

`Dockerfile`, `.dockerignore`, `README.md` (new — was missing entirely), a `pyproject.toml`
packaging fix, and **two real production bugs in `orchestrator.make_providers()`** found
only because "verify local run" meant an actual `docker build` + `docker run`, not just
reading the Dockerfile for plausibility. 370 tests pass; the container was independently
exercised via live HTTP calls, twice (once per bug, before and after each fix).

### Docker environment note

Docker Desktop was installed but not running. Starting it via `Start-Process` (a bash
`&`-backgrounded launch did not actually start the GUI process — needed PowerShell's
`Start-Process` instead) and waiting on `docker info` to succeed was required before any
build/run step could proceed.

### `python:3.12-slim`, not the `3.14` used in local dev

Local dev (task #1) confirmed 3.14 works, but Docker images run manylinux wheels and
3.14 is bleeding-edge enough that a missing Linux wheel for any one dependency would
force a slow source build mid-`docker build`. 3.12 has broad, proven coverage for every
dependency here (confirmed: the actual build pulled clean manylinux wheels for duckdb,
pyarrow, pydantic-core, pillow — no source builds). `requires-python = ">=3.11"` is
satisfied either way.

### Real bug #0, caught before the image even built: no root `README.md`

`pyproject.toml` declares `readme = "README.md"`, which doesn't exist at repo root —
`pip install .` failed immediately with `COPY ... README.md: not found`. This wasn't
just a Docker problem: **the submission requires a root README** (see
`docs/05-SUBMISSION-KIT.md`'s own README structure section) that had simply never been
created. Wrote the real one — architecture diagram, B2/Genblaze usage sections with
concrete specifics, the QA gate explanation with its honest limitation, the AI
providers table, stated limitations (NVIDIA broken, ASR unwired, Hindi numeral gap),
and real run/Docker instructions — not a placeholder to fill in later.

### Real bug #1: `pip install .` would have silently shipped an app with no UI

`pyproject.toml`'s `[tool.setuptools.packages.find]` didn't declare `templates/`/
`static/` as package data. setuptools does not bundle non-`.py` files by default —
without an explicit `[tool.setuptools.package-data]` entry, an installed wheel would
omit every Jinja2 template and the vendored `htmx.min.js`, and `web.py`'s
`Jinja2Templates`/`StaticFiles` would 404 on every page at runtime. Caught by checking
package-data explicitly before writing the Dockerfile, not discovered as a Docker-only
failure — fixed once, source of truth stays in `pyproject.toml` rather than a Dockerfile
workaround.

### Real bug #2: the "zero credentials" fallback crashed on the very first pipeline stage

Built the image, ran the container with **no `.env` mounted** (exactly what a judge
cloning this repo hits first), created a story via the live API, and polled for
completion. It never completed. `curl`'d the SSE events endpoint directly:

```
data: {"stage": "authoring", "detail": "splitting story into 2 scenes", ...}
data: {"stage": "error", "detail": "expected a 'scenes' key, got: {}", ...}
```

`orchestrator.make_providers()`'s no-credentials chat fallback was
`MockChatCompleter(["{}"])` — a test double that returns the literal string `"{}"` for
every call, including the very first scene-split call, which requires a `"scenes"` key.
**Every story creation with zero credentials failed immediately**, directly
contradicting the project's own core promise ("runs fully with zero credentials" —
stated in `docs/02` §11 and in the README just written above). Every prior local test
of this app had a real `.env` with a working NVIDIA key, so `has_nvidia=True` and the
real completer was used — this exact code path had never actually been exercised
end to end before this Docker verification step.

**Fix:** `OfflineChatCompleter` (new, in `polyglo/chat.py`) — production code, not a
test double. Recognises the two real prompt shapes the app sends
(`authoring.SPLIT_PROMPT`'s `"Split this story into {n} scenes"`,
`localize.TRANSLATE_PROMPT`'s `"Text:\n{text}\n\nReturn"`) via regex and returns
well-formed, clearly-labelled placeholder content for each, so the full pipeline can
actually run to completion with zero credentials rather than merely starting and then
immediately failing.

### Real bug #3, found immediately after fixing #2, same function, same root pattern

Re-ran the same offline-pipeline test locally (`NVIDIA_API_KEY=""` etc, faster than a
full Docker rebuild for this iteration) after the `OfflineChatCompleter` fix. Scene
splitting and translation now worked — but **every single segment quarantined at 100%
WER**, for every locale including plain Latin-script ones that should have had no
script/language problem at all.

Root cause: `transcriber: Transcriber | None = MockTranscriber() if not cfg.has_gemini
else None`. `MockTranscriber`'s "echo the correct answer" trick (`self.last_text`) is
only ever populated by `qa/gate.py`'s own `MockNarrator` test double — a coupling the
*production* narrators (`SimulatedNarrator`, `NvidiaNarrator`) know nothing about and
never satisfy. So `MockTranscriber` here always returned an empty/`None` transcript,
scoring 100% WER regardless of what was actually said, and quarantining every segment
unconditionally — a second, independent instance of "a unit-test double got wired into
production code as if it were a working fallback," the same class of bug as
`MockChatCompleter(["{}"])` above. The condition was also backwards relative to any
sensible intent (used the broken double specifically when Gemini was *absent*).

**Fix:** `transcriber = None` unconditionally, until a real ASR transcriber is wired
(task #17-continuation). This correctly degrades the QA gate to `UNVERIFIED` — real
content, honestly flagged as unproven — rather than fabricating a transcript that
scores as a hard failure. Combined with the earlier `UNVERIFIED`-is-shippable fix
(task #14), bundles now actually contain their generated content in zero-credential
mode instead of shipping empty.

### Verified, both fixes together, in the actual running container (not just locally)

```json
{"locale": "es-ES", "image_ref_count": 2, "audio_ref_count": 2}
{"locale": "fr-FR", "image_ref_count": 2, "audio_ref_count": 2}
"dedup": {"total_refs": 8, "unique_blobs": 6, "dedup_ratio": 0.25}
```

Zero credentials, real bundles, real measured dedup, honest `unverified` status
throughout. This is what "the whole app runs with zero credentials" actually means now,
rather than what it merely claimed before this task forced a real end-to-end check.

### Process note

"Verify local run" earned its place in the task list. Every one of the three real bugs
above (missing README, broken chat fallback, broken transcriber fallback) was invisible
to 370 passing tests, because every test's fixtures explicitly construct working mock
providers — none of them exercise `orchestrator.make_providers()`'s own *real*
zero-credential fallback logic, which is precisely the path a judge with no API keys
configured would hit. A green test suite proves the code paths the tests touch; it does
not prove the code paths only a real, credential-less deployment touches. Worth a
dedicated test now — filed as a natural follow-up.

---

## 2026-07-31 — Task #23: Regression test for the zero-credential path

`tests/test_orchestrator_offline.py` — **377 tests pass total** (7 new). Passes in
1.47s standalone.

Directly closes the gap that let both task #16 production bugs ship past 371 passing
tests: every other fixture in this codebase builds its own working mock/simulated
providers by hand, so none of them ever called the *real* `orchestrator.make_providers()`
with zero credentials — exactly the path a judge cloning this repo hits first. This
file forces that exact state (`NVIDIA_API_KEY=""`, `GEMINI_API_KEY=""`, `B2_KEY_ID=""`
via `monkeypatch.setenv` + `reset_config_cache()`) and asserts against the real factory:

- `make_providers()` selects `OfflineChatCompleter`, not the old broken placeholder.
- `make_providers()`'s transcriber is `None`, not the broken `MockTranscriber()`.
- A full pipeline run produces **non-empty** bundles (both bugs completed without
  raising an exception — pytest's default "did it crash" check would have missed
  both; only an explicit content assertion catches a pipeline that "succeeds" while
  silently shipping nothing).
- Segments degrade to `UNVERIFIED`, never `QUARANTINED`, with no real transcriber.
- The dedup invariant holds even in the degraded offline path.
- Scene count matches whatever was actually requested (the narrow symptom of bug #1).

### A second full-suite timing anomaly, correctly NOT chased as a new bug

Immediately after adding this file, a full-suite run took **191.5 seconds** — six
times the ~33s baseline measured right after the SSE idle-timeout fix earlier this
session. Investigated the same way as that earlier incident (checked for stray
processes, checked for a hang) before concluding anything:

- **All 377 tests passed.** No failures, no timeouts on any individual test — the
  defining symptom of the earlier SSE bug (a specific test hanging for minutes) was
  absent entirely. This is a different shape of problem.
- `tasklist` showed only the expected single pytest process (156MB) — no orphaned
  processes this time, unlike the earlier incident.
- `docker ps -a` showed **six unrelated, pre-existing containers actively running**
  (a Penpot stack — frontend/backend/exporter/mcp/postgres/valkey/mailcatcher — plus
  some exited `papiertiger` containers), all restarted ~22 minutes earlier when Docker
  Desktop itself came back up. **These are not part of this project and were not
  touched** — starting Docker Desktop for task #16's verification brought back
  whatever else was already configured to run on this machine, and Docker Desktop's
  own background overhead (its VM backend) plus six live containers is real,
  legitimate CPU/IO contention that would slow down anything on the same box,
  including SQLite writes and DuckDB queries in this test suite.

Correctly distinguished from a code regression: every test still passed, so this is
an environmental cost of a decision made for the *previous* task (starting Docker),
not a new logic bug to hunt for. Did not stop Docker Desktop to reclaim performance —
doing so would also kill the pre-existing, unrelated containers, which are not mine
to manage and may be in active use for something else entirely.

---

## 2026-07-31 — Task #17 continued: real Gemini ASR spike, won decisively, and wired

Closes the remaining scope of task #17's original description: "(c) spike BOTH ASR
paths ... pick a winner on evidence." `polyglo/qa/gemini_transcriber.py` +
`tests/test_gemini_transcriber.py` + `tests/test_make_providers_transcriber_gating.py`
— **400 tests pass total, 32.8s** (back to baseline after the prior entry's transient
191s, confirming that was genuinely just unrelated background Docker load, not a
regression — see that entry).

### The live spike, with real evidence

Generated a real audio clip via Gemini's own TTS (`gemini-2.5-flash-preview-tts`),
then transcribed it back via Gemini's audio-understanding chat call
(`gemini-2.5-flash`), and scored the round trip against the known source text:

```
transcript: 'El gato subió al tejado por la noche.'
WER: 0.0  |  exact match: True
```

**Gemini wins the spike decisively** — real generated audio, real transcription,
zero error. 2 calls spent, tracked via `GeminiBudget` (48/50 remaining that day). The
NVIDIA `NvidiaChatProvider`-with-audio-input path was not separately spiked: real
NVIDIA audio generation is confirmed broken (task #22), so there was no real
NVIDIA-narrated audio available to test transcription against, and testing ASR
against garbage/synthetic bytes proves nothing about real-world accuracy.

### One implementation detail the spike surfaced directly

Gemini's TTS output is raw PCM (`audio/L16;codec=pcm;rate=24000`), not a containered
format. Feeding those bytes to the transcription call without a WAV header first
degraded the round trip — wrapping in a minimal WAV header (`_pcm_to_wav()`) is what
produced the exact-match result above. `GeminiTranscriber` takes an explicit
`audio_mime_hint` per instance (rather than sniffing, which is not reliably possible
for headerless PCM) so this wrapping only happens for audio that actually needs it.

### Wiring into `make_providers()` — the gating is the actual design decision

`GeminiTranscriber` activates **only when both** `cfg.has_gemini` and
`cfg.has_generation` are true — not on either alone:

- `has_gemini` without `has_generation` (a real Gemini key present, but NVIDIA
  image/audio still broken — the account's *actual current state*): `transcriber`
  stays `None`. Sending `SimulatedNarrator`'s fake byte payloads to a real Gemini API
  call would waste real budget on input that can never produce a meaningful
  transcript — confirmed as a deliberate test case
  (`test_no_transcriber_when_gemini_configured_but_generation_still_broken`), because
  it is precisely the account's real state right now and the easiest of the four
  gating combinations to get backwards.
- `has_generation` without `has_gemini`: no way to verify, so `None` — correctly
  degrades to `UNVERIFIED` (shippable, per the earlier `is_good` fix).
- Both true: `GeminiTranscriber(cfg.gemini_api_key, budget=GeminiBudget(...))`. The
  moment task #22 resolves, this activates with no further code change — same
  one-flag-flips-the-behaviour design already established for
  `nvidia_image_audio_broken`.

### Dependency promotion, verified rather than assumed

`google-genai` moved from `[project.optional-dependencies].gemini` to a core
dependency in `pyproject.toml` — it's now used in a real (conditionally activated)
production path, not an opt-in extra. Rebuilt the Docker image from scratch to confirm
this doesn't break anything (own recent lesson: verify claims about builds by actually
building, not by reading the diff and assuming) — clean build, container starts, and
a full story-creation round trip inside the container still produces populated
bundles (`image_ref_count: 2, audio_ref_count: 2`) exactly as before.

### Where this leaves task #17

B2 verified end-to-end (real upload, task #16). NVIDIA chat confirmed live. Both ASR
paths considered; Gemini spiked with real evidence and now wired into production with
correct gating. The only remaining gap is structural, not a bug: `GeminiTranscriber`
cannot be exercised against *real* NVIDIA-narrated audio until task #22 resolves,
because there is no real narrated audio to verify yet — this is the correct, expected
state of the gating logic, not unfinished work on this task's part.

---

## 2026-07-31 — Task #20: Hindi numeral table completed, 0-100

`polyglo/qa/numerals.py`, `tests/test_normalize.py` — **412 tests pass total.**

The original table (0–20, tens only) deliberately declined 21–99 with an explicit
comment: "getting one wrong produces a false WER failure that looks like a TTS bug...
until the table is completed and verified by someone who reads Devanagari." That
caveat mattered — I am not a native Hindi reader, and generating 79 precise Unicode
Devanagari sequences from recall alone carries real transcription risk that plain
"knowing the numbers" doesn't eliminate.

**Grounded the work in an external source rather than pure recall.** Fetched a
structured Hindi numbers table (`englishtohindi.net/hindi-numbers`) covering 21–99,
then independently spot-checked the three entries where a first recollection attempt
diverged from that source (44, 75, 79) against separate search results. All three
confirmed the source's spelling (44 चौवालीस, 75 पचहत्तर, 79 उन्यासी) — reasonable
cross-referenced confidence, though the code comment explicitly still recommends a
native-Hindi-reading spot check before relying on this beyond a hackathon QA gate, in
the same spirit as every other honestly-stated limitation in this project.

Completed `_HI_TABLE` to a literal 0–100 lookup (Hindi 21–99 has no algorithmic
pattern — each number is individually irregular, unlike the European languages'
tens-plus-ones compounds). Updated `COVERAGE["hi"]` from "21-99 unsupported" to
"0-100". Replaced the stale test (`test_hindi_declines_unsupported_compounds`, which
asserted `47` was unsupported — no longer true) with parametrized coverage across the
newly-supported range, a full 0–100 completeness check, a test that decline behavior
now correctly triggers only outside 0–100 (e.g. 101), and a real-sentence
`expand_numerals()` test rather than only the bare lookup.

Swept `README.md` and `docs/PROGRESS.md` for the now-stale "21–99 unsupported" claim
and updated both — worth doing immediately rather than letting a fixed limitation
keep advertising itself as still broken in the docs a judge or future session reads.

---

## 2026-07-31 — Task #22 follow-up: image generation actually works; only audio is dead

`polyglo/config.py`, `polyglo/orchestrator.py`, `polyglo/api.py`,
`tests/test_config_capability.py`, `tests/test_make_providers_transcriber_gating.py`,
`tests/test_orchestrator_offline.py`, `.env.example`, `CLAUDE.md`, `README.md`,
`docs/PROGRESS.md` — full suite still passes (370+ tests, zero credentials).

The earlier task #22 conclusion ("NVIDIA image and audio generation confirmed
non-functional") was itself based on incomplete testing. Re-examined it rather than
just re-asking the user to pick between "ship as-is" and "spend an hour fixing,"
since a bounded, safe investigation was possible first.

**Technique: raw HTTP calls directly against NVIDIA's endpoint, bypassing genblaze
entirely**, to separate "genblaze's plumbing is broken" from "the underlying NVIDIA
service is broken" — the same root-cause-isolation discipline used earlier in this
session for Docker and SSE issues.

```python
resp = httpx.post(
    "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev",
    headers={"Authorization": f"Bearer {cfg.nvidia_api_key}", "Accept": "application/json"},
    json={"prompt": "a simple red circle on white background", "cfg_scale": 5, "seed": 0, "steps": 20},
    timeout=30,
)
```

Result: **200 OK**, a real 32,880-byte JPEG (verified visually — a correct red
circle), `finishReason: SUCCESS`. This model had been sitting unused in the
*fallback* slot the whole session; only `flux.1-schnell` (the configured primary)
had ever been tested directly, and it really is dead — confirmed again through
genblaze's own `NvidiaImageProvider` directly (25.6s, transport timeout) versus
`flux.1-dev` through the same provider (8.09s, 200 OK). Also tried
`stabilityai/stable-diffusion-3-5-large` (the registry's other bundled image model):
404 — NVIDIA's real current name is `stable-diffusion-3.5-large` (dots, no vendor
prefix); not pursued further since `flux.1-dev` already works.

Re-verified audio is **not** a similar false negative: all 3 bundled slugs
(`nvidia/fugatto`, `nvidia/magpie-tts-multilingual`, `nvidia/maxine-voice-font`) 404
at the raw HTTP level. Searched for a renamed replacement (the same pattern that
explained SD3.5) and found NVIDIA now documents "Magpie TTS Flow / Multilingual /
Zeroshot" — Multilingual specifically claims 9-language support including Hindi,
directly relevant to this project. Tried 6 plausible slug variants
(`nvidia/magpie-tts-multilingual`, `nvidia/magpietts-multilingual`,
`nvidia/magpie_tts_multilingual`, `nvidia/tts-magpie-multilingual`,
`nvidia/riva-tts-magpie-multilingual`, `nvidia/magpie-tts`) — all 404. NVIDIA's
Zeroshot/Flow variants reportedly require an access-approval form, which may explain
why no public slug resolves under a guessable name. Concluded audio remains
genuinely blocked without a documented current model ID to test against, and stopped
rather than continuing to guess slugs indefinitely.

**The fix**: split the single `Config.nvidia_image_audio_broken` flag (env
`NVIDIA_IMAGE_AUDIO_BROKEN`) into two independent ones —
`nvidia_image_broken`/`nvidia_audio_broken` (env `NVIDIA_IMAGE_BROKEN` defaults
`false`, `NVIDIA_AUDIO_BROKEN` defaults `true`), with corresponding
`has_image_generation`/`has_audio_generation`. `orchestrator.make_providers()` now
selects `NvidiaVisualGenerator`/`SimulatedVisualGenerator` and
`NvidiaNarrator`/`SimulatedNarrator` independently instead of as one combined
either-or, and its primary visual model is now `flux.1-dev` (with `flux.1-schnell`
demoted to a fallback candidate, kept only in case NVIDIA's transport issue there is
transient). `GeminiTranscriber`'s gating condition changed from `has_gemini AND
has_generation` to `has_gemini AND has_audio_generation` — deliberately the
audio-specific flag, since this verifier checks narration and image generation
working has no bearing on whether there's real audio to verify.

Updated every reference found via `grep -rln "nvidia_image_audio_broken|has_generation"`
across the repo: both test files that exercised the old combined flag directly
(`test_config_capability.py` rewritten around the two-flag model;
`test_make_providers_transcriber_gating.py` rewritten with an explicit new case —
image working but audio still broken must still yield `transcriber=None`), plus
`.env.example`, `CLAUDE.md`, `README.md` (provider table, both Limitations bullets,
the Genblaze-usage bullet about simulated fallback), and `docs/PROGRESS.md`. Also
added `has_image_generation`/`has_audio_generation` to the `/api/status` JSON
response, consistent with this project's "degraded mode must be visible, never
silent" principle already established elsewhere in `api.py`.

This means: this build now performs **real** NVIDIA image generation end to end
(not a mock fallback) whenever an NVIDIA key is present, while narration remains
simulated-but-genuinely-Genblaze-provenanced until a working TTS model is found. No
account creation, deployment, or other user-gated action was needed to land this —
it was a configuration/model-selection bug all along, not the "ship vs. spend an
hour" decision the task was originally framed as needing.

**Verified end to end, not just at the provider level**: ran a real
`make_providers()` → `run_story_pipeline()` round trip (temp DB, temp blob store,
zero mocks in the image path) against the live API. First attempt: 3/3 scenes
generated real, distinct SHA-256-addressed image blobs successfully, dedup summary
computed correctly. A second attempt at the same call hit a transient `500 Internal
Server Error` from NVIDIA's endpoint — worth recording honestly as a known
characteristic of the free-tier NIM endpoint (occasional transient failures), not a
regression in this fix; `flux.1-schnell` remains listed as a fallback candidate in
case that transport issue there ever proves to be the same kind of transient
flakiness rather than a hard dead-end, though it has consistently timed out rather
than 500'd in every test this session.

**Follow-up: queried the real `data/telemetry/steps` Parquet table directly**
(DuckDB) to check whether task #17's open item — "NVIDIA credit cost per call
cannot be measured, moot until #22 resolves" — actually resolves now that image
calls succeed. Real `flux.1-dev` rows: consistent ~7.5–8.2s latency across 6
successful calls (one transient failure logged at ~60s, matching the 500-error
retry/timeout path). But `cost_usd` is `NULL` on every real row — genblaze's NVIDIA
provider simply does not populate it from NVIDIA's API response (only pre-existing
mock/simulated telemetry rows carry a fixed synthetic `cost_usd`, e.g. `0.003`).
So this wasn't a "blocked until #22 resolves" gap after all — it's an architectural
limit of what genblaze's telemetry captures for this provider; real per-call USD
cost would need NVIDIA's own billing dashboard, which is out of scope here. Recorded
against task #17 so a future session doesn't re-open this expecting a different
answer once #22-adjacent work resolves further.

---

## 2026-07-31 — TTS root cause found: self-hosted GPU service, not a wrong slug

User supplied `docs.nvidia.com/nim/speech/latest/tts/index.html` after the session's
own slug-guessing came up empty. Following the links from that page
(`../reference/api-references/tts/http-tts.html`) resolved the actual question this
session had been unable to answer: **why does every plausible Magpie TTS slug 404
against `ai.api.nvidia.com/v1/genai/{model}`?**

Answer: that hosted endpoint was never how TTS is served. NVIDIA's NIM-for-Speech
docs describe deploying Magpie TTS as a **self-hosted microservice container** — base
URL `http://<address>:9000`, meaning you pull the container from NGC and run it on
your own GPU. Its call shape is completely different from the hosted image/chat
pattern too: `POST /v1/audio/synthesize` (`multipart/form-data`) with a
`voice="Magpie-Multilingual.EN-US.Aria"` string (pattern `Model.Locale.Speaker`), not
a `model=` slug in a JSON body. Confirms Magpie TTS Multilingual does support Hindi
and Japanese (directly relevant to this project's locales) — but that support is
irrelevant without GPU infrastructure to actually run the container on, which this
free-tier, no-infrastructure hackathon build was never going to provision.

This closes the investigation definitively rather than leaving it as "still open,
try more slugs." Updated `Config.nvidia_audio_broken`'s docstring
(`polyglo/config.py`) and `docs/04-PROVIDER-STRATEGY.md` §5 (adds a
`[CONFIRMED OUT OF SCOPE]` status distinct from a plain "broken," with the concrete
technical reason) to state this precisely — it reads far better in a submission
write-up to say "this needs GPU infrastructure we didn't provision" than "this is
broken and we don't know why." README updated to match. No code behavior changed
(`nvidia_audio_broken` still defaults `True`) — this was a documentation-accuracy
fix, closing out the one open thread from task #22's follow-up.

---

## 2026-08-01 — Real deploy bug: missing `genblaze-nvidia[chat]` extra

Deployed to Render (first live deploy of this project) with real credentials
configured. `/api/status` correctly reported `mock_mode: false`, all providers
detected. But creating a real story through the live app produced 0 scenes every
time, with no visible error in the API response — `GET /api/stories/{id}` just shows
an empty `scenes: []` forever, silently.

Root cause found via the SSE progress log (`GET /api/stories/{id}/events`), which
`api.py`'s error handling does record even though the plain `GET` endpoint doesn't
surface it:

```
data: {"stage": "error", "detail": "scene splitting failed: ProviderError: openai
package not installed. Run: pip install \"genblaze-nvidia[chat]\""}
```

`genblaze-nvidia`'s chat provider is OpenAI-API-compatible and imports the `openai`
package directly, but that's an optional extra (`genblaze-nvidia[chat]`), not part of
the base install — and `pyproject.toml` only declared `genblaze-nvidia==0.3.3`
(no extra). This had been invisible in every local test this session, including the
"verified end-to-end with real image generation" runs earlier today, because
`openai` happened to already be installed in the local dev `.venv` from earlier
exploration — confirmed via `pip show openai` (present, version 2.51.0, no record of
why). Every one of those "verified live" runs used the local venv directly, never a
clean environment with real credentials at the same time. Docker rebuilds this
session were tested either with zero credentials (mock mode, no chat call) or the
local venv with real credentials (already had `openai`) — never the actual
intersection (clean environment + real chat call) that a fresh deploy hits.

**Fix**: `genblaze-nvidia==0.3.3` → `genblaze-nvidia[chat]==0.3.3` in
`pyproject.toml`. **Verified by rebuilding Docker with `--no-cache`** (forces a truly
fresh dependency resolution, not reusing any cached layer that might have picked up
`openai` transitively) and running it with real credentials via `--env-file .env`:
created a real story, polled it, got back 2 real scenes with real
`black-forest-labs/flux.1-dev` image SHA-256 hashes and a populated `es-ES` bundle
(`qa_status: unverified`, correct given audio is still simulated). This is exactly
the gap this session's own established discipline ("verify by actually running, not
by reading the diff") is meant to catch — it slipped through because the specific
combination (clean env × real creds × chat path) was never exercised together until
an actual deploy did it.

**Broader lesson worth flagging explicit**: a local dev venv accumulating
dependencies over a long session (from earlier exploration, spikes, or manual `pip
install`s) can mask a missing-package bug that only a genuinely clean environment
would catch. `requirements.txt`/`pyproject.toml` correctness can't be verified from
"it runs on my machine," even inside Docker, if the image was ever built with cache
reuse from a run that happened to have the dependency. `--no-cache` rebuilds are
the only fully trustworthy check.

Pushed the fix to `origin/master`; Render auto-deploys on push, so this resolves the
live site without further manual action.

---

## 2026-08-01 — Character/style consistency across scenes, plus a real moderation bug found along the way

`polyglo/authoring.py`, `polyglo/visuals.py`, `polyglo/orchestrator.py`,
`tests/test_authoring.py`, `tests/test_visuals.py`, `tests/test_orchestrator.py`,
`tests/test_api.py`, `tests/test_end_to_end.py` — full suite passes (370+ tests).

User-reported issue, from a live screenshot of a real generated story: every scene's
illustration showed a visibly different cat (different breed/markings) in a
different art style, with no sense that it was the same character across a story.

**Root cause**: `authoring.split_story` generated each scene's `visual_prompt`
independently — nothing in the prompt anchored a consistent character or style
across the 5 separate, independent image-generation calls, so the model reinvented
the subject from scratch each time.

**Fix, attempt 1 (partial, then corrected)**: added a `style_guide` field to the
scene-splitting LLM call — one shared character + art-style description, generated
once per story, prepended to every scene's own `visual_prompt` before it's stored.
Verified this alone works: inspecting the real stored `visual_prompt` values for a
4-scene test story showed the LLM correctly produced a consistent character
description (`"Milo, a small orange cat, is a ball of fluffy fur..."`) and distinct,
well-formed scene-specific action prompts.

Also tried a **fixed per-story image-generation seed** (derived from `story_id`) as
a second consistency lever, reasoning that a shared seed would add cross-scene
similarity in rendering. **This was wrong and reverted**: a live test showed all 4
scenes collapsed to the exact same image (`deduped: true` on every scene). Isolated
testing (calling `run_step` directly, same seed, two genuinely different short
prompts) showed the seed itself doesn't force identical output — so the interaction
between a fixed seed and the new long `style_guide` prefix was the actual cause.
Reverted the seed entirely (kept the optional `seed` parameter on
`VisualGenerator.generate()`/`generate_story_visuals()` for future use, just don't
pass a value from `orchestrator.py`) and re-tested — **still got identical images
across all 4 scenes, and even across a completely different story's scene 0.**
Seed wasn't the cause after all.

**Actual root cause, found by inspecting the raw bytes**: the "identical" images
were a **solid black JPEG**, ~6-7KB, generated in ~1.3-1.6s — compare to real
generations at ~85-120KB and ~7-9s. This is NVIDIA's content-moderation filter
responding with a placeholder image at a normal **HTTP 200, no error field, no
`ok=False`** — nothing in the existing code path had any reason to suspect a
"successful" outcome wasn't a real image. Confirmed the specific trigger by
isolated testing: the `style_guide` phrase *"a small orange cat, is a ball of fluffy
fur... tiny pink nose"* — describing a small character's physical features in
detail — reproducibly triggered it; a neutral rephrasing ("Recurring character: an
orange tabby cat with a light blue collar. Art style: ...") did not, and produced a
correct, verified image on the same model/prompt structure otherwise. Plausible
explanation, not confirmed: an over-broad classifier meant to catch descriptions of
minors pattern-matching on "small" + physical-feature language, even for an
explicitly-named animal.

**Two-part fix, both verified against the live API**:
1. `authoring.SPLIT_PROMPT` now explicitly instructs the LLM to phrase `style_guide`
   as a plain reference-sheet ("Recurring character: <species + 2-3 distinguishing
   features — markings/clothing, not body/size descriptions>. Art style: <...>"),
   and explicitly warns against "small"/"tiny" + body-part phrasing.
2. **Defensive, wording-independent guard** in `visuals.NvidiaVisualGenerator`:
   `_looks_like_moderation_placeholder()` opens the returned image with Pillow,
   converts to grayscale, and checks per-pixel standard deviation — a solid-color
   placeholder has ~0 variance; any real illustration, however plain, does not.
   Raises `VisualError` (a loud, visible failure) rather than silently shipping a
   blank image as if it were a successful generation. This exists because prompt
   wording alone can't be trusted to never trip a third-party moderation filter
   again in the future — a real user's story text could do the same thing this
   session's engineered test case did by accident.

Verified end to end against the live API with a real 4-scene story: all 4 scenes
came back with **distinct, non-deduped image hashes** (previously: 1 unique hash
shared by all 4), and visual inspection of all 4 confirmed the same cat (same
markings, same black collar with tag) across every scene, each showing a genuinely
different moment in the story (day on a rooftop, sunset over mountains, meeting a
bird, stargazing at night) in a consistent flat watercolor illustration style.

Added regression tests for the placeholder-detection logic specifically
(`test_looks_like_moderation_placeholder_*`, `test_nvidia_generator_raises_on_blank_
moderation_placeholder`, `test_nvidia_generator_accepts_a_real_looking_image` — the
last one is the false-positive guard, since a broken detector that flags real plain
illustrations would be a worse bug than the one it fixes) and for the `style_guide`
mechanism itself (`test_split_story_prepends_style_guide_to_every_scene`,
`test_split_story_rejects_missing_style_guide`,
`test_split_story_rejects_blank_style_guide`).

---

## 2026-08-01 — SQLite index survives a Render redeploy, via B2 (not Google Drive)

`polyglo/db.py`, `polyglo/orchestrator.py`, `polyglo/api.py`,
`tests/test_models_db.py` — full suite passes.

User raised a real concern after noticing Render resets on every deploy: worried
the app's data (character reference set, generated scenes) wouldn't survive, and
proposed adding Google Drive as a second storage backend for it.

**Checked before building anything**: listed the real B2 bucket directly. All 20+
blobs from every test run this session — including images from containers that no
longer exist — were still there. B2 storage was never actually the problem; `
make_store()` already returns `B2Backend` whenever B2 credentials are configured
(confirmed: true on Render). What resets is the **SQLite index** (which story/scene
row points at which B2 blob) — the blobs themselves are already durable.

Recommended against Google Drive: B2 is one of the two things this hackathon
explicitly scores ("Use of Backblaze B2"), and it already solves the real problem.
Adding a second, unrelated storage backend under deadline pressure would dilute
that story for no reason — it would also be new OAuth/credential surface area with
zero upside over what B2 already does.

Also worth noting: `db.rebuild_from_b2()` already existed as an explicit stub
(raises `NotImplementedError`, "see task #17") for a more principled version of
this — reconstructing the index purely from B2 objects/manifests, no snapshot
file. Left that stub as-is (still genuinely not built) and added a separate,
simpler mechanism instead: `backup_db_to_b2()` / `restore_db_from_b2()` upload/
download the *whole SQLite file* to a fixed B2 key (`db-snapshot/polyglo.db`).
Faster to build and verify than the full reconstruction approach, and sufficient
for what's actually being asked (make the demo dataset survive a redeploy).

Wired in two places:
- `orchestrator.run_story_pipeline()` calls `backup_db_to_b2()` after its final
  `conn.commit()` — best-effort (wrapped in try/except, emits a `warning` progress
  event rather than failing the story the user is waiting on if B2 has a hiccup).
  Single point of truth rather than duplicating the call in both `api.py`'s JSON
  endpoint and `web.py`'s HTML-form endpoint, which each independently call
  `run_story_pipeline` (a pre-existing duplication documented in CLAUDE.md).
- `api.py` calls `restore_db_from_b2()` once at module import time (before the
  first request can possibly be served) — guarded to never fire if a local DB file
  already exists, so it can't clobber a real local dev database, only fires in a
  genuinely fresh environment.

**Verified against the real B2 bucket, not just unit tests**: built a fresh Docker
image, ran container A, created a real story (triggered a real backup — confirmed
the `db-snapshot/polyglo.db` key existed in the bucket afterward, 61440 bytes).
Killed container A entirely, started container B (`docker run` from the same
image, no shared volume, no prior state) — `GET /api/stories` on the brand-new
container immediately showed the story from container A, full detail (scenes,
bundle, QA status) intact.

---

## 2026-08-01 — Task #24: Gemini as a real production narrator (real audio, finally)

`polyglo/narrate.py`, `polyglo/orchestrator.py`, `polyglo/audio_utils.py` (new),
`polyglo/qa/gemini_transcriber.py`, `tests/test_narrate.py`,
`tests/test_make_providers_transcriber_gating.py`, `tests/test_audio_utils.py` (new)
— full suite passes.

User's own idea, flagged as "the single highest-value item" among a larger backlog
of polish/feature work: since NVIDIA audio is confirmed out of scope (self-hosted
GPU infra, no hosted endpoint — task #22), and Gemini's own TTS was already proven
live during the ASR spike (WER 0.0 round trip), use Gemini as the actual production
`Narrator`, not just the ASR verifier.

**The real architectural tension, thought through before writing code**:
`qa/gate.py`'s own stated design principle is "the verifier must not be the
generator" — `GeminiTranscriber` verifying `GeminiNarrator`'s own output violates
that literally, both are the same model family, so correlated failures could pass
undetected. Investigated whether NVIDIA chat could serve as an independent
audio-input ASR path instead (`genblaze_nvidia.chat()` is OpenAI-wire-compatible,
which in principle supports multimodal content) — decided against spending further
time chasing this specific unknown (uncertain payload shape AND uncertain whether the
hosted endpoint honors it — same category of open question as task #22's
image-conditioning investigation) given the size of the rest of the night's backlog.
Shipped the same-model-family pairing instead, with the trade-off documented loudly
in `GeminiNarrator`'s own docstring, `orchestrator.make_providers()`'s comments, and
the README — real, imperfectly-independent narration beats no real narration at all.

**Implementation**:
- `audio_utils.py` (new) — extracted the PCM->WAV wrapper out of
  `qa/gemini_transcriber.py` so `narrate.GeminiNarrator` can share it without an odd
  cross-package import direction. Its own tests moved to `test_audio_utils.py`.
- `narrate.GeminiNarrator` — calls Gemini's `gemini-2.5-flash-preview-tts` directly
  (no genblaze provider exists for it), then feeds the real resulting bytes through a
  real genblaze `Pipeline` via `MockProvider` seeded with those real bytes — same
  technique `SimulatedNarrator`/`SimulatedVisualGenerator` already use for provenance
  — so this still produces a genuine, hash-verified Manifest, not a bypass of
  Genblaze. Respects `GeminiBudget` exactly like `GeminiTranscriber` (raises
  `BudgetExceeded` before any network call).
- **Real voice switching on retry, not just a relabeled retry**: mapped this
  project's existing abstract `VoicePlan` names (`voice-a`/`voice-b`/`voice-strong`,
  already used identically for the NVIDIA path) to real Gemini prebuilt voice names
  (`Kore`/`Puck`/`Charon`) via `GEMINI_VOICE_NAMES`. Confirmed live: all three
  produce real, distinct-sounding audio — a retry actually changes the voice, not
  just the model string.
- `orchestrator.make_providers()`: narrator selection is now NVIDIA (if it ever
  works) -> Gemini (if configured) -> Simulated. Transcriber activates whenever
  Gemini is configured at all (previously gated on `has_audio_generation`
  specifically) — since Gemini-as-narrator means real audio exists whenever Gemini
  is configured, not only when NVIDIA's does. **Both narrator and transcriber now
  share ONE `GeminiBudget` instance** (constructed once, passed to both) — critical,
  since narration now also costs a call: a 5-scene, 4-locale story is
  `5*4*2 = 40` Gemini calls (narrate + verify per segment), most of the 50/day cap
  in a single run. Documented as a real operating constraint in code comments, not
  hidden.

**Verified against the live API, not just mocks** (1 scene, 1 locale, to conserve
the now-shared daily budget): `make_providers()` correctly selected `GeminiNarrator`
+ `GeminiTranscriber`. The actual result was genuinely interesting: source text "Un
perro corre" (3 words) came back transcribed as "Un perro" (one word dropped),
scored a real WER of 0.333, retried twice on two different real Gemini voices, and
was correctly quarantined when none of the three attempts cleared threshold (6
Gemini calls total: 3 narrate + 3 transcribe). This is exactly the gate working as
designed — genuinely catching a real imperfection on real content, not trivially
passing everything or (the opposite failure mode this project caught earlier,
task #17) always quarantining regardless of actual quality. Also confirmed inside a
clean, `--no-cache`-rebuilt Docker container (provider selection correct, zero
additional API spend) before pushing.

**Honest note on WER sensitivity for short segments**: a single dropped word out of
3 is a 33% WER — the same absolute error would be a much smaller percentage in a
longer sentence. This project's normalize/numeral-expansion work already document
similar short-segment sensitivity; worth keeping in mind when task #18 (threshold
calibration) is eventually revisited now that real samples are possible.

Gemini daily budget used by this session's testing so far today: 6/50 (all from
this one verification run; local testing only — Render's deployed container tracks
its own separate counter).

---

## 2026-08-01 — Task #25: source story autocorrect + CEFR-level restructuring

`polyglo/authoring.py`, `polyglo/chat.py`, `polyglo/db.py`, `polyglo/models.py`,
`polyglo/orchestrator.py`, `polyglo/api.py`, `polyglo/templates/story.html`,
`tests/test_authoring.py`, `tests/test_chat.py`, `tests/test_models_db.py`,
`tests/test_orchestrator.py`, `tests/test_api.py`, `tests/test_end_to_end.py`,
`tests/test_web.py` — full suite passes.

User's idea, from the same backlog session as task #24: correct spelling/grammar in
the raw submitted story and genuinely re-level it for the target CEFR before
scene-splitting, keeping both versions visible rather than silently transforming the
input. New `authoring.grade_source_text()` (a separate LLM call, distinct from
`split_story`, which already asks for CEFR-appropriate scene text but only ever saw
raw input) — non-fatal by design, falls back to the original text on failure so a
grading hiccup doesn't abort story creation.

**A real, previously-nonexistent schema change** (`Story.original_source_text`,
`Story.corrected_source_text`) needed a genuine migration path, not just
`CREATE TABLE IF NOT EXISTS`: `db._migrate_stories_columns()` checks
`PRAGMA table_info(stories)` and `ALTER TABLE ... ADD COLUMN` for anything missing,
run from `init_db()` on every connection. This matters specifically because of task
#19's B2 DB-snapshot mechanism (shipped 2026-08-01 earlier today) — a snapshot taken
before this change, restored into the new code, has a `stories` table missing these
columns entirely; without a real migration, every `save_story()` call after restore
would fail outright. Verified against the real, already-existing production
snapshot in B2 (not just a synthetic test): rebuilt Docker from scratch, booted
against real B2 credentials (restoring the actual pre-existing snapshot), confirmed
`PRAGMA table_info(stories)` showed both new columns after `init_db()` ran, then
created a real story through it successfully.

**A real ordering bug found by that same live test, not by unit tests**: the graded
text was being persisted only *after* `split_story` succeeded — but `split_story`
occasionally fails on its own (a pre-existing, already-documented issue: the chat
model doesn't always honor the exact requested scene count), and when it does, the
already-computed graded text was silently lost along with the exception, never
reaching `dbm.save_story()`. Fixed by persisting `original_source_text`/
`corrected_source_text` immediately after grading, before splitting even runs — the
same "shell record so GET works immediately" pattern already used for the initial
story save. Added a regression test reproducing the exact failure (`split_story`
throws, but the graded text must still read back afterward) — this is the kind of
bug a unit test alone likely wouldn't have surfaced first, since the original test
suite's mocked completers never had a reason to make the split call fail *after* a
successful grading call.

**Also found and fixed while updating tests**: several existing test doubles
(`FlexibleSplitCompleter` in `test_api.py`, `DistinctTranslationCompleter` and
`FlakyOnGerman` in `test_orchestrator.py`, `NarrativeChatCompleter` in
`test_end_to_end.py`) assumed "the first call to the chat completer is always the
split call" — true before this task, no longer true now that grading runs first.
Rewrote each to dispatch on prompt content (checking for
`"Correct any spelling and grammar errors"` vs `"Split this story into"`) instead of
call order, which is also more robust against whatever gets inserted before
splitting next.

**Verified live end to end, not just in Docker**: ran the actual local dev server
and created a real story through the browser UI with deliberately typo'd text
("a smal fish swim in the sea...") — the story page correctly rendered both "As
submitted" and "Corrected & leveled" sections, showing real spelling/grammar fixes
(smal→small, swim→swam, sing→sang). The pipeline then hit the same pre-existing
scene-count-mismatch issue again — and the corrected text remained visible on the
page despite that downstream failure, live-confirming the ordering fix actually
works in the real production code path, not just in the new regression test.

**Known limitation surfaced, not yet fixed**: local dev and the deployed Render
instance share the same real B2 bucket/credentials, so `backup_db_to_b2`'s
`db-snapshot/polyglo.db` key is a single shared slot — local testing and the live
deployment can overwrite each other's snapshot. Not a data-loss risk (B2 blobs
remain the durable source of truth regardless, per `db.py`'s own module docstring),
just a "which demo stories survive the next redeploy" convenience question. Worth
an environment-specific key (e.g. suffixed by a `POLYGLOT_ENV` var) if this becomes
an actual problem before submission — not fixed now, given the size of the rest of
tonight's backlog.

---

## 2026-08-01 — Task #26: real visual design pass, plus two real bugs found live

`polyglo/static/style.css`, `polyglo/templates/base.html`,
`polyglo/templates/index.html`, `polyglo/templates/story.html`, `polyglo/web.py`,
`tests/test_web.py` — full suite passes.

User's own framing: "premium ui not a debug ui." Rewrote the CSS design system (`
polyglo/static/style.css`) — richer color palette, real shadow tokens
(`--shadow-sm/md/lg`), gradient accents on the brand mark/buttons/hero, a proper
type scale, hover/focus states on inputs and interactive elements, a stepper
component and skeleton-loading styles (laid groundwork for tasks #27/#28). Added a
real hero section to the homepage (`index.html`) — a headline, a one-paragraph
pitch, and a 3-step "how it works" visual — replacing the bare form as the first
thing a visitor sees.

**Two real bugs found by live browser verification, neither would have been caught
by the existing test suite (which only ever checks `resp.text` via TestClient, not
actual rendered/computed CSS):**

1. **Stale CSS caching, in a way that affects real users, not just this session's
   testing.** Verifying the redesign in the Browser pane, a *brand-new* tab kept
   showing the OLD stylesheet's computed values even though a direct `curl` to the
   same server proved it was serving the new file correctly — the browser's HTTP
   cache was serving a previously-fetched `/static/style.css` without revalidating,
   because the URL never changes between deploys. This is a real production risk:
   any user who visited the site before a CSS/JS update would keep seeing the old
   version indefinitely after a Render redeploy, since Starlette's `StaticFiles`
   sets normal long-lived cache headers and the URL is static. Fixed with a
   cache-busting query param (`?v=<style.css mtime at process startup>`) computed
   once in `web.py` and threaded through `_ctx()` into `base.html`'s `<script>`/
   `<link>` tags — the same mechanism real frontend build tools use for this exact
   problem, just hand-rolled for a project with no JS build step.
2. **A hardcoded `data-theme="light"` on `<html>` silently overrode every OS/browser
   dark-mode preference.** This predates tonight's session — the original markup
   had it with no corresponding JS, seemingly vestigial scaffolding for a
   theme-toggle that was never built. My new CSS added `:root[data-theme="light"]`/
   `:root[data-theme="dark"]` override rules (for the toggle now built, see below),
   which — combined with the pre-existing hardcoded attribute — took precedence
   over the plain `@media (prefers-color-scheme: dark)` block that previously
   worked correctly. Caught by directly checking `window.matchMedia(...).matches`
   vs. the actual computed `background-color` in the Browser pane: a dark-mode
   browser was still rendering the light palette. Fixed properly rather than
   papered over: removed the hardcoded attribute, and built the real toggle this
   implies — an inline `<head>` script applies a stored `localStorage` preference
   before first paint (no flash of the wrong theme), a `.theme-toggle` button
   (topbar) cycles and persists the choice, and absence of a stored preference
   correctly falls through to the OS setting. Verified live: OS dark-mode preference
   respected by default with no override, manual toggle switches and persists
   across navigation to a different page.

Verified end to end: full test suite (new tests for the hero section, cache-busting
query params, and a regression test pinning `<html>` never hardcoding `data-theme`
again), a `--no-cache` Docker rebuild, and live browser interaction (computed
`background-image`/`box-shadow` values, not just presence of CSS classes — gradient
and shadow tokens confirmed actually rendering, not just declared in the
stylesheet).

---

## 2026-08-01 — Task #27: storybook reading-mode view

`polyglo/web.py`, `polyglo/templates/read.html` (new),
`polyglo/templates/_matrix_fragment.html`, `polyglo/static/style.css`,
`tests/test_web.py` — full suite passes.

New `GET /stories/{story_id}/read/{locale}` route + `read.html` template: a
dedicated per-locale "read it" view distinct from the existing builder/
orchestration story page — one scene per page (real image + translated text +,
now that task #24 made real narration possible, an inline audio player when the
segment has real audio), page-turn navigation via a plain `?page=N` query param
(works with JS disabled; a small enhancement script adds arrow-key navigation on
top). Linked from the story overview page's Bundles table once a locale's bundle
exists.

Deliberately query-param-based rather than JS-driven single-page pagination: every
page is a real, independently-linkable, independently-testable URL
(`?page=0`, `?page=1`, ...), out-of-range values clamp rather than 404 (a stray
`?page=999` from a stale bookmark degrades gracefully to the last real page, not an
error page).

Tests cover: 404 for an unknown story, 404 for a locale with no content yet
(distinguishing "never requested this locale" from "still generating"), correct
first-page default, `?page=` navigation, clamping both above and below the valid
range, scene text/image/QA-status rendering, and the story-overview link appearing
once a bundle exists. Verified live in the Browser pane against real existing data
(a real NVIDIA-generated image loaded and rendered with the new design system's
shadow/radius tokens correctly applied) and via a `--no-cache` Docker rebuild in
zero-credential/offline mode (confirms the reader works with `SimulatedNarrator`/
`SimulatedVisualGenerator` too, not just real providers).

---

## 2026-08-01 — Task #28: progress stepper replaces the raw SSE log as the primary view

`polyglo/web.py`, `polyglo/templates/_matrix_fragment.html`, `tests/test_web.py` —
full suite passes.

Added `_stepper_state()` (`web.py`), which reduces the full per-event progress log
into one entry per canonical pipeline stage — done / active / error / pending —
folding the many per-scene, per-locale `localize`/`narrate`/`qa` events into a small,
human-meaningful sequence: "Writing & illustrating scenes" (authoring + visuals,
merged — both report `stage="authoring"`/`"visuals"` and happen back-to-back before
anything else can) → "Translating" → "Narrating & verifying" (folds `qa`, since
verification is intrinsic to narration, not a separate thing a user waits on) →
"Assembling bundles". The raw log is kept, not deleted — demoted into a `<details>`
disclosure below the stepper, preserving the debugging/transparency value for anyone
who wants it without it being the primary thing a user sees while waiting.

**A real bug in the merge logic, caught by a unit test, not manual testing**: the
first version computed each merged step's status using only the *last* raw stage
index in its group (e.g. "visuals"' index for the authoring+visuals group) — this
meant a story that had only reached `stage="authoring"` (not yet `"visuals"`) showed
that merged step as "pending" instead of "active", since the group's representative
index (visuals') hadn't been reached yet even though the group's first member had.
Fixed by tracking each merged group's `[min, max]` index range instead of a single
representative index: a merged step is "active" whenever the pipeline's overall
progress falls anywhere inside that range, "done" only once progress has moved past
the whole range. 7 unit tests cover this directly (`test_stepper_*` in
`test_web.py`) — empty log, single-stage log, the merged-group edge case that
exposed the bug, monotonic progress through all stages, full completion, and error
display/clearing (a per-locale failure must not permanently paint the whole
stepper red if the pipeline keeps making real progress on other locales afterward).

Verified live: created a real 1-scene, 1-locale story and caught the stepper
mid-flight showing the correct real-time state (`"Writing & illustrating
scenes": done`, `"Translating": done`, `"Narrating & verifying": active`,
`"Assembling bundles": pending`) via computed DOM class inspection, then confirmed
it correctly disappears once the story completes (real narration this run scored
`WER 0.00`, a genuine pass rather than the quarantine seen in task #24's
verification — real evidence the gate doesn't always fail, either).

---

## 2026-08-01 — Task #29: locale flag emoji + a real dashboard chaos-toggle panel

`polyglo/models.py`, `polyglo/web.py`, `polyglo/templates/_chaos_panel.html` (new),
`polyglo/templates/{dashboard,index,read,_matrix_fragment}.html`,
`polyglo/static/style.css`, `tests/test_models_db.py`, `tests/test_web.py` — full
suite passes (487 tests).

Two small, independent pieces of UI polish:

1. **`locale_flag()`** (`models.py`) — a `LOCALE_FLAGS` lookup mapping each
   `SUPPORTED_LOCALES` code to its flag emoji, registered as a Jinja global
   (`templates.env.globals["locale_flag"]`, `web.py`) rather than threaded through
   every view's context dict, since the locale picker, matrix table, and reader all
   want it. Deliberately pure display polish — an unmapped code degrades to an
   empty string rather than an error, and no locale-selection logic anywhere reads
   this table. Tested directly: every `SUPPORTED_LOCALES` code has a real flag,
   an unknown code degrades to `""`, and the two lookup tables stay in sync.
2. **A real dashboard UI control over the chaos toggle**, not just the existing
   JSON endpoint (`POST /api/chaos/{model}/disable`). New
   `POST /chaos-panel/{model}/toggle` in `web.py` reuses the *same*
   `ChaosRegistry` singleton (`_chaos`, imported from `api.py`) the JSON API
   already exposes — this is a second surface over identical state, not a
   separate mechanism, confirmed by a test that toggles via the new HTML route
   and asserts the change is visible through `GET /api/status`. Modeled after
   `_matrix_fragment.html`'s pattern: `_chaos_panel.html` is both included by
   `dashboard.html` on first load and returned standalone by the toggle route
   (htmx `hx-target`/`hx-swap` on the same fragment), so there's one template as
   the single source of truth for both paths. Fixed list of 5 models
   (`_CHAOS_MODELS` in `web.py`) — the real fallback-chain models actually used by
   `orchestrator.make_providers()` (`flux.1-dev`/`flux.1-schnell` image
   primary/fallback, plus the `voice-a`/`voice-b`/`voice-strong` `VoicePlan`
   labels narration retries cycle through), not an arbitrary list.

This directly strengthens the demo's failover beat (`docs/05-SUBMISSION-KIT.md`
§3, 1:50–2:10): a judge (or the demo video) can now click a real button in the
dashboard to kill a model and watch the next run's fallback chain recover, instead
of that only being demonstrable via a raw `curl`/JSON call.

Verified: full suite green (487 tests, up from 480 — 4 new chaos-panel tests, 3
new locale-flag tests), **and live in the browser** — started the real dev server
(`.claude/launch.json`'s `polyglo-web` config), confirmed the dashboard renders all
5 chaos models as "healthy", clicked `flux.1-schnell`'s chip, confirmed it flips to
"disabled" and that `GET /api/status`'s `chaos_disabled_models` reflects the same
change (proving the HTML toggle and the pre-existing JSON API really do share one
`ChaosRegistry`, not just in the tests), then toggled it back and confirmed the
locale-flag emoji render correctly on the homepage's locale picker.

---

## 2026-08-01 — Real bug found via WER-calibration probing: Gemini's free-tier TTS is capped at 3 req/minute, separate from our own daily budget

`polyglo/qa/gate.py`, `tests/test_gate.py` — full suite passes (489 tests).

While gathering more real WER samples for task #18 (this project's own daily
`GeminiBudget` had 42/50 calls remaining, well under cap), ran a real 3-scene,
2-locale (`es-ES`, `hi-IN`) story through the actual `make_providers()` pipeline
(temp DB/store, not the real dev DB — gotcha #1). `es-ES` worked exactly as
expected — two clean passes and one real escalate-then-recover (WER 1.0 → 0.0
after switching to `voice-strong`). Every single `hi-IN` attempt (9 of them: 3
scenes × 3 max attempts) came back as an `"error"` verdict in ~70–90ms each — far
too fast to be a real Gemini network round trip (the `es-ES` calls took 2–10s).

**Root-caused by direct reproduction**, not guessing: called `GeminiNarrator.narrate()`
in isolation with the exact translated Hindi text pulled straight from the run's
SQLite `localized_scenes` table. First call succeeded (real audio, ~5s); the very
next call failed instantly with a real `google.genai` `ClientError`:

```
429 RESOURCE_EXHAUSTED ... generativelanguage.googleapis.com/generate_content_free_tier_requests
limit: 3, model: gemini-2.5-flash-tts ... retry in 24s
```

**This is a hard Google-side quota — 3 TTS requests per minute — completely separate
from and much tighter than this project's own `GeminiBudget` (50/day)**, which
tracks call *count*, not rate. The `es-ES` run's own narrate calls (4 of them) had
already used most of that per-minute allowance before `hi-IN` even started; every
`hi-IN` attempt thereafter 429'd within milliseconds. This is a real, structural risk
for the demo: **any real story with more than ~3 real narration segments inside a
one-minute window will start hitting this**, and the existing retry ladder makes it
*worse*, not better — its whole design (try a different voice, then escalate to a
stronger model) assumes the problem is audio *quality*, but a 429 recurs identically
regardless of which voice is tried, so the old code burned all 3 attempts and
**wrongly quarantined perfectly fine content as a quality failure**, indistinguishable
in the dashboard from a real bad-audio case.

**Fix**: `gate.py`'s new `_is_rate_limit(exc)` checks the wrapped provider-error
string for `"RESOURCE_EXHAUSTED"`/`"429"` (both `NarrationError` and
`GeminiTranscriberError` wrap the raw exception as `f"{type(exc).__name__}: {exc}"`,
so the real Google error text survives into the message gate.py already catches —
no new exception type needed, keeps `gate.py` provider-agnostic). On a detected rate
limit, the loop records the attempt and **stops immediately** instead of continuing
to the next rung — the segment still ends up `QUARANTINED` (same end state as
before), but doesn't waste 2 more doomed attempts and 2 more real, budget-tracked API
calls that could not possibly succeed. Two new tests
(`test_rate_limited_provider_fails_fast_instead_of_burning_the_whole_ladder`,
`test_non_rate_limit_errors_still_exhaust_the_full_retry_ladder`) pin the new
behavior and guard against the fail-fast branch being too broad — a real transient
error with no "429"/"RESOURCE_EXHAUSTED" in it still gets the full 3-attempt ladder,
unchanged.

**What this does NOT fix, and is worth stating honestly in the submission
write-up**: this stops wasting attempts, it does not make rate-limited content
narratable within the same minute. A genuinely correct fix (bounded backoff honoring
the API's own `retryDelay`, ~24s here) was considered and rejected for now — the SSE
endpoint's idle timeout is 20 seconds (gotcha #3), so a blocking ~24s sleep inside a
live request would itself risk timing out the request that's waiting on it. The
practical implication for the demo: **pace real multi-locale runs** (a few seconds
between locales, or fewer real locales in a single take) rather than firing many
locales at once, and name this specific constraint (3 TTS req/min, hard Google-side
free-tier cap) as an honest limitation rather than a vague "sometimes narration
fails."

**Also, an incidental gap surfaced but deliberately not fixed this session, given
time remaining before the deadline**: `Attempt.error` (the real exception string)
never makes it past `GateResult.apply_to()` — `LocalizedScene` and the telemetry
`qa` Parquet table both drop it, so today, a quarantined/errored segment shows *no*
diagnostic text anywhere in the UI, rate-limited or not. Worth a follow-up
(`last_error` field threaded through `models.py` → `apply_to()` → SQLite → the WER
diff panel) if time allows, but out of scope for tonight given everything else still
open before submission.

Gemini daily budget spent on this investigation: 26 calls (8→34 of 50) — mostly the
real 3-scene/2-locale probe run itself (17 calls) plus a direct, budget-tracked
reproduction of the Hindi failure (9 calls) that confirmed the root cause precisely
rather than guessing from the fast-failure timing alone.

---

## 2026-08-01 — OpenRouter narrator + Seedream image consistency, both optional; plus a real audio-upload bug found and fixed

`polyglo/narrate.py`, `polyglo/visuals.py`, `polyglo/orchestrator.py`,
`polyglo/config.py`, `polyglo/qa/gate.py`, `pyproject.toml`, `.env`/`.env.example`,
`tests/test_narrate.py`, `tests/test_visuals.py`,
`tests/test_make_providers_transcriber_gating.py`, `tests/test_config_capability.py`,
`tests/test_orchestrator.py` — full suite passes (514 tests).

User's request, after a live architecture review: research cheaper/free OpenRouter
alternatives for narration and image generation, then build the ones that were
actually worth it, as **optional** additions that never change the existing default
behavior. Surveyed OpenRouter's real catalog (336 models, plus the dedicated
`/v1/audio/speech` and `/v1/images` endpoints) rather than guessing from vendor
reputation — this mattered concretely: MiniMax turned out to be the *most*
expensive TTS option in the whole catalog (not the cheap Chinese option assumed),
and Kimi K3 has zero audio capability at all (text-only), ruling both out for this
use case before any code was written.

**1. `OpenRouterNarrator`** (`polyglo/narrate.py`) — real TTS via Mistral's
`voxtral-mini-tts-2603` through OpenRouter's `/v1/audio/speech` endpoint. Verified
live before committing to the model: real Hindi and real Spanish text both produced
genuine, distinct MP3 audio despite its voice names being English/British-tagged —
confirms real multilingual capability, not just English despite the labels.
`make_providers()`'s narrator order is now NVIDIA (if it ever works) → OpenRouter →
Gemini → simulated. OpenRouter ranks above Gemini specifically because it's a
genuinely different vendor from `GeminiTranscriber` (the verifier) — this is the
actual fix for the "verifier must not be the generator" trade-off `GeminiNarrator`
carries on its own (see that class's docstring, task #24). `GeminiTranscriber` still
verifies either way. Purely additive: with no `OPENROUTER_API_KEY` set, narrator
selection is byte-for-byte unchanged.

**2. `OpenRouterVisualGenerator`** (`polyglo/visuals.py`) — real image generation via
ByteDance's `seedream-4.5` through OpenRouter's `/v1/images` endpoint, gated behind
an explicit **opt-in** flag (`OPENROUTER_PREFER_IMAGES`, default false) separate from
just having a key — NVIDIA stays the default image generator even with
`OPENROUTER_API_KEY` set, so this can never silently change an existing setup.

This exists to fix a real, user-reported bug: NVIDIA-generated character art visibly
drifts across scenes (different cat each time) because `NvidiaImageProvider` has no
image-to-image parameter — cross-scene consistency depends entirely on a *shared
text description* (`authoring.py`'s `style_guide`) repeated into every scene's
prompt, which a diffusion model can still reinterpret differently each time.
Seedream's `input_references` parameter is a real visual anchor instead — a
reference image passed alongside the new prompt. `generate_story_visuals()` now
passes the first scene's own generated image back in as every later scene's
`reference_image` (generators that don't support it, i.e. everything except this
one, simply ignore the kwarg, so this is safe to do unconditionally).

**Verified live, not just unit-tested**, with real spend: a real 3-scene story
(`OPENROUTER_PREFER_IMAGES=true`) produced the same boy in the same red-adjacent
blue shirt and the same golden retriever across three genuinely different
compositions — walking into a market, standing at an orange stall, sitting under a
tree eating bread — same watercolor illustration style throughout. This is a
categorically stronger consistency result than the text-only `style_guide` fix ever
produced. Real cost: ~$0.04/image (confirmed from OpenRouter's own `usage.cost`
field). The same run's narration went through `OpenRouterNarrator` end-to-end too:
real Spanish audio, real Gemini verification, WER 0.0% on all 3 segments (2 of 3
needed one retry before passing) — both new providers working together in the
actual app, not in isolation.

**3. A real, pre-existing bug found by that same live verification, unrelated to
today's new providers but affecting every narrator including Gemini's**: the
generated narration's real audio *bytes* were never uploaded to the blob store.
`qa/gate.py`'s `run()` computes `audio_sha256` from the narrator's returned bytes
(used to verify against the transcript) but the raw bytes themselves were discarded
once `GateResult` was built — only the hash survived. `orchestrator.py` then wrote
that hash onto `LocalizedScene.audio_sha256` and into the bundle's `audio_refs`,
but nothing had ever called `blob_store.put_bytes()` for it, unlike the identical
image path (`orchestrator.py:259`, `blob_store.put_bytes(result.image)`). Every real
"Listen to it" audio player in the app has been fetching a 404 since narration went
real (task #24) — silently invisible because the `<audio>` tag's own `onerror`
handler swaps in a "simulated audio" badge, which looks exactly like the
*intentional* simulated-narrator case rather than a broken real one. Caught only
because this session fetched the blob directly (`curl .../blobs/{sha256}`) instead
of trusting the pipeline's own "success" status and non-empty `audio_refs` count,
the same way the earlier zero-credential bugs (task #23) were only caught by
checking actual output, not just "did it crash."

**Fix**: `Attempt`/`GateResult` (`qa/gate.py`) now carry the real `audio: bytes`
alongside `audio_sha256` (in-memory only — `QAEvent.from_gate_result` extracts named
fields explicitly, so this never bloats the telemetry Parquet table).
`orchestrator.py` uploads `gate_result.audio` to `blob_store` right after
`gate_result.apply_to(ls)`, mirroring the image upload immediately above it, and
overwrites `ls.audio_sha256` with the store's own canonical hash (content-addressed,
so it's the same value either way, but this guarantees it). New regression test
(`test_narrated_audio_is_actually_persisted_to_the_blob_store`,
`tests/test_orchestrator.py`) asserts `blob_store.exists(...)` is true and the
fetched bytes are non-empty — this specific assertion would have failed before the
fix and is now permanent. Re-verified live after the fix, server restart included
(the dev server has no `--reload`): fetched the real audio blob directly, got a real
200 with valid `ID3`-tagged MP3 bytes.

Both new providers' real API keys/config live in `.env` (gitignored, matches the
existing B2/NVIDIA/Gemini keys already there) and `.env.example` documents both new
variables without real values. `requests` is now an explicit `pyproject.toml`
dependency (previously only transitively present) — the same class of gap that
caused the `genblaze-nvidia[chat]` production bug (task, 2026-08-01 "Real deploy
bug" entry): a locally-present-but-undeclared dependency is invisible until a
genuinely clean install hits it.

---

## 2026-08-02 — UI polish (real logo, animation, plain-language copy) + image/video export

User's framing: the app "looks like a demo website," wanted it polished, less text,
a real logo, animations, a way to download images, and a way to turn a story into a
video — with the full plan explained and approved (via plan mode) before any code
was written, given the scope.

**Logo + favicon**: replaced the CSS-gradient-square brand mark (a `::before`
pseudo-element, no actual image anywhere) with a real inline SVG mark — a simple
open-book glyph in a rounded gradient badge, using the existing `--accent`/
`--accent-2` tokens so it matches the palette exactly. `polyglo/static/favicon.svg`
(hardcoded hex colors, since an externally-referenced favicon can't see the page's
own CSS custom properties) plus a matching `<link rel="icon">`. Deliberately
hand-drawn SVG, not AI-generated — instant, crisp at any size, zero generation risk.

**Animation vocabulary**: extended `style.css`'s existing `@keyframes pulse`/
`skeleton-shine` rather than replacing them — a new `fade-up` entrance (staggered
per scene card via `nth-child` delays), a button/chip press-scale on `:active`, and
a toast component (`.download-toast`, triggered by a small `base.html` click
listener on any `[download]`/video-export link) confirming a download started.
Everything gated behind `@media (prefers-reduced-motion: reduce)`, which the CSS
had no handling for at all before this.

**Copy: a second, more aggressive trim pass.** The previous pass (earlier
2026-08-01 entry) fixed jargon *within* sentences; this pass removed several
standalone explanatory paragraphs entirely where the section header and the data
itself already carry the meaning (dashboard's chaos-panel and retry-evidence
captions moved to a `title=` tooltip or were dropped), and shrank the homepage
hero tagline from a 5-sentence paragraph to one line, moving the "how it works"
detail into icons on the existing 3-step cards instead of bare numbers.

**Two new capabilities**, both additive:

1. **Download scene images** — reused the existing `GET /blobs/{sha256}` route
   rather than building a new one: an optional `?download=<filename>` query param
   sets `Content-Disposition: attachment` when present, `None` (byte-for-byte
   unchanged) when absent. Every existing `<img>`/`<audio>` tag across the app is
   unaffected. New download links on the scene grid and the scene-detail page
   (which previously showed no image at all — added a real thumbnail there too).
2. **Narrated video export** — a real downloadable MP4 per story+locale: every
   qualifying scene's image, shown for the length of its own real narration clip,
   audio playing throughout. New `polyglo/video.py`: per scene, one ffmpeg call
   (`-loop 1 -i img -i audio -c:v libx264 -tune stillimage -pix_fmt yuv420p -vf
   scale/pad-to-1024x1024 -c:a aac -shortest`) produces a segment; one final
   ffmpeg concat-demuxer call (`-f concat -c copy`) stitches segments in scene
   order. The scale/pad step exists specifically because scene images aren't
   guaranteed the same resolution (different fallback models can produce
   different sizes) — skipping it risks a `-c copy` concat silently misbehaving.
   A scene is included only if it has a real (magic-byte-verified) image, real
   (magic-byte-verified) audio, and wasn't QUARANTINED; if nothing qualifies, the
   route returns a clear 422, never a fake video made of simulated placeholder
   bytes.

**The ffmpeg sourcing decision, made explicitly rather than defaulted into**:
`imageio-ffmpeg` (a pip package shipping a real, per-platform static ffmpeg
binary) instead of `apt-get install ffmpeg` in the Dockerfile. Confirmed live on
this Windows dev machine before writing any other code: `pip install
imageio-ffmpeg` resolved a real, working ffmpeg 7.1 binary immediately — meaning
the feature could be smoke-tested locally the same day, rather than the
Dockerfile's system-package layer being the first and only place it's ever
exercised (a real risk the night before a deadline: a new `apt-get` line changes
the build's network/dependency-resolution surface exactly when you can least
afford a slow or failed build). New route `GET /stories/{id}/{locale}/video.mp4`
in `web.py`, registered ahead of the existing `{ordinal}` route for the same
reason `/read/{locale}` already is — otherwise `video.mp4` gets swallowed by the
int-typed ordinal route.

**Verified live, not just unit-tested, with real spend**: a real 3-scene story
(`OPENROUTER_PREFER_IMAGES=true`, real Voxtral narration) produced a real 253KB
MP4 with valid `ftyp/isom/avc1` structure — confirmed via magic bytes (full
in-browser playback couldn't be confirmed this session, since the Browser pane's
sandbox only gives file:// URIs outside the project folder a static snapshot, not
real `<video>` execution; the structural verification plus the passing real-ffmpeg
smoke test in `test_video.py` was treated as sufficient given that constraint).

**A user question along the way, answered directly rather than acted on**: why not
use a real video-generation model (ByteDance's Seedance was named) instead of an
ffmpeg slideshow? Real answer, not deferred: a generated video clip is new,
unverified content — it breaks this app's "generate once, verify, then reuse"
invariant, since there's no QA gate for video. It's also slower and more
expensive per call than image generation, and doesn't take the app's real
narration audio as sync input. Recorded as a real v2 idea in the new roadmap doc
below, not attempted this session.

Full suite: 551 tests passed by the end of this entry's work (up from 514 at the
start of it).

---

## 2026-08-02 — Production hardening: rate limiting, spend caps, logging, CI

Prompted directly by the user noticing the app is now live and public with real
credentials behind it, asking for genuine production-readiness input and
authorizing autonomous work on the safe, high-value items while away.

**The single most urgent gap, fixed first**: `POST /stories` (and its JSON twin)
had no limit at all. Anyone who found the live URL could trigger unlimited real
pipeline runs — each one a real chat call, real image generation, and real
narration+ASR calls — burning through real spend and Gemini's own daily call
budget with no ceiling whatsoever.

**New `polyglo/ratelimit.py`**: a plain in-memory sliding-window limiter keyed by
client IP (`X-Forwarded-For`-aware, since Render sits behind a proxy). Story
creation: 5 requests per 10 minutes per IP (the real cost driver). Chaos
toggling: 30/minute (free, but still shared demo state). Wired via a **plain
module-level dependency function** (`_require_story_creation_slot` in `api.py`),
deliberately not a bound closure over the limiter instance — a closure captured
at route-registration time keeps referencing the *original* object forever,
silently ignoring any later reconfiguration or test monkeypatching. Caught and
fixed this in my own first draft before writing a single test, by reasoning
through exactly how Python resolves closures vs. module globals. `web.py`'s HTML
form route imports and reuses this exact function (not a separate instance)
specifically so a caller can't dodge the cap by switching between the JSON API
and the HTML form.

**A complementary aggregate cap**: the per-IP limiter alone doesn't bound the
*total* across every distinct visitor combined — many different IPs each staying
within their own allowance could still add up to unbounded daily volume. New
global daily story-creation cap (`GLOBAL_DAILY_STORY_CAP`, default 100/day),
disk-persisted the same way Gemini's budget already was.

**`qa/budget.py`'s `GeminiBudget` generalized**: renamed the underlying class to
`DailyCallBudget` (kept `GeminiBudget` as a plain alias — zero existing call
sites needed to change), and reused it for a genuinely new gap: OpenRouter is
real, metered, pay-per-use spend (unlike NVIDIA's free tier), and had no cap at
all. `OpenRouterNarrator` and `OpenRouterVisualGenerator` now share ONE
`OpenRouterBudget` instance (same "narrator and verifier share one budget"
reasoning `GeminiNarrator`/`GeminiTranscriber` already established) — narration
and image generation both draw from the same 200/day default cap.

**A real, live-fired test of the rate limiter**: hammered the real dev server's
chaos-toggle endpoint with real HTTP requests until it actually 429'd (at request
31, correctly accounting for 6 earlier manual requests in the same 60s window)
— confirmed a correct `Retry-After` header and that the HTML route and JSON API
route share the exhausted limiter, not two independent ones.

**Basic structured logging** (`polyglo/logging_config.py`): the app had zero
logging configuration before this — a real pipeline failure was only
reconstructable after the fact from telemetry/DB rows, and rate-limit/budget
rejections left no trace anywhere. A small, idempotent stdlib `logging` setup
(plain text, timestamped — not JSON; Render's own log viewer already timestamps
stdout lines, and a real log-shipping destination is a separate infrastructure
decision, not something to bake in silently), plus log calls at the existing
exception-handling sites (story creation start/complete/fail, rate-limit/budget
rejections, video-export failures). Verified live: real story creation logged at
INFO, and a real pipeline failure (the known pre-existing scene-count-mismatch
flakiness — unrelated to this session's changes) correctly logged at ERROR with
the story_id.

**GitHub Actions CI** (`.github/workflows/tests.yml`): 551 tests existed with
nothing enforcing them on push before this. Zero credentials needed in CI, matching
the project's existing zero-credential test discipline.

**`docs/08-PRODUCTION-ROADMAP.md`** (new): an honest list of what's still missing
for this to be a real product beyond a judged demo — SQLite's single-writer
ceiling, no auth/multi-tenancy, synchronous in-process background jobs (progress
state lost on redeploy), no legal/privacy policy, no uptime monitoring, and no
real per-call dollar-cost visibility (NVIDIA's own telemetry doesn't populate
`cost_usd`, a previously-documented architectural limit, not new). Each item
states concretely why it wasn't safe or correctly scoped to attempt in the hours
before a submission deadline, and what the real next step looks like.

Full suite: 551 passed throughout this entry's work, no regressions.

---

## 2026-08-02 — Real image-gen slowness diagnosed and fixed: NVIDIA's dead fallback was making failures worse, not better

User asked directly why image generation was "taking ages." Answered from real data,
not a guess: the live Render dashboard's cost/latency table showed
`black-forest-labs/flux.1-dev` averaging **39s, with a 60s p95, and 1 real failure
out of 2 recent calls** — a live, current symptom, not a historical one.

**Root cause, already partly documented but not fully connected**: NVIDIA's
free-tier NIM image endpoint has a known, real characteristic of occasional
transient slowness/500 errors (README's own Limitations section, task #22). What
hadn't been fixed: `NvidiaVisualGenerator`'s configured fallback model,
`flux.1-schnell`, is the SAME model confirmed **permanently dead** back in task
#22 (times out every time). So the actual failure mode was: NVIDIA's primary call
runs slow or fails (up to the old 180s timeout), THEN the pipeline retried a
model that was already known to always fail too — every real failure cost two
doomed waits, not one.

**Fix, two parts**:
1. `NvidiaVisualGenerator`'s default timeout cut from 180s → 75s — real headroom
   above the observed 60s p95, but bounds worst-case failure to a fraction of
   three full minutes.
2. New `FallbackVisualGenerator` (`polyglo/visuals.py`) — genuine cross-*provider*
   fallback, not genblaze's own same-provider `fallback_models` mechanism. NVIDIA
   stays primary; on a real `VisualError`, it now retries with
   OpenRouter/Seedream (already built for the character-consistency fix) instead
   of the confirmed-dead `flux.1-schnell`, only when `OPENROUTER_API_KEY` is
   configured — with no key, behavior is unchanged (raises the primary's real
   error, same as before, just faster given the shorter timeout). `orchestrator.
   make_providers()` now wires `FallbackVisualGenerator(primary=NvidiaVisualGenerator(...),
   secondary=OpenRouterVisualGenerator(...) or None)`.

**Verified live, and it produced the best possible evidence on the first real
try**: created a real 2-scene story against the local dev server with real
credentials. Server logs show NVIDIA's scene-0 call genuinely failing after ~60s
with `NVIDIA image generate failed (500): Internal Server Error` — a real,
naturally-occurring transient failure, not simulated — followed 9 seconds later
by `Starting pipeline 'openrouter-visual'` → `Pipeline complete: status=completed`.
Scene 1's NVIDIA call succeeded normally that time (9s), never needing the
fallback. Fetched scene 0's actual resulting image: a real, on-topic, good-quality
illustration (a cat at a window, matching the source text), produced by the
fallback path within seconds of the real NVIDIA outage — not a contrived test,
the exact real-world failure this fix targets, caught on the very first live
verification attempt.

Six new tests (`tests/test_visuals.py`) cover `FallbackVisualGenerator` directly
(primary success, secondary takeover, both-fail, no-secondary-configured,
reference-image forwarding); two existing `make_providers()` gating tests
(`tests/test_make_providers_transcriber_gating.py`) updated for the new wrapper
type, plus one new test pinning that a primary failure genuinely reaches the
secondary with its own model string. Full suite: 557 passed.

---

## 2026-08-02 — Real production incident: OOM restart traced to the video-export route having no resource ceiling at all

User reported "video is not downloadable." Investigated the live site directly
rather than guessing: it was returning a real `502` (confirmed via response
headers — Cloudflare's own error page, not this app's), which resolved on retry —
a Render cold-start blip, not a crash on its own. But a genuine Render platform
notification arrived in the same window: **"Web Service polyglo exceeded its
memory limit, which triggered an automatic restart."** The timing — right after
the video-export feature shipped — was the real signal, not a coincidence to
explain away.

**Root cause, found by re-reading the feature's own code with this lens**: the
video-export route had *zero* resource protection, unlike every other real-cost
route added this session (story creation and chaos toggling were both rate-limited
from the start; this one, added later in the same session, was missed). Each real
call: fetches every scene's full image+audio bytes into memory, then runs a real
`ffmpeg` subprocess per scene (`libx264` encoding at 1024×1024, default "medium"
preset — a real memory/CPU cost, not free) with **no limit on how many could run
concurrently**. Two or three overlapping requests — plausible if a user clicks the
button twice, or two people hit the same story — would plausibly be exactly
enough to push a small Render instance over its memory limit.

**Fix, three parts, all in `polyglo/video.py` unless noted:**
1. **A real concurrency limit** — `_MAX_CONCURRENT_ENCODES = 1` via
   `threading.Semaphore`, acquired non-blocking. A second request while one is
   already composing gets an immediate `VideoBusyError` → a real `503` with
   `Retry-After`, not a queued wait (queuing would just move the memory pressure
   from "concurrent ffmpeg processes" to "concurrent held request threads,"
   not fix it) and not a 500.
2. **Lower per-encode resource cost** — resolution dropped from 1024×1024 to
   640×640, and `-preset ultrafast` added to the `libx264` call (previously
   unset, defaulting to `-preset medium`, which trades real encoder-side memory
   and CPU for compression efficiency this feature doesn't need for a short demo
   clip). Both cut memory pressure without changing what the feature does.
3. **A per-IP rate limit on the route itself** (`polyglo/api.py`,
   `_video_export_limiter`, 3 requests/5min) — the same protection story
   creation and chaos toggling already had, extended to cover the route that was
   missed. Also tightened the video-specific scene-count cap from the general
   20-scene story limit to 10 — real ffmpeg encoding is far more memory/CPU-
   hungry per scene than image generation, so the two caps shouldn't share the
   same headroom.

New tests: `tests/test_video.py` gains real concurrency tests (a genuinely
blocking fake `subprocess.run`, run on a background thread, proves a second
call fails fast with `VideoBusyError` while the first is still in flight, and
that the semaphore releases correctly both on success and on failure —
confirmed a leak-on-failure bug would have shipped without that last test).
`tests/test_web.py` gains route-level tests for the 413/503/429 cases.

Full suite: 561 passed (was 557; +4 new tests). Not yet re-verified against a
live Docker container at time of writing — the local dev server confirms the
new limits behave correctly, but the actual OOM only reproduced on Render's
smaller instance, not this dev machine, so the real proof will be whether the
next real deploy stays within its memory limit under real use, not a local test
alone.

---

## 2026-08-02 (late) — OOM fix verified with real numbers; and a documented gotcha turned out to be WRONG in an important way

**Memory fix verified properly.** The previous entry's fix was verified only
indirectly (memory stayed flat during *story generation* in a capped container),
but the actual question — what does a real *video composition* cost — hadn't been
answered, because every test story kept quarantining and the route correctly 422'd.
Closed that gap: composed a real 2-scene video inside a `--memory=512m` container
using real Seedream images and real Voxtral-narrated audio, sampling
`docker stats` throughout.

Real numbers: baseline **172.8MiB**, peak **225.5MiB / 512MiB (44%)** during the
ffmpeg encode, back to **172.9MiB** after. So a real composition costs roughly
**+53MiB peak**, and since `_MAX_CONCURRENT_ENCODES = 1` that peak is the hard
ceiling rather than a per-request multiplier. Comfortably inside a small instance —
the fix holds.

**A previously-documented gotcha was wrong, and the correction matters.** While
diagnosing why every test story quarantined with `wer=None`, checked the real 429
payload's own `quotaId` field rather than inferring from the limit number. It reads
`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, limit 20 — Gemini's free-tier
ASR (`gemini-2.5-flash`) is **20 requests per DAY**, not the "20 req/min" recorded
in `docs/PROGRESS.md` earlier today. (The TTS model's 3/min limit, documented
separately, really is per-minute — the two are genuinely different quota types,
which is exactly how the earlier mistake happened.)

This is a materially different constraint, not a pedantic correction:
- **Waiting doesn't help.** A per-minute read implies "pace your runs"; the real
  per-day limit means once it's gone, it's gone until tomorrow.
- **It directly bounds the demo.** The QA gate is this project's centerpiece, and
  on the free tier it can verify only 20 segments per day *total*. One 5-scene ×
  4-locale story is exactly 20 — a single run can consume the whole day's quota.
- **It explains a whole class of confusing symptoms** (`wer=None` quarantines,
  video export 422ing for "no real narration") that look like app bugs and aren't.

`docs/PROGRESS.md`'s gotcha entry corrected in place, explicitly flagged as a
correction rather than silently edited, with the diagnostic advice (read
`quotaId`, don't guess from the number) that would have avoided the mistake.
