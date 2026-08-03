# 05 — Submission Kit

**Submit by Monday 3 August 2026, 17:00 EDT. Target 12:00 EDT.**

The submission itself is scored. A strong build with a weak write-up loses to an equal
build with a strong one, because judges read the description before they open the app.

---

## 1. Requirements checklist

Straight from the hackathon rules — every line is mandatory.

- [x] **Functional, publicly accessible app** — https://polyglo.onrender.com/
- [x] **GitHub repository** — https://github.com/dj-DeepakJadhav/polyglo
- [ ] **Demo video** — ~3 minutes, public (YouTube/Vimeo) — *Pending recording*
- [x] **Text description** — features + how B2 and Genblaze are used (ready in [`07-DEVPOST-COPY.md`](07-DEVPOST-COPY.md))
- [x] **Clearly defined list of AI providers and models used** — see [`04 §5`](04-PROVIDER-STRATEGY.md)
- [x] **Explanation of B2 integration** — explicitly scored
- [x] **Explanation of Genblaze integration** — explicitly scored
- [ ] Devpost submission form completed

**Test the public URL in an incognito window from a different network.** "Works on my
machine" has ended more hackathon runs than bad code.

---

## 2. README structure

The repo README is doing double duty as documentation and as evidence. Write it in this
order — judges skim top-down and may never reach the bottom.

```markdown
# Polyglo
> One-line pitch: localization QA is a human bottleneck. We made it a pipeline stage.

[Live demo](url) · [3-min video](url)

## What it does
Three sentences. Problem, mechanism, outcome.

## Architecture
The mermaid diagram from docs/02. One picture beats three paragraphs.

## How we use Backblaze B2          ← SCORED. Be specific and concrete.
- Content-addressed blob store (KeyStrategy.CONTENT_ADDRESSABLE) — every asset keyed
  by SHA-256, so N locale bundles reference one image blob instead of copying it
- Genblaze manifests persisted per run
- Parquet telemetry lake (runs/steps/assets/qa) queried live with DuckDB over S3
- Per-locale bundles referencing blobs by hash
- Measured result: <N>% storage deduplication across <M> locales

## How we use Genblaze              ← SCORED. Name the actual APIs.
- Pipeline API for every generation stage (chat, image, audio)
- fallback_models=[...] chains — demonstrated live in the video at <timestamp>
- Canonical SHA-256 manifests; verify-on-upload in the UI
- ObjectStorageSink + S3StorageBackend.for_backblaze()
- ParquetSink for run/step/asset telemetry

## AI providers and models          ← REQUIRED
The table from docs/04 §5.

## The QA gate
Short technical explanation + the honest limitation (WER catches intelligibility,
not naturalness or cultural fit).

## Running locally
Actually test these steps on a clean clone.

## Limitations and what's next
Judges trust builders who name their own gaps.
```

**Concrete numbers beat adjectives.** "94% deduplication across 8 locales" scores; "efficient
storage" does not.

---

## 3. Demo video

3 minutes, public, English (subtitles if needed). Beat sheet from
[01 §6](01-PRODUCT-DESIGN.md#6-demo-script-3-minutes):

| Time | Beat |
|---|---|
| 0:00–0:20 | Problem: LQA means humans listening to every file |
| 0:20–0:40 | Input: one story, CEFR B1, 8 locales, run |
| 0:40–1:10 | Fan-out: images once, translation + narration per locale |
| **1:10–1:50** | **QA gate fails a segment → diff → retry on fallback voice → pass** |
| 1:50–2:10 | Chaos toggle: kill primary TTS, fallback chain completes the run |
| 2:10–2:30 | Dashboard: dedup ratio and cost, live from Parquet |
| 2:30–2:50 | Verify: re-upload audio, extract manifest, `verify()` → green |
| 2:50–3:00 | Close: what this replaces |

### Production notes

- **Rehearse at least three times.** Free — the cache serves everything already generated.
- **Show a real failure.** The single most persuasive moment is the QA gate rejecting bad
  audio. A gate that only ever passes proves nothing. Use a deliberately flawed sample
  (see [04 §2b](04-PROVIDER-STRATEGY.md) — generate negative cases with Gemini Pro).
- **Pre-seed everything.** Nobody wants to watch a 45-second TTS call. Have data loaded;
  generate at most one thing live.
- **Say the numbers out loud.** "Eight locales, one image generation, 94% deduplicated."
- **Name the limitation** in one sentence near the end. It reads as competence, not
  weakness, and it pre-empts the obvious critique.
- Record at 1080p, check audio levels, upload as **public** (not unlisted-only if the
  rules require public — verify the setting).

---

## 4. Devpost description template

```
## The problem
Producing learning content in 20 languages means 20× the QA. Localization QA is
still humans listening to audio files against error schemas — the same way it
worked a decade ago. It's the reason small teams ship two languages, not twenty.

## What Polyglo does
One source story fans out to N locales. Scene visuals are generated once and shared
across every locale by content hash. Narration is generated per locale — then
verified automatically: we transcribe the generated audio back with a different
model and diff it against the text that produced it. Word Error Rate above threshold
means the audio is wrong, and the pipeline retries on a fallback voice, escalates,
and finally quarantines the segment for a human.

## Why it's different
Cross-modal verification (TTS → ASR → diff) is an established method in speech
research. What's missing is anyone putting it inside a production content pipeline
as a blocking gate. That's what we built.

## How we use Backblaze B2
[specifics + the measured dedup number]

## How we use Genblaze
[specifics + named APIs]

## Honest limitations
WER catches intelligibility failures — mispronunciation, truncation, language drift.
It does not catch unnatural prosody or cultural inappropriateness. This shrinks the
human review queue; it doesn't eliminate it. Language coverage is bounded by TTS and
ASR support, which is materially worse for low-resource languages.
```

---

## 5. Failure modes that cost submissions

| Mistake | Prevention |
|---|---|
| Private repo, no access granted | Make it public Monday morning; verify while logged out |
| Video set to private | Check in an incognito window |
| App URL dead at judging time | Deploy Friday, not Sunday; use a host that doesn't cold-sleep |
| Missing provider/model list | Maintain [04 §5](04-PROVIDER-STRATEGY.md) as you build |
| Vague B2 / Genblaze explanation | Name specific APIs and give measured numbers |
| Submitting at 16:55 EDT | Target 12:00 EDT |
| Secrets committed to the repo | `.env` in `.gitignore` from commit one; scan before going public |

---

## 6. Final morning sequence

```
1. Rehearse demo ×3                       (free — cache serves everything)
2. Record and upload video, set PUBLIC
3. Finalize README, paste in real numbers
4. Scan repo for secrets, make public
5. Verify app URL in incognito, different network
6. Verify repo + video URLs while logged out
7. Complete Devpost form
8. Submit — target 12:00 EDT
9. Stop. Do not add features.
```
