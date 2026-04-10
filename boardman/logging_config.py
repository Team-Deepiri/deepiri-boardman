"""Console logging for boardman (works alongside uvicorn's handlers)."""

from __future__ import annotations

import logging
import sys

from boardman.settings import settings


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO

    root = logging.getLogger()
    # Honor LOG_LEVEL so boardman.* INFO is not dropped when root was left at WARNING
    root.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        root.addHandler(h)

    logging.getLogger("boardman").setLevel(level)

    lc = logging.getLogger("langchain")
    lc.setLevel(level)
    if level == logging.INFO:
        for name in (
            "langchain_core",
            "langchain_ollama",
            "openai",
            "anthropic",
            "google_genai",
            "httpx",
            "httpcore",
        ):
            logging.getLogger(name).setLevel(logging.WARNING)
