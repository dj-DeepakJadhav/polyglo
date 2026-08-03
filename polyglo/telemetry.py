"""Telemetry: our own QA table, plus DuckDB queries over Genblaze's Parquet lake.

Genblaze's ``ParquetSink`` already writes ``runs`` / ``steps`` / ``assets`` tables,
hive-partitioned as ``<table>/dt=<date>/tenant_id=<id>/modality=<m>/provider=<p>/
<run_id>.parquet``. Confirmed by running a real (mock-provider) pipeline against a
local ``ParquetSink`` and reading the output schema directly — not guessed from docs,
which is worth stating because the Backblaze README examples were wrong about the
`.run()` return type in exactly the same way (see ``docs/06``).

Actual schemas, as produced:

    runs:   run_id, parent_run_id, tenant_id, project_id, name, status,
            step_count, canonical_hash, created_at
    steps:  run_id, step_id, provider, model, step_type, modality, status,
            prompt, seed, params_json, asset_count, retries, cost_usd,
            error, error_code, started_at, completed_at
    assets: run_id, step_id, asset_id, url, media_type, sha256, size_bytes,
            (+ nullable video/audio metadata columns)

Notably **no ``latency_ms`` column** on ``steps`` — only ``started_at``/``completed_at``.
Latency is computed in SQL from that pair rather than invented as a field genblaze
doesn't actually emit.

This module adds one table of our own — ``qa`` — for QA-gate outcomes, which genblaze
has no concept of. It follows the same one-file-per-write, hive-partitioned convention
so a single DuckDB glob can read every table uniformly, local or (later) over S3.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from polyglo.qa.gate import GateResult

__all__ = [
    "QAEvent",
    "TelemetryStore",
    "purge_fixture_telemetry",
    "restore_telemetry_from_b2",
    "seed_fixture_telemetry",
    "snapshot_telemetry_to_b2",
]

_QA_SCHEMA = pa.schema([
    ("story_id", pa.string()),
    ("locale", pa.string()),
    ("ordinal", pa.int64()),
    ("attempt", pa.int64()),
    ("voice_model", pa.string()),
    ("wer", pa.float64()),
    ("status", pa.string()),
    ("latency_ms", pa.int64()),
    ("ts", pa.string()),
])


@dataclass
class QAEvent:
    """One QA-gate attempt. One row per :class:`polyglo.qa.gate.Attempt`, not one
    row per gate run — the retry history is exactly what the dashboard needs to prove
    the gate is doing real work, and collapsing it to a single final row would erase it.
    """

    story_id: str
    locale: str
    ordinal: int
    attempt: int
    voice_model: str
    status: str
    wer: float | None = None
    latency_ms: int = 0
    ts: str | None = None

    def __post_init__(self) -> None:
        if self.ts is None:
            self.ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    @classmethod
    def from_gate_result(cls, story_id: str, ordinal: int, locale: str,
                         result: GateResult) -> list[QAEvent]:
        return [
            cls(
                story_id=story_id, locale=locale, ordinal=ordinal,
                attempt=a.n, voice_model=a.voice_model,
                status=a.status, wer=a.wer, latency_ms=a.latency_ms,
            )
            for a in result.attempts
        ]


class TelemetryStore:
    """Writes our ``qa`` table and queries all four tables with DuckDB.

    ``base_dir`` is a filesystem path today. The same glob-over-tables approach works
    against an ``s3://`` URI once DuckDB's httpfs extension is configured against B2 —
    flagged ``[VERIFY]`` pending task #21 (B2 credentials are currently broken, so this
    is untested against the real endpoint, only against local fixtures).
    """

    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)
        (self.base_dir / "qa").mkdir(parents=True, exist_ok=True)

    def snapshot_to_b2(self, cfg=None) -> bool:  # noqa: ANN001
        """Persist this lake to B2. See ``snapshot_telemetry_to_b2``.

        A thin method so call sites that already hold a store (the orchestrator) don't
        have to reach for ``base_dir`` and risk the divergence that already bit us
        once: a path recomputed from ``_cfg`` is not necessarily the path this store
        actually reads from, and when they differed the write silently went nowhere.
        """
        return snapshot_telemetry_to_b2(self.base_dir, cfg)

    # -- writing --------------------------------------------------------

    def write_qa_events(self, events: list[QAEvent]) -> Path | None:
        if not events:
            return None
        d = date.today().isoformat()
        partition = self.base_dir / "qa" / f"dt={d}"
        partition.mkdir(parents=True, exist_ok=True)
        path = partition / f"{uuid.uuid4()}.parquet"

        table = pa.Table.from_pylist(
            [
                {
                    "story_id": e.story_id, "locale": e.locale, "ordinal": e.ordinal,
                    "attempt": e.attempt, "voice_model": e.voice_model,
                    "wer": e.wer, "status": e.status,
                    "latency_ms": e.latency_ms, "ts": e.ts,
                }
                for e in events
            ],
            schema=_QA_SCHEMA,
        )
        pq.write_table(table, path)
        return path

    def write_gate_result(self, story_id: str, ordinal: int, locale: str,
                          result: GateResult) -> Path | None:
        return self.write_qa_events(QAEvent.from_gate_result(story_id, ordinal, locale, result))

    # -- glob helpers -----------------------------------------------------

    def _glob(self, table: str) -> str:
        p = self.base_dir / table
        return str(p / "**" / "*.parquet").replace("\\", "/")

    def _has_any(self, table: str) -> bool:
        return any((self.base_dir / table).rglob("*.parquet"))

    def _query(self, sql: str) -> list[dict[str, Any]]:
        rel = duckdb.sql(sql)
        cols = rel.columns
        return [dict(zip(cols, row, strict=True)) for row in rel.fetchall()]

    # -- dashboard queries -------------------------------------------------

    def dedup_stats(self) -> dict[str, Any]:
        """Reference count vs unique blob count, from genblaze's own asset table.

        Mirrors ``polyglo.db.dedup_stats`` but reads it from the Parquet lake rather
        than SQLite — the two should agree, and disagreement would mean the sink and
        the local index have drifted.
        """
        if not self._has_any("assets"):
            return {"total_refs": 0, "unique_blobs": 0, "dedup_ratio": 0.0}
        rows = self._query(f"""
            SELECT count(*) AS total_refs, count(DISTINCT sha256) AS unique_blobs
            FROM read_parquet('{self._glob("assets")}', union_by_name=true)
        """)
        r = rows[0]
        total, uniq = int(r["total_refs"] or 0), int(r["unique_blobs"] or 0)
        ratio = 1.0 - (uniq / total) if total else 0.0
        return {"total_refs": total, "unique_blobs": uniq, "dedup_ratio": ratio}

    def qa_effectiveness(self) -> list[dict[str, Any]]:
        """Per-attempt verdict counts and mean WER.

        ``status`` here is the per-attempt verdict from ``Attempt.status``
        (pass/retry/escalate/error) — NOT the scene-level ``QAStatus``
        (pass/retried/quarantined/unverified), which lives on ``LocalizedScene``
        in SQLite. Conflating the two vocabularies was an actual bug caught by
        ``seed_fixture_telemetry`` initially using the wrong one, which silently
        broke :meth:`qa_retry_evidence`.

        ``ORDER BY n DESC, status`` — the status is a required tiebreaker; without
        it, equal-count groups return in an order DuckDB does not guarantee, which
        made two identically-seeded fixture runs compare unequal in testing.
        """
        if not self._has_any("qa"):
            return []
        return self._query(f"""
            SELECT status, count(*) AS n, round(avg(wer), 4) AS avg_wer
            FROM read_parquet('{self._glob("qa")}', union_by_name=true)
            GROUP BY status
            ORDER BY n DESC, status
        """)

    def attempts_for(self, story_id: str, locale: str, ordinal: int) -> list[dict[str, Any]]:
        """Every attempt recorded for one scene/locale, in order.

        This is what the story detail page renders as the retry history — the same
        events `qa_retry_evidence()` aggregates across the whole story, but for one
        cell of the matrix. Escaped/parameterized manually since DuckDB's read_parquet
        glob is embedded in the SQL string already (see `_query`); story_id/locale are
        server-generated slugs, not user-supplied query text, so this is consistent
        with every other query in this class rather than a new injection surface.
        """
        if not self._has_any("qa"):
            return []
        story_id_sql = story_id.replace("'", "''")
        locale_sql = locale.replace("'", "''")
        return self._query(f"""
            SELECT * FROM read_parquet('{self._glob("qa")}', union_by_name=true)
            WHERE story_id = '{story_id_sql}' AND locale = '{locale_sql}' AND ordinal = {int(ordinal)}
            ORDER BY attempt
        """)

    def qa_retry_evidence(self) -> list[dict[str, Any]]:
        """Segments that failed at least once before eventually passing.

        This is the single most important query for the demo: it is the proof that
        the gate does real work, not just a pass-through.
        """
        if not self._has_any("qa"):
            return []
        return self._query(f"""
            WITH seg AS (
                SELECT story_id, locale, ordinal,
                       max(attempt) AS attempts,
                       max(CASE WHEN status = 'pass' THEN 1 ELSE 0 END) AS ever_passed
                FROM read_parquet('{self._glob("qa")}', union_by_name=true)
                GROUP BY story_id, locale, ordinal
            )
            SELECT * FROM seg WHERE attempts > 1 AND ever_passed = 1
            ORDER BY attempts DESC
        """)

    def cost_latency_by_model(self) -> list[dict[str, Any]]:
        """Spend, call count, and p95 latency per model, from genblaze's step table.

        Latency is computed from started_at/completed_at — steps has no latency_ms
        column, so inventing one here instead would silently diverge from what
        genblaze actually recorded.
        """
        if not self._has_any("steps"):
            return []
        return self._query(f"""
            SELECT
                model,
                count(*) AS calls,
                round(sum(cost_usd), 4) AS spend_usd,
                round(avg(date_diff('millisecond', started_at::TIMESTAMP,
                                    completed_at::TIMESTAMP)), 1) AS avg_latency_ms,
                round(quantile_cont(
                    date_diff('millisecond', started_at::TIMESTAMP,
                             completed_at::TIMESTAMP), 0.95), 1) AS p95_latency_ms,
                sum(CASE WHEN status != 'succeeded' THEN 1 ELSE 0 END) AS failures
            FROM read_parquet('{self._glob("steps")}', union_by_name=true)
            GROUP BY model
            ORDER BY spend_usd DESC
        """)

    def run_lineage(self, run_id: str) -> dict[str, Any] | None:
        """A single run's record plus its steps — the provenance drill-down."""
        if not self._has_any("runs"):
            return None
        runs = self._query(f"""
            SELECT * FROM read_parquet('{self._glob("runs")}', union_by_name=true)
            WHERE run_id = '{run_id}'
        """)
        if not runs:
            return None
        steps = self._query(f"""
            SELECT * FROM read_parquet('{self._glob("steps")}', union_by_name=true)
            WHERE run_id = '{run_id}' ORDER BY started_at
        """) if self._has_any("steps") else []
        return {"run": runs[0], "steps": steps}


