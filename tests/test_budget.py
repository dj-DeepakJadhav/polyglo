"""Tests for the Gemini daily call budget.

The user asked explicitly for a daily cap on Gemini usage. These tests exist to prove
the cap actually blocks calls rather than just logging a warning, and that it survives
a process restart within the same day (persisted to disk, not in-memory only).
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from polyglo.qa.budget import BudgetExceeded, GeminiBudget


@pytest.fixture()
def path(tmp_path):
    return tmp_path / "gemini_budget.json"


def test_starts_at_zero(path):
    b = GeminiBudget(cap=10, path=path)
    assert b.used() == 0
    assert b.remaining() == 10


def test_spend_increments_and_returns_running_total(path):
    b = GeminiBudget(cap=10, path=path)
    assert b.spend() == 1
    assert b.spend(3) == 4
    assert b.used() == 4
    assert b.remaining() == 6


def test_cap_is_a_hard_gate_not_a_warning(path):
    """The whole point: calls past the cap must raise, not silently proceed."""
    b = GeminiBudget(cap=3, path=path)
    b.spend(3)
    with pytest.raises(BudgetExceeded):
        b.spend(1)
    assert b.used() == 3        # the rejected call must not be counted


def test_would_exceed_predicts_without_spending(path):
    b = GeminiBudget(cap=5, path=path)
    b.spend(4)
    assert b.would_exceed(1) is False
    assert b.would_exceed(2) is True
    assert b.used() == 4        # unchanged — a check, not a spend


def test_exceeding_leaves_state_unmodified(path):
    b = GeminiBudget(cap=2, path=path)
    b.spend(2)
    with pytest.raises(BudgetExceeded):
        b.spend(5)
    assert b.used() == 2


# ---------------------------------------------------------------------------
# Persistence across process restarts
# ---------------------------------------------------------------------------


def test_state_persists_across_instances(path):
    """A restarted process must not get a fresh budget mid-day."""
    GeminiBudget(cap=10, path=path).spend(4)
    reloaded = GeminiBudget(cap=10, path=path)
    assert reloaded.used() == 4


def test_persisted_file_is_plain_json(path):
    GeminiBudget(cap=10, path=path).spend(2)
    data = json.loads(path.read_text())
    assert data["count"] == 2
    assert data["day"] == date.today().isoformat()


def test_missing_file_starts_fresh(path):
    assert not path.exists()
    assert GeminiBudget(cap=10, path=path).used() == 0


def test_corrupt_file_fails_open_to_zero_not_to_a_crash(path):
    """A corrupted budget file must degrade to zero-used, not raise on every call."""
    path.write_text("{not valid json")
    assert GeminiBudget(cap=10, path=path).used() == 0


# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------


def test_a_new_day_resets_the_counter(path):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({"day": yesterday, "count": 9}))
    b = GeminiBudget(cap=10, path=path)
    assert b.used() == 0
    assert b.spend() == 1


def test_cap_can_be_raised_between_instances(path):
    GeminiBudget(cap=2, path=path).spend(2)
    looser = GeminiBudget(cap=100, path=path)
    assert looser.remaining() == 98
