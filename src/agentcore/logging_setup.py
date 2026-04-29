"""structlog configuration. Call `configure_logging()` once at process start."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "info") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )
    # Botocore logs credential-token discovery at INFO. That is useful
    # for AWS SDK debugging but noisy in normal agent runs, especially
    # when Bedrock bearer-token auth is configured.
    logging.getLogger("botocore.tokens").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )
