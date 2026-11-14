"""Structured logging for AMPULY system."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_dir: Path | None = None, level: str = "INFO") -> int | None:
    """Configure loguru for AMPULY with file + console output.

    The returned id is the file sink. The caller must pass it to
    ``close_log_file`` when the home is finished, or Windows cannot delete
    ``ampuly.log``.
    """
    logger.remove()

    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - {message}",
        colorize=True,
        # Never print the values on a traceback's lines: they hold API keys in headers and people's text in bodies.
        diagnose=False,
    )

    if log_dir:
        from datetime import date
        day_dir = log_dir / date.today().isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        return logger.add(
            str(day_dir / "ampuly.log"),
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            rotation="50 MB",
            retention="30 days",
            diagnose=False,
        )
    return None


def close_log_file(handler_id: int | None) -> None:
    """Detach one file sink opened by ``setup_logging`` and close its handle."""
    if handler_id is None:
        return
    try:
        logger.remove(handler_id)
    except ValueError:
        return
