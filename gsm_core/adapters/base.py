"""GameAdapter: the per-game interface. Generic behaviour lives here and in monitor.py;
adapters override only what differs for their game."""

import json
from pathlib import Path

from .. import config


class GameAdapter:
    #: seconds between reconcile() calls in monitor mode; None disables
    reconcile_interval: int | None = None
    #: how long to wait after start() before is_running() must be true
    start_grace = 8

    def __init__(self, cfg: dict, log, notifier):
        self.cfg = cfg
        self.instance = cfg["name"]
        self.log = log
        self.notifier = notifier
        self.state = config.state_dir(self.instance)

    # ── helpers ──────────────────────────────────────────────────────────────
    def notify(self, text: str, **kw) -> None:
        self.notifier.send(self.instance, text, **kw)

    def _state(self) -> dict:
        try:
            return json.loads((self.state / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self, **updates) -> None:
        s = self._state()
        s.update(updates)
        (self.state / "state.json").write_text(json.dumps(s, indent=1))

    @property
    def marker_dir(self) -> Path:
        """Where .last_update lives (game dirs keep it for continuity with the bash managers)."""
        return self.state

    @property
    def hold_file(self) -> Path:
        return self.state / "hold"

    # ── required ────────────────────────────────────────────────────────────
    def is_running(self) -> bool:
        raise NotImplementedError

    def start(self) -> bool:
        raise NotImplementedError

    def stop(self) -> bool:
        raise NotImplementedError

    # ── optional ────────────────────────────────────────────────────────────
    def players_online(self) -> int | None:
        """Player count, or None if this game can't report it."""
        return None

    def current_version(self) -> str:
        return "unknown"

    def latest_version(self) -> str:
        """Latest available version. Raise on failure."""
        raise NotImplementedError

    def install_update(self, latest: str) -> None:
        """Install `latest` while the server is stopped. Raise on failure."""
        raise NotImplementedError

    def post_start(self) -> None:
        pass

    def reconcile(self) -> None:
        """Periodic per-game fix-ups (monitor mode)."""

    def health_check(self) -> None:
        """Extra per-tick checks while the server is believed running (monitor mode)."""

    def extra_commands(self) -> dict:
        """{name: (help, func(args: list[str]) -> int)}"""
        return {}

    def status_lines(self) -> list[str]:
        return [f"version: {self.current_version()}"]

    # ── generic update flow ─────────────────────────────────────────────────
    def check_update(self) -> tuple[str, str]:
        latest = self.latest_version()
        current = self.current_version()
        self.log(f"Installed: {current}")
        self.log(f"Latest:    {latest}")
        return current, latest

    def daily_update(self) -> None:
        """Called once per day by the monitor (after the day has been recorded)."""
        try:
            current, latest = self.check_update()
        except Exception as e:  # noqa: BLE001
            fails = self._state().get("update_check_failures", 0) + 1
            self._save_state(update_check_failures=fails)
            self.log(f"Update check failed ({e}); will retry tomorrow")
            if fails >= 3:
                self.notify(f"Update check has failed {fails} days running: {e}", dedupe=20 * 3600)
            return
        self._save_state(update_check_failures=0)

        if current == latest:
            self.log("Already up to date — no restart needed")
            return

        players = self.players_online()
        if players:
            self.log(f"{players} player(s) online — deferring update {current} -> {latest} until tomorrow")
            self.notify(f"Update {current} -> {latest} deferred: {players} player(s) online")
            return

        self.apply_update(current, latest)

    def apply_update(self, current: str, latest: str) -> bool:
        self.log(f"Update available: {current} -> {latest}")
        self.stop()
        ok = True
        try:
            self.install_update(latest)
            self.log(f"Update complete: {current} -> {latest}")
        except Exception as e:  # noqa: BLE001
            ok = False
            self.log(f"ERROR: update failed: {e}")
            self.notify(f"Update {current} -> {latest} FAILED: {e}. Restarting old version.")
        started = self.start()
        if ok and started:
            self.notify(f"Updated {current} -> {latest}", dedupe=0)
        elif not started:
            self.notify("Server failed to start after update attempt", dedupe=0)
        return ok and started

    def manual_update(self) -> int:
        current, latest = self.check_update()
        if current == latest:
            self.log("Already on latest version")
            return 0
        return 0 if self.apply_update(current, latest) else 1
