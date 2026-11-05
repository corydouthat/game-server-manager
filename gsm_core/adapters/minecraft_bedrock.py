"""Minecraft Bedrock Dedicated Server."""

import re
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path

from .. import net
from .minecraft_common import MinecraftAdapter

LINKS_API = "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"
DOWNLOAD_TYPE = "serverBedrockLinux"
# Never overwritten by an update.
PRESERVE = {"server.properties", "allowlist.json", "permissions.json", "worlds",
            "packetlimitconfig.json", "profanity_filter.wlist"}
# 1.26.61+: NetherNet is the only transport; this console error means nobody can join.
TRANSPORT_ERROR = "Players will not be able to connect"
UDP_PORTS_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d+):(\d+)$")


class Adapter(MinecraftAdapter):
    reconcile_interval = 600
    start_grace = 5

    def __init__(self, cfg, log, notifier):
        super().__init__(cfg, log, notifier)
        self.binary = cfg.get("server_binary", "bedrock_server")
        # Keep server-udp-ports' public IP in sync with the home IP (needed when
        # players connect through a port forward with transport=nethernet).
        self.sync_public_ip = cfg.get("sync_public_ip", True)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def preflight(self) -> bool:
        binary = self.server_dir / self.binary
        if not binary.exists():
            self.log(f"ERROR: Server binary not found at {binary}. Run 'gsm {self.instance} update'.")
            return False
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        return True

    def launch_command(self) -> str:
        return f"LD_LIBRARY_PATH=. ./{self.binary}"

    # ── updates ─────────────────────────────────────────────────────────────
    def _latest_url(self) -> str:
        data = net.fetch_json(LINKS_API, timeout=15)
        for link in data.get("result", {}).get("links", []):
            if link.get("downloadType") == DOWNLOAD_TYPE:
                return link["downloadUrl"]
        raise RuntimeError(f"no {DOWNLOAD_TYPE} link in Mojang API response")

    @staticmethod
    def _version_from_url(url: str) -> str:
        m = re.search(r"bedrock-server-([0-9.]+)", url)
        if not m:
            raise RuntimeError(f"can't parse version from {url}")
        return m.group(1).rstrip(".")

    def latest_version(self) -> str:
        return self._version_from_url(self._latest_url())

    def install_update(self, latest: str) -> None:
        url = self._latest_url()
        if self._version_from_url(url) != latest:
            raise RuntimeError("latest version changed mid-update; will retry next check")
        self.backup_server()
        with tempfile.TemporaryDirectory(prefix="gsm-bedrock-") as tmp:
            zpath = Path(tmp) / "bedrock-server.zip"
            self.log(f"Downloading version {latest}...")
            net.download(url, zpath)
            if zpath.stat().st_size < 1000:
                raise RuntimeError(f"downloaded file looks invalid ({zpath.stat().st_size} bytes)")
            extracted = Path(tmp) / "extracted"
            with zipfile.ZipFile(zpath) as z:
                z.extractall(extracted)
            self.log("Installing (preserving configuration)...")
            for src in extracted.iterdir():
                if src.name in PRESERVE:
                    continue
                dest = self.server_dir / src.name
                if src.is_dir():
                    shutil.copytree(src, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dest)
        self.version_file.write_text(latest + "\n")

    # ── NetherNet public IP sync ───────────────────────────────────────────
    def _udp_ports_ip(self) -> tuple[str | None, str | None]:
        """(current advertised IP or None, raw server-udp-ports value)."""
        raw = self.props.get("server-udp-ports")
        m = UDP_PORTS_RE.match(raw or "")
        return (m.group(1) if m else None), raw

    def sync_ip(self, restart: bool = True) -> bool:
        """Rewrite server-udp-ports if the public IP changed. Returns True if it changed."""
        if (self.props.get("transport") or "nethernet") != "nethernet":
            return False
        public = net.public_ipv4()
        if not public:
            self.log("WARNING: could not determine public IP; skipping server-udp-ports sync")
            return False
        current, raw = self._udp_ports_ip()
        if current == public:
            return False
        if current is None and raw and not raw.isdigit():
            self.log(f"server-udp-ports={raw} isn't a single port or IP:port:port; not touching it")
            return False
        if current:
            _, pub_port, local_port = UDP_PORTS_RE.match(raw).groups()
        else:
            pub_port = local_port = raw or self.props.get("server-port") or "19132"
        new = f"{public}:{pub_port}:{local_port}"
        self.props.set("server-udp-ports", new)
        self.log(f"Public IP changed {current} -> {public}; set server-udp-ports={new}")
        self._save_state(ip_restart_pending=True)
        self.notify(f"Public IP changed {current} -> {public}. Updated server-udp-ports; "
                    "restarting when no players are online.", dedupe=0)
        if restart:
            self._restart_if_pending()
        return True

    def _restart_if_pending(self) -> None:
        if not self._state().get("ip_restart_pending"):
            return
        if not self.is_running():
            self._save_state(ip_restart_pending=False)  # next start picks it up
            return
        players = self.players_online()
        if players:
            self.log(f"{players} player(s) online — IP-change restart postponed")
            return
        self.log("Restarting to apply new server-udp-ports")
        self.stop()
        time.sleep(5)
        if self.start():
            self._save_state(ip_restart_pending=False)
            self.notify("Restarted with new public IP — outside joins should work again", dedupe=0)

    def reconcile(self) -> None:
        if self.sync_public_ip:
            self.sync_ip()

    def post_start(self) -> None:
        super().post_start()
        # The transport error is printed once during startup.
        if TRANSPORT_ERROR in self.screen.console(scrollback=True):
            self.log(f"ERROR: console says '{TRANSPORT_ERROR}' — check transport= in server.properties")
            self.notify(f"Console says '{TRANSPORT_ERROR}' — check transport= in server.properties",
                        dedupe=6 * 3600, key=f"{self.instance}-transport-error")

    def health_check(self) -> None:
        # A pending IP-change restart is retried every tick until players leave.
        self._restart_if_pending()

    def extra_commands(self) -> dict:
        def sync(_args):
            changed = self.sync_ip()
            if not changed:
                ip, raw = self._udp_ports_ip()
                self.log(f"server-udp-ports={raw} already matches public IP" if ip else f"No change (server-udp-ports={raw})")
            return 0
        return {"sync-ip": ("Sync server-udp-ports with the current public IP (restarts if empty)", sync)}

    def status_lines(self) -> list[str]:
        lines = super().status_lines()
        lines.append(f"transport: {self.props.get('transport') or 'nethernet (default)'}  "
                     f"server-udp-ports: {self.props.get('server-udp-ports') or '(ephemeral)'}")
        return lines
