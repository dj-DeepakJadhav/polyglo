"""Tests for the in-memory sliding-window rate limiter.

Route-level tests (a real request actually gets a 429) live in test_api.py /
test_web.py, next to the routes they protect — this file only tests the
standalone RateLimiter/client_ip primitives.
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from polyglo.ratelimit import RateLimiter, client_ip


def test_allows_up_to_max_requests_within_the_window():
    limiter = RateLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        limiter.check("key-a")  # must not raise


def test_raises_429_once_max_requests_exceeded():
    limiter = RateLimiter(max_requests=2, window_seconds=60)
    limiter.check("key-a")
    limiter.check("key-a")
    with pytest.raises(HTTPException) as exc_info:
        limiter.check("key-a")
    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers


def test_different_keys_have_independent_limits():
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    limiter.check("key-a")  # uses up key-a's only slot
    limiter.check("key-b")  # key-b is untouched, must not raise


def test_old_hits_outside_the_window_are_forgotten():
    limiter = RateLimiter(max_requests=1, window_seconds=0.05)
    limiter.check("key-a")
    time.sleep(0.08)
    limiter.check("key-a")  # the first hit has aged out — must not raise


def test_reset_clears_all_recorded_hits():
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    limiter.check("key-a")
    limiter.reset()
    limiter.check("key-a")  # must not raise post-reset


class _FakeRequest:
    def __init__(self, headers: dict, client_host: str | None):
        self.headers = headers
        self.client = type("C", (), {"host": client_host})() if client_host else None


def test_client_ip_prefers_x_forwarded_for():
    req = _FakeRequest({"x-forwarded-for": "203.0.113.5, 10.0.0.1"}, "10.0.0.1")
    assert client_ip(req) == "203.0.113.5"


def test_client_ip_falls_back_to_request_client_host():
    req = _FakeRequest({}, "127.0.0.1")
    assert client_ip(req) == "127.0.0.1"


def test_client_ip_handles_missing_client_gracefully():
    req = _FakeRequest({}, None)
    assert client_ip(req) == "unknown"
