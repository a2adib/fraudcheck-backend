"""
Centralized logging configuration using structlog.

- LOCAL/TESTING: colored, human-readable console output
- STAGING/PRODUCTION: JSON structured logging for log aggregation

FR-14.5 forbids credentials, tokens, and full phone numbers ever reaching a log line.
That is enforced at the call site (mask with :func:`src.common.utils.mask_phone`) and
asserted by a test that scans the captured log output for the whole suite.
"""

import logging
import sys

import structlog

from src.constants import Environment


def setup_logging(environment: Environment) -> None:
    """
    Configure structlog and stdlib logging for the entire application.

    Call once at startup (before any logger is used).
    """
    is_local = environment in (Environment.LOCAL, Environment.TESTING)
    log_level = logging.DEBUG if is_local else logging.INFO

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer() if is_local else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Quiet down noisy third-party loggers.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if environment == Environment.LOCAL else logging.WARNING
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
