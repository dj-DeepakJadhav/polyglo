# 09 — Devpost form: field-by-field paste text

Everything below is written to be pasted directly into the Devpost submission form,
in form order. Every number and claim is from a real run or test recorded in
`docs/SESSION-LOG.md` / `docs/PROGRESS.md`. Nothing here is aspirational.

---

## Screen 1 — Project overview → General info

### Project name (60 char limit)

```
Polyglo
```

Alternate, if you want the descriptor in the gallery card title (49 chars):

```
Polyglo — verified multilingual story localization
```

### Elevator pitch (200 char limit)

Recommended (193 chars):

```
Localization QA still means humans listening to every audio file. Polyglo makes it a pipeline stage: one story fans out to N languages, narration auto-verified, bundles deduped on Backblaze B2.
```

Alternate, more mechanism-forward (186 chars):

```
One story to N languages, with the QA humans normally do by ear turned into a blocking pipeline gate: TTS to ASR to word-error diff to retry to quarantine. Assets deduped on Backblaze B2.
```

### Thumbnail

3:2 ratio, JPG/PNG/GIF, max 5MB. Best candidate: a screenshot of the locale matrix
with the per-locale flags and QA statuses visible, or a side-by-side of the same
character across three scenes (the Seedream consistency result) — that image is the
most immediately legible "this is real generative media" frame we have.

---

## Screen 2 — Project Story → About the project

Paste the whole block below into the "About the project" box.

