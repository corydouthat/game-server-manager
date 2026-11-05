"""RuneScape: Dragonwilds dedicated server (Docker Compose)."""

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ..backup import backup_dir
from ..runtime.compose import ComposeRuntime
from .base import GameAdapter

STEAM_APP_ID = "4019830"


class Adapter(GameAdapter):
    start_grace = 5

    def __init__(self, cfg, log, notifier):
        super().__init__(cfg, log, notifier)
        self.compose_dir = Path(cfg["compose_dir"]).expanduser()
        self.rt = ComposeRuntime(self.compose_dir, cfg.get("container", "runescape-dragonwilds"))
        self.manifest = self.compose_dir / "server-files" / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf"
        self.save_dir = self.compose_dir / "server-files" / "RSDragonwilds" / "Saved" / "SaveGames"

    @property
    def marker_dir(self) -> Path:
        return self.compose_dir

    def is_running(self) -> bool:
        return self.rt.is_running()

    def start(self) -> bool:
        if self.is_running():
            self.log("Server is already running")
            return True
        if not self.rt.compose_file.exists():
            self.log(f"ERROR: docker-compose.yml not found in {self.compose_dir}")
            return False
        self.log("Starting Dragonwilds server...")
        self.rt.compose("up", "-d", log=self.log)
        time.sleep(self.start_grace)
        if self.is_running():
            self.log("Server started successfully")
            self._save_state(restart_count=self.rt.restart_count())
            return True
        self.log(f"ERROR: Server failed to start. Check: docker logs {self.rt.container}")
        return False

    def stop(self) -> bool:
        if not self.is_running():
            self.log("Server is not running")
            return True
        self.log("Stopping server...")
        self.rt.compose("stop", log=self.log)
        if self.is_running():
            self.log("WARNING: Server did not stop cleanly")
            return False
        self.log("Server stopped")
        return True

    def current_version(self) -> str:
        try:
            m = re.search(r'"buildid"\s+"(\d+)"', self.manifest.read_text())
            if m:
                return f"build {m.group(1)}"
        except OSError:
            pass
        # This image doesn't keep a Steam manifest; the server binary's mtime
        # changes whenever Steam updates the game.
        binary = self.compose_dir / "server-files/RSDragonwilds/Binaries/Linux/RSDragonwildsServer-Linux-Shipping"
        try:
            return f"binary {datetime.fromtimestamp(binary.stat().st_mtime):%Y-%m-%d %H:%M}"
        except OSError:
            return "unknown"

    def backup_saves(self) -> None:
        if not self.save_dir.is_dir():
            self.log("No save directory found — skipping backup")
            return
        backup_dir(self.save_dir, self.compose_dir, "dragonwilds-saves-backup_",
                   int(self.cfg["max_backups"]), self.log)

    def daily_update(self) -> None:
        # The image updates itself from Steam on recreate; there's no cheap "latest"
        # check, so do the same nightly backup + pull + recreate as the bash manager.
        before = self.current_version()
        self.log(f"Updating Dragonwilds server (current: {before})...")
        try:
            self.backup_saves()
        except Exception as e:  # noqa: BLE001
            self.log(f"ERROR: save backup failed ({e}); skipping update")
            self.notify(f"Save backup failed, update skipped: {e}")
            return
        self.log("Pulling latest Docker image...")
        if self.rt.compose("pull", log=self.log) != 0:
            self.notify("docker compose pull failed (update skipped)")
            return
        self.log("Restarting with new image...")
        self.rt.compose("up", "-d", "--force-recreate", log=self.log)
        self._save_state(restart_count=None)
        # Steam update happens inside the container after start; report what we can.
        after = self.current_version()
        self.log(f"Update complete ({after})")
        if after != before:
            self.notify(f"Updated {before} -> {after}", dedupe=0)

    def manual_update(self) -> int:
        self.daily_update()
        return 0

    def health_check(self) -> None:
        # restart: unless-stopped hides crashes from a simple "is it running" check.
        count = self.rt.restart_count()
        if count is None:
            return
        last = self._state().get("restart_count")
        if last is not None and count > last:
            self.log(f"WARNING: container restarted by Docker ({last} -> {count})")
            self.notify(f"Container crashed and was restarted by Docker (restart count {count})")
        if last != count:
            self._save_state(restart_count=count)

    def extra_commands(self) -> dict:
        def backup(_a):
            self.backup_saves()
            return 0

        def logs(_a):
            return subprocess.call(["docker", "logs", "--tail=50", "-f", self.rt.container])

        return {"backup": ("Back up save files only", backup),
                "logs": ("Tail container logs (Ctrl+C to exit)", logs)}

    def status_lines(self) -> list[str]:
        return [f"version: {self.current_version()}", f"container: {self.rt.container}  compose: {self.compose_dir}"]
