"""docker compose runtime."""

import subprocess
from pathlib import Path


class ComposeRuntime:
    def __init__(self, compose_dir: Path, container: str):
        self.compose_dir = compose_dir
        self.container = container
        self.compose_file = compose_dir / "docker-compose.yml"

    def compose(self, *args: str, log=None) -> int:
        r = subprocess.run(
            ["docker", "compose", "-f", str(self.compose_file), *args],
            capture_output=True, text=True,
        )
        if log:
            for line in (r.stdout + r.stderr).strip().splitlines():
                log(f"  {line}")
        return r.returncode

    def inspect(self, fmt: str) -> str | None:
        r = subprocess.run(["docker", "inspect", "-f", fmt, self.container], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def is_running(self) -> bool:
        return self.inspect("{{.State.Running}}") == "true"

    def restart_count(self) -> int | None:
        v = self.inspect("{{.RestartCount}}")
        return int(v) if v and v.isdigit() else None
