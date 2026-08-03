"""In-memory rate limiting for the app's real-money endpoints.

Why this exists: the app is a single, publicly reachable Render instance with real
NVIDIA/Gemini/OpenRouter credentials behind it. Before this module, POST /stories
(and its JSON twin, POST /api/stories) had no limit at all — anyone who found the
URL could trigger unlimited real pipeline runs, each one a real chat call, real
image generation, and real narration+ASR calls, burning through real spend and
Gemini's own daily call budget with no ceiling. This is a deliberately simple,
single-process, in-memory limiter (a sliding window per client IP) rather than a
Redis-backed one — this app already assumes a single instance elsewhere (SQLite,
in-memory ChaosRegistry/progress log), so a distributed limiter would be new
architecture solving a problem this deployment doesn't have yet. If the app ever
runs multiple instances, this needs to move to a shared store (Redis/B2-backed
counter, same shape as qa/budget.py's GeminiBudget) — noted, not solved here.
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request

__all__ = ["RateLimiter", "client_ip"]


def client_ip(request: Request) -> str:
    """Render (and most PaaS hosts) sit behind a reverse proxy — request.client.host
    would be the proxy's own address for every request without this."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """A plain sliding-window limiter: at most `max_requests` calls to `check()`
    for the same key within any `window_seconds` window. Thread-safe (FastAPI's
    default sync-route threadpool means concurrent requests are real here, not
    theoretical)."""

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] < cutoff:
                hits.pop(0)
            if len(hits) >= self.max_requests:
                retry_after = max(1, int(hits[0] + self.window_seconds - now))
                raise HTTPException(
                    429,
                    f"Rate limit exceeded: max {self.max_requests} requests per "
                    f"{int(self.window_seconds)}s. Try again in ~{retry_after}s.",
                    headers={"Retry-After": str(retry_after)},
                )
            hits.append(now)

    def reset(self) -> None:
        """Tests must call this (or construct a fresh instance) between runs —
        without it, this process-level singleton's hit history leaks across
        every test in the suite that hits a rate-limited route, exactly the kind
        of shared-mutable-singleton gotcha this project's ChaosRegistry/progress
        log already needed a per-test reset for."""
        with self._lock:
            self._hits.clear()
