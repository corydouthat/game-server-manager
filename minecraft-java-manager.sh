#!/bin/bash

# Minecraft Java Server Manager
# Version: 1.0.0

SCRIPT_VERSION="1.0.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/minecraft-java-manager.conf"

DEFAULT_SERVER_DIR="${SCRIPT_DIR}/minecraft-java-server"
DEFAULT_SERVER_JAR="server.jar"
DEFAULT_SCREEN_NAME="minecraft-java-server"
DEFAULT_MIN_RAM="2G"
DEFAULT_MAX_RAM="8G"
DEFAULT_LOG_FILE=""
DEFAULT_GAMERULES=""
DEFAULT_AUTO_UPDATE="false"  # Java version must match all clients — off by default
DEFAULT_MAX_BACKUPS=5

MOJANG_MANIFEST_URL="https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

load_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        LOG_FILE="${SCRIPT_DIR}/minecraft-java-manager.log"
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

    [ ! -d "$SERVER_DIR" ] && mkdir -p "$SERVER_DIR" 2>/dev/null

    if [ -z "$LOG_FILE" ]; then
        LOG_FILE="${SERVER_DIR}/minecraft-java-manager.log"
    fi

    LAST_UPDATE_FILE="${SERVER_DIR}/.last_update"
    VERSION_FILE="${SERVER_DIR}/.server_version"
    MAX_BACKUPS="${MAX_BACKUPS:-$DEFAULT_MAX_BACKUPS}"

    declare -gA GAMERULES
    if [ -n "$GAMERULES_STRING" ]; then
        IFS=',' read -ra RULES <<< "$GAMERULES_STRING"
        for rule in "${RULES[@]}"; do
            IFS='=' read -r key value <<< "$rule"
            [ -n "$key" ] && GAMERULES["$key"]="$value"
        done
    fi
}

create_default_config() {
    cat > "$CONFIG_FILE" << EOF
# Minecraft Java Server Manager Configuration
# Version: $SCRIPT_VERSION

SERVER_DIR="$DEFAULT_SERVER_DIR"

# Name of the JAR file in SERVER_DIR to launch
SERVER_JAR="$DEFAULT_SERVER_JAR"

SCREEN_NAME="$DEFAULT_SCREEN_NAME"

# Java heap size
MIN_RAM="$DEFAULT_MIN_RAM"
MAX_RAM="$DEFAULT_MAX_RAM"

LOG_FILE="$DEFAULT_LOG_FILE"

# Game rules on start (comma-separated key=value pairs, or leave empty)
GAMERULES_STRING="$DEFAULT_GAMERULES"

# Auto-update during monitor mode — CAUTION: updating breaks clients on older versions
# Run 'update' manually to control when clients need to update
AUTO_UPDATE="$DEFAULT_AUTO_UPDATE"

MAX_BACKUPS=$DEFAULT_MAX_BACKUPS
EOF
    chmod 640 "$CONFIG_FILE"
    log "Default config created at $CONFIG_FILE"
}

log() {
    local log_file="${LOG_FILE:-/tmp/minecraft-java-manager.log}"
    mkdir -p "$(dirname "$log_file")" 2>/dev/null
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$log_file"
}

is_server_running() {
    screen -list 2>/dev/null | grep -qF ".${SCREEN_NAME}"
}

server_command() {
    screen -S "$SCREEN_NAME" -p 0 -X stuff "$1$(printf \\r)"
}

players_connected() {
    if ! is_server_running; then
        return 1
    fi

    server_command "list"
    sleep 2

    local log_file="${SERVER_DIR}/logs/latest.log"
    if [ -f "$log_file" ]; then
        local count
        count=$(tail -30 "$log_file" | grep -oP 'There are \K[0-9]+(?= of a max)' | tail -1)
        [ -n "$count" ] && [ "$count" -gt 0 ] && return 0
    fi

    return 1
}

