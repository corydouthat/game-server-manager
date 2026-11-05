# game-server-manager

One tool to run, monitor, update and get alerted about game servers on a Linux box.
Python 3.11+ standard library only — no pip installs.

Supported out of the box:

| Adapter | Game | Runtime |
|---|---|---|
| `minecraft_bedrock` | Minecraft Bedrock Dedicated Server | GNU `screen` |
| `minecraft_java` | Minecraft Java Edition | GNU `screen` |
| `dragonwilds` | RuneScape: Dragonwilds | Docker Compose |

Each server is an **instance**: a TOML file in `instances/` naming its adapter and settings.
A monitor per instance runs as a systemd user service. Alerts go to Telegram.

## What the monitor does

- Restarts the server if it goes down; alerts on each restart.
- **Restart-loop protection:** 3 failed restarts in 15 min → one alert, then back off 30 min.
- **Daily update** at `update_check_time`: once per day, skipped if already current,
  deferred if players are online. Backs up before installing.
- **Bedrock:** every 10 min, keeps `server-udp-ports` in `server.properties` pointed at your
  current public IP (needed for NetherNet behind a port forward) and restarts once nobody's on.
  Alerts if the console says players can't connect.
- **Dragonwilds:** alerts when Docker silently restarts a crashed container.
- One monitor per instance, enforced by a lock file.

## Requirements

- Python 3.11+, `screen` (Minecraft), Docker + Compose (Dragonwilds), `java` (Minecraft Java)
- systemd user session (for running monitors as services)

## Setup

```bash
cd game-server-manager

# 1. Instances: copy an example per server and edit paths
cp instances/minecraft-bedrock.toml.example instances/bedrock.toml
chmod 640 instances/*.toml

# 2. Telegram (optional but recommended)
cp global.toml.example global.toml && chmod 640 global.toml
#    create a bot with @BotFather, put bot_token + chat_id in global.toml, then:
./gsm notify "hello from gsm"

# 3. Run monitors under systemd
./gsm install-service
loginctl enable-linger $USER            # once: start at boot without logging in
systemctl --user enable --now gsm@bedrock
```

## Commands

```
gsm list                          all instances + status
gsm notify <message>              send through the Telegram relay (usable from other scripts)
gsm <instance> start              start (and clear a hold)
gsm <instance> stop               stop and HOLD — the monitor won't restart it until 'start'
gsm <instance> restart
gsm <instance> status
gsm <instance> update             update now if a new version exists
gsm <instance> check-update
gsm <instance> reload-monitor     restart the systemd monitor without touching the server
gsm <instance> help               includes adapter-specific commands:
    bedrock:     sync-ip
    java:        whitelist add|remove|list|on|off
    dragonwilds: backup, logs
```

Console access is unchanged for screen-based servers: `screen -r <screen_name>` (detach with Ctrl-A D).
Logs: the instance's `log_file` and `journalctl --user -u gsm@<instance>`.

`systemctl --user stop gsm@<instance>` stops the server gracefully too.

## Adding a game

1. If it runs in `screen` or Docker Compose, start from the closest adapter in
   `gsm_core/adapters/` (Dragonwilds is ~150 lines).
2. Subclass `GameAdapter` (`gsm_core/adapters/base.py`) and implement `is_running`, `start`, `stop`;
   optionally `players_online`, `current_version`/`latest_version`/`install_update`
   (or override `daily_update`), `health_check`, `reconcile`, `extra_commands`.
3. Add `instances/<name>.toml` with `adapter = "<module name>"`.

Restart handling, update scheduling, holds, locking and notifications come from the core.

## Tests

```bash
python3 -m unittest discover -s tests
```

## Legacy

`legacy/` holds the previous bash managers (Minecraft, and Dragonwilds — merged with its
git history from the former `dragonwilds-manager` repo). They'll be removed once the
Python manager has run in production for a while.
