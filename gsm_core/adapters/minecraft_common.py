"""Shared pieces for screen-based Minecraft servers (Bedrock and Java)."""

import re
import time
from pathlib import Path

from ..runtime.screen import ScreenRuntime
from .base import GameAdapter

# Bedrock: "There are 0/10 players online:"   Java: "There are 0 of a max of 10 players online:"
PLAYERS_RE = re.compile(r"There are (\d+)(?:/\d+| of a max of \d+) players? online")


class Properties:
    """Minimal server.properties editor that preserves comments and ordering."""

    def __init__(self, path: Path):
        self.path = path

    def get(self, key: str) -> str | None:
        if not self.path.exists():
            return None
        for line in self.path.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
        return None

    def set(self, key: str, value: str) -> None:
        lines = self.path.read_text().splitlines() if self.path.exists() else []
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
        tmp = self.path.with_suffix(".gsm-tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(self.path)


class MinecraftAdapter(GameAdapter):
    stop_command = "stop"
    gamerule_delay = 10

    def __init__(self, cfg, log, notifier):
        super().__init__(cfg, log, notifier)
        self.server_dir = Path(cfg["server_dir"]).expanduser()
        self.screen = ScreenRuntime(cfg["screen_name"], self.instance)
        self.props = Properties(self.server_dir / "server.properties")
        self.version_file = self.server_dir / ".server_version"

    @property
    def marker_dir(self) -> Path:
        return self.server_dir

    def is_running(self) -> bool:
        return self.screen.is_running()

    def stop(self) -> bool:
        return self.screen.stop(self.stop_command, self.log)

    def launch_command(self) -> str:
        raise NotImplementedError

    def preflight(self) -> bool:
        return True

    def start(self) -> bool:
        if self.is_running():
            self.log("Server is already running")
            return True
        if not self.preflight():
            return False
        self.log(f"Starting {self.instance} (version: {self.current_version()})...")
        self.screen.start(self.launch_command(), cwd=self.server_dir)
        time.sleep(self.start_grace)
        if not self.is_running():
            self.log("ERROR: Server failed to start")
            return False
        self.log("Server started successfully")
        self.post_start()
        return True

    def post_start(self) -> None:
        rules = self.cfg.get("gamerules", {})
        if not rules:
            return
        self.log("Applying game rules...")
        time.sleep(self.gamerule_delay)
        for rule, value in rules.items():
            value = str(value).lower() if isinstance(value, bool) else value
            self.log(f"Setting {rule}={value}")
            self.screen.send(f"gamerule {rule} {value}")
            time.sleep(1)
        self.log("Game rules applied")

    def read_console_for_players(self) -> str:
        return self.screen.console()

    def players_online(self) -> int | None:
        if not self.is_running():
            return 0
        self.screen.send("list")
        time.sleep(2)
        counts = PLAYERS_RE.findall(self.read_console_for_players())
        return int(counts[-1]) if counts else None

    def current_version(self) -> str:
        try:
            return self.version_file.read_text().strip() or "unknown"
        except OSError:
            return "unknown"

    def backup_server(self) -> None:
        from ..backup import backup_dir

        backup_dir(
            self.server_dir, self.server_dir.parent, f"{self.server_dir.name}_backup_",
            int(self.cfg["max_backups"]), self.log,
        )

    def status_lines(self) -> list[str]:
        return [f"version: {self.current_version()}", f"screen: {self.screen.session} (attach: screen -r {self.screen.session})"]
