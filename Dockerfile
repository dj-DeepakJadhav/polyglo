# Targets Render (Docker web service) as of this session — HF Spaces' free tier
# stopped supporting the Docker SDK (Docker/Gradio now require a paid PRO plan,
# discovered when actually trying to create the Space; only Static Spaces, which
# can't run this app's Python backend, stay free). CMD below reads $PORT with a
# 7860 fallback so the same image still works unmodified on HF Spaces (which
# expects 7860 specifically) or any other platform that injects its own $PORT
# (Render, Cloud Run, etc.) — no per-host Dockerfile fork needed.
#
# python:3.12-slim rather than the 3.14 used in local dev (confirmed to work via
# task #1) — Docker images run manylinux wheels, and 3.12 has broad, proven wheel
# coverage for every dependency here (duckdb, pyarrow, pydantic-core, pillow). 3.14
# is bleeding-edge enough that a slow/missing Linux wheel for any one dependency
# would force a source build mid-`docker build`, which is exactly the kind of
# surprise you don't want discovering for the first time during a Monday-morning
# deploy. requires-python in pyproject.toml is ">=3.11", so 3.12 satisfies it.
FROM python:3.12-slim

WORKDIR /app

# --no-install-recommends keeps the image lean; ca-certificates is required for
# HTTPS calls to NVIDIA/Gemini/B2's S3-compatible endpoint.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifests first so Docker's layer cache is reused across builds
# that only change application code, not dependencies — meaningfully faster
# rebuild-and-redeploy cycles right before a demo.
COPY pyproject.toml README.md ./
COPY polyglo/ ./polyglo/

# Installs from pyproject.toml, including the package-data fix that bundles
# templates/static into the install (see pyproject.toml's own comment on this —
# it was a real, caught-before-shipping gap: setuptools does not include non-.py
# files by default).
RUN pip install --no-cache-dir .

# Runtime data directory. Genuinely ephemeral in a container — SQLite is a
# rebuildable cache (see db.py's own module docstring) and B2 is the durable
# store, so losing this on a container restart is expected and fine, not a bug.
RUN mkdir -p /app/data
ENV POLYGLO_DATA_DIR=/app/data
ENV POLYGLO_DB_PATH=/app/data/polyglo.db

# Real secrets (B2_KEY_ID, B2_APP_KEY, NVIDIA_API_KEY, GEMINI_API_KEY, etc.) are
# supplied at runtime via the platform's own secrets mechanism (HF Spaces:
# Settings → Repository secrets) — never baked into this image. Config.py already
# degrades gracefully to mock/simulated providers when they're absent, so the
# container is fully functional with zero secrets configured, exactly like local
# dev (see docs/02 §11 and Config.banner()).

EXPOSE 7860

# polyglo.web:app, not polyglo.api:app — web.py imports api.py's `app` and adds
# the HTML routes to the SAME instance, so targeting web gets both route sets.
# Single worker: this app's story-creation background tasks and in-memory
# progress log are process-local (see api.py's own module docstring on that
# design boundary) — multiple workers would each see a different, incomplete
# progress log for the same story.
#
# Shell form (not exec-form JSON array) specifically so ${PORT:-7860} actually
# expands — the exec form never invokes a shell, so env-var substitution
# silently doesn't happen and every deploy would get the literal string
# "${PORT:-7860}" as its port argument.
CMD uvicorn polyglo.web:app --host 0.0.0.0 --port ${PORT:-7860}