# ---------------------------------------------------------------------------
# Synthetic fixtures
#
# TEST/DEV HELPER ONLY. This used to be called from the live dashboard routes
# whenever they found an empty lake, and that was a genuine bug: it writes fixture
# Parquet into the real telemetry directory, so the *next* request saw a non-empty
# lake, reported its source as "live", and served `story_id="fixture-story"` rows as
# production telemetry. Real runs append to the same tables, so the contamination was
# permanent and unfilterable. Both routes now show an honest empty state instead —
# see web.dashboard_page / api.dashboard. Do not call this from application code.
# ---------------------------------------------------------------------------


def purge_fixture_telemetry(base_dir: Path | str) -> dict[str, int]:
    """Delete fixture rows from an already-contaminated lake, keeping real ones.

    Needed because the fix to stop *writing* fixtures doesn't undo writes that already
    happened. This dev machine's lake had real rows (``flux.1-dev``, ``seedream-4.5``,
    ``voxtral``, a real ``openrouter-real-test-*`` story) interleaved with fixture rows
    in the same tables — so wiping the directory would have thrown away genuine
    history, and leaving it alone would have meant filming ``fixture-story`` in the
    demo video and snapshotting the contamination to B2 permanently.

    Fixtures are identifiable rather than merely guessable, which is what makes this
    safe: ``seed_fixture_telemetry`` stamps every run it creates with
    ``name="fixture-image"``, and every QA event with ``story_id="fixture-story"``.
    Steps and assets are matched by the run_ids those runs own, not by heuristics on
    cost or timestamp.

    Rewrites each Parquet file in place without its fixture rows, removing files that
    end up empty. Returns rows removed per table.

    The deployed instance needs no equivalent action: its lake lives on ephemeral disk,
    so the next redeploy starts empty, and nothing re-seeds it now.
    """
    removed = {"runs": 0, "steps": 0, "assets": 0, "qa": 0}
    base = Path(base_dir)
    if not base.exists():
        return removed

    # Which run_ids belong to fixture runs — collected first, since steps/assets carry
    # no marker of their own and are only identifiable by ownership.
    fixture_run_ids: set[str] = set()
    for path in sorted((base / "runs").rglob("*.parquet")) if (base / "runs").exists() else []:
        table = pq.read_table(path)
        if "name" not in table.column_names:
            continue
        names = table.column("name").to_pylist()
        ids = table.column("run_id").to_pylist()
        fixture_run_ids.update(
            rid for rid, name in zip(ids, names, strict=False) if name == "fixture-image"
        )

    def _rewrite(path: Path, keep_mask: list[bool], table_name: str) -> None:
        table = pq.read_table(path)
        dropped = len(keep_mask) - sum(keep_mask)
        if dropped == 0:
            return
        removed[table_name] += dropped
        kept = table.filter(pa.array(keep_mask))
        if kept.num_rows == 0:
            try:
                path.unlink()
            except PermissionError:
                import gc
                gc.collect()
                try:
                    path.unlink()
                except Exception:
                    pq.write_table(kept, path)
        else:
            pq.write_table(kept, path)

    for table_name, column, predicate in (
        ("runs", "run_id", lambda v: v in fixture_run_ids),
        ("steps", "run_id", lambda v: v in fixture_run_ids),
        ("assets", "run_id", lambda v: v in fixture_run_ids),
        ("qa", "story_id", lambda v: v == "fixture-story"),
    ):
        directory = base / table_name
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.parquet")):
            table = pq.read_table(path)
            if column not in table.column_names:
                continue
            values = table.column(column).to_pylist()
            _rewrite(path, [not predicate(v) for v in values], table_name)

    return removed