```markdown
## Inspiration

We wanted to ship graded, comprehensible-input reading material in twenty languages
instead of two. Generation was never the blocker — models make scenes, images and
narration cheaply. The blocker is **quality assurance**: somebody sits with a
transcript and listens to every generated audio file in every language, checking the
voice actually said the words. That step is manual, boring, expensive, and it scales
linearly with locale count, which is exactly why small teams ship two languages.

So we stopped treating QA as a human step that happens after the pipeline, and built
it as a stage *inside* the pipeline.

## What it does

Polyglo is a content factory. One source story goes in; per-locale, publish-ready
bundles come out on Backblaze B2.

- **Author** — the source text is autocorrected and restructured to a target CEFR
  level, then split into scenes.
- **Generate visuals once** — each scene's illustration is generated a single time and
  shared by every locale via its SHA-256 content hash. Images never get regenerated
  per language. Later scenes are conditioned on the first scene's image so the same
  character and art style persist across the story.
- **Translate and narrate** — each locale gets its own translation and its own real
  generated narration audio.
- **Verify automatically** — this is the centrepiece. The pipeline transcribes the
  generated audio back using a *different* model than the one that produced it,
  normalizes both sides (including numeral expansion, so "3" and "tres" compare
  equal), and scores Word Error Rate against the text that was fed to TTS. Above
  threshold, it retries on a different voice, escalates to a stronger model, and only
  then quarantines the segment for human review. Wrong-language output and
  untranslated source leakage are caught by a separate text gate.
- **Publish** — per-locale bundles land in a content-addressed store on B2, each
  pipeline run carrying a hash-verified manifest you can re-verify in the UI.
- **Export** — a narrated MP4 per locale, composed with ffmpeg: scene images with
  Ken Burns motion panning, the real narration audio, and high-contrast burned-in
  subtitles.

It's all visible in a live dashboard: the locale matrix with per-segment QA status,
the WER diff panel showing exactly which words drifted, a dedup ratio computed live
from the telemetry lake with DuckDB, and a **chaos toggle** that lets anyone — a judge
included — force a model to fail on demand and watch the fallback chain recover in
real time.

**The honest boundary, stated up front:** the gate verifies the TTS round trip, not
translation accuracy. We prove the audio says the words it was given. We do not prove
those words are a correct translation — a perfect WER score is fully consistent with
fluently narrating a bad translation. What we automated is the half of localization QA
that is real, currently manual, and expensive.

## How we built it

- **Genblaze** runs image generation and narration, each as a real
  `Pipeline().step().run()` emitting a hash-verified `Manifest`. The text steps
  (authoring, scene splitting, translation) call the chat helper directly, because they
  produce story structure rather than an asset that needs a manifest.
  `ObjectStorageSink` + `S3StorageBackend.for_backblaze()` persist runs to B2, and a
  reusable `ParquetSink` writes the telemetry lake.
- **Backblaze B2 as a content-addressed blob store.** Every image and audio asset is
  keyed by its own SHA-256 hash, so writing identical bytes twice is a no-op. A real
  run of 4 scenes × 4 locales produced **32 asset references resolving to 20 unique
  blobs — 37.5% deduplicated**, entirely from scene images being shared instead of
  copied per language. The ratio grows with locale count, because images stay fixed at
  `scenes` while references grow at `scenes × locales`. The bucket runs with SSE-B2
  encryption and Object Lock available for immutable approved bundles.
- **A telemetry lake on top of B2** — Genblaze's own `runs`/`steps`/`assets` Parquet
  tables plus our own `qa` table, queried live with DuckDB for the dashboard's dedup
  ratio, retry evidence and per-model latency.
- **Models, deliberately mixed across vendors** so the verifier isn't the generator:
  NVIDIA NIM `meta/llama-3.1-8b-instruct` for authoring and translation and
  `black-forest-labs/flux.1-dev` for images; Google Gemini
  `gemini-2.5-flash-preview-tts` for narration and `gemini-2.5-flash` for ASR
  verification; optional OpenRouter providers for Mistral Voxtral narration and
  ByteDance Seedream image-to-image, each with its own fallback model.
- **FastAPI + server-rendered Jinja2 + htmx** for the UI, with an SSE progress
  stepper. `imageio-ffmpeg` for video export. SQLite for the index, snapshotted to B2
  so it survives an ephemeral-disk redeploy. Docker, deployed on Render, auto-deploying
  from master, with GitHub Actions running the full suite on every push.

Almost all of this runs on free tiers. NVIDIA NIM for text and images, Gemini for TTS
and the ASR check, plus one free OpenRouter model as a narrator fallback. The only
metered spend is a capped 200-calls-a-day OpenRouter budget. Those tiers have real edges:
Gemini TTS allows 3 requests a minute and ASR 20 a day, so one 5-scene, 4-locale story
eats a whole day of verification quota, and NVIDIA's free image endpoint is slow and
sometimes 500s. What you see is the free tier's ceiling, not the architecture's. Every
provider sits behind one small interface, so a paid model is a config change.

## Challenges we ran into

**NVIDIA has no hosted TTS.** We planned narration on NVIDIA's own audio models and
burned real time on 404s before finding the actual reason: Magpie ships as a
self-hosted GPU microservice you deploy yourself via NGC, not a hosted API call the way
chat and image generation are. No amount of slug-guessing fixes that. We moved
narration to Gemini and documented the boundary rather than hand-waving it.

**Our fallback model was already dead.** Image generation was "taking ages." Real
dashboard telemetry — not a guess — showed flux.1-dev at 39s average, 60s p95, with
real failures. The root cause wasn't just NVIDIA's free-tier slowness: the configured
fallback was `flux.1-schnell`, which we'd confirmed permanently dead earlier in the
build. Every real failure therefore cost a long primary timeout *plus* an equally
doomed fallback timeout. We cut the timeout 180s → 75s and built a genuine
cross-*provider* fallback to OpenRouter/Seedream. First live verification caught the
exact real failure it targets: a genuine NVIDIA 500 mid-run, followed 9 seconds later
by a successful fallback producing a correct illustration.

**A silent 404 that looked exactly like a feature.** Narrated audio bytes were computed
and used for verification but never uploaded to the blob store — so every "Listen to
it" link had 404'd since narration went real. It was invisible because the `<audio>`
tag's own error fallback renders identically to the *intentional* simulated-audio case.
Found only by fetching a real audio URL. Fixed and pinned with a permanent regression
test.

**A real production OOM.** Render alerted that the service had exceeded its memory
limit and restarted, right after video export shipped. The route had zero resource
protection — unlike every other real-cost route — while loading every scene's media
into memory and running real libx264 encodes with no concurrency limit at all. Fixed
three ways (a semaphore capping concurrent encodes at 1 so a second request fails fast
with 503 rather than queuing memory pressure, lower resolution plus `-preset
ultrafast`, and a per-IP rate limit), then verified with real numbers in a real
`--memory=512m` container: a genuine composition peaked at **225.5MiB / 512MiB**
and returned to a 172.9MiB baseline.

**Free-tier quotas that break the demo, not just the budget.** Gemini's TTS model caps
at 3 requests per minute and its ASR model at **20 requests per day** — two genuinely
different quota types, and we initially recorded the second one wrong as per-minute.
The correction matters: waiting doesn't help a daily quota, and a single 5-scene ×
4-locale story consumes the entire day's ASR allowance. Worse, the retry ladder was
burning all three attempts on 429s and then quarantining perfectly good content as a
*quality* failure. The gate now detects the rate limit and stops immediately.

**Two production bugs a green test suite never caught.** The zero-credential chat
fallback returned literally `"{}"` for every call, crashing every offline story
creation. And `genblaze-nvidia`'s chat provider imports `openai` directly, which lives
behind an optional extra — invisible locally because `openai` was already in our venv,
and only surfacing on a genuinely clean Render deploy. Both slipped through because
every fixture built its own mock providers instead of exercising the real
`make_providers()`. That's now a test.

## Accomplishments that we're proud of

- **The gate genuinely fires on real content.** A real run produced a real 3-word
  narration with one word dropped, scored a real WER of 0.333, retried on two
  different real voices, and correctly quarantined when none cleared threshold. Not a
  mocked demo path.
- **A measured storage claim, not an asserted one** — 32 references to 20 blobs, 37.5%
  deduplicated, from a real multi-locale run against a live B2 bucket.
- **Failure recovery you can trigger yourself.** The chaos toggle makes fallback
  behaviour demonstrable on camera instead of described in a slide, and it caught a real
  NVIDIA outage doing exactly what it was built for.
- **Character consistency across scenes** via real reference-image conditioning — same
  boy, same dog, same art style across three genuinely different generated scenes.
- **We fixed a real production incident with real measurements** rather than a plausible
  guess, and can state the memory ceiling as a number.
- **561 tests, CI on every push, and a zero-credential mode that produces real bundles**
  rather than crashing or emitting empty output.
- **Limitations written down loudly** — same-vendor verifier pairing, uncalibrated WER
  thresholds, translation accuracy explicitly not claimed. We'd rather be trusted on
  the narrow claim than doubted on a broad one.

## What we learned

- **Verify by building and running, not by reading the diff.** Both of our worst bugs
  passed code review and the test suite, and were caught by a `--no-cache` Docker
  rebuild and a real HTTP fetch respectively.
- **A fallback pointing at a dead model is worse than no fallback** — it converts one
  timeout into two.
- **Read the error payload, don't infer it.** The 429's own `quotaId` field told us the
  real quota; the limit number alone led us to the wrong conclusion for a day.
- **Distinguish provider errors from quality failures.** `wer=None` on a quarantine
  means the call failed, not that the content was bad. Conflating them makes a gate
  lie about your content.
- **Cross-modal verification is a genuinely underused production pattern.** The
  technique exists in speech-evaluation research; what's missing from the localization
  pipelines we looked at is running it as a *blocking gate* with a real
  retry/escalate/quarantine state machine behind it.

## What's next for Polyglo

- **Translation-accuracy scoring** — the half we deliberately didn't claim.
  Back-translation plus semantic similarity, or an LLM-as-judge rubric, as a second
  gate alongside the WER gate.
- **Calibrate the WER thresholds against a real distribution.** Current thresholds
  (pass ≤0.10, retry ≤0.25) aren't contradicted by any real sample we have, but they
  aren't formally calibrated either, and we say so rather than implying otherwise.
- **A fully independent verifier vendor by default**, so the narrator and the
  transcriber are never the same model family.
- **Postgres and a real job queue** in place of SQLite plus in-process background jobs,
  and environment-scoped DB snapshot keys so local runs can't touch production's index.
- **Object Lock immutability as the default publish step** for approved bundles, not
  just an available capability.
- **Prosody and naturalness scoring**, plus a proper human review queue for the
  segments that legitimately need a person — shrinking that queue is the goal, not
  pretending it's empty.
```

