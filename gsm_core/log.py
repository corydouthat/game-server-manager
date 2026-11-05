"""Timestamped logging to stdout (journald under systemd) and a per-instance file."""

from datetime import datetime
from pathlib import Path


class Log:
    def __init__(self, instance: str, path: Path):
        self.instance = instance
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        try:
            with open(self.path, "a") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"(could not write log file {self.path}: {e})", flush=True)