def _telemetry_snapshot_key(cfg=None) -> str:  # noqa: ANN001
    """Per-environment key, same reasoning as ``db._db_snapshot_key``: local dev and
    the deployed instance must never write to the same object, or local test runs
    would replace the numbers a live visitor sees.

    A function, not a module constant, so ``POLYGLO_ENV`` is read per call.
    """
    from polyglo.config import get_config

    cfg = cfg or get_config()
    return f"telemetry-snapshot/{cfg.env}/telemetry.zip"


def _parquet_files(base_dir: Path) -> list[Path]:
    return sorted(base_dir.rglob("*.parquet")) if base_dir.exists() else []


def snapshot_telemetry_to_b2(base_dir: Path | str, cfg=None) -> bool:  # noqa: ANN001
    """Upload the whole Parquet lake to B2 as one zip. Returns whether it uploaded.

    Why this exists: the lake lived only on local disk, and Render wipes local disk on
    every redeploy and every OOM restart. So real telemetry evaporated constantly, the
    dashboard found an empty lake, and (until this commit) seeded fixtures over it and
    called them live. The blobs themselves were never at risk — they're content-
    addressed in B2 — and neither was the story index, which ``db.backup_db_to_b2``
    already snapshots. The analytics lake was the one durable-storage gap left.

    A zip rather than per-file puts: the lake is many small hive-partitioned files, and
    one object keeps this a single round trip that either fully succeeds or leaves the
    previous snapshot untouched. Small enough to hold in memory at hackathon scale —
    at real volume this wants incremental per-partition sync instead, which is noted in
    docs/08-PRODUCTION-ROADMAP.md rather than pretended away here.

    A no-op (returns False) when B2 isn't configured or the lake is empty, matching
    ``backup_db_to_b2``'s quiet-degradation contract — an app running without B2 is a
    supported mode, not an error.
    """
    import io
    import zipfile

    from polyglo.config import get_config
    from polyglo.store import B2Backend

    cfg = cfg or get_config()
    base = Path(base_dir)
    files = _parquet_files(base)
    if not cfg.has_b2 or not files:
        return False

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, arcname=str(path.relative_to(base)).replace("\\", "/"))
    B2Backend(cfg).put(_telemetry_snapshot_key(cfg), buf.getvalue())
    return True