---

## Screen 2 — Built with (25 tag limit)

Every tag below is something actually in the dependency tree or the deployment, checked
against `pyproject.toml` and the provider modules. 24 tags:

```
python
fastapi
genblaze
backblaze-b2
amazon-s3
boto3
nvidia-nim
flux
google-gemini
openrouter
mistral
seedream
duckdb
apache-parquet
ffmpeg
sqlite
htmx
jinja2
uvicorn
pillow
docker
render
github-actions
pytest
```

Devpost's tag box autocompletes, so type each one and pick the existing tag where it
offers one — matching an existing tag makes the project show up in that tag's gallery,
a freshly-created near-duplicate doesn't.

---

## Screen 2 — "Try it out" links

Two links, in this order (the first one is the one judges click):

```
https://polyglo.onrender.com/
```

```
https://github.com/dj-DeepakJadhav/polyglo
```

Optionally add a third pointing straight at the dashboard, since that's where the
storage and QA evidence lives and it isn't obvious from the homepage:

```
https://polyglo.onrender.com/dashboard
```

---

## Screen 2 — Image gallery (up to 15, 3:2, ≤5MB each)

No images are committed to the repo, so these need capturing from the live site. Set the
browser window to a 3:2 ratio first (1400×933 works) so nothing gets letterboxed.
Ordered by how much each one earns — the first image is the gallery thumbnail and the one
most judges will actually look at.

