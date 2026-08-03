# 04 — Provider & Budget Strategy

**Target out-of-pocket cost: $0.00.**

---

## 1. The free stack

| Need | Provider | Cost | Status |
|---|---|---|---|
| Storage | **Backblaze B2** | Free to 10 GB | `[VERIFIED]` real upload confirmed, task #16/#21 |
| Chat / translation | **NVIDIA NIM** — `meta/llama-3.1-8b-instruct` | Free credits | `[VERIFIED]` live |
| Image generation | **NVIDIA NIM** — `black-forest-labs/flux.1-dev` | Free credits | `[VERIFIED]` live, task #22 follow-up |
| TTS | **NVIDIA NIM** — Riva / Magpie TTS | Free credits | `[CONFIRMED BROKEN]` all bundled + plausible current slugs 404, task #22 |
| ASR (QA gate) | Gemini API (`gemini-2.5-flash`) | Free tier | `[VERIFIED]` WER 0.0 on a real round trip |

**NVIDIA NIM free tier:** ~1,000 inference credits on signup, **up to 5,000 on request**,
**no credit card**, no expiry, 40 requests/minute. One key covers all four modalities via
`genblaze-nvidia`:

```python
from genblaze_nvidia import (
    NvidiaImageProvider,   # SD 3.5, SDXL, FLUX
    NvidiaAudioProvider,   # Riva TTS + Fugatto music/SFX
    NvidiaChatProvider,    # Nemotron/Llama/Mistral/Qwen — accepts image, audio, video input
    chat, achat,           # OpenAI-compatible convenience functions
)
```

That `NvidiaChatProvider` accepts **audio input** is what makes ASR-inside-Genblaze
plausible. Confirm it in Session 0.6.

### `[VERIFY]` — the one number that sets your scope

NVIDIA does not publish a clean credits-per-generation table. **1,000 credits could be
1,000 images or 100.** Measure it in your first hour:

```
1. Check credit balance
2. One NvidiaImageProvider call
3. One NvidiaAudioProvider call
4. Check balance again
```

Then size the demo: `locales × scenes × (1 TTS + expected_retries)` plus `scenes × 1 image`.
At 8 locales × 5 scenes that is 40 TTS calls + 5 images + retries. Comfortable on 1,000
credits unless per-call cost is unexpectedly high.

---

## 2. Where Gemini fits — read this carefully

You said you have **Gemini Pro**. There is a distinction that matters:

| Product | What it is | API access? |
|---|---|---|
| **Gemini Pro / Google One AI Premium** | Consumer chat subscription (web + app) | ❌ **No** |
| **Google AI Studio / Gemini API** | Developer API, separate product | ✅ Yes, has its own free tier |

**Your subscription does not give you API keys.** But both are useful, in different ways.

### 2a. Gemini API (free tier) — as the ASR verifier

This is the genuinely valuable technical use. Gemini has **native audio understanding** —
feed it an audio file and ask for a transcript. That makes it a strong candidate for the
QA gate's `Transcriber`.

Note that `genblaze-google` ships **only `VeoProvider` (video) and `ImagenProvider`
(image)** — no chat, no TTS, no transcription. `[VERIFIED]` So Gemini cannot be your ASR
*through Genblaze*; call `google-genai` directly.

**This costs you nothing on the judging criteria.** The QA gate is a *validation* stage,
not a generation stage. Every generation step still runs through Genblaze, which is what
"Use of Genblaze" measures. Architecturally it is also cleaner: your verifier should be a
different model family from your generator, or you are grading homework with the same
model that wrote it.

```python
# polyglo/qa/gemini_transcriber.py
from google import genai

class GeminiTranscriber:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)

    def transcribe(self, audio_path: str, locale: str) -> str:
        audio = self.client.files.upload(file=audio_path)
        resp = self.client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                audio,
                f"Transcribe this {locale} audio verbatim. "
                f"Output only the transcript, no commentary, no translation.",
            ],
        )
        return resp.text.strip()
```

`[VERIFY]` free-tier rate limits in the AI Studio dashboard — Google's public docs point
you there rather than publishing numbers, so check your own account.

**Recommendation:** spike this alongside the NVIDIA path Friday morning. Gemini is likely
the better transcriber; NVIDIA keeps everything on one key. Pick on evidence.

### 2b. Gemini Pro subscription — as your unpaid content team

The subscription cannot serve traffic, but it can do a lot of work that would otherwise
burn API credits or your own hours:

