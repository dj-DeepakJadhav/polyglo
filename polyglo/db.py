"""SQLite index.

This database is a **rebuildable cache, never the source of truth**. Every fact in
here also exists in B2 (manifests, bundles, blobs). If it is lost, ``rebuild_from_b2``
reconstructs it. That constraint is deliberate: it keeps the durable record in the
object store, where the hackathon's "B2 and data orchestration" criterion lives, and
stops SQLite quietly becoming load-bearing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from polyglo.config import get_config
from polyglo.models import (
    AssetKind,
    Bundle,
    DedupStats,
    LocalizedScene,
    QAStatus,
    Scene,
    Story,
    utcnow,
)

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS stories (
    story_id      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    cefr          TEXT NOT NULL,
    source_locale TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenes (
    story_id      TEXT    NOT NULL,
    ordinal       INTEGER NOT NULL,
    source_text   TEXT    NOT NULL,
    visual_prompt TEXT    NOT NULL,
    image_sha256  TEXT,
    PRIMARY KEY (story_id, ordinal),
    FOREIGN KEY (story_id) REFERENCES stories(story_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS localized_scenes (
    story_id     TEXT    NOT NULL,
    ordinal      INTEGER NOT NULL,
    locale       TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    audio_sha256 TEXT,
    qa_status    TEXT    NOT NULL DEFAULT 'pending',
    wer          REAL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    transcript   TEXT,
    voice_model  TEXT,
    PRIMARY KEY (story_id, ordinal, locale),
    FOREIGN KEY (story_id, ordinal)
        REFERENCES scenes(story_id, ordinal) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bundles (
    story_id       TEXT NOT NULL,
    locale         TEXT NOT NULL,
    manifest_uri   TEXT NOT NULL,
    canonical_hash TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (story_id, locale),
    FOREIGN KEY (story_id) REFERENCES stories(story_id) ON DELETE CASCADE
);

-- Every (bundle -> blob) edge. This table is what makes the dedup claim
-- measurable rather than asserted: COUNT(*) vs COUNT(DISTINCT sha256).
CREATE TABLE IF NOT EXISTS bundle_refs (
    story_id TEXT NOT NULL,
    locale   TEXT NOT NULL,
    sha256   TEXT NOT NULL,
    kind     TEXT NOT NULL,
    PRIMARY KEY (story_id, locale, sha256),
    FOREIGN KEY (story_id, locale)
        REFERENCES bundles(story_id, locale) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ls_status ON localized_scenes(qa_status);
CREATE INDEX IF NOT EXISTS idx_ls_story  ON localized_scenes(story_id, locale);
CREATE INDEX IF NOT EXISTS idx_refs_sha  ON bundle_refs(sha256);
"""


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_config().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_STORIES_MIGRATED_COLUMNS = {
    "original_source_text": "TEXT",
    "corrected_source_text": "TEXT",
}


