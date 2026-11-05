"""Timestamped directory backups with rotation."""

import shutil
from datetime import datetime
from pathlib import Path


def backup_dir(src: Path, dest_parent: Path, prefix: str, keep: int, log) -> Path:
    dest = dest_parent / f"{prefix}{datetime.now():%Y%m%d_%H%M%S}"
    log(f"Creating backup at {dest}")
    shutil.copytree(src, dest, symlinks=True)
    rotate(dest_parent, prefix, keep, log)
    return dest


def rotate(parent: Path, prefix: str, keep: int, log) -> None:
    backups = sorted(p for p in parent.glob(f"{prefix}*") if p.is_dir())
    for old in backups[: max(0, len(backups) - keep)]:
        log(f"Removing old backup: {old}")
        shutil.rmtree(old)
