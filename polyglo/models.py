"""Core domain model.

Design invariant that everything else depends on:

    A Scene owns ONE image, shared by every locale.
    A LocalizedScene owns ONE audio file, unique per locale.

That asymmetry is the product. Storage grows with ``locales x audio``, never
``locales x (audio + images)``. Anything that generates an image per locale is a bug,
not an optimisation opportunity.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class QAStatus(str, Enum):
    """Outcome of the cross-modal QA gate for a single localized scene."""

    PENDING = "pending"          # not yet narrated or not yet verified
    PASS = "pass"                # verified on the first attempt
    RETRIED = "retried"          # verified, but only after one or more retries
    QUARANTINED = "quarantined"  # failed every attempt; needs a human
    UNVERIFIED = "unverified"    # no ASR available; audio exists but is ungraded

    @property
    def is_terminal(self) -> bool:
        return self in (QAStatus.PASS, QAStatus.RETRIED, QAStatus.QUARANTINED)

    @property
    def is_good(self) -> bool:
        """Shippable? RETRIED counts — it passed, just not first time."""
        return self in (QAStatus.PASS, QAStatus.RETRIED)


class CEFR(str, Enum):
    A1 = "A1"
    A2 = "A2"
    B1 = "B1"
    B2 = "B2"
    C1 = "C1"
    C2 = "C2"


class AssetKind(str, Enum):
    IMAGE = "image"
    AUDIO = "audio"


# ---------------------------------------------------------------------------
# Locales
#
# Deliberately a short list of well-supported languages. TTS and ASR quality
# collapses for low-resource languages, and demoing on those would produce
# failures that say nothing about our pipeline. docs/01 commits to naming this
# limitation out loud rather than hiding it.
# ---------------------------------------------------------------------------

SUPPORTED_LOCALES: dict[str, str] = {
    "en-US": "English (US)",
    "es-ES": "Spanish (Spain)",
    "fr-FR": "French",
    "de-DE": "German",
    "it-IT": "Italian",
    "pt-BR": "Portuguese (Brazil)",
    "hi-IN": "Hindi",
    "ja-JP": "Japanese",
}

DEFAULT_LOCALES = ["es-ES", "fr-FR", "de-DE", "hi-IN"]

# Flag emoji per locale — UI polish only (task #29), never used for anything
# functional (no locale logic depends on this), so an unmapped code just gets no
# flag rather than breaking anything.
LOCALE_FLAGS: dict[str, str] = {
    "en-US": "\U0001F1FA\U0001F1F8",
    "es-ES": "\U0001F1EA\U0001F1F8",
    "fr-FR": "\U0001F1EB\U0001F1F7",
    "de-DE": "\U0001F1E9\U0001F1EA",
    "it-IT": "\U0001F1EE\U0001F1F9",
    "pt-BR": "\U0001F1E7\U0001F1F7",
    "hi-IN": "\U0001F1EE\U0001F1F3",
    "ja-JP": "\U0001F1EF\U0001F1F5",
}


def locale_name(code: str) -> str:
    return SUPPORTED_LOCALES.get(code, code)


def locale_flag(code: str) -> str:
    return LOCALE_FLAGS.get(code, "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow() -> str:
    """ISO-8601 UTC timestamp. Stored as TEXT so SQLite comparisons sort correctly."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 48) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    # Drop apostrophes rather than treating them as separators, so "Niño's"
    # slugifies to "ninos" and not "nino-s".
    ascii_text = re.sub(r"['’]", "", ascii_text)
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "story"


def new_story_id(title: str) -> str:
    return f"{slugify(title, 32)}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass
class Scene:
    """One beat of a story. Locale-independent.

    ``image_sha256`` is shared by every locale's bundle — see module docstring.
    """

    story_id: str
    ordinal: int
    source_text: str
    visual_prompt: str
    image_sha256: str | None = None

    @property
    def has_image(self) -> bool:
        return bool(self.image_sha256)


@dataclass
class LocalizedScene:
    """One scene rendered into one target locale: translated text plus narration."""

    story_id: str
    ordinal: int
    locale: str
    text: str
    audio_sha256: str | None = None
    qa_status: QAStatus = QAStatus.PENDING
    wer: float | None = None
    attempts: int = 0
    transcript: str | None = None   # what the ASR heard — the UI diffs against this
    voice_model: str | None = None  # which model finally produced acceptable audio

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_sha256)

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.story_id, self.ordinal, self.locale)


@dataclass
class Story:
    story_id: str
    title: str
    cefr: str
    source_locale: str
    created_at: str = field(default_factory=utcnow)
    scenes: list[Scene] = field(default_factory=list)
    # Task #25: the raw text the user submitted, and the spelling/grammar-corrected,
    # CEFR-leveled version actually used for scene splitting — both kept so the
    # transformation is visible rather than silently applied. None until authoring's
    # grading step runs (offline zero-credential mode currently skips it).
    original_source_text: str | None = None
    corrected_source_text: str | None = None

    @classmethod
    def create(
        cls,
        title: str,
        cefr: str = CEFR.B1.value,
        source_locale: str = "en-US",
    ) -> Story:
        return cls(
            story_id=new_story_id(title),
            title=title,
            cefr=cefr,
            source_locale=source_locale,
        )


@dataclass
class Bundle:
    """A per-locale deliverable. References blobs by hash — it never copies them."""

    story_id: str
    locale: str
    manifest_uri: str
    canonical_hash: str
    image_refs: list[str] = field(default_factory=list)
    audio_refs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)

    @property
    def all_refs(self) -> list[str]:
        return [*self.image_refs, *self.audio_refs]


@dataclass
class DedupStats:
    """The headline demo number, computed from real reference counts."""

    total_refs: int
    unique_blobs: int
    bytes_stored: int = 0
    bytes_naive: int = 0

    @property
    def dedup_ratio(self) -> float:
        """Fraction of references served by a blob that already existed."""
        if self.total_refs == 0:
            return 0.0
        return 1.0 - (self.unique_blobs / self.total_refs)

    @property
    def bytes_saved(self) -> int:
        return max(0, self.bytes_naive - self.bytes_stored)

    def summary(self) -> str:
        return (
            f"{self.total_refs} references -> {self.unique_blobs} unique blobs "
            f"({self.dedup_ratio:.1%} deduplicated)"
        )