apply_gamerules() {
    [ ${#GAMERULES[@]} -eq 0 ] && return 0

    log "Applying game rules..."
    sleep 15

    for rule in "${!GAMERULES[@]}"; do
        log "Setting $rule=${GAMERULES[$rule]}"
        server_command "gamerule $rule ${GAMERULES[$rule]}"
        sleep 1
    done

    log "Game rules applied"
}

check_java() {
    if ! command -v java &>/dev/null; then
        log "ERROR: Java is not installed or not in PATH"
        return 1
    fi
    log "Java: $(java -version 2>&1 | head -1)"
    return 0
}

check_eula() {
    local eula_file="${SERVER_DIR}/eula.txt"

    if [ -f "$eula_file" ] && grep -q "eula=true" "$eula_file"; then
        return 0
    fi

    if [ "${AUTO_ACCEPT_EULA:-false}" = "true" ]; then
        log "Auto-accepting EULA (AUTO_ACCEPT_EULA=true in config)"
        log "You agree to Mojang's EULA: https://www.minecraft.net/en-us/eula"
        if [ -f "$eula_file" ]; then
            sed -i 's/eula=false/eula=true/' "$eula_file"
        else
            echo "eula=true" > "$eula_file"
        fi
        return 0
    fi

    log "ERROR: Minecraft EULA not accepted."
    log "Read and accept the EULA at: https://www.minecraft.net/en-us/eula"
    log "Then either:"
    log "  1) Set AUTO_ACCEPT_EULA=true in $CONFIG_FILE"
    log "  2) Run: echo 'eula=true' > ${SERVER_DIR}/eula.txt"
    return 1
}

start_server() {
    if is_server_running; then
        log "Server is already running"
        return 0
    fi

    local jar="${SERVER_DIR}/${SERVER_JAR}"
    if [ ! -f "$jar" ]; then
        log "ERROR: Server JAR not found at $jar"
        log "Run '$0 update' to download the latest server JAR, or set SERVER_JAR in config"
        return 1
    fi

    check_java || return 1
    check_eula || return 1

    local version
    version=$(get_current_version)
    log "Starting Minecraft Java server (version: $version)..."
    log "*** Clients must be on version $version to connect ***"

    cd "$SERVER_DIR" || { log "ERROR: Cannot cd to $SERVER_DIR"; return 1; }

    screen -dmS "$SCREEN_NAME" java -Xms"$MIN_RAM" -Xmx"$MAX_RAM" -jar "$SERVER_JAR" nogui
    sleep 8

    if is_server_running; then
        log "Server started successfully"
        apply_gamerules
        return 0
    else
        log "ERROR: Server failed to start. Check ${SERVER_DIR}/logs/latest.log"
        return 1
    fi
}

stop_server() {
    if ! is_server_running; then
        log "Server is not running"
        return 0
    fi

    log "Stopping server gracefully..."
    server_command "stop"

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
        curl -s --max-time 20 "$url" 2>/dev/null
    elif command -v wget &>/dev/null; then
        wget --timeout=20 -qO- "$url" 2>/dev/null
    else
        log "ERROR: curl or wget required"
        return 1
    fi
}

parse_manifest() {
    local json="$1" key="$2"
    if command -v jq &>/dev/null; then
        echo "$json" | jq -r "$key" 2>/dev/null
    elif command -v python3 &>/dev/null; then
        echo "$json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print($key)
" 2>/dev/null
    else
        return 1
    fi
}

get_latest_java_info() {
    local manifest
    manifest=$(fetch_url "$MOJANG_MANIFEST_URL") || { log "ERROR: Could not reach Mojang manifest"; return 1; }

    local latest_version version_url download_url sha1

    if command -v jq &>/dev/null; then
        latest_version=$(echo "$manifest" | jq -r '.latest.release')
        version_url=$(echo "$manifest" | jq -r --arg v "$latest_version" '.versions[] | select(.id == $v) | .url')
        local version_meta
        version_meta=$(fetch_url "$version_url") || { log "ERROR: Could not fetch version metadata"; return 1; }
        download_url=$(echo "$version_meta" | jq -r '.downloads.server.url')
        sha1=$(echo "$version_meta" | jq -r '.downloads.server.sha1')
    elif command -v python3 &>/dev/null; then
        latest_version=$(echo "$manifest" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['latest']['release'])")
        version_url=$(echo "$manifest" | python3 -c "
import json, sys
d = json.load(sys.stdin)
v = '$latest_version'
print(next(x['url'] for x in d['versions'] if x['id'] == v))
")
        local version_meta
        version_meta=$(fetch_url "$version_url") || { log "ERROR: Could not fetch version metadata"; return 1; }
        download_url=$(echo "$version_meta" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['downloads']['server']['url'])")
        sha1=$(echo "$version_meta" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['downloads']['server']['sha1'])")
    else
        log "ERROR: jq or python3 is required for Java server update checks"
        log "Install with: sudo dnf install jq   or   sudo dnf install python3"
        return 1
    fi

    if [ -z "$download_url" ] || [ "$download_url" = "null" ]; then
        log "ERROR: Could not parse download URL from Mojang manifest"
        return 1
    fi

    echo "$latest_version|$download_url|$sha1"
}

get_current_version() {
    [ -f "$VERSION_FILE" ] && cat "$VERSION_FILE" || echo "unknown"
}

check_update() {
    log "Checking for updates..."

    local info
    info=$(get_latest_java_info) || return 1

    local latest current
    latest=$(echo "$info" | cut -d'|' -f1)
    current=$(get_current_version)

    log "Installed: $current"
    log "Latest:    $latest"

    if [ "$current" = "$latest" ]; then
        log "Server is up to date"
        return 0
    else
        log "Update available: $current -> $latest"
        log "NOTE: Run '$0 update' when all players can also update their clients"
        return 1
    fi
}

cleanup_backups() {
    local parent base
    parent=$(dirname "$SERVER_DIR")
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

    local info
    info=$(get_latest_java_info) || return 1

    local latest current download_url sha1
    latest=$(echo "$info" | cut -d'|' -f1)
    download_url=$(echo "$info" | cut -d'|' -f2)
    sha1=$(echo "$info" | cut -d'|' -f3)
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

    local tmp="/tmp/mc-java-update-$$"
    mkdir -p "$tmp"

    log "Downloading server JAR for version $latest..."
    local ok=0
    if command -v wget &>/dev/null; then
        wget --timeout=300 --tries=3 -q "$download_url" -O "$tmp/server.jar" && ok=1
    elif command -v curl &>/dev/null; then
        curl -L --http1.1 --max-time 300 --retry 3 -s "$download_url" -o "$tmp/server.jar" && ok=1
    fi

    if [ "$ok" -eq 0 ] || [ ! -f "$tmp/server.jar" ]; then
        log "ERROR: Download failed"
        rm -rf "$tmp"
        return 1
    fi

    # Verify SHA1 if we have it
    if [ -n "$sha1" ] && [ "$sha1" != "null" ] && command -v sha1sum &>/dev/null; then
        local actual_sha1
        actual_sha1=$(sha1sum "$tmp/server.jar" | cut -d' ' -f1)
        if [ "$actual_sha1" != "$sha1" ]; then
            log "ERROR: SHA1 mismatch — download may be corrupt"
            log "Expected: $sha1"
            log "Got:      $actual_sha1"
            rm -rf "$tmp"
            return 1
        fi
        log "SHA1 verified"
    fi

    cp -f "$tmp/server.jar" "${abs_server_dir}/${SERVER_JAR}"
    echo "$latest" > "$VERSION_FILE"
    echo "$(date +%Y%m%d)" > "$LAST_UPDATE_FILE"
    rm -rf "$tmp"

    cleanup_backups

    log "======================================================"
    log "UPDATE COMPLETE: $current -> $latest"
    log "*** ALL CLIENTS MUST UPDATE TO VERSION $latest ***"
    log "======================================================"
}

check_and_update() {
    [ "${AUTO_UPDATE:-false}" != "true" ] && return 0

    local today last
    today=$(date +%Y%m%d)
    [ -f "$LAST_UPDATE_FILE" ] && last=$(cat "$LAST_UPDATE_FILE")
    [ "$today" = "$last" ] && return 0

    log "Daily update check triggered (AUTO_UPDATE=true)"

    # Mark the day as checked up front, regardless of outcome. Otherwise a day
    # with no update applied (already latest, or a transient API error) never
    # records LAST_UPDATE_FILE, the once-per-day guard above never trips, and the
    # monitor loop stops/restarts the server every cycle — a restart storm.
    echo "$today" > "$LAST_UPDATE_FILE"

    local info latest current
    info=$(get_latest_java_info) || { log "Update check failed; will retry tomorrow"; return 0; }
    latest=$(echo "$info" | cut -d'|' -f1)
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
    log "Starting Minecraft Java Server Manager v$SCRIPT_VERSION"
    [ "${AUTO_UPDATE:-false}" != "true" ] && log "Auto-update is disabled. Run '$0 update' to update manually."
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

# ── Whitelist management ─────────────────────────────────────────────────────

validate_player_name() {
    [[ "$1" =~ ^[A-Za-z0-9_]{1,16}$ ]]
}

format_uuid() {
    local u="$1"
    echo "${u:0:8}-${u:8:4}-${u:12:4}-${u:16:4}-${u:20:12}"
}

get_player_uuid() {
    local player="$1"
    local response
    response=$(fetch_url "https://api.mojang.com/users/profiles/minecraft/$player") || return 1
    local raw_uuid
    if command -v jq &>/dev/null; then
        raw_uuid=$(echo "$response" | jq -r '.id' 2>/dev/null)
    elif command -v python3 &>/dev/null; then
        raw_uuid=$(echo "$response" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])" 2>/dev/null)
    else
        raw_uuid=$(echo "$response" | grep -oP '"id"\s*:\s*"\K[^"]+')
    fi
    [ -n "$raw_uuid" ] && [ "$raw_uuid" != "null" ] && format_uuid "$raw_uuid"
}

update_server_property() {
    local key="$1" value="$2"
    local props="${SERVER_DIR}/server.properties"
    if [ -f "$props" ]; then
        if grep -q "^${key}=" "$props"; then
            sed -i "s|^${key}=.*|${key}=${value}|" "$props"
        else
            echo "${key}=${value}" >> "$props"
        fi
    fi
}

whitelist_list() {
    local wl="${SERVER_DIR}/whitelist.json"
    if [ ! -f "$wl" ]; then
        log "Whitelist file not found: $wl"
        return 0
    fi

    log "Whitelisted players:"
    if command -v jq &>/dev/null; then
        jq -r '.[] | "  \(.name)  (\(.uuid))"' "$wl"
    elif command -v python3 &>/dev/null; then
        python3 -c "
import json
with open('$wl') as f:
    for p in json.load(f):
        print('  {}  ({})'.format(p['name'], p['uuid']))
"
    else
        grep -oP '"name"\s*:\s*"\K[^"]+' "$wl"
    fi
}

whitelist_add() {
    local player="$1"
    if ! validate_player_name "$player"; then
        log "ERROR: Invalid player name '$player' (alphanumeric and underscores, max 16 chars)"
        return 1
    fi

    if is_server_running; then
        log "Adding $player to whitelist via server console..."
        server_command "whitelist add $player"
        sleep 1
        server_command "whitelist reload"
        log "Done. Run '$0 whitelist list' to verify"
        return 0
    fi

    log "Server offline — adding $player to whitelist.json directly..."

    local uuid
    uuid=$(get_player_uuid "$player")
    if [ -z "$uuid" ]; then
        log "ERROR: Could not look up UUID for '$player'. Check the username and try again."
        return 1
    fi
    log "UUID: $uuid"

    local wl="${SERVER_DIR}/whitelist.json"
    [ ! -f "$wl" ] && echo "[]" > "$wl"

    if grep -q "\"$player\"" "$wl"; then
        log "$player is already in the whitelist"
        return 0
    fi

    local ok=0
    if command -v python3 &>/dev/null; then
        python3 -c "
import json
wl = '$wl'
with open(wl) as f:
    data = json.load(f)
data.append({'uuid': '$uuid', 'name': '$player'})
with open(wl, 'w') as f:
    json.dump(data, f, indent=2)
" && ok=1
    elif command -v jq &>/dev/null; then
        local tmp
        tmp=$(mktemp)
        jq ". += [{\"uuid\": \"$uuid\", \"name\": \"$player\"}]" "$wl" > "$tmp" && mv "$tmp" "$wl" && ok=1
    fi

    if [ "$ok" -eq 1 ]; then
        log "Added $player to whitelist"
    else
        log "ERROR: python3 or jq required to edit whitelist.json offline"
        log "Add manually to $wl:"
        log "  {\"uuid\": \"$uuid\", \"name\": \"$player\"}"
        return 1
    fi
}

whitelist_remove() {
    local player="$1"
    if ! validate_player_name "$player"; then
        log "ERROR: Invalid player name '$player'"
        return 1
    fi

    if is_server_running; then
        log "Removing $player from whitelist via server console..."
        server_command "whitelist remove $player"
        sleep 1
        server_command "whitelist reload"
        log "Done"
        return 0
    fi

    log "Server offline — removing $player from whitelist.json..."

    local wl="${SERVER_DIR}/whitelist.json"
    if [ ! -f "$wl" ]; then
        log "Whitelist file not found"
        return 1
    fi

    local ok=0
    if command -v python3 &>/dev/null; then
        python3 -c "
import json
wl = '$wl'
with open(wl) as f:
    data = json.load(f)
data = [p for p in data if p['name'].lower() != '$player'.lower()]
with open(wl, 'w') as f:
    json.dump(data, f, indent=2)
" && ok=1
    elif command -v jq &>/dev/null; then
        local tmp
        tmp=$(mktemp)
        jq "[.[] | select(.name | ascii_downcase != (\"$player\" | ascii_downcase))]" "$wl" > "$tmp" && mv "$tmp" "$wl" && ok=1
    fi

    if [ "$ok" -eq 1 ]; then
        log "Removed $player from whitelist"
    else
        log "ERROR: python3 or jq required to edit whitelist.json offline"
        return 1
    fi
}

whitelist_on() {
    update_server_property "white-list" "true"
    update_server_property "enforce-whitelist" "true"
    if is_server_running; then
        server_command "whitelist on"
    fi
    log "Whitelist enabled"
}

whitelist_off() {
    update_server_property "white-list" "false"
    if is_server_running; then
        server_command "whitelist off"
    fi
    log "Whitelist disabled (enforce-whitelist unchanged)"
}

status_server() {
    local version
    version=$(get_current_version)
    if is_server_running; then
        log "RUNNING  | version: $version | screen: $SCREEN_NAME"
        log "         | Clients must be on version $version"
    else
        log "STOPPED  | last version: $version"
    fi
}

show_version() {
    echo "Minecraft Java Server Manager v$SCRIPT_VERSION"
    echo "Config:         $CONFIG_FILE"
    echo "Server version: $(get_current_version)"
}

# ── Entry point ──────────────────────────────────────────────────────────────

load_config

case "$1" in
    start)         start_server ;;
    stop)          stop_server ;;
    restart)       stop_server; sleep 5; start_server ;;
    update)        stop_server; sleep 5; update_server; sleep 5; start_server ;;
    check-update)  check_update ;;
    status)        status_server ;;
    monitor)       monitor_server ;;
    version)       show_version ;;
    whitelist)
        case "$2" in
            add)    whitelist_add "$3" ;;
            remove) whitelist_remove "$3" ;;
            list)   whitelist_list ;;
            on)     whitelist_on ;;
            off)    whitelist_off ;;
            *)
                echo "Usage: $0 whitelist {add <player>|remove <player>|list|on|off}"
                exit 1
                ;;
        esac
        ;;
    *)
        echo "Minecraft Java Server Manager v$SCRIPT_VERSION"
        echo "Usage: $0 {start|stop|restart|update|check-update|status|monitor|version|whitelist}"
        echo ""
        echo "  start              - Start the server"
        echo "  stop               - Stop the server"
        echo "  restart            - Restart the server"
        echo "  update             - Update to latest version and restart"
        echo "                       (warns clients must also update)"
        echo "  check-update       - Check for updates without acting"
        echo "  status             - Show running status and version"
        echo "  monitor            - Start with auto-restart (default)"
        echo "  version            - Show manager version"
        echo ""
        echo "  whitelist add <player>    - Add player to whitelist"
        echo "  whitelist remove <player> - Remove player from whitelist"
        echo "  whitelist list            - Show all whitelisted players"
        echo "  whitelist on              - Enable whitelist"
        echo "  whitelist off             - Disable whitelist"
        exit 1
        ;;
esac