| # | Shot | URL |
|---|---|---|
| 1 | Same character across three scenes, side by side — the reference-image consistency result. Strongest single frame; it's the one that reads as real generative media at a glance. | a story page's "Pictures, drawn once" strip |
| 2 | The locale matrix: per-locale flags, per-scene QA status, WER numbers | `/stories/<slug>` |
| 3 | Dashboard dedup panel — references vs unique blobs vs % saved | `/dashboard` |
| 4 | Dashboard WER results table + the retry ("caught and fixed automatically") panel | `/dashboard` |
| 5 | The chaos toggle panel with a model switched off | `/dashboard` |
| 6 | Per-locale reader view with the scene image and audio player | `/stories/<slug>/read/<locale>` |
| 7 | A frame from an exported MP4 showing the burned-in subtitle over a scene image | local file |
| 8 | The manifest verify page with a pass result | `/verify` |
| 9 | The progress stepper mid-run | `/stories/<slug>` while generating |

Nine is plenty. Fifteen mediocre screenshots score worse than nine deliberate ones.

---

## Screen 2 — Video demo link (required)

```
[TODO — paste the public YouTube/Vimeo/Facebook/Youku URL after recording]
```

Must be public and on one of those hosts per the rules. Beat sheet: `docs/05-SUBMISSION-KIT.md` §3.

Two constraints while recording, both learned the hard way:
- Gemini TTS allows **3 requests per minute**. Space out real narration calls or a locale
  quarantines on camera.
- Gemini ASR allows **20 requests per day**, and it does not reset hourly. A 5-scene ×
  4-locale story is exactly 20 segments — one run can eat the whole day's verification
  quota. Record with few scenes and few locales.

---

## Screen 3 — Additional info (judges and organizers only)

### App URL

```
https://polyglo.onrender.com/
```

### GitHub Repo URL

```
https://github.com/dj-DeepakJadhav/polyglo
```

Public, confirmed via `gh repo view`. No contributor grant needed for `b2genblaze`.

### Providers and models