def restore_telemetry_from_b2(base_dir: Path | str, cfg=None) -> bool:  # noqa: ANN001
    """Restore the lake from B2 when this environment has none locally.

    Guarded on the local lake being genuinely empty, exactly like
    ``db.restore_db_from_b2``: this must never overwrite real local telemetry with a
    stale snapshot from a previous deploy. It only fires on a fresh container with an
    empty data dir — the case that used to zero the dashboard.

    ``arcname`` is written with forward slashes by the snapshot above, so extraction is
    portable between the Windows dev box and the Linux container.
    """
    import io
    import zipfile

    from polyglo.config import get_config
    from polyglo.store import B2Backend

    cfg = cfg or get_config()
    base = Path(base_dir)
    if not cfg.has_b2 or _parquet_files(base):
        return False

    backend = B2Backend(cfg)
    key = _telemetry_snapshot_key(cfg)
    if not backend.exists(key):
        return False

    base.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(backend.get(key))) as zf:
        for name in zf.namelist():
            # Refuse absolute paths and any traversal outside base_dir. The snapshot is
            # our own, but a zip is still attacker-shaped input if the bucket is ever
            # writable by anything else, and the check costs nothing.
            target = (base / name).resolve()
            if not str(target).startswith(str(base.resolve())):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
    return True


