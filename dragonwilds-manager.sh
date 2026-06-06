#!/bin/bash

# DragonWilds Server Manager
# Version: 2.0.0

SCRIPT_VERSION="2.0.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$(cd "${SCRIPT_DIR}/.." && pwd)/dragonwilds-manager.conf"

DEFAULT_COMPOSE_DIR="${HOME}/repos/runescape-dragonwilds-server-docker"
DEFAULT_UPDATE_CHECK_TIME="03:00"
DEFAULT_LOG_FILE=""
DEFAULT_MAX_BACKUPS=5

load_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        LOG_FILE="${SCRIPT_DIR}/dragonwilds-manager.log"
        log "Config not found. Creating default at $CONFIG_FILE"
        create_default_config
    fi

    local perms last_digit
    perms=$(stat -c "%a" "$CONFIG_FILE" 2>/dev/null || stat -f "%Lp" "$CONFIG_FILE" 2>/dev/null)
    last_digit="${perms: -1}"
    if [[ "$last_digit" =~ [2367] ]]; then
        echo "ERROR: $CONFIG_FILE is world-writable. Fix with: chmod o-w $CONFIG_FILE"
        exit 1
    fi

    source "$CONFIG_FILE"

    COMPOSE_DIR="${COMPOSE_DIR:-$DEFAULT_COMPOSE_DIR}"

    if [ -z "$LOG_FILE" ]; then
        LOG_FILE="${COMPOSE_DIR}/dragonwilds-manager.log"
    fi

    LAST_UPDATE_FILE="${COMPOSE_DIR}/.last_update"
    MAX_BACKUPS="${MAX_BACKUPS:-$DEFAULT_MAX_BACKUPS}"
    UPDATE_CHECK_TIME="${UPDATE_CHECK_TIME:-$DEFAULT_UPDATE_CHECK_TIME}"
}

create_default_config() {
    cat > "$CONFIG_FILE" << EOF
# DragonWilds Server Manager Configuration
# Version: $SCRIPT_VERSION

COMPOSE_DIR="$DEFAULT_COMPOSE_DIR"

# Time of day to check for updates in monitor mode (24h HH:MM)
UPDATE_CHECK_TIME="$DEFAULT_UPDATE_CHECK_TIME"

LOG_FILE="$DEFAULT_LOG_FILE"

# Number of save backups to keep
MAX_BACKUPS=$DEFAULT_MAX_BACKUPS
EOF
    chmod 640 "$CONFIG_FILE"
    log "Default config created at $CONFIG_FILE"
}

log() {
    local log_file="${LOG_FILE:-/tmp/dragonwilds-manager.log}"
    mkdir -p "$(dirname "$log_file")" 2>/dev/null
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$log_file"
}

compose() {
    docker compose -f "${COMPOSE_DIR}/docker-compose.yml" "$@"
}

is_server_running() {
    compose ps --status running 2>/dev/null | grep -q "runescape-dragonwilds"
}

get_local_buildid() {
    local manifest="${COMPOSE_DIR}/server-files/steamapps/appmanifest_4019830.acf"
    if [ -f "$manifest" ]; then
        grep -oP '"buildid"\s+"\K[0-9]+' "$manifest" | head -1
    else
        echo "unknown"
    fi
}

start_server() {
    if is_server_running; then
        log "Server is already running"
        return 0
    fi

    if [ ! -f "${COMPOSE_DIR}/docker-compose.yml" ]; then
        log "ERROR: docker-compose.yml not found at ${COMPOSE_DIR}"
        return 1
    fi

    log "Starting DragonWilds server..."
    compose up -d 2>&1 | tee -a "$LOG_FILE"
    sleep 5

    if is_server_running; then
        log "Server started successfully"
        return 0
    else
        log "ERROR: Server failed to start. Check logs with: docker logs runescape-dragonwilds"
        return 1
    fi
}

