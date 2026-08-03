# 01 — Product Design

## 1. The problem

Producing language-learning material in many languages has three costs that scale badly:

1. **Translation** — cheap and largely solved by LLMs.
2. **Narration** — cheap per unit with modern TTS, but *unverified*. A TTS model can
   mispronounce a loanword, silently truncate a clause, drift into the wrong language
   mid-sentence, or mangle a numeral. Nobody notices until a learner does.
3. **Quality assurance** — expensive, human, and the actual bottleneck.

That third cost is the one nobody has automated. Localization QA (LQA) vendors —
Testronic, TestPapas, Smartling — sell it as a **human service**: people listen to audio
files and grade them against error schemas. It is slow, it does not scale, and it is the
reason small teams ship two languages instead of twenty.

Meanwhile the *visual* half of the content is identical in every language, yet most
pipelines regenerate or re-store it per locale, multiplying cost for zero benefit.

## 2. What Polyglo does

A content factory for graded, comprehensible-input learning material.

```
source story ──> scenes ──> images (generated ONCE, shared by all locales)
                    │
                    └─────> translation ──> narration ──> [QA GATE] ──> locale bundle
                                                              │
                                                     fail ────┘ retry / escalate / quarantine
```

**The two ideas that make it more than a wrapper:**

### Idea 1 — The cross-modal QA gate

After generating narration, transcribe it back with a *different* model and diff the
transcript against the text that produced it. Word Error Rate above threshold means the
audio is wrong. Retry with a different voice, escalate to a stronger model, and after N
attempts quarantine the segment and flag it for a human.

This is a known evaluation technique in speech research — the metric has a name
(PCTS, percentage of completely correct transcribed sentences) and there is published
work on QA for speech synthesis using ASR. **The novelty is not the method; it is that
nobody has put it inside a content production pipeline as a blocking gate.**

That framing is deliberately honest and it is *stronger* than claiming invention: the
method is externally validated, so we only have to prove the engineering.

### Idea 2 — Locale-matrix dedup

Visuals are locale-independent. Generate the scene images once, address them by SHA-256,
and have all twenty locale bundles reference the same blobs. Storage cost stays flat as
locales scale; only audio grows.

This is measurable, and the measurement is the demo: *"20 locales, 1× image generation
cost, N% storage deduplicated"* — read live out of our own telemetry, not asserted.

## 3. Users

| User | Job to be done |
|---|---|
| **Primary — edtech content team** | Ship a course in 20 languages without a 20× QA bill |
| Independent language educator | Produce graded readers with narration they can trust |
| Comprehensible-input creator (YouTube/podcast) | High-volume multilingual episodes |
| Localization engineer | Automate the first pass so humans only see flagged segments |

The pitch is aimed at the **primary** user. The others are expansion, not the demo.

## 4. Competitive position

**Do not pitch this against dubbing tools.** HeyGen, Rask, ElevenLabs Dubbing, and
Papercup all localize *existing video*. That is a different job from generating original
graded material across a locale matrix, and we lose that comparison on polish.

**Pitch against human LQA.** Smartling, Testronic, and TestPapas sell manual listening
against error schemas. That is the cost we remove.

| | Them | Us |
|---|---|---|
| AI dubbing tools | Localize existing video, no QA gate | Generate original graded content, QA gate blocks bad output |
| LQA vendors | Human listeners, days of turnaround | Automated first pass, humans only see quarantined segments |
| DIY scripts | No provenance, no dedup, no retry | Manifests, content-addressed storage, fallback chains |

**Honest weaknesses to acknowledge rather than hide:**

- Multilingual TTS is commoditized. The models are not our contribution.
- WER-based QA catches intelligibility failures, **not** naturalness, prosody, or
  cultural appropriateness. It reduces human review; it does not eliminate it. Say this
  out loud in the demo — overclaiming here is the fastest way to lose credibility with a
  judge who knows the field.
- Language coverage is bounded by TTS and ASR model support, which is much worse for
  low-resource languages. Demo with well-supported languages and name the limitation.

## 5. Feature set

### Must have (the submission is these)

- **F1 — Story authoring.** Source text in, CEFR level target, split into scenes.
- **F2 — Scene visuals.** One image per scene, generated once, content-addressed.
- **F3 — Locale fan-out.** Translate scenes into N target locales, CEFR-preserving.
- **F4 — Narration.** TTS per scene per locale.
- **F5 — QA gate.** ASR round-trip, WER scoring, retry → escalate → quarantine.
- **F6 — Bundles.** Per-locale package written to B2, referencing shared image blobs.
- **F7 — Provenance.** Genblaze manifest per run, hash-verified, `verify()` in the UI.
- **F8 — Telemetry.** Parquet run/step/asset/QA tables on B2, queried with DuckDB.
- **F9 — Dedup dashboard.** Live storage-saved and cost-saved figures from real data.

### Should have

- **F10 — Text validation gate.** Language ID on translated text (catch untranslated
  leakage and wrong-language drift) before spending TTS credits. Cheap, high value.
- **F11 — Failover demo.** Deliberately break the primary TTS model, show the Genblaze
  fallback chain complete the run, show it recorded in the manifest.

### Could have `[CUT]`

- **F12** — Human review queue UI for quarantined segments (a list is enough)
- **F13** — Per-character voice consistency across locales
- **F14** — Export to Anki / SCORM
- **F15** — Video assembly (**explicitly out of scope** — cost and time)

## 6. Demo script (3 minutes)

The video is a scored deliverable. Structure it as problem → system → proof.

| Time | Beat | What is on screen |
|---|---|---|
| 0:00–0:20 | **Problem** | "Localization QA means humans listening to every file." State the cost. |
| 0:20–0:40 | **Input** | One story, CEFR B1, pick 8 locales. Hit run. |
| 0:40–1:10 | **Fan-out** | Live progress: scenes → images once → translation → narration per locale. |
| 1:10–1:50 | **The QA gate** | A segment **fails** WER. Show the diff. Show the retry on a fallback voice. Show it pass. *This is the most important 40 seconds of the video.* |
| 1:50–2:10 | **Failover** | Kill the primary TTS provider. Fallback chain completes the run. Manifest records it. |
| 2:10–2:30 | **Dedup** | Dashboard: 8 locales, 1× image cost, N% deduplicated. Real numbers from Parquet. |
| 2:30–2:50 | **Provenance** | Download a bundle, re-upload the audio, extract the embedded manifest, `verify()` → green. |
| 2:50–3:00 | **Close** | One line on what this replaces. |

**Rehearsal note:** every rehearsal after the first costs zero API credits, because the
content-addressed cache serves everything already generated. Seed the demo data early and
rehearse as often as you like.

## 7. Explicit non-goals

- Not a dubbing tool. No lip sync, no video.
- Not a translation engine. We orchestrate models; we do not train them.
- Not a replacement for human review. It is a first-pass filter that shrinks the queue.
- Not a consumer learning app. The learner-facing product is out of scope; this is the
  factory behind it.
