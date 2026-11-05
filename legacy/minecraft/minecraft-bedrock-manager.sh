#!/bin/bash

# Minecraft Bedrock Server Manager
# Version: 1.2.0

SCRIPT_VERSION="1.2.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/minecraft-bedrock-manager.conf"

DEFAULT_SERVER_DIR="${SCRIPT_DIR}/minecraft-bedrock-server"
DEFAULT_SERVER_BINARY="bedrock_server"
DEFAULT_SCREEN_NAME="minecraft-bedrock-server"
DEFAULT_UPDATE_CHECK_TIME="03:00"
DEFAULT_LOG_FILE=""
DEFAULT_GAMERULES="keepInventory=true,playersSleepingPercentage=30,showCoordinates=true"
DEFAULT_MAX_BACKUPS=5

MOJANG_API_URL="https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"

load_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        LOG_FILE="${SCRIPT_DIR}/minecraft-bedrock-manager.log"
        log "Config not found. Creating default at $CONFIG_FILE"
        create_default_config
    fi

    # Refuse to source a world-writable config (arbitrary code execution risk)
    local perms last_digit
    perms=$(stat -c "%a" "$CONFIG_FILE" 2>/dev/null || stat -f "%Lp" "$CONFIG_FILE" 2>/dev/null)
    last_digit="${perms: -1}"
    if [[ "$last_digit" =~ [2367] ]]; then
        echo "ERROR: $CONFIG_FILE is world-writable. Fix with: chmod o-w $CONFIG_FILE"
        exit 1
    fi

    source "$CONFIG_FILE"

    [ ! -d "$SERVER_DIR" ] && mkdir -p "$SERVER_DIR" 2>/dev/null

    if [ -z "$LOG_FILE" ]; then
        LOG_FILE="${SERVER_DIR}/minecraft-bedrock-manager.log"
    fi

    LAST_UPDATE_FILE="${SERVER_DIR}/.last_update"
    VERSION_FILE="${SERVER_DIR}/.server_version"
    MAX_BACKUPS="${MAX_BACKUPS:-$DEFAULT_MAX_BACKUPS}"

    declare -gA GAMERULES
    IFS=',' read -ra RULES <<< "$GAMERULES_STRING"
    for rule in "${RULES[@]}"; do
        IFS='=' read -r key value <<< "$rule"
        [ -n "$key" ] && GAMERULES["$key"]="$value"
    done
}

create_default_config() {
    cat > "$CONFIG_FILE" << EOF
# Minecraft Bedrock Server Manager Configuration
# Version: $SCRIPT_VERSION

SERVER_DIR="$DEFAULT_SERVER_DIR"
SERVER_BINARY="$DEFAULT_SERVER_BINARY"
SCREEN_NAME="$DEFAULT_SCREEN_NAME"
UPDATE_CHECK_TIME="$DEFAULT_UPDATE_CHECK_TIME"
LOG_FILE="$DEFAULT_LOG_FILE"
GAMERULES_STRING="$DEFAULT_GAMERULES"
MAX_BACKUPS=$DEFAULT_MAX_BACKUPS
EOF
    chmod 640 "$CONFIG_FILE"
    log "Default config created at $CONFIG_FILE"
}

log() {
    local log_file="${LOG_FILE:-/tmp/minecraft-bedrock-manager.log}"
    mkdir -p "$(dirname "$log_file")" 2>/dev/null
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$log_file"
}

is_server_running() {
    screen -list 2>/dev/null | grep -qF ".${SCREEN_NAME}"
}

players_connected() {
    if ! is_server_running; then
        return 1
    fi

    local dump_file="/tmp/mc_bedrock_screen_$$.txt"
    screen -S "$SCREEN_NAME" -p 0 -X stuff "list$(printf \\r)"
    sleep 2
    screen -S "$SCREEN_NAME" -p 0 -X hardcopy "$dump_file" 2>/dev/null

    local player_count=0
    if [ -f "$dump_file" ]; then
        player_count=$(grep -oP 'There are \K[0-9]+(?= player)' "$dump_file" | tail -1)
        rm -f "$dump_file"
    fi

    [ -n "$player_count" ] && [ "$player_count" -gt 0 ]
}