```
Text (authoring, CEFR grading, scene splitting, translation)
- NVIDIA NIM `meta/llama-3.1-8b-instruct`. Free tier.

Images
- NVIDIA NIM `black-forest-labs/flux.1-dev`, primary. Free tier. 75s timeout, cut from
  180s after real telemetry showed 39s average and 60s p95.
- OpenRouter `bytedance-seed/seedream-4.5`, cross-provider fallback. Metered, inside the
  daily cap. Picked for one specific reason: it takes a reference image as conditioning,
  and NVIDIA's image path has no parameter for that. The reference pass is what holds the
  same character and art style steady from scene to scene. It has already saved a live
  run. On the first real verification pass, NVIDIA returned a 500 Internal Server Error
  on scene 0 after about 60 seconds; 9 seconds later the log shows
  `Starting pipeline 'openrouter-visual'`, then `status=completed`, with a real on-topic
  illustration at the end of it. Nothing simulated about that one.
- OpenRouter `microsoft/mai-image-2.5-pro`, the fallback behind Seedream. Metered.

Narration
- OpenRouter `mistralai/voxtral-mini-tts-2603`. Metered. First-choice narrator whenever
  OPENROUTER_API_KEY is set. It ranks above Gemini on purpose. Gemini runs the ASR check,
  so leaving Gemini as narrator meant one vendor generating the audio and grading it.
- OpenRouter `fish-audio/s2.1-pro-free:free`, Voxtral's fallback. Free.
- Google `gemini-2.5-flash-preview-tts`. Free tier. The narrator when there is no
  OpenRouter key.

Verification
- Google `gemini-2.5-flash`. Transcribes the generated audio back so the WER diff has
  something to score against. Free tier.

Scene animation, off by default
- fal `fal-ai/ltx-video`, Replicate `lightricks/ltx-video`, OpenRouter
  `lightricks/ltx-video`. Metered when you turn one on. SimulatedVideoGenerator is what
  runs otherwise, so a fresh clone with zero keys still completes a run.

Two models we do not use, and why. NVIDIA's Magpie TTS ships as a self-hosted GPU
microservice through NGC, not a hosted API, so every plausible slug 404s because there is
no hosted endpoint behind any of them. And `black-forest-labs/flux.1-schnell` is dead. It
times out on every call. It used to be the configured image fallback, which meant every
genuine NVIDIA failure cost two doomed waits instead of one.

On cost: text and images run on NVIDIA NIM's free tier, TTS and ASR on Gemini's, and one
narrator fallback is a free OpenRouter model. The only metered spend is a capped
OpenRouter budget, 200 calls a day by default, enforced by our own DailyCallBudget. The
ceilings are specific. Gemini TTS allows 3 requests per minute; Gemini ASR allows 20 per
day, which we read straight off the 429's quotaId field
(GenerateRequestsPerDayPerProjectPerModel-FreeTier) rather than inferring it. Twenty a
day means one 5-scene, 4-locale story eats an entire day of verification quota. NVIDIA's
free image tier is slow and it intermittently 500s. So what's on show is the free tier's
ceiling, not the architecture's. Providers sit behind one small interface with real
fallback chains, so upgrading to paid top-tier models is a config change instead of a
rewrite, and the WER gate is what makes that swap safe: a worse model shows up as rising
WER and gets caught before it ships.
```

### B2 and Genblaze usage

```
The name of a file is a fingerprint of what is inside it, so the same picture cannot be
stored twice no matter how many languages ask for it. Every image and audio asset lands
in B2 under the SHA-256 of its own bytes (polyglo/store.py, B2Backend over the
S3-compatible API through boto3). Writing identical bytes a second time is a no-op. That
is why scene art gets generated once off the source story instead of once per language.

Measured on a real 4-scene by 4-locale run: 32 asset references resolving to 20 unique
blobs. 37.5% deduplicated. That figure is COUNT(*) versus COUNT(DISTINCT sha256) on the
bundle_refs table, not an estimate. It improves with every locale you add, because image
count stays pinned at `scenes` while references grow at `scenes x locales`.

The bucket runs SSE-B2. Object Lock is one argument on ObjectStorageSink
(ObjectLockConfig), so an approved bundle can be frozen.

Render's local disk is ephemeral, so the SQLite index is snapshotted to B2 at
db-snapshot/<env>/polyglo.db and restored on boot. That key was a single fixed path at
first, and it was a live hazard: a local pipeline run overwrote the exact snapshot the
deployed instance restores from. Content-addressed blobs were never at risk, only the
index. Keying it off POLYGLO_ENV fixed it. The blobs stay the durable source of truth;
the DB is a cache you can throw away.

On the Genblaze side, image generation and narration each run as a real
Pipeline().step().run() and emit a hash-verified Manifest. The text steps call the chat
helper directly, since they produce story structure, not an asset that needs a manifest.
ObjectStorageSink with S3StorageBackend.for_backblaze() persists runs to the bucket. You
do not have to take the dashboard's word for any of it. Drop a manifest JSON into /verify
and you get pass/fail on the manifest's own hash plus every declared output checksum.

A reusable ParquetSink writes a telemetry lake: Genblaze's own runs / steps / assets
tables, plus a `qa` table of ours. DuckDB queries it live. The dedup ratio on the
dashboard, the per-model latency, the retry evidence behind each quarantine, all of that
is a query against real run data rather than a number we typed in.

Here is what the retry evidence looks like when the gate fires. A real 3-word narration
lost one word in transcription, scored WER 0.333, retried on two different real Gemini
voices, and was quarantined when none of them cleared threshold. A different case
escalated from WER 1.0 to 0.0 on voice-strong and shipped. There is also a clean 0.0
exact match. Those rows sit in the lake and the dashboard reads them from there.

fallback_models=[...] chains are wired into the providers, and a chaos toggle
(POST /api/chaos/{model}/disable, with a real panel in the dashboard) lets you knock a
model out yourself and watch the chain recover on screen.

One Genblaze detail worth passing on: ObjectStorageSink is single-use, because its
close() fires in a finally block when a run finishes, so it has to be built fresh per
run. A bare ParquetSink is not. We reused a sink and it cost us an afternoon.
```