def seed_fixture_telemetry(base_dir: Path | str, *, seed: int = 7) -> TelemetryStore:
    """Write a plausible-looking runs/steps/assets/qa dataset for dashboard dev.

    Deterministic given ``seed`` (no ``random`` module — hand-rolled LCG — so the
    fixture is reproducible without touching disallowed nondeterminism sources).
    """
    store = TelemetryStore(base_dir)
    state = seed

    def rnd() -> float:
        nonlocal state
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF

    locales = ["es-ES", "fr-FR", "de-DE", "hi-IN"]
    models = ["nvidia/magpie-tts-multilingual", "gemini-2.5-flash-preview-tts"]
    image_shas = [f"{i:064x}" for i in range(5)]     # 5 scenes, shared across locales

    runs_rows, steps_rows, assets_rows = [], [], []
    qa_events: list[QAEvent] = []

    for scene_idx, sha in enumerate(image_shas):
        for locale in locales:
            run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scene_idx}-{locale}-img"))
            runs_rows.append({
                "run_id": run_id, "parent_run_id": None, "tenant_id": "default",
                "project_id": None, "name": "fixture-image", "status": "completed",
                "step_count": 1, "canonical_hash": f"{scene_idx}{hash(locale) & 0xff:02x}" * 8,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            steps_rows.append({
                "run_id": run_id, "step_id": str(uuid.uuid4()), "provider": "nvidia-image",
                "model": "black-forest-labs/flux.1-schnell", "step_type": "generate",
                "modality": "image", "status": "succeeded", "prompt": f"scene {scene_idx}",
                "seed": scene_idx, "params_json": "{}", "asset_count": 1, "retries": 0,
                "cost_usd": 0.003, "error": None, "error_code": None,
                "started_at": "2026-07-31T10:00:00+00:00",
                "completed_at": "2026-07-31T10:00:02+00:00",
            })
            assets_rows.append({
                "run_id": run_id, "step_id": steps_rows[-1]["step_id"], "asset_id": "img",
                "url": f"b2://polyglo/blobs/{sha[:2]}/{sha[2:4]}/{sha}",
                "media_type": "image/png", "sha256": sha, "size_bytes": 204800,
                **{k: None for k in ("width", "height", "duration", "frame_rate",
                                     "video_codec", "video_bitrate", "color_space",
                                     "has_audio", "sample_rate", "channels",
                                     "audio_codec", "track_count")},
            })

            # Attempt 1 fails high, attempt 2 recovers on the alternate voice —
            # the retry-and-recover shape the demo's centerpiece 40 seconds show.
            # Status vocabulary matches Attempt.status exactly: pass/retry/escalate —
            # NOT the scene-level QAStatus (pass/retried/quarantined), which lives on
            # LocalizedScene in SQLite, not in this table.
            for attempt in range(1, 3):
                wer = max(0.0, round(0.31 - (attempt - 1) * 0.28 + rnd() * 0.03, 3))
                passed = wer <= 0.10
                status = "pass" if passed else "retry"
                qa_events.append(QAEvent(
                    story_id="fixture-story", locale=locale, ordinal=scene_idx,
                    attempt=attempt, voice_model=models[attempt - 1],
                    status=status, wer=wer, latency_ms=int(800 + rnd() * 400),
                ))
                if passed:
                    break

    for table, rows in (("runs", runs_rows), ("steps", steps_rows), ("assets", assets_rows)):
        d = date.today().isoformat()
        part = Path(base_dir) / table / f"dt={d}" / "tenant_id=default"
        part.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows), part / f"{uuid.uuid4()}.parquet")

    store.write_qa_events(qa_events)
    return store