| Use | Why it helps |
|---|---|
| **Author the seed stories** | Generate CEFR-graded source stories in the chat UI, paste into seed data. Zero API credits, better quality than a cheap NIM model. |
| **Produce golden reference translations** | Generate known-good translations for your 8 locales. Use them as **test fixtures** to calibrate WER thresholds — you need ground truth for Session Saturday-evening, and this is where it comes from. |
| **Calibrate the QA gate** | Ask it to produce deliberately *flawed* translations (dropped clause, wrong-language leakage) as negative test cases. Proves your gate actually fires. |
| **Write the Devpost copy and README** | Both are scored deliverables. |
| **Draft and tighten the demo script** | 3 minutes is brutally short; iterate the script in chat. |
| **Long-context code review** | Paste modules, ask for failure modes before you hit them at 2am. |

The negative-test-case idea is worth calling out: a QA gate you cannot demonstrate
*failing* is unconvincing. Manufacturing realistic failures is exactly what a strong chat
model is good at, and it costs you nothing.

---

## 3. Credit conservation rules

1. **Request the 5,000-credit upgrade on Friday morning.** Free, but not instant.
2. **Develop against `quickstart_local.py`** — manifest build and verify with zero API
   calls. The blob store, telemetry, bundles, UI, and dashboard can all be built without
   generating a single asset.
3. **Never regenerate.** Seed ~30 assets once; the content-addressed cache serves every
   subsequent run. Your product feature is also your budget control.
4. **Text gate before TTS.** Rejecting a bad translation costs a fraction of a chat call;
   discovering it after narration costs a TTS call plus an ASR call plus a retry.
5. **Cap retries at 3.** Without a cap, a systematically failing locale will drain the
   budget overnight.
6. **Use two *free* models for the failover demo.** The Genblaze fallback chain is just as
   impressive between two NIM models as between two paid vendors.

---

## 4. Fallback plan if credits run dry

In priority order:

1. Cached assets keep the deployed app fully functional for judges — nothing breaks.
2. Reduce scope: 8 locales → 4, 5 scenes → 3.
3. Gemini API free tier can cover chat and possibly image generation.
4. Hugging Face Inference free tier for TTS `[VERIFY]` — no Genblaze adapter, would need
   a direct call, so treat as emergency only.
5. Worst case, ~$5 on Replicate (FLUX Schnell at $0.003/image) restores image generation
   entirely. Keep this in your back pocket; do not plan around it.

---

## 5. Providers/models list — required for submission

Final, as-shipped table (`orchestrator.make_providers()` is the single source of
truth for every model name below — pull from there again if this ever drifts).

| Stage | Provider | Model | Via | Status |
|---|---|---|---|---|
| Scene splitting | NVIDIA NIM | `meta/llama-3.1-8b-instruct` | `genblaze-nvidia` | `[VERIFIED]` live |
| Translation | NVIDIA NIM | `meta/llama-3.1-8b-instruct` | `genblaze-nvidia` | `[VERIFIED]` live |
| Image generation | NVIDIA NIM | `black-forest-labs/flux.1-dev` | `genblaze-nvidia` | `[VERIFIED]` live, real image confirmed |
| Image fallback | NVIDIA NIM | `black-forest-labs/flux.1-schnell` | `genblaze-nvidia` | `[CONFIRMED BROKEN]` transport timeout |
| TTS (narration) | NVIDIA NIM | Magpie TTS (Riva) | `genblaze-nvidia` | `[CONFIRMED OUT OF SCOPE]` self-hosted NIM container only — see below |
| ASR (QA gate) | Gemini API | `gemini-2.5-flash` | direct (`google-genai`) | `[VERIFIED]` WER 0.0 on a real round trip |
| Storage | Backblaze B2 | — | `genblaze-s3` (S3-compatible, `boto3`) | `[VERIFIED]` real upload confirmed |

**Why TTS is out of scope, not just "broken":** NVIDIA's own NIM-for-Speech
documentation (`docs.nvidia.com/nim/speech/latest/reference/api-references/tts/http-tts.html`)
shows Magpie TTS is a self-hosted microservice — you deploy the container yourself
onto your own GPU via NGC (`http://<address>:9000`), with a call shape
(`POST /v1/audio/synthesize`, `voice="Magpie-Multilingual.EN-US.Aria"`) entirely
different from the hosted `ai.api.nvidia.com/v1/genai/{model}` endpoint that image
and chat use. Every model slug tried against that hosted endpoint this session
404'd because no hosted endpoint for this model family exists there — not a stale
registry, not a wrong slug, a genuine infrastructure requirement (a GPU to run the
container on) outside this project's free-tier, no-infrastructure scope. Worth
saying plainly in the submission write-up rather than glossing over it — judges
trust builders who name their own gaps precisely (see `05-SUBMISSION-KIT.md`).
