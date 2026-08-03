# Polyglo — Documentation Index

**Comprehensible-input content factory.** One source story → N locales → verified narration + shared visuals → per-locale bundles on Backblaze B2.

Built for the [Backblaze Generative Media Hackathon](https://backblaze-generative-media.devpost.com/).
**Deadline: Monday 3 August 2026, 17:00 EDT.**

---

## The one-sentence pitch

> Localization quality assurance currently means humans listening to every audio file. We made it a pipeline stage.

## The four judging criteria, and where we win each

| Criterion | Our answer |
|---|---|
| Real-World Utility | Localization is a ~$65B market; LQA is still a manual cost centre |
| Production Readiness | Automated cross-modal QA gate: TTS → ASR → diff → retry → escalate → quarantine |
| B2 Storage & Data Orchestration | Locale-matrix fan-out with content-addressed dedup and a provable saving ratio |
| Use of Genblaze | Chat + image + audio, fan-out, fallback chains, manifests, ParquetSink |

---

## Documents

| Doc | Read it for |
|---|---|
| **[PROGRESS](PROGRESS.md)** | **Start here in a new session** — current status, blockers, hard-won gotchas |
| [01 — Product Design](01-PRODUCT-DESIGN.md) | Problem, users, competitive position, feature set, demo script |
| [02 — Technical Architecture](02-TECHNICAL-ARCHITECTURE.md) | System design, data model, pipeline stages, B2 layout, QA gate spec |
| [03 — Build Plan](03-BUILD-PLAN.md) | Hour-by-hour schedule, cut lines, risk register |
| [04 — Provider & Budget Strategy](04-PROVIDER-STRATEGY.md) | Zero-cost provider matrix, credit conservation, where Gemini Pro fits |
| [05 — Submission Kit](05-SUBMISSION-KIT.md) | Devpost checklist, demo video script, README requirements |
| [06 — Genblaze API Notes](06-GENBLAZE-API-NOTES.md) | Verified API surface, corrections to the README's published examples |
| [07 — Devpost Copy](07-DEVPOST-COPY.md) | Ready-to-paste submission text, filled in with real measured numbers |
| [SESSION-LOG](SESSION-LOG.md) | Full chronological build log — every bug found and fixed, in detail |

---

## Status conventions used in these docs

- **`[VERIFIED]`** — confirmed against official docs or source during research
- **`[VERIFY]`** — assumption that must be tested before it is relied on
- **`[CUT]`** — scope that gets dropped first if time runs short

Anything marked `[VERIFY]` is a genuine unknown, not a formality. Several of them
(NVIDIA credit cost per generation, ASR path viability) determine the shape of the
build and should be closed in the first session.
