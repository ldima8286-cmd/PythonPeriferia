"""Logging setup shared by the daemon and the CLI."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import LogConfig

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def setup(cfg: LogConfig) -> None:
    level = LEVELS.get(cfg.level.lower(), logging.INFO)
    fmt = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
    datefmt = "%H:%M:%S"

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(stream)

    if cfg.file:
        path = Path(cfg.file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(logging.Formatter(fmt, datefmt))
        root.addHandler(rotating)
