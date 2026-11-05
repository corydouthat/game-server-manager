"""HTTP helpers (stdlib urllib)."""

import ipaddress
import json
import shutil
import time
import urllib.request
from pathlib import Path

USER_AGENT = "game-server-manager/1.0"
PUBLIC_IP_SERVICES = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://ifconfig.me/ip",
)


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url: str, timeout: int = 20):
    return json.loads(fetch(url, timeout))


def download(url: str, dest: Path, timeout: int = 300, tries: int = 3) -> None:
    last = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * attempt)
    raise RuntimeError(f"download failed after {tries} tries: {last}")


def public_ipv4() -> str | None:
    """Current public IPv4, requiring two services to agree when possible."""
    answers = []
    for url in PUBLIC_IP_SERVICES:
        try:
            ip = fetch(url, timeout=10).decode().strip()
            ipaddress.IPv4Address(ip)
        except Exception:  # noqa: BLE001
            continue
        answers.append(ip)
        if answers.count(ip) >= 2:
            return ip
    # Only one service answered: trust it rather than report nothing.
    return answers[0] if len(answers) == 1 else None
