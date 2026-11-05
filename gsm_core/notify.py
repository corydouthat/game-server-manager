"""Single notification relay (Telegram) shared by every instance.

Configure in global.toml:

    [telegram]
    bot_token = "123456:ABC..."
    chat_id = "123456789"

Never raises: a failed notification is logged and otherwise ignored, so alerting
problems can't take down a monitor.
"""

import hashlib
import json
import socket
import time
import urllib.parse
import urllib.request

from . import config

DEDUPE_FILE = "notify-sent.json"


class Notifier:
    def __init__(self, global_cfg: dict, log=print):
        tg = global_cfg.get("telegram", {})
        self.token = str(tg.get("bot_token", "")).strip()
        self.chat_id = str(tg.get("chat_id", "")).strip()
        self.host = global_cfg.get("host_label") or socket.gethostname().split(".")[0]
        self.log = log

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _seen_recently(self, key: str, window: int) -> bool:
        path = config.state_dir() / DEDUPE_FILE
        now = time.time()
        try:
            sent = json.loads(path.read_text())
        except (OSError, ValueError):
            sent = {}
        sent = {k: t for k, t in sent.items() if now - t < 7 * 86400}
        if key in sent and now - sent[key] < window:
            return True
        sent[key] = now
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sent))
        tmp.replace(path)
        return False

    def send(self, instance: str | None, text: str, dedupe: int = 3600, key: str | None = None) -> bool:
        """Send a message. Identical messages (or the same key) within `dedupe` seconds are dropped."""
        prefix = f"[{instance}@{self.host}] " if instance else f"[{self.host}] "
        msg = prefix + text
        if not self.enabled:
            self.log(f"(notify: Telegram not configured) {msg}")
            return False
        dkey = hashlib.sha1((key or msg).encode()).hexdigest()
        if dedupe and self._seen_recently(dkey, dedupe):
            return False
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": msg}).encode()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            with urllib.request.urlopen(url, data=data, timeout=15) as r:
                ok = json.load(r).get("ok", False)
        except Exception as e:  # noqa: BLE001 — never let alerting crash the caller
            # Don't log the URL: it contains the bot token.
            self.log(f"WARNING: Telegram send failed: {type(e).__name__}")
            return False
        if not ok:
            self.log("WARNING: Telegram API returned ok=false")
        return ok
