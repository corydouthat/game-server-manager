"""Minecraft Java Edition server."""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid as uuidlib
from pathlib import Path

from .. import net
from .minecraft_common import MinecraftAdapter

MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


class Adapter(MinecraftAdapter):
    gamerule_delay = 15

    def __init__(self, cfg, log, notifier):
        super().__init__(cfg, log, notifier)
        self.jar = cfg.get("server_jar", "server.jar")
        self.min_ram = cfg.get("min_ram", "2G")
        self.max_ram = cfg.get("max_ram", "8G")

    # ── lifecycle ───────────────────────────────────────────────────────────
    def preflight(self) -> bool:
        if not (self.server_dir / self.jar).exists():
            self.log(f"ERROR: Server JAR not found at {self.server_dir / self.jar}. Run 'gsm {self.instance} update'.")
            return False
        if not shutil.which("java"):
            self.log("ERROR: Java is not installed or not in PATH")
            return False
        v = subprocess.run(["java", "-version"], capture_output=True, text=True).stderr.splitlines()
        self.log(f"Java: {v[0] if v else 'unknown'}")
        return self._check_eula()

    def _check_eula(self) -> bool:
        eula = self.server_dir / "eula.txt"
        if eula.exists() and "eula=true" in eula.read_text():
            return True
        if self.cfg.get("auto_accept_eula", False):
            self.log("Auto-accepting EULA (auto_accept_eula = true): https://www.minecraft.net/en-us/eula")
            text = eula.read_text().replace("eula=false", "eula=true") if eula.exists() else "eula=true\n"
            if "eula=true" not in text:
                text += "eula=true\n"
            eula.write_text(text)
            return True
        self.log("ERROR: Minecraft EULA not accepted. Read https://www.minecraft.net/en-us/eula, then set "
                 "auto_accept_eula = true in the instance config.")
        return False

    def launch_command(self) -> str:
        return f"java -Xms{self.min_ram} -Xmx{self.max_ram} -jar '{self.jar}' nogui"

    def start(self) -> bool:
        ok = super().start()
        if ok:
            self.log(f"*** Clients must be on version {self.current_version()} to connect ***")
        return ok

    def read_console_for_players(self) -> str:
        log = self.server_dir / "logs" / "latest.log"
        try:
            return "\n".join(log.read_text(errors="replace").splitlines()[-30:])
        except OSError:
            return ""

    # ── updates ─────────────────────────────────────────────────────────────
    def _latest_info(self) -> tuple[str, str, str]:
        manifest = net.fetch_json(MANIFEST_URL)
        version = manifest["latest"]["release"]
        meta_url = next(v["url"] for v in manifest["versions"] if v["id"] == version)
        server = net.fetch_json(meta_url)["downloads"]["server"]
        return version, server["url"], server["sha1"]

    def latest_version(self) -> str:
        return self._latest_info()[0]

    def install_update(self, latest: str) -> None:
        version, url, sha1 = self._latest_info()
        if version != latest:
            raise RuntimeError("latest version changed mid-update; will retry next check")
        self.backup_server()
        with tempfile.TemporaryDirectory(prefix="gsm-java-") as tmp:
            jar = Path(tmp) / "server.jar"
            self.log(f"Downloading server JAR for version {latest}...")
            net.download(url, jar)
            actual = hashlib.sha1(jar.read_bytes()).hexdigest()
            if actual != sha1:
                raise RuntimeError(f"SHA1 mismatch (expected {sha1}, got {actual})")
            self.log("SHA1 verified")
            shutil.copy2(jar, self.server_dir / self.jar)
        self.version_file.write_text(latest + "\n")
        self.log(f"*** ALL CLIENTS MUST UPDATE TO VERSION {latest} ***")

    def apply_update(self, current, latest) -> bool:
        ok = super().apply_update(current, latest)
        if ok:
            self.notify(f"Java clients must now use version {latest}", dedupe=0)
        return ok

    # ── whitelist ───────────────────────────────────────────────────────────
    def _wl_path(self) -> Path:
        return self.server_dir / "whitelist.json"

    def _wl_load(self) -> list:
        try:
            return json.loads(self._wl_path().read_text())
        except (OSError, ValueError):
            return []

    def _wl_save(self, data: list) -> None:
        self._wl_path().write_text(json.dumps(data, indent=2) + "\n")

    def _wl_add(self, player: str) -> int:
        if self.is_running():
            self.screen.send(f"whitelist add {player}")
            time.sleep(1)
            self.screen.send("whitelist reload")
            self.log(f"Added {player} via server console. Run 'gsm {self.instance} whitelist list' to verify")
            return 0
        try:
            raw = net.fetch_json(f"https://api.mojang.com/users/profiles/minecraft/{player}")["id"]
        except Exception as e:  # noqa: BLE001
            self.log(f"ERROR: could not look up UUID for '{player}': {e}")
            return 1
        data = self._wl_load()
        if any(p["name"].lower() == player.lower() for p in data):
            self.log(f"{player} is already in the whitelist")
            return 0
        data.append({"uuid": str(uuidlib.UUID(raw)), "name": player})
        self._wl_save(data)
        self.log(f"Added {player} to whitelist.json")
        return 0

    def _wl_remove(self, player: str) -> int:
        if self.is_running():
            self.screen.send(f"whitelist remove {player}")
            time.sleep(1)
            self.screen.send("whitelist reload")
            self.log(f"Removed {player} via server console")
            return 0
        data = [p for p in self._wl_load() if p["name"].lower() != player.lower()]
        self._wl_save(data)
        self.log(f"Removed {player} from whitelist.json")
        return 0

    def _wl_toggle(self, on: bool) -> int:
        self.props.set("white-list", "true" if on else "false")
        if on:
            self.props.set("enforce-whitelist", "true")
        if self.is_running():
            self.screen.send("whitelist on" if on else "whitelist off")
        self.log("Whitelist enabled" if on else "Whitelist disabled (enforce-whitelist unchanged)")
        return 0

    def whitelist(self, args: list[str]) -> int:
        usage = f"Usage: gsm {self.instance} whitelist {{add <player>|remove <player>|list|on|off}}"
        if not args:
            print(usage)
            return 1
        sub, rest = args[0], args[1:]
        if sub in ("add", "remove"):
            if not rest or not PLAYER_RE.match(rest[0]):
                self.log("ERROR: invalid player name (alphanumeric and underscores, max 16 chars)")
                return 1
            return self._wl_add(rest[0]) if sub == "add" else self._wl_remove(rest[0])
        if sub == "list":
            for p in self._wl_load():
                print(f"  {p['name']}  ({p['uuid']})")
            return 0
        if sub in ("on", "off"):
            return self._wl_toggle(sub == "on")
        print(usage)
        return 1

    def extra_commands(self) -> dict:
        return {"whitelist": ("add <player> | remove <player> | list | on | off", self.whitelist)}
