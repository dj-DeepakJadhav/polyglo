"""Structured logging setup.

Before this module, the app had no logging configuration at all — real pipeline
errors were only visible by reconstructing state from telemetry/DB rows after the
fact, and rate-limit/budget rejections left no trace anywhere. This is a
deliberately small addition: configure the standard library's own `logging`
module once, at import time, and add log calls at the handful of places that
already catch exceptions or reject a request — no new error-handling paths, no
new dependencies (structlog/loguru would be reasonable choices but aren't needed
for what this app actually requires right now).

Format is plain text with a timestamp, level, and logger name — not JSON. Render
and most PaaS log viewers already timestamp and index stdout lines themselves;
JSON would help for shipping to something like Datadog/CloudWatch Insights, but
adding a log-shipping destination is a real, separate infrastructure decision
(see docs/08-PRODUCTION-ROADMAP.md), not something to bake in silently here.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent — safe to call from multiple entry points (api.py, web.py, any
    future CLI) without double-registering handlers."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    ))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    # Quiet down noisy third-party loggers that would otherwise dominate output
    # at INFO level without adding much signal.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
