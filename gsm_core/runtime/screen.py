"""GNU screen runtime: a server process inside a detached, attachable screen session."""

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


class ScreenRuntime:
    def __init__(self, session: str, instance: str):
        self.session = session
        self.instance = instance

    def is_running(self) -> bool:
        out = subprocess.run(["screen", "-list"], capture_output=True, text=True).stdout
        return re.search(rf"^\s*\d+\.{re.escape(self.session)}\s", out, re.M) is not None

    def start(self, command: str, cwd: Path, env: dict | None = None) -> None:
        screen_cmd = ["screen", "-dmS", self.session, "bash", "-c", command]
        full_env = {**os.environ, **(env or {})}
        # Under systemd, put the game in its own scope so a monitor crash/restart
        # (which kills the monitor's cgroup) doesn't take the game server with it.
        if os.environ.get("INVOCATION_ID") and shutil.which("systemd-run"):
            scope = f"gsm-{self.instance}-game-{int(time.time())}"
            r = subprocess.run(
                ["systemd-run", "--user", "--scope", "--quiet", f"--unit={scope}", *screen_cmd],
                cwd=cwd, env=full_env,
            )
            if r.returncode == 0:
                return
        subprocess.run(screen_cmd, cwd=cwd, env=full_env, check=True)

    def send(self, line: str) -> None:
        subprocess.run(["screen", "-S", self.session, "-p", "0", "-X", "stuff", f"{line}\r"], check=False)

    def console(self, scrollback: bool = False) -> str:
        """Snapshot of the session's screen (optionally including scrollback)."""
        fd, path = tempfile.mkstemp(prefix="gsm-hardcopy-")
        os.close(fd)
        try:
            args = ["hardcopy", "-h", path] if scrollback else ["hardcopy", path]
            subprocess.run(["screen", "-S", self.session, "-p", "0", "-X", *args], check=False)
            time.sleep(0.5)
            return Path(path).read_text(errors="replace")
        finally:
            os.unlink(path)

    def quit(self) -> None:
        subprocess.run(["screen", "-S", self.session, "-X", "quit"], check=False)

    def stop(self, stop_command: str, log, timeout: int = 60) -> bool:
        if not self.is_running():
            log("Server is not running")
            return True
        log("Stopping server gracefully...")
        self.send(stop_command)
        for _ in range(timeout):
            if not self.is_running():
                log("Server stopped")
                return True
            time.sleep(1)
        log("WARNING: Force-killing server")
        self.quit()
        time.sleep(2)
        return not self.is_running()
