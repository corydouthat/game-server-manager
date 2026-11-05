"""Offline tests for the generic monitor logic. Run: python3 -m unittest discover -s tests"""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="gsm-test-")
os.environ["XDG_STATE_HOME"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gsm_core import monitor as monitor_mod  # noqa: E402
from gsm_core.adapters import minecraft_bedrock  # noqa: E402
from gsm_core.adapters.base import GameAdapter  # noqa: E402
from gsm_core.adapters.minecraft_common import PLAYERS_RE, Properties  # noqa: E402
from gsm_core.notify import Notifier  # noqa: E402


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, instance, text, **kw):
        self.sent.append(text)
        return True


class FakeAdapter(GameAdapter):
    def __init__(self, name="fake", **cfg):
        base = {"name": name, "adapter": "fake", "auto_update": True, "update_check_time": "00:00",
                "check_interval": 30, "max_backups": 5}
        base.update(cfg)
        self.lines = []
        super().__init__(base, self.lines.append, FakeNotifier())
        self.running = True
        self.start_ok = True
        self.starts = self.stops = self.installs = 0
        self.version, self.latest, self.players = "1", "1", 0
        self.marker = Path(tempfile.mkdtemp(dir=TMP))

    @property
    def marker_dir(self):
        return self.marker

    def is_running(self):
        return self.running

    def start(self):
        self.starts += 1
        self.running = self.start_ok
        return self.start_ok

    def stop(self):
        self.stops += 1
        self.running = False
        return True

    def current_version(self):
        return self.version

    def latest_version(self):
        if isinstance(self.latest, Exception):
            raise self.latest
        return self.latest

    def players_online(self):
        return self.players

    def install_update(self, latest):
        self.installs += 1
        self.version = latest


class MonitorTests(unittest.TestCase):
    def test_daily_check_runs_once_and_no_restart_when_current(self):
        a = FakeAdapter()
        m = monitor_mod.Monitor(a)
        m.maybe_daily_update()
        m.maybe_daily_update()
        self.assertEqual((a.stops, a.starts), (0, 0))
        self.assertEqual((a.marker / ".last_update").read_text().strip(), datetime.now().strftime("%Y%m%d"))
        self.assertEqual(sum("Daily update check triggered" in l for l in a.lines), 1)

    def test_daily_update_applies_new_version(self):
        a = FakeAdapter()
        a.latest = "2"
        monitor_mod.Monitor(a).maybe_daily_update()
        self.assertEqual((a.version, a.stops, a.starts, a.installs), ("2", 1, 1, 1))
        self.assertTrue(any("Updated 1 -> 2" in s for s in a.notifier.sent))

    def test_update_deferred_with_players(self):
        a = FakeAdapter()
        a.latest, a.players = "2", 3
        monitor_mod.Monitor(a).maybe_daily_update()
        self.assertEqual((a.version, a.stops), ("1", 0))

    def test_failed_check_still_marks_day(self):
        a = FakeAdapter()
        a.latest = RuntimeError("api down")
        m = monitor_mod.Monitor(a)
        m.maybe_daily_update()
        m.maybe_daily_update()
        self.assertEqual(sum("Daily update check triggered" in l for l in a.lines), 1)

    def test_auto_update_off(self):
        a = FakeAdapter(auto_update=False)
        a.latest = "2"
        monitor_mod.Monitor(a).maybe_daily_update()
        self.assertFalse((a.marker / ".last_update").exists())

    def test_crash_restart_and_loop_backoff(self):
        a = FakeAdapter(name="loop")
        a.running, a.start_ok = False, False
        m = monitor_mod.Monitor(a)
        for _ in range(6):
            m.handle_down()
        self.assertEqual(a.starts, 3)  # 3 attempts, then backoff
        self.assertTrue(any("Restart loop" in s for s in a.notifier.sent))
        self.assertGreater(m.backoff_until, 0)

    def test_crash_restart_success_notifies(self):
        a = FakeAdapter(name="ok")
        a.running = False
        monitor_mod.Monitor(a).handle_down()
        self.assertTrue(a.running)
        self.assertIn("Server was down — restarted OK", a.notifier.sent)

    def test_hold_skips_tick(self):
        a = FakeAdapter(name="held")
        a.running = False
        a.hold_file.touch()
        monitor_mod.Monitor(a).tick()
        self.assertEqual(a.starts, 0)


