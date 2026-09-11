"""Minimal process logging configuration."""

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(log_level: str) -> None:
    """Configure root output and the package logger at ``log_level``."""

    normalized_level = log_level.upper()
    numeric_level = getattr(logging, normalized_level, None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Unsupported log level: {log_level}")

    logging.basicConfig(level=numeric_level, format=_LOG_FORMAT)
    logging.getLogger("agent_runtime").setLevel(numeric_level)
