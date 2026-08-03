"""Configuration and credential detection.

The whole system is designed to run with NO credentials (mock providers, local
filesystem blob store, fixture telemetry) so that development and testing never
depend on API keys or burn inference credits. Credentials, when present, upgrade
individual subsystems independently — B2 without NVIDIA works, NVIDIA without
Gemini works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _flag(key: str, default: float) -> float:
    raw = _env(key)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class B2Config:
    key_id: str
    app_key: str
    bucket: str
    endpoint: str

    @property
    def available(self) -> bool:
        return bool(self.key_id and self.app_key and self.bucket)


@dataclass(frozen=True)
class GeminiConfig:
    """Gemini is rate-limited by explicit user instruction, not just by cost.

    Enforced by :class:`polyglo.qa.budget.GeminiBudget`, which persists its counter to
    ``data_dir`` so the cap survives process restarts within a day — a fresh call budget
    would let it be reset by accident.
    """

    daily_call_cap: int = 50


@dataclass(frozen=True)
class OpenRouterConfig:
    """OpenRouter is genuinely pay-per-use (unlike NVIDIA's free tier), so it gets
    the same disk-persisted daily call cap Gemini has — see qa/budget.py."""

    daily_call_cap: int = 200


@dataclass(frozen=True)
class GlobalConfig:
    """App-wide caps that exist independent of any one provider — the per-client
    rate limiter (ratelimit.py) bounds one caller's request rate, but does not
    bound the total across every distinct visitor combined. This is that
    aggregate governor: at most this many real story-creation requests, from
    anyone, per day."""

    daily_story_cap: int = 100


@dataclass(frozen=True)
class QAConfig:
    """QA gate thresholds.

    Deliberately config-driven: the defaults are an untested starting guess and
    must be calibrated against real samples (see docs/03, task #18). A gate that
    never fires is theatre; one that always fires burns the credit budget.
    """

    wer_pass: float = 0.10
    wer_retry: float = 0.25
    max_attempts: int = 3


@dataclass(frozen=True)
class Config:
    b2: B2Config
    qa: QAConfig
    gemini: GeminiConfig
    nvidia_api_key: str
    gemini_api_key: str
    openrouter_api_key: str
    data_dir: Path
    db_path: Path
    admin_key: str = ""
    fal_api_key: str = ""
    replicate_api_key: str = ""
    openrouter: OpenRouterConfig = OpenRouterConfig()
    global_: GlobalConfig = GlobalConfig()

    @property
    def env(self) -> str:
        """Deployment environment name, used to namespace anything shared across
        environments via B2 — currently the SQLite snapshot key (``db._db_snapshot_key``).

        Defaults to ``"dev"`` so a developer who sets nothing can never write to
        production's slot by accident; the deployed instance opts IN by setting
        ``POLYGLO_ENV=prod``. That direction matters: the failure mode this fixes
        (a local run overwriting the story index a live visitor sees) is caused by
        dev, so dev must be the one that's safe by default.
        """
        return _env("POLYGLO_ENV", "dev").strip().lower() or "dev"

    @property
    def quality_mode(self) -> str:
        """Returns 'pro' (SOTA premium models) or 'free' (default low-cost / free-tier models).
        Set via POLYGLO_QUALITY_MODE=pro env variable.
        """
        return _env("POLYGLO_QUALITY_MODE", "free").strip().lower() or "free"

    @property
    def gemini_budget_path(self) -> Path:
        return self.data_dir / "gemini_budget.json"

    @property
    def openrouter_budget_path(self) -> Path:
        return self.data_dir / "openrouter_budget.json"

    @property
    def global_story_budget_path(self) -> Path:
        return self.data_dir / "global_story_budget.json"

    # -- capability flags -------------------------------------------------
    @property
    def has_b2(self) -> bool:
        return self.b2.available

    @property
    def has_nvidia(self) -> bool:
        return bool(self.nvidia_api_key)

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_openrouter(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def prefer_openrouter_images(self) -> bool:
        """Opt-in only. NVIDIA stays the default image generator either way — this
        exists to fix a real character-consistency bug (OpenRouter's Seedream model
        supports real image-to-image reference conditioning; NVIDIA's provider has no
        such parameter), not to replace a working default. Defaults false; an operator
        flips it on deliberately, it's never inferred from ``has_openrouter`` alone.
        """
        return _env("OPENROUTER_PREFER_IMAGES", "false").lower() in ("true", "1", "yes")

    @property
    def nvidia_image_broken(self) -> bool:
        """Whether NVIDIA image generation is known non-functional.

        Re-investigated 2026-07-31 (see docs/SESSION-LOG.md, task #22 follow-up):
        the originally-configured primary model (``flux.1-schnell``) really is dead
        (times out), but ``flux.1-dev`` works — verified with a real raw-HTTP call
        (200 OK, genuine JPEG) and directly through genblaze's own provider (8s,
        200 OK). Image generation is NOT broken; the bug was model selection, now
        fixed in ``orchestrator.make_providers`` (flux.1-dev is primary). Defaults
        False; flip via env only if a future model swap regresses.
        """
        return _env("NVIDIA_IMAGE_BROKEN", "false").lower() in ("true", "1", "yes")

    @property
    def nvidia_audio_broken(self) -> bool:
        """Whether NVIDIA audio (TTS) generation is known non-functional.

        Root cause confirmed 2026-07-31 via NVIDIA's own NIM-for-Speech docs
        (docs.nvidia.com/nim/speech/latest/reference/api-references/tts/http-tts.html):
        Magpie TTS is shipped as a self-hosted NIM microservice (base URL
        ``http://<address>:9000``, deployed via NGC onto your own GPU) with a
        completely different call shape (``POST /v1/audio/synthesize`` with a
        ``voice="Magpie-Multilingual.EN-US.Aria"`` string) — NOT a hosted
        ``ai.api.nvidia.com/v1/genai/{model}`` endpoint the way image/chat are. Every
        slug tried against that hosted endpoint 404s because no hosted endpoint for
        this model family exists there; this isn't a stale-registry or wrong-slug
        problem, it requires provisioning GPU infrastructure, out of scope for a
        free-tier build. Defaults True; flip via env only if self-hosting this
        becomes in scope.
        """
        return _env("NVIDIA_AUDIO_BROKEN", "true").lower() in ("true", "1", "yes")

    @property
    def has_image_generation(self) -> bool:
        """Can we generate real images right now?"""
        return self.has_nvidia and not self.nvidia_image_broken

    @property
    def has_audio_generation(self) -> bool:
        """Can we generate real narration audio right now?"""
        return self.has_nvidia and not self.nvidia_audio_broken

    @property
    def has_video_generation(self) -> bool:
        """Can we generate real AI video clips right now?"""
        return bool(self.fal_api_key or self.replicate_api_key)

    @property
    def has_generation(self) -> bool:
        """Can we generate SOME real media right now? Used only for coarse UI banners —
        provider selection must use the per-modality flags above, not this."""
        return self.has_image_generation or self.has_audio_generation

    @property
    def has_asr(self) -> bool:
        """Can the QA gate actually verify? Either path will do."""
        return self.has_gemini or self.has_nvidia

    @property
    def mock_mode(self) -> bool:
        return not self.has_generation

    def missing(self) -> list[str]:
        out = []
        if not self.has_b2:
            out.append("B2 (B2_KEY_ID / B2_APP_KEY / B2_BUCKET)")
        if not self.has_nvidia:
            out.append("NVIDIA (NVIDIA_API_KEY)")
        if not self.has_gemini:
            out.append("Gemini (GEMINI_API_KEY)")
        return out

    def banner(self) -> str:
        """Human-readable status for the UI. Degradation must be visible."""
        if not self.missing():
            return "All providers configured."
        return (
            "Running in demo mode with sample data. No API keys set yet. "
            "Missing: " + "; ".join(self.missing())
        )


@lru_cache(maxsize=1)
def get_config() -> Config:
    data_dir = Path(_env("POLYGLO_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = Path(_env("POLYGLO_DB_PATH", str(data_dir / "polyglo.db"))).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    return Config(
        b2=B2Config(
            key_id=_env("B2_KEY_ID"),
            app_key=_env("B2_APP_KEY"),
            bucket=_env("B2_BUCKET", "polyglo"),
            endpoint=_env("B2_ENDPOINT"),
        ),
        qa=QAConfig(
            wer_pass=_flag("QA_WER_PASS", 0.10),
            wer_retry=_flag("QA_WER_RETRY", 0.25),
            max_attempts=int(_flag("QA_MAX_ATTEMPTS", 3)),
        ),
        gemini=GeminiConfig(
            daily_call_cap=int(_flag("GEMINI_DAILY_CALL_CAP", 50)),
        ),
        openrouter=OpenRouterConfig(
            daily_call_cap=int(_flag("OPENROUTER_DAILY_CALL_CAP", 200)),
        ),
        global_=GlobalConfig(
            daily_story_cap=int(_flag("GLOBAL_DAILY_STORY_CAP", 100)),
        ),
        nvidia_api_key=_env("NVIDIA_API_KEY"),
        gemini_api_key=_env("GEMINI_API_KEY"),
        openrouter_api_key=_env("OPENROUTER_API_KEY"),
        admin_key=_env("POLYGLO_ADMIN_KEY"),
        fal_api_key=_env("FAL_API_KEY"),
        replicate_api_key=_env("REPLICATE_API_KEY"),
        data_dir=data_dir,
        db_path=db_path,
    )


def reset_config_cache() -> None:
    """Tests mutate the environment; let them re-read it."""
    get_config.cache_clear()