---

## Pre-submission check on the live site — 2026-08-03, run against production

Read-only pass over `https://polyglo.onrender.com/`. Credentials are healthy:
`/api/status` returns `{"banner":"All providers configured.","has_b2":true,`
`"has_nvidia":true,"has_image_generation":true,"has_gemini":true,"mock_mode":false}`
(`has_audio_generation:false` is the expected NVIDIA-TTS flag, not a problem).

Three things a judge would see that undercut the strongest claims:

1. **The dashboard currently shows almost no evidence.** It reads 2 references, 2 files
   stored, **0.0% storage saved**, and the only two models listed are `voice-a` and
   `mock-image`. The Parquet telemetry lake lives on Render's local disk and is **not**
   snapshotted to B2 the way the SQLite index is, so every redeploy or restart wipes the
   dashboard's evidence. This is the headline "B2 Storage & Data Orchestration" exhibit
   and it is currently blank.
2. **The showcase story is mostly `unverified`.** "The Mischievous Trickster"
   (B1, 3 scenes, 8 locales) shows **43.8% deduplicated** and 0 quarantined, but only
   en-US scenes 0 and 1 carry a real `pass` at WER 0.00 — the other 22 of 24 segments are
   `unverified`. The QA gate is the centrepiece, and the page mostly shows it not having run.
3. **The story list reads like a scratchpad.** 13 stories, 11 named `Log Test`,
   `Fallback Sanity Check`, `Fallback Sanity Check 2`, `Audio Fix Verify`,
   `OpenRouter Real Test 2`, `Stepper Live Check`, `DB Backup Verification Story`,
   `Story`. Six of them have 0 scenes, so they're dead links from a judge's point of view.

**Highest-leverage fix, roughly 15 minutes:** do one clean real run on the live site with
a good title and a small shape — 3 scenes × 2 locales, so 6 segments, well inside the
20/day ASR quota — spaced out enough to respect the 3/min TTS cap. That single run
repopulates the dashboard with real model names, real latency and a real dedup
percentage, and gives the locale matrix real `pass` rows instead of a wall of
`unverified`. Do it *after* the last deploy of the day, since a restart clears telemetry
again. If there's time, also delete or rename the 0-scene test stories.

**One thing to confirm on Render while you're in there:** the B2 DB snapshot key is
env-scoped now — `db.py:432` returns `f"db-snapshot/{cfg.env}/polyglo.db"`, defaulting to
`dev`. That only protects production if Render actually sets **`POLYGLO_ENV=prod`**. If it
doesn't, both environments still write `db-snapshot/dev/polyglo.db` and a local pipeline
run would overwrite the index production restores on its next restart. Check the env var;
if it's missing, either set it or just don't run local pipeline tests after the clean run.
(`docs/PROGRESS.md` gotcha 0b still describes the old single fixed key — that entry is
stale and should be corrected.)
