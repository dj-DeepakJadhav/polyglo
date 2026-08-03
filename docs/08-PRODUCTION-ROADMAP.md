# 08 — Production readiness roadmap

Honest assessment, written after a hackathon-polish session (2026-08-02) that added
real, deployed protections (rate limiting, daily spend caps, structured logging, CI).
This doc is the "what's next if this became a real product" list — the items below
are real gaps, not filler, and none of them were safe or scoped-right to attempt in
the hours before a submission deadline. Read `docs/PROGRESS.md` first for what's
already shipped; this is specifically the *not yet done* list.

---

## Done this session (see SESSION-LOG.md for full detail)

- Per-IP rate limiting on story creation and chaos toggling (`polyglo/ratelimit.py`)
- A global daily story-creation cap, independent of per-IP limits (`polyglo/api.py`)
- A dedicated OpenRouter daily call budget, shared between narration and image
  generation (`polyglo/qa/budget.py`'s `DailyCallBudget`)
- Basic structured logging (`polyglo/logging_config.py`)
- GitHub Actions CI running the full suite on every push (`.github/workflows/tests.yml`)
- Real image download (`?download=` on the existing blob route) and a real
  narrated-video export (`polyglo/video.py`, ffmpeg via `imageio-ffmpeg`)

These close the most urgent gap (a public instance with real credentials and no
abuse protection at all) and the cheapest wins (CI, logging). Everything below is
either a genuinely large lift, an out-of-scope decision only the user can make, or
both.

**Docker-verified, not just tested locally**: rebuilt the image (`docker build
--no-cache`), confirmed `imageio-ffmpeg` resolves a real Linux ffmpeg binary inside
the container (no Dockerfile change needed — same `pip install` path as every other
dependency), created a real story with real credentials inside the container, and
downloaded a real, valid MP4 from the video-export route running in the container
itself. Also hammered the real chaos-toggle endpoint inside the container past 30
requests and confirmed the rate limiter actually returns 429 under real load, not
just in a mocked test.

---

## 1. Data layer: SQLite won't scale past one instance

**What's true today:** SQLite is a single file, and only one process can safely
write to it at a time. `polyglo/db.py`'s own docstring already accepts this
("single-process... design — correct for a hackathon demo, not meant to scale past
it"). Render's redeploy-resets-local-disk behavior is already handled (the index is
backed up to B2 and restored on boot — see `db.restore_db_from_b2`), but that's a
*durability* fix, not a *concurrency* one.

**What breaks in production:** the moment you run more than one instance (for
uptime or load), each instance has its own SQLite file — story A created on
instance 1 is invisible to a request that lands on instance 2. Render's autoscaling
or even just a rolling redeploy with two instances briefly up would cause real,
visible data inconsistency.

**Real next step:** migrate to a real multi-writer database (Postgres is the
obvious choice — Render has a managed Postgres offering). This is a genuine
migration: `polyglo/db.py`'s raw SQL would need a real ORM or query-builder layer
(or careful DB-agnostic SQL), and every test fixture using `dbm.connect()` against
a temp SQLite file needs an equivalent Postgres-testcontainer or SQLite-compatible
fallback story. Not a same-day change; budget a dedicated session for it.

## 2. No authentication or multi-tenancy

**What's true today:** anyone who reaches the URL can create stories, see every
other story ever created (the homepage lists them all), toggle the chaos demo for
everyone, and consume the shared budgets this session just added.

**What breaks in production:** this is fine for a single-demo hackathon URL, not
for multiple real users. There's no concept of "your stories" vs "someone else's."

**Real next step:** the smallest real fix is a shared-secret gate (one app-wide
password/API key checked via a dependency, similar to how rate limiting is wired)
if this only ever needs to be "not fully open to the internet." A *real* multi-user
product needs actual accounts (sessions, a users table, `story.owner_id`,
authorization checks on every route) — a meaningfully sized feature, not a patch.

## 3. Synchronous, in-process background jobs

**What's true today:** `BackgroundTasks` runs the pipeline in a thread from
FastAPI's own threadpool, and progress lives in an in-memory dict
(`polyglo/api.py`'s `_progress`). This is explicitly documented as "single-process...
not meant to scale past it."

**What breaks in production:** progress state disappears on every restart/redeploy
(a user watching a story generate loses that view if Render redeploys mid-run,
though the story itself still completes and persists). At real concurrency, many
simultaneous story generations compete for the same limited threadpool, and there's
no back-pressure beyond the caps added this session.

**Real next step:** a real task queue (Celery+Redis, or a lighter option like Arq/
RQ) decouples "accept the request" from "do the work," survives restarts, and gives
real retry/backoff semantics. This also naturally solves the video-export feature's
current constraint (synchronous encode, capped at 20 scenes to avoid a timeout) —
a queued job has no such ceiling.

## 4. Legal / privacy — nothing exists today

**What's true today:** users submit real story text, which flows through NVIDIA,
Gemini, and (optionally) OpenRouter — three separate third-party AI vendors, each
with their own data-retention and training-use policies. There is no privacy
policy, no terms of service, and no stated data-retention policy anywhere in the
app or its docs.

**What breaks in production:** this is a real legal exposure the moment this is a
product real people use, not just a judged demo. Even a hackathon submission
arguably benefits from a one-paragraph honest disclosure ("your story text is sent
to NVIDIA/Google/OpenRouter for processing; we don't otherwise store or share it").

**Real next step:** at minimum, a plain-language privacy note in the README/app
footer before any real users touch it beyond judges. A real product needs actual
counsel-reviewed ToS/privacy policy, especially given COPPA-adjacent concerns if
this is ever marketed at language learners who might be minors.

## 5. Monitoring, alerting, and uptime visibility

**What's true today:** `GET /api/status` exists and is a reasonable health check,
but nothing polls it. There's no uptime monitoring, no alert if the app goes down,
no alert if a budget/rate-limit cap is being hit repeatedly (which the logging
added this session makes *visible* in logs, but nobody is watching those logs
proactively).

**Real next step:** a free-tier uptime monitor (UptimeRobot, Better Uptime, or
Render's own health-check + notification feature) hitting `/api/status` on a
schedule. For budget exhaustion specifically, the logged warnings this session
added are a real foundation — piping them to something that pages a human (even
just an email digest) is the next step, not a rebuild.

## 6. Cost visibility beyond call counts

**What's true today:** `GeminiBudget`/OpenRouter's `DailyCallBudget` cap *call
counts*, not dollars — this was a deliberate, user-requested design (see
`qa/budget.py`'s docstring), and it's a reasonable proxy. But NVIDIA's own
telemetry doesn't populate real `cost_usd` per call (a confirmed, documented
architectural limit of what genblaze's NVIDIA provider surfaces — see
`docs/SESSION-LOG.md`), so there's no real per-request dollar figure available
anywhere in the app today.

**Real next step:** NVIDIA and OpenRouter both have their own billing dashboards —
real cost tracking would mean either polling their billing APIs (if available) or
accepting call-count as the only practical proxy and documenting that limitation
plainly (which this doc now does).

---

## A real v2 feature idea, not a gap: AI-animated scene video

Raised directly by the user during this session (comparing to ByteDance's
Seedance): instead of (or alongside) the current ffmpeg slideshow-of-real-assets
video export, generate short animated video clips per scene via a real video-
generation model. Deliberately not attempted this session — see the chat
conversation for the full reasoning, in short:

- It generates *new*, unverified content, breaking this app's core "generate once,
  verify, then reuse" invariant — there's no QA gate for a generated video clip.
- Real video-gen models are slow (tens of seconds to minutes per clip) and
  meaningfully more expensive per call than image generation — a real reliability
  and cost risk to introduce without a proper spike.
- These models don't take the app's real, verified narration audio as sync input —
  you'd either lose the narration entirely or need a separate, nontrivial lip-sync
  feature.

Worth a real spike post-submission if this becomes an ongoing project: prototype
against one scene, measure real latency/cost, and decide whether it's a "video
export upgrade" or a distinct, separately-QA'd feature.

---

## Priority, if picking up this list later

Roughly in order of "real user impact per hour of work":

1. Legal/privacy one-paragraph disclosure (cheap, real exposure reduction)
2. Uptime monitoring (cheap, closes a real blind spot)
3. Auth (shared-secret version first, real accounts later) — directly limits the
   abuse surface the rate limiting/budgets added this session only partially cover
4. Task queue migration — unlocks removing the video-export scene cap and fixes
   progress-state-lost-on-redeploy
5. Postgres migration — the biggest lift, but only actually blocking once you need
   more than one instance