stop_server() {
    if ! is_server_running; then
        log "Server is not running"
        return 0
    fi

    log "Stopping server..."
    compose stop 2>&1 | tee -a "$LOG_FILE"
    sleep 5

    if ! is_server_running; then
        log "Server stopped"
        return 0
    fi

    log "WARNING: Server did not stop cleanly"
    return 1
}

backup_saves() {
    local save_dir="${COMPOSE_DIR}/server-files/RSDragonwilds/Saved/SaveGames"
    if [ ! -d "$save_dir" ]; then
        log "No save directory found — skipping backup"
        return 0
    fi

    local backup_dir="${COMPOSE_DIR}/dragonwilds-saves-backup_$(date +%Y%m%d_%H%M%S)"
    log "Backing up saves to $backup_dir"
    cp -r "$save_dir" "$backup_dir"

    mapfile -t backups < <(find "${COMPOSE_DIR}" -maxdepth 1 -name "dragonwilds-saves-backup_*" -type d | sort)
    local count=${#backups[@]}
    if [ "$count" -gt "$MAX_BACKUPS" ]; then
        local excess=$(( count - MAX_BACKUPS ))
        for (( i=0; i<excess; i++ )); do
            log "Removing old backup: ${backups[$i]}"
            rm -rf "${backups[$i]}"
        done
    fi
}

update_server() {
    log "Updating DragonWilds server (current build: $(get_local_buildid))..."

    backup_saves

    log "Pulling latest Docker image..."
    compose pull 2>&1 | tee -a "$LOG_FILE"

    log "Restarting with new image..."
    compose up -d --force-recreate 2>&1 | tee -a "$LOG_FILE"

    echo "$(date +%Y%m%d)" > "$LAST_UPDATE_FILE"
    log "Update complete (build: $(get_local_buildid))"
}

check_and_update() {
    local today last
    today=$(date +%Y%m%d)
    [ -f "$LAST_UPDATE_FILE" ] && last=$(cat "$LAST_UPDATE_FILE")
    [ "$today" = "$last" ] && return 0

    [ "$(date +%H:%M)" != "$UPDATE_CHECK_TIME" ] && return 0

    log "Daily update check triggered"
    update_server
}

monitor_server() {
    log "Starting DragonWilds Server Manager v$SCRIPT_VERSION"
    start_server

    while true; do
        sleep 30
        # Docker restart: unless-stopped handles crash recovery automatically.
        # Monitor loop only needs to trigger the daily update.
        check_and_update
    done
}

status_server() {
    if is_server_running; then
        log "RUNNING  | build: $(get_local_buildid) | container: runescape-dragonwilds"
    else
        log "STOPPED  | last build: $(get_local_buildid)"
    fi
    compose ps 2>/dev/null
}

show_logs() {
    docker logs --tail=50 -f runescape-dragonwilds
}

show_version() {
    echo "DragonWilds Server Manager v$SCRIPT_VERSION"
    echo "Config:      $CONFIG_FILE"
    echo "Compose dir: $COMPOSE_DIR"
    echo "Build ID:    $(get_local_buildid)"
}

# ── Entry point ───────────────────────────────────────────────────────────────

load_config

case "${1:-monitor}" in
    start)   start_server ;;
    stop)    stop_server ;;
    restart) stop_server; sleep 5; start_server ;;
    update)  update_server ;;
    backup)  backup_saves ;;
    status)  status_server ;;
    logs)    show_logs ;;
    monitor) monitor_server ;;
    version) show_version ;;
    *)
        echo "DragonWilds Server Manager v$SCRIPT_VERSION"
        echo "Usage: $0 {start|stop|restart|update|backup|status|logs|monitor|version}"
        echo ""
        echo "  start   - Start the server container"
        echo "  stop    - Stop the server container"
        echo "  restart - Restart the server container"
        echo "  update  - Backup saves, pull latest image, and restart"
        echo "  backup  - Back up save files only"
        echo "  status  - Show running status and build ID"
        echo "  logs    - Tail container logs (Ctrl+C to exit)"
        echo "  monitor - Start with daily auto-updates (default)"
        echo "  version - Show manager version"
        exit 1
        ;;
esac
