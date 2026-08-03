"""Daily call budget — originally Gemini-specific, now a generic reusable primitive.

The user asked explicitly to keep Gemini usage capped on a daily basis — not just cost,
a call-count limit. This is a hard gate: once the cap is hit, ``spend()`` raises rather
than letting a caller silently keep going, and the counter persists to disk so
restarting the process does not reset it mid-day.

Originally NOT applied to NVIDIA — that budget is credits, tracked upstream, and
governed by `docs/04`'s "measure cost first" rule instead. Now also reused for
OpenRouter (a genuinely pay-per-use vendor, unlike NVIDIA's free tier) and for a
GLOBAL daily story-creation cap (`polyglo/api.py`) — the per-IP rate limiter
(`polyglo/ratelimit.py`) bounds one client's rate, but doesn't bound the total
across every distinct visitor combined; this is the complementary aggregate cap.
``GeminiBudget`` is kept as an alias so every existing import/call site (and its
own tests) is unaffected by the rename.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

__all__ = ["BudgetExceeded", "DailyCallBudget", "GeminiBudget"]


class BudgetExceeded(RuntimeError):
    def __init__(self, used: int, cap: int, *, label: str = "Daily"):
        super().__init__(
            f"{label} call cap reached ({used}/{cap}). "
            f"Raise the configured cap or wait until tomorrow."
        )
        self.used = used
        self.cap = cap


@dataclass
class _State:
    day: str
    count: int


class DailyCallBudget:
    """Thread-safe, disk-persisted counter of calls for the current day.

    One instance is normally shared for the process lifetime; construct with the same
    ``path`` to share state across processes (the FastAPI app and any CLI script).
    """

    def __init__(self, cap: int, path: Path | str, *, label: str = "Daily"):
        self.cap = cap
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._label = label

    def _today(self) -> str:
        return date.today().isoformat()

    def _read(self) -> _State:
        if not self.path.is_file():
            return _State(self._today(), 0)
        try:
            data = json.loads(self.path.read_text())
            state = _State(data.get("day", ""), int(data.get("count", 0)))
        except (json.JSONDecodeError, ValueError, OSError):
            return _State(self._today(), 0)
        if state.day != self._today():
            return _State(self._today(), 0)   # new day, fresh budget
        return state

    def _write(self, state: _State) -> None:
        self.path.write_text(json.dumps({"day": state.day, "count": state.count}))

    def used(self) -> int:
        with self._lock:
            return self._read().count

    def remaining(self) -> int:
        return max(0, self.cap - self.used())

    def spend(self, n: int = 1) -> int:
        """Consume ``n`` calls. Raises :class:`BudgetExceeded` if that would exceed the
        cap — checked and written atomically under the lock so concurrent callers
        cannot race past the limit."""
        with self._lock:
            state = self._read()
            if state.count + n > self.cap:
                raise BudgetExceeded(state.count, self.cap, label=self._label)
            state.count += n
            self._write(state)
            return state.count

    def would_exceed(self, n: int = 1) -> bool:
        return self.used() + n > self.cap


# Alias: every pre-existing call site (orchestrator.py, tests) constructs/imports
# this under the original name. Same class, same behavior — the rename above is
# additive, not a breaking change.
GeminiBudget = DailyCallBudget
