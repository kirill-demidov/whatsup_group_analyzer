"""
Детализированное логирование для whatsapp-teachers.
Уровень: LOG_LEVEL (DEBUG, INFO, WARNING, ERROR). Вывод: в stderr или в файл (LOG_PATH).
"""
import logging
import os
import sys
from pathlib import Path

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_PATH = os.environ.get("LOG_PATH", "").strip()

def _setup_logger() -> logging.Logger:
    root = logging.getLogger("whatsapp-teachers")
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    if root.handlers:
        return root

    fmt = "%(asctime)s [%(levelname)s] %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    root.addHandler(stderr)

    if LOG_PATH:
        path = Path(LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(formatter)
        root.addHandler(fh)

    return root


logger = _setup_logger()


def get_logger(name: str) -> logging.Logger:
    """Логгер для подмодуля, например get_logger('sheets')."""
    return logging.getLogger(f"whatsapp-teachers.{name}")
