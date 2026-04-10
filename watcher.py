#!/usr/bin/env python3
"""Run uvicorn and restart the server when files under boardman/ change.

Used as the single docker compose entrypoint (see Dockerfile + docker-compose).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _should_restart(changes: set) -> bool:
    for _, path_str in changes:
        p = path_str.lower()
        if "__pycache__" in p or p.endswith(".pyc"):
            continue
        if p.endswith(".py"):
            return True
    return False


def main() -> None:
    root = Path(__file__).resolve().parent
    watch_dir = root / "boardman"
    if not watch_dir.is_dir():
        print(f"watcher: missing directory {watch_dir}", file=sys.stderr)
        sys.exit(1)

    host = os.environ.get("SERVICE_HOST", "0.0.0.0")
    port = os.environ.get("SERVICE_PORT", "8090")
    log_level = os.environ.get("LOG_LEVEL", "info").lower()

    argv = [
        sys.executable,
        "-m",
        "uvicorn",
        "boardman.main:app",
        "--host",
        host,
        "--port",
        port,
        "--log-level",
        log_level,
    ]

    try:
        from watchfiles import watch
    except ImportError as e:
        print("watcher: install watchfiles (included with uvicorn[standard])", file=sys.stderr)
        raise SystemExit(1) from e

    proc: subprocess.Popen[bytes] | None = None

    def stop() -> None:
        nonlocal proc
        if proc is None or proc.poll() is not None:
            proc = None
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proc = None

    def start() -> None:
        nonlocal proc
        print(f"watcher: starting {' '.join(argv[1:])}", flush=True)
        proc = subprocess.Popen(argv, cwd=str(root))

    def handle_signal(signum: int, _frame: object) -> None:
        stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    start()
    try:
        for changes in watch(
            watch_dir,
            debounce=400,
            raise_interrupt=False,
            stop_event=None,
        ):
            if not _should_restart(changes):
                continue
            print(f"watcher: code change, restarting ({len(changes)} path(s))", flush=True)
            stop()
            time.sleep(0.1)
            start()
    finally:
        stop()


if __name__ == "__main__":
    main()
