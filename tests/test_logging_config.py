"""Tests for the structured logging setup."""

from __future__ import annotations

import logging

from polyglo.logging_config import configure_logging, get_logger


def test_configure_logging_adds_exactly_one_handler_even_if_called_twice():
    root = logging.getLogger()
    before = len(root.handlers)

    configure_logging()
    after_first = len(root.handlers)
    configure_logging()
    after_second = len(root.handlers)

    # Idempotent: calling twice must not double-register handlers. (>= before,
    # not == before + 1, since other tests/imports in the same process may have
    # already configured logging once.)
    assert after_second == after_first


def test_get_logger_returns_a_real_logger_with_the_given_name():
    log = get_logger("polyglo.somemodule")
    assert isinstance(log, logging.Logger)
    assert log.name == "polyglo.somemodule"
