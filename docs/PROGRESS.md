# Progress — read this first in a new session

Snapshot as of **2026-08-02, late** (UI polish pass; image-download + narrated-video
export; production hardening — rate limiting, daily spend caps, structured logging,
CI; a real image-gen slowness fix; and a **real production OOM incident on Render**,
root-caused and fixed). Deadline: **Monday 3 August 2026, 17:00 EDT** (target
12:00 EDT to leave buffer). This doc is the fast-context entry point; for full
narrative detail on every decision and bug, read
[`SESSION-LOG.md`](SESSION-LOG.md) (append-only, chronological, and considerably
longer than this file).

**Keep this file current.** Every substantive change should update this snapshot in
the same commit — a `/update-progress` skill exists to make that a one-command step
(see `.claude/skills/update-progress/`). This file going stale has caused real
wasted work before (see "Corrections made this update" below).

If you're picking this project up in a new conversation: read this file, skim
[`CLAUDE.md`](../CLAUDE.md) (project-level tooling instructions — the repo is indexed
into `codebase-memory-mcp`, use that before grep+read for code discovery), then check
`git log --oneline` and `git status` against current reality before assuming anything
below is still true — this file has been stale in non-trivial ways before (see
"Corrections made this update" below).

---

## What this project is, in one paragraph

