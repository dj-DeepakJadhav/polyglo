# CLAUDE.md

Project-level guidance for Claude Code sessions working in this repository.

## Code exploration: use codebase-memory-mcp first

This repo is indexed into the `codebase-memory-mcp` knowledge graph (project name:
`C-DJ-Hackathon-Backblaze-Generative-Media-Hackathon-polyglo`). **Prefer its tools over
raw `Grep`/`Read` for any code-discovery task** — finding a definition, tracing a call
chain, understanding what calls what, or getting an overview of the architecture. It
returns precise, structural results (signatures, line ranges, call graphs) instead of
requiring you to read whole files into context, which is the token-usage difference
that matters across a long session.

Reach for it before grep/read when the task is *"where is X defined / what calls X /
how does X relate to Y / give me the shape of this codebase"* — not for editing files,
reading a file you already know you need in full, or non-code content (docs, configs).

| Need | Tool |
|---|---|
| Find a function/class/route by name or description | `search_graph` (BM25/pattern/semantic) |
| Follow a call chain (who calls this, what does this call) | `trace_path` |
| Get exact source for a known symbol | `get_code_snippet` |
| Text search but ranked/deduplicated by structure | `search_code` (graph-augmented grep) |
| Project structure, clusters, entry points, hotspots | `get_architecture` |
| Complex relationship queries | `query_graph` (Cypher) |

If the index looks stale after a large batch of edits, re-run `index_repository` with
`mode="fast"` (quick) or `"full"` (thorough, includes semantic edges) before relying on
search results. Still use `Grep`/`Read` freely for non-code files, exact string matches,
and configs — the graph doesn't replace those, it replaces "grep then read five files to
find one function."

The persisted graph artifact (`.codebase-memory/`) is gitignored — it's local dev
tooling, rebuildable via `index_repository`, not a project deliverable.

## What this project is

See `docs/README.md` for the full picture. Short version: Polyglo is a
comprehensible-input content factory built for the Backblaze Generative Media
Hackathon — one source story, generated once, fanned out to N locales, with narration
verified automatically via a cross-modal QA gate (TTS → ASR → WER diff → retry →
quarantine). `docs/SESSION-LOG.md` is the append-only build log; read its most recent
entries before assuming anything about current state, since several early assumptions
in `docs/01`–`docs/04` were revised during the actual build (see `docs/06` for the
Genblaze API corrections specifically).

## Known constraints, as of the last session-log entry

- NVIDIA image generation works (`flux.1-dev` is the correct primary model;
  `flux.1-schnell`, the original primary, is what's actually dead). NVIDIA audio/TTS
  generation is confirmed broken upstream (every bundled + plausible current-catalog
  slug 404s). Gated independently via `Config.has_image_generation` /
  `Config.has_audio_generation` (`nvidia_image_broken` defaults `false`,
  `nvidia_audio_broken` defaults `true`). Chat is confirmed working. Don't assume
  either modality's state without checking these flags and the session log — they
  are NOT the same flag.
- B2 credentials are live and verified (real upload confirmed working).
- Test fixtures that set `POLYGLO_DATA_DIR`/`POLYGLO_DB_PATH` via
  `monkeypatch.setenv` **must** call `polyglo.config.reset_config_cache()` right
  after and in teardown — `get_config()` is `@lru_cache`'d and several modules call it
  fresh internally, so skipping this silently writes test data into the real dev
  database (has happened, was cleaned up).
- `polyglo.api` and `polyglo.web` each import `make_providers` as their own separate
  name binding from `polyglo.orchestrator` — a fixture patching one does NOT patch the
  other. Any new test file with HTML/JSON routes must patch `make_providers` on every
  module that imports it, or it will silently make real network calls.
