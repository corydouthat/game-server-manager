"""Generic monitor loop: crash restart (with loop detection), daily updates, reconcile hooks.

Signals:
  SIGTERM (systemctl stop / shutdown) -> stop the game gracefully, then exit 0
  SIGINT  (Ctrl-C in a terminal)      -> exit, leave the game running
  SIGHUP  (gsm <inst> reload-monitor)  -> exit 75, leave the game running (systemd restarts the monitor)
"""

import fcntl
import signal
import sys
import threading
import time
import traceback
from datetime import datetime

RESTART_WINDOW = 15 * 60
MAX_RESTARTS = 3
BACKOFF = 30 * 60


class Monitor:
    def __init__(self, adapter):
        self.a = adapter
        self.cfg = adapter.cfg
        self.log = adapter.log
        self.wake = threading.Event()
        self.exit_mode: str | None = None
        self.restarts: list[float] = []
        self.backoff_until = 0.0
        self.next_reconcile = time.time() + 60

    # ── signals ─────────────────────────────────────────────────────────────
    def _on_signal(self, signum, _frame):
        self.exit_mode = {signal.SIGTERM: "stop", signal.SIGINT: "detach", signal.SIGHUP: "reload"}[signum]
        self.wake.set()

    def _acquire_lock(self):
        self._lock = open(self.a.state / "monitor.lock", "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.log(f"ERROR: another monitor is already running for {self.a.instance}; exiting")
            sys.exit(0)  # clean exit so systemd doesn't restart-loop us

    # ── main loop ───────────────────────────────────────────────────────────
    def run(self) -> int:
        self._acquire_lock()
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, self._on_signal)

        self.log(f"Starting monitor for {self.a.instance} ({self.cfg['adapter']})")
        if not self.cfg["auto_update"]:
            self.log(f"Auto-update is disabled. Run 'gsm {self.a.instance} update' to update manually.")
        try:
            boot_id = open("/proc/sys/kernel/random/boot_id").read().strip()
        except OSError:
            boot_id = ""
        self.a.notify("Monitor started", dedupe=7 * 86400, key=f"{self.a.instance}-start-{boot_id}")

        try:
            if self.a.hold_file.exists():
                self.log("Server is on hold (stopped with 'gsm stop'); not starting. Use 'gsm start'.")
            elif not self.a.start():
                self.restarts.append(time.time())
            while not self.exit_mode:
                self.wake.wait(self.cfg["check_interval"])
                if self.exit_mode:
                    break
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001
                    self.log("ERROR in monitor tick:\n" + traceback.format_exc())
                    self.a.notify(f"Monitor error: {type(e).__name__}: {e}")
        except Exception as e:
            self.log("FATAL: monitor crashed:\n" + traceback.format_exc())
            self.a.notify(f"Monitor crashed: {type(e).__name__}: {e} (systemd will restart it)", dedupe=0)
            raise

        if self.exit_mode == "stop":
            self.log("Received SIGTERM — stopping server")
            self.a.stop()
            return 0
        if self.exit_mode == "reload":
            self.log("Received SIGHUP — exiting monitor, server left running")
            return 75
        self.log("Monitor exiting, server left running")
        return 0

    def tick(self) -> None:
        if self.a.hold_file.exists():
            return
        if self.a.is_running():
            self.a.health_check()
        else:
            self.handle_down()
        self.maybe_daily_update()
        if self.a.reconcile_interval and time.time() >= self.next_reconcile:
            self.next_reconcile = time.time() + self.a.reconcile_interval
            self.a.reconcile()

    def handle_down(self) -> None:
        now = time.time()
        if now < self.backoff_until:
            return
        self.restarts = [t for t in self.restarts if now - t < RESTART_WINDOW]
        if len(self.restarts) >= MAX_RESTARTS:
            self.backoff_until = now + BACKOFF
            self.restarts.clear()
            self.log(f"ERROR: restart loop ({MAX_RESTARTS} restarts in {RESTART_WINDOW // 60} min); "
                     f"backing off {BACKOFF // 60} min")
            self.a.notify(f"Restart loop: server failed {MAX_RESTARTS}x in {RESTART_WINDOW // 60} min. "
                          f"Backing off {BACKOFF // 60} min. Check the log.", dedupe=0)
            return
        self.log("WARNING: Server not running, restarting...")
        self.restarts.append(now)
        ok = self.a.start()
        self.a.notify("Server was down — restarted OK" if ok else "Server was down — restart FAILED",
                      dedupe=0 if not ok else 600)

    def maybe_daily_update(self) -> None:
        if not self.cfg["auto_update"]:
            return
        marker = self.a.marker_dir / ".last_update"
        today = datetime.now().strftime("%Y%m%d")
        try:
            if marker.read_text().strip() == today:
                return
        except OSError:
            pass
        hh, mm = (int(x) for x in str(self.cfg["update_check_time"]).split(":"))
        now = datetime.now()
        if now.hour * 60 + now.minute < hh * 60 + mm:
            return
        self.log("Daily update check triggered")
        # Record the day *before* checking: if nothing gets installed (already
        # current, API error) the guard above must still trip, or every tick
        # re-runs the check and restarts the server (the old bootloop bug).
        marker.write_text(today + "\n")
        self.a.daily_update()
