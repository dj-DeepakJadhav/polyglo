"""Content-addressed blob store.

Every asset is keyed by the SHA-256 of its own bytes:

    blobs/<sha[0:2]>/<sha[2:4]>/<sha256>

Two consequences the product depends on:

1. **Dedup is automatic.** Writing identical bytes twice is a no-op the second time.
   Twenty locale bundles referencing the same scene image cost one blob.
2. **Re-running is free.** Once the demo dataset exists, every rehearsal is served from
   the store rather than regenerated. The dedup feature funds its own demo.

Two backends behind one protocol: ``LocalBackend`` (filesystem, needs no credentials,
used by every test) and ``B2Backend`` (S3 API). The interface is identical so the whole
system is developable and testable with no keys.

We talk to B2 with boto3 directly rather than reusing Genblaze's ``S3StorageBackend``.
That is deliberate: Genblaze owns its own asset/manifest keyspace under the sink, while
this store owns the bundle/blob keyspace. Keeping them separate means neither can
surprise the other with a key-layout change, and boto3 is already a hard dependency of
``genblaze-s3`` so it costs nothing.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

from polyglo.config import Config, get_config

BLOB_PREFIX = "blobs"


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def sha256_stream(fh: BinaryIO, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    while block := fh.read(chunk):
        h.update(block)
    fh.seek(0)
    return h.hexdigest()


def blob_key(sha: str) -> str:
    """Fan out by hash prefix so no single directory or key prefix gets hot."""
    if len(sha) != 64:
        raise ValueError(f"expected a 64-char sha256, got {len(sha)} chars")
    return f"{BLOB_PREFIX}/{sha[0:2]}/{sha[2:4]}/{sha}"


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def size(self, key: str) -> int: ...
    def uri(self, key: str) -> str: ...


class LocalBackend:
    """Filesystem backend. No credentials, no network — the default for dev and tests."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then move, so a crash mid-write cannot leave a
        # truncated blob sitting at a hash that claims to describe it.
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        shutil.move(str(tmp), str(p))

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def size(self, key: str) -> int:
        return self._path(key).stat().st_size

    def uri(self, key: str) -> str:
        return self._path(key).as_uri()


class B2Backend:
    """Backblaze B2 over the S3-compatible API."""

    def __init__(self, cfg: Config | None = None):
        cfg = cfg or get_config()
        if not cfg.has_b2:
            raise RuntimeError(
                "B2 credentials absent — set B2_KEY_ID / B2_APP_KEY / B2_BUCKET "
                "(see .env.example), or use LocalBackend."
            )
        import boto3  # imported lazily so no-credentials mode never needs it

        self.bucket = cfg.b2.bucket
        endpoint = cfg.b2.endpoint or None
        if endpoint and not endpoint.startswith("http"):
            endpoint = f"https://{endpoint}"
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=cfg.b2.key_id,
            aws_secret_access_key=cfg.b2.app_key,
        )

    def put(self, key: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def size(self, key: str) -> int:
        return int(self._s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"])

    def uri(self, key: str) -> str:
        return f"b2://{self.bucket}/{key}"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class PutResult:
    sha256: str
    key: str
    uri: str
    size: int
    deduped: bool  # True => bytes were already present, nothing uploaded


@dataclass
class StoreStats:
    """In-process accounting. Feeds the dashboard's cache-hit figure."""

    writes: int = 0
    dedup_hits: int = 0
    bytes_written: int = 0
    bytes_deduped: int = 0
    _seen: set[str] = field(default_factory=set)

    @property
    def total_puts(self) -> int:
        return self.writes + self.dedup_hits

    @property
    def hit_rate(self) -> float:
        return self.dedup_hits / self.total_puts if self.total_puts else 0.0


class BlobStore:
    """Content-addressed store over any ``StorageBackend``."""

    def __init__(self, backend: StorageBackend):
        self.backend = backend
        self.stats = StoreStats()

    # -- writes ----------------------------------------------------------

    def put_bytes(self, data: bytes) -> PutResult:
        sha = sha256_bytes(data)
        key = blob_key(sha)
        size = len(data)

        if self.backend.exists(key):
            self.stats.dedup_hits += 1
            self.stats.bytes_deduped += size
            self.stats._seen.add(sha)
            return PutResult(sha, key, self.backend.uri(key), size, deduped=True)

        self.backend.put(key, data)
        self.stats.writes += 1
        self.stats.bytes_written += size
        self.stats._seen.add(sha)
        return PutResult(sha, key, self.backend.uri(key), size, deduped=False)

    def put_file(self, path: Path | str) -> PutResult:
        return self.put_bytes(Path(path).read_bytes())

    # -- reads -----------------------------------------------------------

    def get(self, sha: str) -> bytes:
        return self.backend.get(blob_key(sha))

    def exists(self, sha: str) -> bool:
        return self.backend.exists(blob_key(sha))

    def size(self, sha: str) -> int:
        return self.backend.size(blob_key(sha))

    def uri(self, sha: str) -> str:
        return self.backend.uri(blob_key(sha))

    # -- integrity -------------------------------------------------------

    def verify(self, sha: str) -> bool:
        """Re-hash stored bytes and confirm they still match their own address.

        Content-addressing makes corruption detectable for free — the key *is* the
        checksum. Cheap to run, and it is what lets the UI show a green tick.
        """
        try:
            return sha256_bytes(self.get(sha)) == sha
        except (FileNotFoundError, KeyError):
            return False

    # -- accounting ------------------------------------------------------

    def dedup_summary(self) -> str:
        s = self.stats
        return (
            f"{s.total_puts} puts -> {s.writes} stored, {s.dedup_hits} deduped "
            f"({s.hit_rate:.1%} hit rate, {s.bytes_deduped:,} bytes avoided)"
        )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def make_store(cfg: Config | None = None) -> BlobStore:
    """B2 when credentials exist, local filesystem otherwise.

    Degradation is silent here by design — the caller decides whether to surface it.
    ``Config.banner()`` is what the UI shows.
    """
    cfg = cfg or get_config()
    if cfg.has_b2:
        return BlobStore(B2Backend(cfg))
    return BlobStore(LocalBackend(cfg.data_dir / "blobs_local"))