**Polyglo** — a comprehensible-input content factory. One source story → CEFR-graded
scenes → each scene's image generated **once**, shared by every target locale →
translation → narration → **automated QA gate** that transcribes the narration back and
diffs it against the source text (WER-scored), retrying on a different voice or
escalating to a stronger model before quarantining for human review. Built for the
[Backblaze Generative Media Hackathon](https://backblaze-generative-media.devpost.com/).
Full product rationale in [`01-PRODUCT-DESIGN.md`](01-PRODUCT-DESIGN.md).

The pitch line: *"Localization QA is still humans listening to every audio file. We
made it a pipeline stage."*

## Status: the core product is fully built and deployed; what's left is submission logistics

```
DONE:    #1–17, #20, #21, #22, #23, #24, #25, #26, #27, #28
         (environment, full pipeline, API, UI, Docker, B2, real image gen, real ASR,
          Hindi numerals, zero-cred regression test, Gemini-as-real-narrator, story
          autocorrect/CEFR restructure, visual design pass, reader view, progress
          stepper)
DONE, ALREADY LIVE: #19 — deployed to Render (not HF Spaces, see correction below),
         real credentials configured, auto-deploys on push. This is NOT blocked on
         a user-gated account action anymore.
DONE:    #29 — per-locale flag emoji + a real dashboard UI chaos toggle (not just
         the JSON API). Committed, verified live in the browser, full suite passes.
DONE (this session): a real bug found while gathering #18 calibration data and
         fixed — Gemini's free-tier TTS model hard-caps at **3 requests/minute**,
         separate from and tighter than this project's own daily `GeminiBudget`.
         The old retry ladder burned all 3 attempts on a 429 and wrongly
         quarantined perfectly good content as a quality failure. `gate.py` now
         detects the rate-limit and stops immediately (same end state, no wasted
         attempts/budget). See `docs/SESSION-LOG.md`'s 2026-08-01 entry for the
         full root-cause trail. **Demo-relevant**: pace real multi-locale runs
         (a few seconds apart), don't fire many real locales in one burst.
DONE (this session): user-facing copy across every template rewritten in plain
         language, no jargon, no em-dashes as sentence joiners — the app used to
         read like an engineering spec ("CEFR-levels your text", "cross-modal QA
         gate", "content-addressed hashing"). Live-verified, full suite passes.
DONE (this session): two optional OpenRouter providers, both off by default and
         verified live with real spend, not just unit tests —
         1. `OpenRouterNarrator` (Mistral Voxtral TTS) — ranks above Gemini as
            narrator whenever `OPENROUTER_API_KEY` is set, fixing the
            "verifier must not be generator" trade-off `GeminiNarrator` carries
            alone (Gemini still verifies either way).
         2. `OpenRouterVisualGenerator` (ByteDance Seedream) — opt-in via
            `OPENROUTER_PREFER_IMAGES=true`; fixes the real character-consistency
            bug in NVIDIA's image path (no image-to-image parameter there) via
            Seedream's real reference-image conditioning. Verified live: same boy,
            same dog, same art style across 3 genuinely different real scenes.
         Neither key nor flag being set changes any existing default behavior.
DONE (this session): a real, pre-existing bug found by the live verification above,
         unrelated to the new providers but affecting Gemini narration too —
         narrated audio bytes were computed and used for verification but never
         uploaded to the blob store, so every real "Listen to it" audio link has
         404'd since narration went real (task #24), silently masked by the
         `<audio>` tag's own `onerror` fallback (looks identical to the
         *intentional* simulated-audio case). Fixed in `qa/gate.py` +
         `orchestrator.py`; pinned with a permanent regression test; re-verified
         live after the fix (real fetchable MP3, valid `ID3` bytes). See
         `docs/SESSION-LOG.md` for the full trace.
DONE (this session): image generation was "taking ages" — diagnosed from real live
         dashboard telemetry (flux.1-dev: 39s avg, 60s p95, real failures), not a
         guess. Root cause was NOT just NVIDIA's known free-tier slowness: the
         configured NVIDIA fallback model was `flux.1-schnell`, confirmed
         permanently dead back in task #22 — so every real failure cost a long
         primary timeout AND an equally doomed fallback timeout. Fixed: timeout
         180s→75s, plus a new `FallbackVisualGenerator` routing real failures to
         OpenRouter/Seedream (a genuinely different, working provider) instead.
         Verified live on the first real attempt: a genuine NVIDIA 500 mid-run was
         followed 9s later by a successful OpenRouter fallback producing a real,
         correct illustration.
DONE (this session): **a real production OOM incident.** Render sent a
         "exceeded its memory limit, triggered an automatic restart" alert shortly
         after the video-export feature shipped. Root cause: that route had **zero
         resource protection** — unlike story creation and chaos toggling, which
         were rate-limited from the start, the video route (added later the same
         session) was missed. Each call loads every scene's image+audio into memory
         and runs real ffmpeg/libx264 encodes at 1024×1024 with **no concurrency
         limit at all**. Fixed three ways: a `threading.Semaphore` capping
         concurrent encodes to 1 (second request → fast 503, not a queued wait);
         resolution 1024→640 plus `-preset ultrafast` (cuts per-encode memory);
         and a per-IP rate limit (3/5min) plus a video-specific 10-scene cap.
         **Verified with real numbers in a real `--memory=512m` container**: a
         genuine 2-scene video composition (real Seedream images + real Voxtral
         narration, real ffmpeg) peaked at **225.5MiB / 512MiB (44%)** and
         returned to a 172.9MiB baseline afterward. With concurrency capped at 1,
         that peak is the ceiling — comfortably inside a small instance.
OPEN:    #18 — WER threshold calibration, strictly. Real data now spans a clean
         pass (WER 0.0), a real escalate-then-recover (1.0 → 0.0 on `voice-strong`),
         and the rate-limit case above (not a WER data point at all, now excluded
         from the retry ladder rather than muddying threshold data). Current
         thresholds (pass ≤0.10, retry ≤0.25) have not been contradicted by any
         real sample yet — no evidence they're wrong, just not formally calibrated
         against a real distribution. Recommend: ship as reasonable-but-uncalibrated
         and say so honestly in the write-up, unless more real samples are gathered
         first (budget-constrained, see below).
REMAINING: submission logistics — see "Remaining work" below.
```

Run `TaskList` at session start to check for tracked tasks — it was empty as of this
update, so this file plus `git log`/`git status` is the authoritative source of truth
right now, not the task tracker.

## Corrections made this update — read this if you remember the old version of this file

The previous snapshot (2026-07-31 night) had two significant staleness bugs, both
now fixed here:

1. **Deployment is not blocked on an HF Spaces account.** The project was live-deployed
   to **Render** on 2026-08-01 (`docs/SESSION-LOG.md`, "Real deploy bug: missing
   `genblaze-nvidia[chat]` extra") — first live deploy, real credentials, auto-deploy
   on push to `origin/master` confirmed working (a follow-up fix was pushed and
   resolved the live site with no manual redeploy step). `docs/03-BUILD-PLAN.md` had
   said Hugging Face Spaces; that target changed to Render along the way (see
   `c84086d Make Dockerfile host-agnostic: honor $PORT, target Render instead of HF`).
   **What's actually still open here**: the live URL and demo video are still `(#)`
   placeholders in `README.md`'s top line and `docs/05-SUBMISSION-KIT.md`'s checklist
   is entirely unchecked — pasting in the real URL/video link is copy-editing, not
   redeploying.
2. **Real NVIDIA audio is not the only path to real narration anymore.** Task #24
   (2026-08-01) made **Gemini** (`gemini-2.5-flash-preview-tts`) the real production
   `Narrator`, not just the ASR verifier — `orchestrator.make_providers()` now tries
   NVIDIA narration first (still dead, self-hosted-GPU scope boundary, unchanged),
   falls back to `GeminiNarrator` (real audio), then `SimulatedNarrator`. Verified
   against the live API: a real 3-word narration lost a word in transcription, scored
   real WER 0.333, retried on two different real Gemini voices, and was correctly
   quarantined when none cleared threshold — the QA gate now genuinely exercises real
   content, not just `unverified` placeholders. `has_audio_generation` (the
   NVIDIA-specific flag) still correctly reports `false`; the transcriber's gating
   condition changed to activate whenever Gemini is configured at all, independent of
   that NVIDIA flag, because Gemini-as-narrator means real audio exists whenever
   Gemini is configured.

## What's actually built and working right now

- **Full pipeline**: `authoring.py` (now with autocorrect + CEFR restructuring, task
  #25) → `visuals.py` (real `flux.1-dev` images, cross-scene character consistency) →
  `localize.py` → `narrate.py` (real Gemini narration, task #24) → `qa/gate.py` →
  bundling, orchestrated by `orchestrator.py`.
- **QA gate, now exercising real content**: TTS → ASR round-trip → WER →
  retry/escalate/quarantine (`polyglo/qa/gate.py`, `wer.py`, `normalize.py`,
  `numerals.py`, `text_gate.py`). Real evidence: WER 0.0 (exact-match spike) and WER
  0.333 (a real retry-and-quarantine case), both against live Gemini calls.
- **Content-addressed storage**: `store.py` (local + real B2, both verified working).
- **Real Genblaze integration**: every generation step is an actual
  `Pipeline().step().run()` producing a genuine, hash-verified `Manifest`
  (`polyglo/pipeline.py`).
- **Telemetry**: Genblaze's own `runs`/`steps`/`assets` Parquet tables plus our own
  `qa` table, queried live with DuckDB (`telemetry.py`).
- **FastAPI + server-rendered UI**: story creation, htmx-polling locale matrix, WER
  diff panel, dashboard (now with a chaos-toggle *panel*, task #29, not just the JSON
  endpoint), a dedicated per-locale reader view (`read.html`, task #27), a progress
  stepper replacing the raw SSE log as the primary in-flight view (task #28), a real
  visual/design pass with dark-mode support (task #26).
- **Docker**: builds and runs correctly, **verified via a real `docker build` +
  `docker run` + live HTTP calls**, including `--no-cache` rebuilds specifically to
  catch dependency bugs that a cached layer would mask (this is exactly how the
  `genblaze-nvidia[chat]` extra bug and the real deploy bug below were caught).
- **Zero-credential mode is genuinely functional** — real bundles, real dedup ratio,
  honest `unverified` QA status, no crash, no empty output. Don't assume it still works
  after a change without re-testing it the same way (see "How to verify" below).
- **Real image generation**: `flux.1-dev` is the configured primary model
  (`orchestrator.make_providers()`), confirmed live (200 OK, verified real JPEG).
  `flux.1-schnell` (the original primary) really is dead (times out).
- **Real narration + real ASR verification, both wired into production** (task #24):
  `polyglo/narrate.py`'s `GeminiNarrator` and `polyglo/qa/gemini_transcriber.py`'s
  `GeminiTranscriber` share one `GeminiBudget` instance (narration now also spends
  budget, not just verification — a 5-scene, 4-locale story is ~40 Gemini calls).
  **Respect the daily cap** (`polyglo/qa/budget.py`) — hard cap, not advisory.
- **Live in production on Render**, auto-deploying from `origin/master`. Real B2
  credentials, real NVIDIA chat/image, real Gemini narration+ASR all configured.
  SQLite index snapshotted to B2 so it survives a Render redeploy (Render's local disk
  is ephemeral); B2 blobs remain the durable source of truth regardless.

## Known, accepted trade-off (not a bug): Gemini verifies Gemini

`qa/gate.py`'s own design principle is "the verifier must not be the generator."
Task #24 knowingly violates this literally — `GeminiNarrator` and `GeminiTranscriber`
are the same model family, so correlated failures could pass undetected. Investigated
and rejected using NVIDIA chat as an independent audio-input ASR path (uncertain
payload shape/support, not worth the time against the rest of the backlog). Documented
loudly in code comments and the README rather than hidden. Worth naming explicitly in
the submission write-up as an honest limitation, not glossing over it.

## Two production bugs Docker verification found that a green test suite never caught

**This is the single most important lesson from this build**, and it happened twice
independently — worth keeping in mind for any further "verify by reading the diff"
temptation.

1. **Zero-credential path** (task #23 era): the chat fallback returned literally
   `"{}"` for every call — every zero-credential story creation crashed immediately.
   Fixed with `OfflineChatCompleter` (`polyglo/chat.py`). Task #23
   (`tests/test_orchestrator_offline.py`) now exercises the real `make_providers()`
   zero-credential path directly, asserting non-empty bundles.
2. **First real Render deploy** (2026-08-01): `genblaze-nvidia`'s chat provider
   imports `openai` directly but that's an optional extra
   (`genblaze-nvidia[chat]`), not the base install. Every local test/Docker run this
   whole session had `openai` already present in the dev `.venv` from earlier
   exploration, masking the gap until a genuinely clean environment (a fresh Render
   deploy) hit it. Fixed in `pyproject.toml`; verified via a `--no-cache` Docker
   rebuild specifically because cached layers can hide exactly this kind of bug.

Both bugs slipped through a passing test suite because every test fixture builds its
own mock providers directly, never the real `make_providers()` with zero/real
credentials in a genuinely clean environment. If you add new provider-selection logic,
add a test that goes through `make_providers()` itself, not a hand-built mock.

## #18 — WER threshold calibration (the one substantively open product item)

Real data now exists (task #24's real WER 0.333 case, plus the WER 0.0 spike), but
calibration itself — deciding the actual pass/retry/escalate/quarantine thresholds
from real samples rather than the current placeholder — hasn't been done. Two data
points isn't enough for real calibration; this needs either more real narrate+ASR
round trips (budget-constrained, see `GeminiBudget`) or an explicit decision to ship
the current thresholds as reasonable-but-uncalibrated and name that honestly in the
submission write-up. **Worth a decision before Monday**, not urgent engineering work.

## Hard-won gotchas — read before touching these areas again

0. **Gemini's free tier limits — and the ASR one is PER DAY, not per minute.**
   **CORRECTED 2026-08-02 late** (an earlier version of this entry said "20/min"
   — that was wrong, and it matters a lot). Read from the real 429 payload's own
   `quotaId`:
   - ASR (`gemini-2.5-flash`): **20 requests per DAY**
     (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`). Not per minute.
   - TTS (`gemini-2.5-flash-preview-tts`): **3 requests per minute**
     (`...PerMinute...`), confirmed separately earlier.

   Both are Google-side, separate from this project's own `GeminiBudget`, and
   both surface as `429 RESOURCE_EXHAUSTED`.

   **Symptom to recognize**: segments quarantining with `status='error'` and
   **`wer=None`** — no WER score at all. `wer=None` on a quarantine means a
   *provider error*, not a quality failure. Check for a 429 before assuming the
   gate or the audio is broken.

   **Why this matters for the demo, seriously**: the QA gate is this project's
   centerpiece, and on the free tier it can only verify **20 segments per day
   total**. A single 5-scene × 4-locale story is 20 segments — the entire day's
   ASR quota in one run. Waiting does NOT help; it resets daily, not hourly.
   Plan demo/recording runs accordingly (few scenes, few locales), or expect
   everything to quarantine with `wer=None` and correctly refuse to produce a
   video. To diagnose which quota was hit, read the `quotaId` field in the 429
   body rather than guessing from the number.
0b. **The B2 DB snapshot key is shared between local dev and the live deploy**
   (`db-snapshot/polyglo.db`, a single fixed key — `db.py`). Local pipeline runs
   overwrite the same snapshot the deployed instance restores from, so heavy local
   testing can replace the live site's visible story list on its next restart.
   Blobs are content-addressed and never lost; only the *index* is affected. A real,
   known architectural gap (documented in `docs/08-PRODUCTION-ROADMAP.md`), not yet
   fixed — an env-specific key suffix is the obvious fix if it becomes a problem.
1. **`get_config()` is `@lru_cache`'d; `db.session()` calls it fresh internally.**
   Any test fixture setting `POLYGLO_DATA_DIR`/`POLYGLO_DB_PATH` via
   `monkeypatch.setenv` **must** call `polyglo.config.reset_config_cache()`
   immediately after, and again in teardown. Skipping this silently writes test data
   into the real dev database — happened once (48 stories), was cleaned up.
2. **`polyglo.api` and `polyglo.web` each import `make_providers` as their own
   separate name binding** from `polyglo.orchestrator`. A fixture patching one does
   NOT patch the other. Any new HTML-route test file must patch `make_providers` on
   every module that imports it.
3. **The SSE endpoint's idle timeout was 5 minutes; it's now 20 seconds.** A test that
   opens the stream and walks away without waiting for completion left the
   server-side generator running for the full idle budget (measured: 312s → 24s after
   the fix). `request.is_disconnected()` is correct for a real ASGI server but
   Starlette's `TestClient` doesn't reliably trigger it — the idle budget itself, not
   disconnect detection, bounds worst-case time.
4. **Hyphenated words get split into multiple WER tokens** by `normalize.py`. When
   engineering a corrupted transcript for a test, compute the actual WER via
   `polyglo.qa.wer.score()` rather than estimating.
5. **Numeral expansion happens before WER comparison** — "3" vs "tres" must match.
   Hindi 0–100 is a hand-verified literal lookup table, not an algorithm.
6. **`ObjectStorageSink` is single-use; a bare `ParquetSink` is not.** Never reuse an
   `ObjectStorageSink` across runs.
7. **PowerShell piping (`Select-Object -Last N`, `Tee-Object`) fully buffers pytest's
   output** until the process exits. Use `python -u -m pytest ... -v > file.txt`
   (unbuffered + verbose + plain redirect) for genuine incremental progress.
8. **`TaskStop` on a background task may not kill child processes** it spawned
   (confirmed: orphaned `python.exe` processes survived multiple `TaskStop` calls on
   Windows). Check `tasklist //FI "IMAGENAME eq python.exe"` before assuming a "fresh"
   run's odd behavior is a new bug.
9. **`pip install .` does not bundle non-`.py` files by default.** `templates/`/
   `static/` needed an explicit `[tool.setuptools.package-data]` entry in
   `pyproject.toml`.
10. **A full-suite run can genuinely take 6x longer for reasons that are NOT a code
    regression.** Pre-existing, unrelated Docker containers on this machine compete
    for CPU/IO. Check whether tests still *pass* before chasing a "slowdown."
11. **Verify a build claim by actually building, not by reading the diff.** Rebuilding
    Docker from scratch and re-running a real story creation is what actually confirms
    a dependency change is safe — this caught the `genblaze-nvidia[chat]` bug above.
12. **On this machine, run tests with the project's own `.venv`, not the system
    Python.** `python -m pytest` against system Python fails to even collect
    (`genblaze_core`, `pyarrow` missing) — use
    `.venv/Scripts/python.exe -m pytest`. Also, a `langsmith` pytest-autoload plugin
    in some environments fails to import (`ModuleNotFoundError: xxhash._xxhash`);
    if that happens, run with `-p no:langsmith` or `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`
    (the latter also disables other autoloaded plugins, so prefer `-p no:langsmith`
    first).

## How to verify nothing is broken

```bash
# Full suite (Windows, this project's venv) — 487 tests as of this update, zero
# credentials needed, ~1 minute
.venv/Scripts/python.exe -m pytest -q -p no:langsmith

# Zero-credential pipeline actually works (not just "doesn't crash") —
# this is the check that would have caught the offline-path production bug:
NVIDIA_API_KEY="" B2_KEY_ID="" GEMINI_API_KEY="" python -c "
from polyglo import db as dbm
from polyglo.store import BlobStore, LocalBackend
from polyglo.telemetry import TelemetryStore
from polyglo.models import Story, DEFAULT_LOCALES
from polyglo.orchestrator import run_story_pipeline, make_providers
from polyglo.config import get_config
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
conn = dbm.connect(tmp/'t.db'); dbm.init_db(conn)
store = BlobStore(LocalBackend(tmp/'blobs')); telemetry = TelemetryStore(tmp/'telemetry')
providers = make_providers(get_config(), chaos=None)
story = Story.create('Smoke Test', cefr='B1')
outcome = run_story_pipeline(story, 'a short story', 2, list(DEFAULT_LOCALES),
                              conn, store, telemetry, providers)
assert all(len(b.image_refs) > 0 and len(b.audio_refs) > 0 for b in outcome.bundles), 'EMPTY BUNDLE — REGRESSION'
print('OK:', outcome.dedup.summary())
"

# Docker — full build + run + live check (use --no-cache before trusting a
# dependency change — this is what caught the genblaze-nvidia[chat] bug)
docker build --no-cache -t polyglo:test .
docker run -d --name polyglo-check -p 7861:7860 polyglo:test
curl -s http://localhost:7861/api/status
# create a story, poll GET /api/stories/{id}, confirm bundles are non-empty
docker stop polyglo-check && docker rm polyglo-check
```

## Remaining work, roughly in priority order

1. ~~Commit task #29~~ — **done**.
2. ~~Rate-limit fail-fast fix~~ — **done**.
3. ~~User-facing copy rewrite (plain language, no jargon)~~ — **done**.
4. ~~OpenRouter narrator (Voxtral) + image-consistency fix (Seedream), both
   optional~~ — **done**, real spend, live-verified.
5. ~~Audio-blob-upload bug (real narration was never actually fetchable)~~ —
   **done**, fixed and pinned with a permanent regression test.
6. ~~Live Render URL pasted into README/Devpost copy~~ — **done**
   (`https://polyglo.onrender.com/`).
7. ~~UI polish (real logo, animations, plain-language copy) + image/video
   export~~ — **done**, real spend, live-verified (real 253KB MP4, valid
   `ftyp`/`isom`/`avc1` structure, from a real 3-scene story).
8. ~~Production hardening (rate limiting, daily spend caps, structured
   logging, CI)~~ — **done**, live-verified (a real 429 fired against the dev
   server after real repeated requests). See `docs/08-PRODUCTION-ROADMAP.md`
   for what's still genuinely missing beyond this.
9. ~~Slow/failing image generation~~ — **done**, root-caused from real telemetry
   (a dead fallback model doubling every failure's cost), fixed, live-verified.
10. ~~Production OOM incident on Render~~ — **done**, root-caused to the
    video-export route having no concurrency limit or rate limit at all, fixed
    three ways, verified in a real `--memory=512m` container (flat 172MB/512MB).
11. **#18 — decide on WER threshold calibration.** Real data now exists across
   several genuine outcomes (see status block above); current thresholds aren't
   contradicted by anything real yet. Recommend shipping as-is and naming it
   honestly in the write-up rather than spending more real budget chasing more
   samples this close to the deadline.
10. **Record and upload the 3-minute demo video** (public), per the beat sheet in
    `docs/05-SUBMISSION-KIT.md` §3. **Requires the user** — recording/narrating a
    video isn't something an agent session can do. Strongest beats to show, in
    order of how demo-able they are right now: the character-consistency fix
    (Seedream, same character across scenes — genuinely striking side-by-side),
    the new video-export download, the chaos-toggle failover, and the QA-gate
    retry/quarantine moment. **Pace real narration during recording** — no more
    than ~3 real Gemini TTS calls within any one-minute window (fewer if
    `OPENROUTER_API_KEY` isn't set, since Gemini is then both narrator and
    verifier), or a locale will wrongly quarantine on-camera.
11. **Final README polish + Devpost form**, per `docs/05-SUBMISSION-KIT.md` §2/§4 —
    `docs/07-DEVPOST-COPY.md` is current as of this session and ready to paste, video
    link excepted. **Submitting the Devpost form itself requires the user's
    account** — an agent session can draft the text but should not submit it.
12. **Repo is already public** (`github.com/dj-DeepakJadhav/polyglo`, confirmed via
    `gh repo view`) and `.env` is gitignored — a secrets scan this session found
    nothing real committed. Still worth a final look right before submission per the
    submission kit's own checklist.
13. **Optional, post-submission only**: a free/cheap Chinese-vendor model survey was
    done for both chat (text) and image generation on OpenRouter — see chat history
    for the full comparison. Headline: no genuinely free, good chat model was found
    (the one free option, `inclusionai/ling-3.0-flash:free`, has no benchmark data);
    `bytedance-seed/seedream-4.5` (already integrated above) was the standout find.
    Not worth further integration before the deadline.

## Key files, if you need to jump straight to something

| Area | File |
|---|---|
| Domain model, the core dedup invariant | `polyglo/models.py` |
| Narrated video export (ffmpeg, concurrency-capped) | `polyglo/video.py` |
| Rate limiting (per-IP, shared across HTML+JSON routes) | `polyglo/ratelimit.py` |
| Daily call/spend budgets (Gemini, OpenRouter, global) | `polyglo/qa/budget.py` |
| Keeping this file current | `.claude/skills/update-progress/` |
| QA gate state machine | `polyglo/qa/gate.py` |
| WER scoring + normalization | `polyglo/qa/wer.py`, `qa/normalize.py`, `qa/numerals.py` |
| Pipeline orchestration | `polyglo/orchestrator.py` |
| Provider selection (real vs simulated vs offline) | `orchestrator.make_providers()` |
| Real narrators (Gemini task #24, OpenRouter/Voxtral) | `polyglo/narrate.py` (`GeminiNarrator`, `OpenRouterNarrator`) |
| Real ASR verifier | `polyglo/qa/gemini_transcriber.py` |
| Real image generators (NVIDIA, optional OpenRouter/Seedream) | `polyglo/visuals.py` (`NvidiaVisualGenerator`, `OpenRouterVisualGenerator`) |
| Shared Gemini call budget | `polyglo/qa/budget.py` |
| Zero-credential regression test | `tests/test_orchestrator_offline.py` |
| Audio-blob-upload regression test | `tests/test_orchestrator.py::test_narrated_audio_is_actually_persisted_to_the_blob_store` |
| Narrated video export | `polyglo/video.py` (`compose_story_video`), route in `polyglo/web.py` |
| Rate limiting (per-IP) | `polyglo/ratelimit.py` |
| Daily spend caps (Gemini, OpenRouter, global story count) | `polyglo/qa/budget.py` (`DailyCallBudget`) |
| Structured logging setup | `polyglo/logging_config.py` |
| CI | `.github/workflows/tests.yml` |
| FastAPI JSON routes | `polyglo/api.py` |
| HTML UI routes (chaos panel, reader, stepper, video export) | `polyglo/web.py` |
| Per-locale reader view | `polyglo/templates/read.html` |
| Dashboard chaos toggle | `polyglo/templates/_chaos_panel.html` |
| Telemetry / DuckDB queries | `polyglo/telemetry.py` |
| Config + credential detection | `polyglo/config.py` |
| Full chronological build log | `docs/SESSION-LOG.md` |
| Product rationale | `docs/01-PRODUCT-DESIGN.md` |
| Architecture + API surface | `docs/02-TECHNICAL-ARCHITECTURE.md` |
| Submission checklist | `docs/05-SUBMISSION-KIT.md` |
| Honest production-readiness gaps (SQLite, auth, legal, monitoring) | `docs/08-PRODUCTION-ROADMAP.md` |
