"""Runtime chaos toggle for the failover demo.

`/api/chaos/{provider}/disable` (docs/02 §9) needs some in-process, per-process state
that a running pipeline consults live, so a demo can kill a "provider" mid-run and watch
Genblaze's ``fallback_models`` chain — or, upstream of that, the QA gate's own
alternate-voice/escalation ladder — actually recover on camera.

This is a legitimate testing affordance, not a hack: it's the only way to make provider
failure demonstrable without waiting for (or faking) a real outage.
"""

from __future__ import annotations

import threading

__all__ = ["ChaosRegistry"]


class ChaosRegistry:
    """Thread-safe set of model identifiers currently forced to fail.

    Shared by reference between the API layer (which toggles it) and whichever
    ``SimulatedNarrator``/``SimulatedVisualGenerator`` instances the orchestrator
    constructs for a run — constructed fresh per run with ``registry.disabled`` passed
    in as ``fail_models``, so toggling mid-run affects only attempts that haven't
    started their model call yet, matching how a real provider outage would behave.
    """

    def __init__(self) -> None:
        self._disabled: set[str] = set()
        self._lock = threading.Lock()

    def disable(self, model: str) -> None:
        with self._lock:
            self._disabled.add(model)

    def enable(self, model: str) -> None:
        with self._lock:
            self._disabled.discard(model)

    def is_disabled(self, model: str) -> bool:
        with self._lock:
            return model in self._disabled

    def snapshot(self) -> list[str]:
        with self._lock:
            return sorted(self._disabled)

    def reset(self) -> None:
        with self._lock:
            self._disabled.clear()