apply_gamerules() {
    [ ${#GAMERULES[@]} -eq 0 ] && return 0

    log "Applying game rules..."
    sleep 10

    for rule in "${!GAMERULES[@]}"; do
        log "Setting $rule=${GAMERULES[$rule]}"
        screen -S "$SCREEN_NAME" -p 0 -X stuff "gamerule $rule ${GAMERULES[$rule]}$(printf \\r)"
        sleep 1
    done

    log "Game rules applied"
}

start_server() {
    if is_server_running; then
        log "Server is already running"
        return 0
    fi

    if [ ! -f "${SERVER_DIR}/${SERVER_BINARY}" ]; then
        log "ERROR: Server binary not found at ${SERVER_DIR}/${SERVER_BINARY}"
        log "Run '$0 update' to download the server"
        return 1
    fi

    log "Starting Minecraft Bedrock server (version: $(get_current_version))..."
    cd "$SERVER_DIR" || { log "ERROR: Cannot cd to $SERVER_DIR"; return 1; }
    chmod +x "$SERVER_BINARY"

    screen -dmS "$SCREEN_NAME" bash -c "LD_LIBRARY_PATH=. ./$SERVER_BINARY"
    sleep 5

    if is_server_running; then
        log "Server started successfully"
        apply_gamerules
        return 0
    else
        log "ERROR: Server failed to start"
        return 1
    fi
}

stop_server() {
    if ! is_server_running; then
        log "Server is not running"
        return 0
    fi

    log "Stopping server gracefully..."
    screen -S "$SCREEN_NAME" -p 0 -X stuff "stop$(printf \\r)"

    for i in {1..60}; do
        if ! is_server_running; then
            log "Server stopped"
            return 0
        fi
        sleep 1
    done

    log "WARNING: Force-killing server"
    screen -S "$SCREEN_NAME" -X quit
    sleep 2
}

fetch_url() {
    local url="$1"
    if command -v curl &>/dev/null; then
        curl -s --max-time 15 "$url" 2>/dev/null
    elif command -v wget &>/dev/null; then
        wget --timeout=15 -qO- "$url" 2>/dev/null
    else
        log "ERROR: curl or wget required"
        return 1
    fi
}

get_latest_download_url() {
    local json
    json=$(fetch_url "$MOJANG_API_URL") || return 1

    if [ -z "$json" ]; then
        log "ERROR: Empty response from Mojang API"
        return 1
    fi

    local url=""

    if command -v jq &>/dev/null; then
        url=$(echo "$json" | jq -r '.result.links[] | select(.downloadType == "serverBedrockLinux") | .downloadUrl' 2>/dev/null | head -1)
    elif command -v python3 &>/dev/null; then
        url=$(echo "$json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data.get('result', {}).get('links', []):
    if item.get('downloadType') == 'serverBedrockLinux':
        print(item.get('downloadUrl', ''))
        break
" 2>/dev/null)
    else
        # Fallback: grep both possible field orderings
        url=$(echo "$json" | grep -oP '"downloadType"\s*:\s*"serverBedrockLinux"[^}]*"downloadUrl"\s*:\s*"\K[^"]+' | head -1)
        if [ -z "$url" ]; then
            url=$(echo "$json" | grep -oP '"downloadUrl"\s*:\s*"\K[^"]+(?=[^}]*"downloadType"\s*:\s*"serverBedrockLinux")' | head -1)
        fi
    fi

    if [ -z "$url" ]; then
        log "ERROR: Could not parse download URL. Install jq or python3 for reliable parsing."
        return 1
    fi

    echo "$url"
}

extract_version_from_url() {
    echo "$1" | grep -oP 'bedrock-server-\K[0-9.]+' | sed 's/\.$//'
}

get_current_version() {
    [ -f "$VERSION_FILE" ] && cat "$VERSION_FILE" || echo "unknown"
}

check_update() {
    log "Checking for updates..."
    local url
    url=$(get_latest_download_url) || return 1

    local latest current
    latest=$(extract_version_from_url "$url")
    current=$(get_current_version)

    log "Installed: $current"
    log "Latest:    $latest"

    if [ "$current" = "$latest" ]; then
        log "Already up to date"
        return 0
    else
        log "Update available: $current -> $latest"
        return 1
    fi
}

cleanup_backups() {
    local parent
    parent=$(dirname "$SERVER_DIR")
    local base
    base=$(basename "$SERVER_DIR")

    mapfile -t backups < <(find "$parent" -maxdepth 1 -name "${base}_backup_*" -type d | sort)

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
    log "Checking for server updates..."

    local url
    url=$(get_latest_download_url) || return 1

    local latest current
    latest=$(extract_version_from_url "$url")
    current=$(get_current_version)

    log "Installed: $current"
    log "Latest:    $latest"

    if [ "$current" = "$latest" ]; then
        log "Already on latest version"
        return 0
    fi

    log "Updating $current -> $latest..."

    local abs_server_dir
    abs_server_dir="$(cd "$SERVER_DIR" && pwd)"

    local backup="${abs_server_dir}_backup_$(date +%Y%m%d_%H%M%S)"
    log "Creating backup at $backup"
    cp -r "$abs_server_dir" "$backup"

    local tmp="/tmp/mc-bedrock-update-$$"
    mkdir -p "$tmp"

    log "Downloading version $latest..."
    local ok=0
    if command -v wget &>/dev/null; then
        wget --timeout=300 --tries=3 -q "$url" -O "$tmp/bedrock-server.zip" && ok=1
    elif command -v curl &>/dev/null; then
        curl -L --http1.1 --max-time 300 --retry 3 -s "$url" -o "$tmp/bedrock-server.zip" && ok=1
    fi

    if [ "$ok" -eq 0 ] || [ ! -f "$tmp/bedrock-server.zip" ]; then
        log "ERROR: Download failed"
        rm -rf "$tmp"
        return 1
    fi

    local size
    size=$(stat -c%s "$tmp/bedrock-server.zip" 2>/dev/null || stat -f%z "$tmp/bedrock-server.zip" 2>/dev/null)
    if [ -z "$size" ] || [ "$size" -lt 1000 ]; then
        log "ERROR: Downloaded file looks invalid ($size bytes)"
        rm -rf "$tmp"
        return 1
    fi

    if ! unzip -q "$tmp/bedrock-server.zip" -d "$tmp/extracted"; then
        log "ERROR: Failed to extract archive"
        rm -rf "$tmp"
        return 1
    fi

    log "Installing (preserving configuration)..."

    cp -f "$tmp/extracted/$SERVER_BINARY" "$abs_server_dir/"
    find "$tmp/extracted" -maxdepth 1 -name "*.so" -exec cp -f {} "$abs_server_dir/" \;

    local preserve=("server.properties" "allowlist.json" "permissions.json" "worlds" "packetlimitconfig.json" "profanity_filter.wlist")
    for src in "$tmp/extracted"/*; do
        local name
        name=$(basename "$src")
        local skip=0
        for p in "${preserve[@]}"; do
            [ "$name" = "$p" ] && skip=1 && break
        done
        [ "$skip" -eq 0 ] && cp -rf "$src" "$abs_server_dir/" 2>/dev/null
    done

    echo "$latest" > "$VERSION_FILE"
    echo "$(date +%Y%m%d)" > "$LAST_UPDATE_FILE"
    rm -rf "$tmp"

    cleanup_backups
    log "Update complete: $current -> $latest"
}

check_and_update() {
    local today last
    today=$(date +%Y%m%d)
    [ -f "$LAST_UPDATE_FILE" ] && last=$(cat "$LAST_UPDATE_FILE")
    [ "$today" = "$last" ] && return 0

    # Fire once per day, on the first check at or after the scheduled time.
    # (Matching HH:MM exactly could miss the whole day if the monitor loop never
    #  happened to sample the clock during that one-minute window.)
    local now_min sched_min
    now_min=$(( 10#$(date +%H) * 60 + 10#$(date +%M) ))
    sched_min=$(( 10#${UPDATE_CHECK_TIME%%:*} * 60 + 10#${UPDATE_CHECK_TIME##*:} ))
    [ "$now_min" -lt "$sched_min" ] && return 0

    log "Daily update check triggered"

    # Mark the day as checked up front, regardless of outcome. Otherwise a day
    # with no update applied (already latest, or a transient API error) never
    # records LAST_UPDATE_FILE, the once-per-day guard above never trips, and the
    # monitor loop re-runs this every cycle — a restart storm.
    echo "$today" > "$LAST_UPDATE_FILE"

    local url latest current
    url=$(get_latest_download_url) || { log "Update check failed; will retry tomorrow"; return 0; }
    latest=$(extract_version_from_url "$url")
    current=$(get_current_version)
    log "Installed: $current"
    log "Latest:    $latest"

    if [ "$current" = "$latest" ]; then
        log "Already up to date — no restart needed"
        return 0
    fi

    if players_connected; then
        log "Players online — deferring update until tomorrow's check"
        return 0
    fi

    log "Update available: $current -> $latest"
    stop_server
    sleep 5
    update_server
    sleep 5
    start_server
}

monitor_server() {
    log "Starting Minecraft Bedrock Server Manager v$SCRIPT_VERSION"
    start_server

    while true; do
        sleep 30

        if ! is_server_running; then
            log "WARNING: Server not running, restarting..."
            start_server
        fi

        check_and_update
    done
}

status_server() {
    if is_server_running; then
        log "RUNNING  | version: $(get_current_version) | screen: $SCREEN_NAME"
    else
        log "STOPPED  | last version: $(get_current_version)"
    fi
}

show_version() {
    echo "Minecraft Bedrock Server Manager v$SCRIPT_VERSION"
    echo "Config:         $CONFIG_FILE"
    echo "Server version: $(get_current_version)"
}

# ── Entry point ──────────────────────────────────────────────────────────────

load_config

case "${1:-monitor}" in
    start)        start_server ;;
    stop)         stop_server ;;
    restart)      stop_server; sleep 5; start_server ;;
    update)       stop_server; sleep 5; update_server; sleep 5; start_server ;;
    check-update) check_update ;;
    status)       status_server ;;
    monitor)      monitor_server ;;
    version)      show_version ;;
    *)
        echo "Minecraft Bedrock Server Manager v$SCRIPT_VERSION"
        echo "Usage: $0 {start|stop|restart|update|check-update|status|monitor|version}"
        echo ""
        echo "  start        - Start the server"
        echo "  stop         - Stop the server"
        echo "  restart      - Restart the server"
        echo "  update       - Update to latest version and restart"
        echo "  check-update - Check for updates (no action taken)"
        echo "  status       - Show running status and version"
        echo "  monitor      - Start with auto-restart and daily updates (default)"
        echo "  version      - Show manager version"
        exit 1
        ;;
esac
