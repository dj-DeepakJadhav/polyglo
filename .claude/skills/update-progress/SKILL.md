---
name: update-progress
description: Update docs/PROGRESS.md (and SESSION-LOG.md) to reflect what actually changed, so a fresh session picks up accurate context. Use after landing any substantive change - a feature, a bug fix, a config/provider change, a live-verification result, or a production incident. Also use when the user says "update progress", "update the docs", or asks for the project state to be written down.
---

# Update PROGRESS.md

`docs/PROGRESS.md` is the fast-context entry point every new session reads first.
It has gone stale in non-trivial ways before and caused real wasted work (a whole
session believed deployment was blocked on an account that was already live). Keeping
it current is not bookkeeping — it is the thing that makes the next session correct.

## When to run this

After anything a future session would be wrong to not know:
- a feature shipped, a bug fixed, a provider/config default changed
- a live verification produced a real result (good or bad)
- a production incident happened and was diagnosed
- a real constraint was discovered (a rate limit, a dead model, an API quirk)

Skip it for pure refactors, comment/typo fixes, or work that changed nothing about
how the system behaves or what's still open.

## What to do

**1. Establish real current state — don't work from memory.**

```bash
git log --oneline -15
git status --short
```

Run the test suite and use the real number, never an assumed one:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/Scripts/python.exe -m pytest -p no:langsmith
```

(On this machine the project venv is required — system Python can't even collect the
suite. `-p no:langsmith` avoids an unrelated broken plugin import.)

**2. Update `docs/PROGRESS.md`.** Touch only what actually changed:

- **The snapshot line at the top** — date and a one-line summary of this batch of work.
- **The status block** — add a `DONE (this session):` entry for what landed. State what
  was actually verified and how (live run, Docker, real API call), not just "added X".
  Move anything that's now finished out of `OPEN:`/`REMAINING:`.
- **"Hard-won gotchas"** — add an entry for any real constraint discovered (a rate
  limit, a dead model slug, a surprising API behavior). Include the *symptom* someone
  would actually see, so the next person recognizes it. This section is the highest-value
  part of the file.
- **"Remaining work"** — strike through what's done, add anything newly discovered.
- **"Key files"** table — add genuinely new modules.

**3. Append to `docs/SESSION-LOG.md`** (append-only, chronological) for anything with a
real narrative: what the symptom was, how it was diagnosed, what the root cause turned
out to be, what was verified. PROGRESS.md is the summary; SESSION-LOG.md is the detail.

**4. Update `README.md`** only if user-facing behavior, limitations, or the provider
table changed.

## Rules that matter

- **Real numbers only.** Test counts, latencies, memory figures, WER scores — read them
  from an actual run or real telemetry. Never estimate or carry forward a stale figure.
- **Be honest about what wasn't verified.** "Not yet verified in Docker" is far more
  useful to the next session than silence implying it was.
- **Corrections beat additions.** If something in the file is now wrong, fix it rather
  than appending a contradiction. A confidently wrong PROGRESS.md is worse than a thin one.
- **Commit the doc update with the code change**, not as a separate later commit — that's
  what keeps them from drifting apart.