class ParsingTests(unittest.TestCase):
    def test_players_regex(self):
        self.assertEqual(PLAYERS_RE.findall("INFO] There are 2/10 players online:"), ["2"])
        self.assertEqual(PLAYERS_RE.findall("INFO]: There are 0 of a max of 10 players online:"), ["0"])

    def test_bedrock_version_from_url(self):
        url = "https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-1.26.62.1.zip"
        self.assertEqual(minecraft_bedrock.Adapter._version_from_url(url), "1.26.62.1")


class BedrockIpSyncTests(unittest.TestCase):
    def make(self, udp_ports):
        d = Path(tempfile.mkdtemp(dir=TMP))
        (d / "server.properties").write_text(
            f"# comment\nserver-port=19132\ntransport=nethernet\nserver-udp-ports={udp_ports}\nlevel-name=x\n")
        a = minecraft_bedrock.Adapter(
            {"name": "bedrock-test", "adapter": "minecraft_bedrock", "server_dir": str(d), "screen_name": "nope-test",
             "max_backups": 5}, [].append, FakeNotifier())
        a._save_state(ip_restart_pending=False)
        return a, d

    def test_rewrites_ip_and_keeps_ports(self):
        a, d = self.make("1.2.3.4:29489:19489")
        with mock.patch("gsm_core.net.public_ipv4", return_value="5.6.7.8"):
            self.assertTrue(a.sync_ip(restart=False))
        self.assertEqual(Properties(d / "server.properties").get("server-udp-ports"), "5.6.7.8:29489:19489")
        self.assertIn("# comment", (d / "server.properties").read_text())
        self.assertTrue(a._state()["ip_restart_pending"])

    def test_plain_port_gets_ip_prefix(self):
        a, d = self.make("19132")
        with mock.patch("gsm_core.net.public_ipv4", return_value="5.6.7.8"):
            a.sync_ip(restart=False)
        self.assertEqual(Properties(d / "server.properties").get("server-udp-ports"), "5.6.7.8:19132:19132")

    def test_no_change_when_matching(self):
        a, _ = self.make("5.6.7.8:19132:19132")
        with mock.patch("gsm_core.net.public_ipv4", return_value="5.6.7.8"):
            self.assertFalse(a.sync_ip(restart=False))

    def test_unknown_ip_does_nothing(self):
        a, d = self.make("5.6.7.8:19132:19132")
        with mock.patch("gsm_core.net.public_ipv4", return_value=None):
            self.assertFalse(a.sync_ip(restart=False))

    def test_pending_restart_waits_for_players(self):
        a, _ = self.make("5.6.7.8:19132:19132")
        a._save_state(ip_restart_pending=True)
        with mock.patch.object(a, "is_running", return_value=True), \
             mock.patch.object(a, "players_online", return_value=2), \
             mock.patch.object(a, "stop") as stop:
            a._restart_if_pending()
            stop.assert_not_called()
        self.assertTrue(a._state()["ip_restart_pending"])


class NotifyTests(unittest.TestCase):
    def test_dedupe_and_unconfigured(self):
        n = Notifier({}, log=lambda *_: None)
        self.assertFalse(n.enabled)
        self.assertFalse(n._seen_recently("k1", 3600))
        self.assertTrue(n._seen_recently("k1", 3600))

    def test_send_posts_and_hides_token_on_error(self):
        logs = []
        n = Notifier({"telegram": {"bot_token": "SECRET", "chat_id": "42"}}, log=logs.append)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom SECRET")):
            self.assertFalse(n.send("x", "hello-unique-1", dedupe=0))
        self.assertFalse(any("SECRET" in l for l in logs))


if __name__ == "__main__":
    unittest.main()