def _migrate_stories_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the original ``CREATE TABLE`` (task #25) to any
    ``stories`` table that predates them — including one just restored from a B2
    snapshot (see ``restore_db_from_b2``) taken before this migration existed.
    ``CREATE TABLE IF NOT EXISTS`` alone never adds columns to an existing table, so
    this is a real, separate step, not redundant with the schema script above.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(stories)")}
    for column, sql_type in _STORIES_MIGRATED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE stories ADD COLUMN {column} {sql_type}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_stories_columns(conn)
    row = conn.execute("SELECT COUNT(*) AS n FROM schema_meta").fetchone()
    if row["n"] == 0:
        conn.execute(
            "INSERT INTO schema_meta (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow()),
        )
    conn.commit()


@contextmanager
def session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stories & scenes
# ---------------------------------------------------------------------------


def save_story(conn: sqlite3.Connection, story: Story) -> None:
    conn.execute(
        """INSERT INTO stories
               (story_id, title, cefr, source_locale, created_at,
                original_source_text, corrected_source_text)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(story_id) DO UPDATE SET
               title=excluded.title, cefr=excluded.cefr,
               source_locale=excluded.source_locale,
               original_source_text=COALESCE(excluded.original_source_text,
                                             stories.original_source_text),
               corrected_source_text=COALESCE(excluded.corrected_source_text,
                                              stories.corrected_source_text)""",
        (story.story_id, story.title, story.cefr, story.source_locale, story.created_at,
         story.original_source_text, story.corrected_source_text),
    )
    for scene in story.scenes:
        save_scene(conn, scene)


def save_scene(conn: sqlite3.Connection, scene: Scene) -> None:
    conn.execute(
        """INSERT INTO scenes (story_id, ordinal, source_text, visual_prompt, image_sha256)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(story_id, ordinal) DO UPDATE SET
               source_text=excluded.source_text,
               visual_prompt=excluded.visual_prompt,
               image_sha256=COALESCE(excluded.image_sha256, scenes.image_sha256)""",
        (
            scene.story_id,
            scene.ordinal,
            scene.source_text,
            scene.visual_prompt,
            scene.image_sha256,
        ),
    )


def get_story(conn: sqlite3.Connection, story_id: str) -> Story | None:
    row = conn.execute(
        "SELECT * FROM stories WHERE story_id = ?", (story_id,)
    ).fetchone()
    if row is None:
        return None
    story = Story(
        story_id=row["story_id"],
        title=row["title"],
        cefr=row["cefr"],
        source_locale=row["source_locale"],
        created_at=row["created_at"],
        original_source_text=row["original_source_text"],
        corrected_source_text=row["corrected_source_text"],
    )
    story.scenes = get_scenes(conn, story_id)
    return story


def list_stories(conn: sqlite3.Connection) -> list[Story]:
    rows = conn.execute("SELECT story_id FROM stories ORDER BY created_at DESC").fetchall()
    return [s for r in rows if (s := get_story(conn, r["story_id"])) is not None]


def get_scenes(conn: sqlite3.Connection, story_id: str) -> list[Scene]:
    rows = conn.execute(
        "SELECT * FROM scenes WHERE story_id = ? ORDER BY ordinal", (story_id,)
    ).fetchall()
    return [
        Scene(
            story_id=r["story_id"],
            ordinal=r["ordinal"],
            source_text=r["source_text"],
            visual_prompt=r["visual_prompt"],
            image_sha256=r["image_sha256"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Localized scenes
# ---------------------------------------------------------------------------


def save_localized(conn: sqlite3.Connection, ls: LocalizedScene) -> None:
    conn.execute(
        """INSERT INTO localized_scenes
               (story_id, ordinal, locale, text, audio_sha256,
                qa_status, wer, attempts, transcript, voice_model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(story_id, ordinal, locale) DO UPDATE SET
               text=excluded.text,
               audio_sha256=excluded.audio_sha256,
               qa_status=excluded.qa_status,
               wer=excluded.wer,
               attempts=excluded.attempts,
               transcript=excluded.transcript,
               voice_model=excluded.voice_model""",
        (
            ls.story_id,
            ls.ordinal,
            ls.locale,
            ls.text,
            ls.audio_sha256,
            QAStatus(ls.qa_status).value,
            ls.wer,
            ls.attempts,
            ls.transcript,
            ls.voice_model,
        ),
    )


def _row_to_localized(r: sqlite3.Row) -> LocalizedScene:
    return LocalizedScene(
        story_id=r["story_id"],
        ordinal=r["ordinal"],
        locale=r["locale"],
        text=r["text"],
        audio_sha256=r["audio_sha256"],
        qa_status=QAStatus(r["qa_status"]),
        wer=r["wer"],
        attempts=r["attempts"],
        transcript=r["transcript"],
        voice_model=r["voice_model"],
    )


def get_localized(
    conn: sqlite3.Connection, story_id: str, locale: str | None = None
) -> list[LocalizedScene]:
    if locale:
        rows = conn.execute(
            """SELECT * FROM localized_scenes
               WHERE story_id = ? AND locale = ? ORDER BY ordinal""",
            (story_id, locale),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM localized_scenes
               WHERE story_id = ? ORDER BY locale, ordinal""",
            (story_id,),
        ).fetchall()
    return [_row_to_localized(r) for r in rows]


def get_quarantined(conn: sqlite3.Connection) -> list[LocalizedScene]:
    """The human review queue."""
    rows = conn.execute(
        "SELECT * FROM localized_scenes WHERE qa_status = ? ORDER BY story_id, locale, ordinal",
        (QAStatus.QUARANTINED.value,),
    ).fetchall()
    return [_row_to_localized(r) for r in rows]


def qa_summary(conn: sqlite3.Connection, story_id: str | None = None) -> dict[str, int]:
    sql = "SELECT qa_status, COUNT(*) AS n FROM localized_scenes"
    params: Sequence[object] = ()
    if story_id:
        sql += " WHERE story_id = ?"
        params = (story_id,)
    sql += " GROUP BY qa_status"
    return {r["qa_status"]: r["n"] for r in conn.execute(sql, params).fetchall()}


# ---------------------------------------------------------------------------
# Bundles & dedup accounting
# ---------------------------------------------------------------------------


def save_bundle(conn: sqlite3.Connection, bundle: Bundle) -> None:
    conn.execute(
        """INSERT INTO bundles (story_id, locale, manifest_uri, canonical_hash, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(story_id, locale) DO UPDATE SET
               manifest_uri=excluded.manifest_uri,
               canonical_hash=excluded.canonical_hash""",
        (
            bundle.story_id,
            bundle.locale,
            bundle.manifest_uri,
            bundle.canonical_hash,
            bundle.created_at,
        ),
    )
    conn.execute(
        "DELETE FROM bundle_refs WHERE story_id = ? AND locale = ?",
        (bundle.story_id, bundle.locale),
    )
    rows = [
        (bundle.story_id, bundle.locale, sha, AssetKind.IMAGE.value)
        for sha in bundle.image_refs
    ] + [
        (bundle.story_id, bundle.locale, sha, AssetKind.AUDIO.value)
        for sha in bundle.audio_refs
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO bundle_refs (story_id, locale, sha256, kind) VALUES (?, ?, ?, ?)",
        rows,
    )


def get_bundles(conn: sqlite3.Connection, story_id: str) -> list[Bundle]:
    out: list[Bundle] = []
    for r in conn.execute(
        "SELECT * FROM bundles WHERE story_id = ? ORDER BY locale", (story_id,)
    ).fetchall():
        refs = conn.execute(
            "SELECT sha256, kind FROM bundle_refs WHERE story_id = ? AND locale = ?",
            (r["story_id"], r["locale"]),
        ).fetchall()
        out.append(
            Bundle(
                story_id=r["story_id"],
                locale=r["locale"],
                manifest_uri=r["manifest_uri"],
                canonical_hash=r["canonical_hash"],
                created_at=r["created_at"],
                image_refs=[x["sha256"] for x in refs if x["kind"] == AssetKind.IMAGE.value],
                audio_refs=[x["sha256"] for x in refs if x["kind"] == AssetKind.AUDIO.value],
            )
        )
    return out


def dedup_stats(conn: sqlite3.Connection, story_id: str | None = None) -> DedupStats:
    """Reference count vs unique blob count. The headline demo number."""
    sql = "SELECT COUNT(*) AS total, COUNT(DISTINCT sha256) AS uniq FROM bundle_refs"
    params: Sequence[object] = ()
    if story_id:
        sql += " WHERE story_id = ?"
        params = (story_id,)
    row = conn.execute(sql, params).fetchone()
    return DedupStats(total_refs=row["total"] or 0, unique_blobs=row["uniq"] or 0)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def rebuild_from_b2(conn: sqlite3.Connection, store) -> int:  # noqa: ANN001
    """Reconstruct the index from bundles and manifests in B2.

    Not yet implemented — it exists to keep the "SQLite is a cache" contract honest
    and to make the recovery path explicit rather than aspirational. Wire it once
    the B2 backend is exercised against a real bucket (task #17).
    """
    raise NotImplementedError(
        "rebuild_from_b2 requires a live B2 backend; see task #17 in docs/03"
    )


def _db_snapshot_key(cfg=None) -> str:  # noqa: ANN001
    """Per-environment snapshot key, so local dev and the deployed instance never
    write to the same object.

    This was a single fixed key (``db-snapshot/polyglo.db``) and that was a real
    hazard, not a theoretical one: every local pipeline run overwrote the exact
    snapshot the production instance restores from on its next restart, so heavy
    local testing could replace the story list a live visitor sees. Blobs are
    content-addressed and were never at risk — only the index. Keyed off
    ``POLYGLO_ENV`` (default ``"dev"``); production sets ``POLYGLO_ENV=prod``.

    Deliberately a function rather than a module constant so the env var is read
    per call — a constant evaluated at import time would bake in whatever was set
    when the module first loaded, which breaks both tests and any runtime change.
    """
    from polyglo.config import get_config

    cfg = cfg or get_config()
    return f"db-snapshot/{cfg.env}/polyglo.db"


def backup_db_to_b2(db_path: Path, cfg=None) -> None:  # noqa: ANN001
    """Upload the whole SQLite file to this environment's B2 key, overwriting the
    previous copy for that same environment only (see ``_db_snapshot_key``).

    Deliberately NOT the same thing as ``rebuild_from_b2`` above (which would
    reconstruct records purely from B2 objects/manifests — the more "principled"
    approach, still unimplemented). This is the pragmatic version: a platform like
    Render wipes local disk on every redeploy, so the story/scene *index* (which
    B2 blob belongs to which story) would otherwise be lost even though the blobs
    themselves survive fine in B2 (confirmed directly — 2026-08-01, see
    docs/SESSION-LOG.md: every blob generated this session was still in the bucket
    after multiple container restarts). A no-op, not an error, when B2 isn't
    configured — callers are expected to check ``cfg.has_b2`` if they care, but
    this function itself degrading quietly matches ``Config``'s existing "upgrade
    only what's available" design rather than forcing every call site to guard it.
    """
    from polyglo.config import get_config
    from polyglo.store import B2Backend

    cfg = cfg or get_config()
    if not cfg.has_b2:
        return
    B2Backend(cfg).put(_db_snapshot_key(cfg), db_path.read_bytes())


def restore_db_from_b2(db_path: Path, cfg=None) -> bool:  # noqa: ANN001
    """Download the latest snapshot into ``db_path`` if one exists and no local copy
    already does. Returns whether a restore actually happened.

    The "no local copy already" guard is deliberate: this must never clobber a real
    local dev database with a stale snapshot from a previous deploy — it only fires
    on a genuinely fresh environment (a new Render container with an empty data
    dir), which is exactly the situation this exists to fix.
    """
    from polyglo.config import get_config
    from polyglo.store import B2Backend

    cfg = cfg or get_config()
    if not cfg.has_b2 or db_path.exists():
        return False
    backend = B2Backend(cfg)
    key = _db_snapshot_key(cfg)
    if not backend.exists(key):
        return False
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(backend.get(key))
    return True
