# 03 — Build Plan

**Deadline: Monday 3 August 2026, 17:00 EDT** (= 21:00 UTC).
If you are in IST that is **02:30 on Tuesday 4 August** — you get all of Monday. Confirm
your own conversion before relying on it, and target **Monday 12:00 EDT** regardless so
a failed upload is not fatal.

Elapsed budget from Friday morning: **~3.5 working days.**

---

## Session 0 — Close the unknowns (first 90 minutes, before any feature code)

Everything downstream assumes these. Do not skip, do not do them in parallel with
building — the answers change what you build.

| # | Task | Why it is blocking |
|---|---|---|
| 0.1 | Create B2 bucket + application key | Nothing works without it |
| 0.2 | Get NVIDIA API key at `build.nvidia.com`, **request the 5,000-credit upgrade** | Approval is not instant — ask early |
| 0.3 | Run `examples/quickstart_local.py` | Proves Genblaze installs and manifests build, zero credits |
| 0.4 | Run `examples/quickstart.py` against your bucket | Proves B2 auth + upload + verify |
| 0.5 | **Measure credit cost of one image and one TTS call** | Sets your entire demo scope |
| 0.6 | **Spike both ASR paths** (NvidiaChatProvider audio input, and Gemini direct) | Decides the QA gate implementation |
| 0.7 | Confirm `.run()` return signature; write a thin wrapper | Stops an API surprise from rippling through every module |

**Write down the answer to 0.5 and 0.6 before continuing.** If ASR fails both ways, you
fall back to F10 (text gate only) and re-scope the demo around dedup + failover. Better to
know that on Friday morning than Sunday night.

---

## Friday — Foundation

| Block | Work |
|---|---|
| Morning | Session 0. Repo scaffold, config, `.env`, dependency install. |
| Midday | `store.py` — content-addressed B2 blob store, put/get/exists by SHA-256. Test with dummy bytes, **zero credits**. |
| Afternoon | `models.py`, SQLite schema, `telemetry.py` skeleton. Wire `ObjectStorageSink` with `ParquetSink`. |
| Evening | `authoring.py` — story → scenes → visual prompts. First real chat calls. Seed 1 story, 5 scenes. |

**Friday exit criterion:** a story exists in SQLite with scenes, and dummy blobs round-trip
through B2 by hash. No images, no audio yet.

---

## Saturday — The engine

| Block | Work |
|---|---|
| Morning | `visuals.py` — generate 5 scene images **once**. Confirm content-addressing dedups on a repeat run. |
| Midday | `localize.py` — fan out to 8 locales. Text gate (F10). |
| Afternoon | `narrate.py` — TTS per locale-scene. Fallback chain configured. |
| **Evening** | **`qa/` — the ASR round-trip gate.** Normalization, WER, thresholds, retry/escalate/quarantine. Calibrate thresholds on ~20 samples. |

**Saturday exit criterion — this is the decision point:**

> The QA gate must be **working end-to-end by Saturday night.** If it is not, cut it
> (see Cut Lines) and spend Sunday making dedup + failover + provenance excellent instead.
> Do not carry a broken gate into Sunday hoping to fix it.

---

## Sunday — Surface

| Block | Work |
|---|---|
| Morning | FastAPI routes + SSE progress. |
| Midday | UI: story view, locale matrix grid, QA status per cell, WER diff panel. |
| Afternoon | Dashboard (DuckDB queries), verify-on-upload widget, `/api/chaos` toggle. |
| Evening | **Seed the full demo dataset.** Deploy to HF Spaces. Click every path as a stranger would. |

**Sunday exit criterion:** a public URL a judge can open, with data already in it.

---

## Monday — Ship

| Block | Work |
|---|---|
| Early | Rehearse the demo 3× (free — the cache serves everything). Fix only what breaks on camera. |
| Morning | **Record the 3-minute video.** Upload public to YouTube. |
| Mid-morning | README: architecture diagram, **explicit "how we use B2 and Genblaze" section**, model/provider list. Make repo public. |
| **12:00 EDT** | **Submit.** |
| After | Buffer for anything broken. Do not add features. |

---

## Cut lines — decide in this order

Cut from the bottom up. Each line above is more valuable than everything below it.

```
KEEP ALWAYS  1. Content-addressed dedup + measurable saving
             2. Genblaze fallback chains (visible failover)
             3. Manifest verify-on-upload
             4. Parquet telemetry + dashboard
             ─────────────────────────────────────────────
CUT FIRST    5. ASR QA gate          ← cut if not working Saturday night
             6. Text gate (F10)      ← cheap, probably keep
             7. Locale count 8 → 4
             8. Scene count 5 → 3
             9. Review queue UI      ← a plain list is fine
            10. Any styling beyond legible
```

Lines 1–4 are the four judging criteria made concrete. They survive every cut.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **ASR path fails both ways** | Medium | High | Spike both Friday morning; `Transcriber` interface makes the swap one line; cut line 5 exists |
| **NVIDIA credits run out** | Medium | High | Measure cost first (0.5); cache everything; request 5,000 upgrade Friday; never regenerate in dev |
| **WER thresholds mis-calibrated** | High | Medium | Calibrate on 20 known-good samples Saturday; wrong thresholds either burn credits on retries or make the gate theatre |
| **TTS language coverage is thin** | Medium | Medium | Pick 8 well-supported locales; name the limitation in the demo rather than hiding it |
| **Genblaze API differs from docs** | Medium | Medium | Session 0.7 wrapper; the docs showed inconsistent `.run()` returns |
| **Deploy fails Sunday night** | Low | High | Deploy a hello-world to HF Spaces on **Friday**, not Sunday |
| **Time overrun** | High | High | Cut lines above; Monday is for shipping, not building |

---

## Discipline notes

- **Zero-credit development.** Build everything against `quickstart_local.py` and cached
  blobs. Only generate new media when you deliberately choose to.
- **Seed early, rehearse free.** Once the demo dataset exists, the content-addressed cache
  makes every rehearsal cost nothing. This is the single biggest practical advantage of
  the architecture — use it.
- **Deploy on Friday.** A hello-world on the real host, on day one, converts your largest
  Sunday-night risk into a Friday non-event.
- **The README is scored.** "Explanation of B2 and Genblaze integration" and a "clearly
  defined list of AI providers and models used" are explicit submission requirements.
  Budget 45 real minutes for it, not five.
