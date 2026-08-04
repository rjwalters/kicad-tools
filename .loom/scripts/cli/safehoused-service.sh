#!/usr/bin/env bash
# safehoused-service.sh — register `safehoused` as a supervised service on an
# interactive host: a launchd LaunchAgent on macOS, a `systemd --user` service
# on Linux. This is the provisioning mechanic the safehouse "New-host
# onboarding" runbook points at (see .loom/docs/safehouse.md), the local /
# interactive-host counterpart to the cloud-host path (#3998).
#
# It deliberately MIRRORS loom-daemon-start.sh's own supervised-service pattern
# (#3972 launchd / #4268 systemd --user, plus the --print-plist / --print-unit
# preview modes) so operators who already know how the loom-daemon service is
# provisioned see the same shape here.
#
# ---------------------------------------------------------------------------
# OWNERSHIP DECISION (issue #4346): who owns the safehoused service files?
# ---------------------------------------------------------------------------
# This wrapper is deliberately safehoused-AGNOSTIC. It does NOT vendor
# safehoused's real invocation, CLI flags, config schema, or the key-backup /
# steady-state teardown semantics (`Backups::wait_for_steady_state`) — those
# are owned by the external `rjwalters/safehouse` repo, which is the only place
# that can know safehoused's true ExecStart and lifecycle. This script only
# supervises an OPERATOR-SUPPLIED binary (`--bin` / `--exec`), bakes a minimal,
# non-secret environment, and applies the launchd/systemd supervision contract.
#
# So: the safehouse repo owns the AUTHORITATIVE service definition if/when it
# ships one; if it does, this repo's runbook points at that and this generator
# remains the documented fallback + the concrete, testable mechanic loom can
# offer today. loom does not, and should not, encode safehoused's argv — that
# would rot the moment the external repo changes it.
#
# ---------------------------------------------------------------------------
# Usage:
#   safehoused-service.sh install         Render + install + enable + start the service
#   safehoused-service.sh uninstall       Stop + disable + remove the service definition
#   safehoused-service.sh status          Report whether the supervised service is loaded/running
#   safehoused-service.sh --print-plist   Print the launchd LaunchAgent plist that WOULD be installed and exit (no side effects)
#   safehoused-service.sh --print-unit    Print the systemd --user unit that WOULD be installed and exit (no side effects)
#   safehoused-service.sh --help          Show this help
#
# Parameters (precedence: flag > env > config > default):
#   --bin PATH        The safehoused binary. Env SAFEHOUSED_BIN; default:
#                     `command -v safehoused`, else ~/.cargo/bin/safehoused.
#   --exec "ARGV"     Full ExecStart / ProgramArguments override (whitespace-
#                     separated argv, no shell quoting). Env SAFEHOUSED_EXEC.
#                     Default: the resolved binary alone (no invented flags).
#   --socket PATH     The AF_UNIX socket safehoused binds. Resolved (when not
#                     passed) via the SAME env>config>default chain the daemon
#                     and worker MCP injection use (safehouse.socket config >
#                     $LOOM_SAFEHOUSE_SOCKET > $SAFEHOUSED_SOCKET). Baked into
#                     the service env as SAFEHOUSED_SOCKET so the daemon and its
#                     clients agree on one path.
#   --config PATH     safehoused's own config file. Env SAFEHOUSED_CONFIG. Baked
#                     into the service env as SAFEHOUSED_CONFIG when set.
#   --log PATH        Service stdout/stderr log. Env SAFEHOUSED_LOG; default
#                     ~/.loom/logs/safehoused.log.
#   --label LABEL     macOS LaunchAgent label. Env SAFEHOUSED_LAUNCHD_LABEL;
#                     default com.rjwalters.safehoused.
#   --unit NAME       Linux systemd --user unit. Env SAFEHOUSED_SYSTEMD_UNIT;
#                     default safehoused.service.
#   --no-launchd      macOS: skip launchd; not supported (there is no nohup
#                     fallback here — this script's whole purpose is supervision).
#
# Supervision policy (deliberately DIFFERENT from loom-daemon's):
#   loom-daemon uses KeepAlive:{SuccessfulExit:true} / Restart=on-success
#   because it has a clean-exit restart PRIMITIVE (exit 0 == intentional
#   relaunch). safehoused has no such primitive — it is a persistent connection
#   daemon that should simply stay up. So this renders:
#     * launchd:  KeepAlive=true  (relaunch on ANY exit) + RunAtLoad=true
#     * systemd:  Restart=always + RestartSec=5 + WantedBy=default.target
#   Both survive re-login; reboot survival on a headless Linux host additionally
#   needs `loginctl enable-linger "$USER"` once (surfaced by `install`).
#
# Exit codes:
#   0  success (installed / uninstalled / status printed / preview printed)
#   1  usage error / binary not found / install failed
#   2  unsupported platform (no launchd on macOS, no reachable systemd --user on Linux)

set -uo pipefail

# ---------- output helpers ----------
if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi
err()  { echo -e "${RED}$*${NC}" >&2; }
warn() { echo -e "${YELLOW}$*${NC}" >&2; }
ok()   { echo -e "${GREEN}$*${NC}"; }

show_help() {
    # Print the leading comment banner (line 2 through the last comment line
    # before `set -uo pipefail`), stripping the leading "# ".
    awk 'NR>=2 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

# ---------- repo root (for config resolution only) ----------
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then echo "$dir"; return 0; fi
        if [[ -f "$dir/.git" ]]; then
            local gitdir main_repo
            gitdir=$(sed 's/^gitdir: //' "$dir/.git")
            main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
            if [[ -d "$main_repo/.loom" ]]; then echo "$main_repo"; return 0; fi
        fi
        dir="$(dirname "$dir")"
    done
    echo ""
}

# ---------- shared libs (domain / systemd detection + socket resolver) ----------
_LOOM_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" 2>/dev/null && pwd)"
if [[ -r "$_LOOM_LIB_DIR/launchd-domain.sh" ]]; then
    # shellcheck source=../lib/launchd-domain.sh
    source "$_LOOM_LIB_DIR/launchd-domain.sh"
fi
if [[ -r "$_LOOM_LIB_DIR/systemd-user.sh" ]]; then
    # shellcheck source=../lib/systemd-user.sh
    source "$_LOOM_LIB_DIR/systemd-user.sh"
fi
# mcp-config.sh gives us the SAME safehouse.socket resolver (env>config>default)
# the daemon status line and worker MCP injection use — so the socket this
# service binds and the socket loom connects to are resolved identically.
if [[ -r "$_LOOM_LIB_DIR/mcp-config.sh" ]]; then
    # shellcheck source=../lib/mcp-config.sh
    source "$_LOOM_LIB_DIR/mcp-config.sh"
fi
# canonical_daemon_path() (#4831) — the same shared canonical PATH superset
# loom-daemon-start.sh's resolve_plist_path() renders, sourced here instead of
# a fourth hand-maintained copy (see lib/canonical-daemon-path.sh).
if [[ -r "$_LOOM_LIB_DIR/canonical-daemon-path.sh" ]]; then
    # shellcheck source=../lib/canonical-daemon-path.sh
    source "$_LOOM_LIB_DIR/canonical-daemon-path.sh"
fi

# ---------- XML escaping (launchd plist) ----------
xml_escape() {
    local s="$1"
    s="${s//&/&amp;}"
    s="${s//</&lt;}"
    s="${s//>/&gt;}"
    printf '%s' "$s"
}

# ---------- deterministic service PATH ----------
# The same canonical minimal PATH loom-daemon-start.sh bakes into its plist
# (#4172), sourced from lib/canonical-daemon-path.sh (#4831) so this is no
# longer a separately-maintained copy of that set: hermetic, reproducible
# across hosts/sessions, never the invoking shell's interactive PATH.
# Override with SAFEHOUSED_PATH (verbatim).
resolve_service_path() {
    if [[ -n "${SAFEHOUSED_PATH:-}" ]]; then
        printf '%s' "${SAFEHOUSED_PATH}"
        return 0
    fi
    if declare -F canonical_daemon_path >/dev/null 2>&1; then
        canonical_daemon_path
        return 0
    fi
    # Degraded fallback if lib/canonical-daemon-path.sh could not be sourced
    # -- keep byte-for-byte identical to the lib's definition.
    printf '%s' "${HOME}/.local/bin:${HOME}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
}

# ---------- label / unit resolvers ----------
resolve_label() {
    echo "${SAFEHOUSED_LAUNCHD_LABEL:-com.rjwalters.safehoused}"
}
resolve_unit() {
    echo "${SAFEHOUSED_SYSTEMD_UNIT:-safehoused.service}"
}

# ---------- binary resolution ----------
locate_safehoused_bin() {
    if [[ -n "${SAFEHOUSED_BIN:-}" ]]; then echo "${SAFEHOUSED_BIN}"; return 0; fi
    if command -v safehoused >/dev/null 2>&1; then command -v safehoused; return 0; fi
    if [[ -x "${HOME}/.cargo/bin/safehoused" ]]; then echo "${HOME}/.cargo/bin/safehoused"; return 0; fi
    echo ""
}

# ---------- launchd plist rendering ----------
# render_launchd_plist <label> <workdir> <log_path> <socket> <config> <argv...>
# Pure string rendering — safe on ANY platform (used by --print-plist). No
# forwarded PATs: safehoused holds its Matrix credentials in its own config /
# store, so this env is minimal and non-secret by construction.
render_launchd_plist() {
    local label="$1" workdir="$2" log_path="$3" socket="$4" config="$5"
    shift 5
    local -a argv=("$@")
    local path_value; path_value="$(resolve_service_path)"

    local env_entries=""
    env_entries+="        <key>PATH</key>\n        <string>$(xml_escape "$path_value")</string>\n"
    env_entries+="        <key>HOME</key>\n        <string>$(xml_escape "$HOME")</string>\n"
    if [[ -n "$socket" ]]; then
        env_entries+="        <key>SAFEHOUSED_SOCKET</key>\n        <string>$(xml_escape "$socket")</string>\n"
    fi
    if [[ -n "$config" ]]; then
        env_entries+="        <key>SAFEHOUSED_CONFIG</key>\n        <string>$(xml_escape "$config")</string>\n"
    fi

    printf '<?xml version="1.0" encoding="UTF-8"?>\n'
    printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    printf '<plist version="1.0">\n<dict>\n'
    printf '    <key>Label</key>\n    <string>%s</string>\n' "$(xml_escape "$label")"
    printf '    <key>ProgramArguments</key>\n    <array>\n'
    local a
    for a in "${argv[@]}"; do
        printf '        <string>%s</string>\n' "$(xml_escape "$a")"
    done
    printf '    </array>\n'
    printf '    <key>WorkingDirectory</key>\n    <string>%s</string>\n' "$(xml_escape "$workdir")"
    printf '    <key>EnvironmentVariables</key>\n    <dict>\n'
    printf '%b' "$env_entries"
    printf '    </dict>\n'
    printf '    <key>RunAtLoad</key>\n    <true/>\n'
    # KeepAlive=true (persistent daemon, no clean-exit restart primitive):
    # relaunch on ANY exit. Contrast loom-daemon's KeepAlive:{SuccessfulExit:true}.
    printf '    <key>KeepAlive</key>\n    <true/>\n'
    printf '    <key>ProcessType</key>\n    <string>Background</string>\n'
    printf '    <key>StandardOutPath</key>\n    <string>%s</string>\n' "$(xml_escape "$log_path")"
    printf '    <key>StandardErrorPath</key>\n    <string>%s</string>\n' "$(xml_escape "$log_path")"
    printf '</dict>\n</plist>\n'
}

# ---------- systemd --user unit rendering ----------
# render_systemd_unit <workdir> <log_path> <socket> <config> <exec_line>
# Pure string rendering — safe on ANY platform (used by --print-unit). The Linux
# mirror of render_launchd_plist. Restart=always is the persistent-daemon analog
# of launchd KeepAlive=true (NOT loom-daemon's Restart=on-success).
render_systemd_unit() {
    local workdir="$1" log_path="$2" socket="$3" config="$4" exec_line="$5"
    local path_value; path_value="$(resolve_service_path)"

    local env_lines=""
    env_lines+="Environment=PATH=${path_value}\n"
    env_lines+="Environment=HOME=${HOME}\n"
    if [[ -n "$socket" ]]; then
        env_lines+="Environment=SAFEHOUSED_SOCKET=${socket}\n"
    fi
    if [[ -n "$config" ]]; then
        env_lines+="Environment=SAFEHOUSED_CONFIG=${config}\n"
    fi

    printf '[Unit]\n'
    printf 'Description=safehoused fleet-comms daemon (loom-supervised)\n'
    printf 'After=network-online.target\n'
    printf 'Wants=network-online.target\n'
    printf '\n'
    printf '[Service]\n'
    printf 'Type=simple\n'
    printf 'WorkingDirectory=%s\n' "$workdir"
    printf 'ExecStart=%s\n' "$exec_line"
    # Restart=always == launchd KeepAlive=true: a persistent daemon with no
    # clean-exit restart primitive is simply kept up. RestartSec bounds the
    # relaunch rate so a hard-failing safehoused does not hot-loop.
    printf 'Restart=always\n'
    printf 'RestartSec=5\n'
    printf '%b' "$env_lines"
    printf 'StandardOutput=append:%s\n' "$log_path"
    printf 'StandardError=append:%s\n' "$log_path"
    printf '\n'
    printf '[Install]\n'
    printf 'WantedBy=default.target\n'
}

# ============================================================================
# Argument / parameter resolution
# ============================================================================
ACTION=""
PRINT_PLIST=false
PRINT_UNIT=false
OPT_BIN=""
OPT_EXEC=""
OPT_SOCKET=""
OPT_CONFIG=""
OPT_LOG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        install|uninstall|status)
            if [[ -n "$ACTION" ]]; then err "Multiple actions given: '$ACTION' and '$1'"; exit 1; fi
            ACTION="$1"; shift ;;
        --print-plist) PRINT_PLIST=true; shift ;;
        --print-unit)  PRINT_UNIT=true; shift ;;
        --help|-h)     show_help; exit 0 ;;
        --bin)    OPT_BIN="${2:-}"; shift 2 ;;
        --exec)   OPT_EXEC="${2:-}"; shift 2 ;;
        --socket) OPT_SOCKET="${2:-}"; shift 2 ;;
        --config) OPT_CONFIG="${2:-}"; shift 2 ;;
        --log)    OPT_LOG="${2:-}"; shift 2 ;;
        --label)  SAFEHOUSED_LAUNCHD_LABEL="${2:-}"; shift 2 ;;
        --unit)   SAFEHOUSED_SYSTEMD_UNIT="${2:-}"; shift 2 ;;
        --no-launchd)
            err "--no-launchd is not supported: this script's purpose is supervision (there is no nohup fallback)."
            exit 1 ;;
        --*) err "Unknown flag: $1"; echo ""; show_help; exit 1 ;;
        *)   err "Unexpected argument: $1"; exit 1 ;;
    esac
done

REPO_ROOT="$(find_repo_root)"

# Binary: flag > env > discovery.
SAFEHOUSED_BIN="${OPT_BIN:-${SAFEHOUSED_BIN:-}}"
RESOLVED_BIN="$(locate_safehoused_bin)"

# Socket: flag > (config > LOOM_SAFEHOUSE_SOCKET > SAFEHOUSED_SOCKET via the
# shared resolver). The resolver soft-degrades to empty when nothing resolves.
RESOLVED_SOCKET="$OPT_SOCKET"
if [[ -z "$RESOLVED_SOCKET" ]] && command -v loom_mcp_safehouse_socket >/dev/null 2>&1; then
    RESOLVED_SOCKET="$(loom_mcp_safehouse_socket "${REPO_ROOT:-$PWD}")"
fi
[[ "$RESOLVED_SOCKET" == "null" ]] && RESOLVED_SOCKET=""

# Config: flag > env.
RESOLVED_CONFIG="${OPT_CONFIG:-${SAFEHOUSED_CONFIG:-}}"

# Log: flag > env > default.
RESOLVED_LOG="${OPT_LOG:-${SAFEHOUSED_LOG:-${HOME}/.loom/logs/safehoused.log}}"

# ExecStart argv: --exec override (whitespace-split) > resolved binary alone.
declare -a ARGV
if [[ -n "$OPT_EXEC" ]]; then
    # Intentionally word-split (no shell quoting inside --exec).
    # shellcheck disable=SC2206
    ARGV=($OPT_EXEC)
elif [[ -n "$RESOLVED_BIN" ]]; then
    ARGV=("$RESOLVED_BIN")
else
    ARGV=()
fi
EXEC_LINE="${ARGV[*]:-}"

LABEL="$(resolve_label)"
UNIT="$(resolve_unit)"
WORKDIR="$HOME"

# ---------- --print-plist / --print-unit: pure inspection, no side effects ----------
if [[ "$PRINT_PLIST" == "true" ]]; then
    if [[ ${#ARGV[@]} -eq 0 ]]; then
        # Render with a placeholder so the preview is still valid XML; warn on stderr.
        warn "safehoused binary not found (set --bin / SAFEHOUSED_BIN); previewing with a placeholder ProgramArguments."
        ARGV=("/path/to/safehoused")
    fi
    render_launchd_plist "$LABEL" "$WORKDIR" "$RESOLVED_LOG" "$RESOLVED_SOCKET" "$RESOLVED_CONFIG" "${ARGV[@]}"
    exit 0
fi
if [[ "$PRINT_UNIT" == "true" ]]; then
    preview_exec="$EXEC_LINE"
    if [[ -z "$preview_exec" ]]; then
        warn "safehoused binary not found (set --bin / SAFEHOUSED_BIN); previewing with a placeholder ExecStart."
        preview_exec="/path/to/safehoused"
    fi
    render_systemd_unit "$WORKDIR" "$RESOLVED_LOG" "$RESOLVED_SOCKET" "$RESOLVED_CONFIG" "$preview_exec"
    exit 0
fi

# ---------- from here on we need a real action ----------
if [[ -z "$ACTION" ]]; then
    show_help
    exit 0
fi

IS_DARWIN=false
[[ "$(uname -s)" == "Darwin" ]] && IS_DARWIN=true

# ============================================================================
# status
# ============================================================================
if [[ "$ACTION" == "status" ]]; then
    if [[ "$IS_DARWIN" == "true" ]]; then
        if ! command -v launchctl >/dev/null 2>&1; then err "launchctl not found."; exit 2; fi
        domain="$(resolve_launchd_domain 2>/dev/null || echo "gui/$(id -u)")"
        service="${domain}/${LABEL}"
        if launchctl print "$service" >/dev/null 2>&1; then
            pid=$(launchctl print "$service" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid = /{gsub(/[^0-9]/, "", $2); print $2; exit}')
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                ok "safehoused: running (launchd $service, pid $pid)"
            else
                warn "safehoused: loaded but not currently running (launchd $service)"
            fi
        else
            echo "safehoused: not installed (launchd $service)"
        fi
        exit 0
    fi
    # Linux
    if ! command -v systemctl >/dev/null 2>&1; then err "systemctl not found."; exit 2; fi
    if systemctl --user status "$UNIT" >/dev/null 2>&1; then
        state="$(systemctl --user is-active "$UNIT" 2>/dev/null)"
        ok "safehoused: $state (systemd --user $UNIT)"
    else
        echo "safehoused: not installed (systemd --user $UNIT)"
    fi
    exit 0
fi

# ============================================================================
# install / uninstall — need a resolved binary (install only) and a supervisor
# ============================================================================
if [[ "$ACTION" == "install" ]]; then
    if [[ ${#ARGV[@]} -eq 0 ]]; then
        err "safehoused binary not found."
        err "Build it from the rjwalters/safehouse checkout, then pass --bin PATH or set SAFEHOUSED_BIN."
        exit 1
    fi
fi

if [[ "$IS_DARWIN" == "true" ]]; then
    # ---------------- macOS: launchd LaunchAgent ----------------
    if ! command -v launchctl >/dev/null 2>&1; then
        err "launchctl not found on Darwin — cannot supervise safehoused."
        exit 2
    fi
    DOMAIN="$(resolve_launchd_domain)"
    SERVICE="${DOMAIN}/${LABEL}"
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST_FILE="$PLIST_DIR/${LABEL}.plist"

    if [[ "$ACTION" == "uninstall" ]]; then
        if launchctl print "$SERVICE" >/dev/null 2>&1; then
            launchctl bootout "$SERVICE" >/dev/null 2>&1 || true
        fi
        rm -f "$PLIST_FILE"
        ok "safehoused LaunchAgent removed ($SERVICE)"
        exit 0
    fi

    # install
    mkdir -p "$PLIST_DIR" "$(dirname "$RESOLVED_LOG")"
    render_launchd_plist "$LABEL" "$WORKDIR" "$RESOLVED_LOG" "$RESOLVED_SOCKET" "$RESOLVED_CONFIG" "${ARGV[@]}" > "$PLIST_FILE"
    echo "Launchd label:  $LABEL"
    echo "Launchd plist:  $PLIST_FILE"

    if launchctl print "$SERVICE" >/dev/null 2>&1; then
        launchctl bootout "$SERVICE" >/dev/null 2>&1 || true
    fi
    BOOTSTRAP_ERR="$(mktemp)"
    if ! launchctl bootstrap "$DOMAIN" "$PLIST_FILE" 2>"$BOOTSTRAP_ERR"; then
        err "launchctl bootstrap failed for $SERVICE:"
        cat "$BOOTSTRAP_ERR" >&2 2>/dev/null || true
        rm -f "$BOOTSTRAP_ERR"
        exit 1
    fi
    rm -f "$BOOTSTRAP_ERR"
    launchctl kickstart -k "$SERVICE" >/dev/null 2>&1 || true
    ok "safehoused installed + started under launchd ($SERVICE)."
    echo "Verify with:    ./.loom/scripts/cli/safehoused-service.sh status"
    echo "Then confirm:   loom-daemon status   (Safehouse: line should read 'connected')"
    exit 0
fi

# ---------------- Linux: systemd --user ----------------
if ! command -v systemctl >/dev/null 2>&1; then
    err "systemctl not found — this script supervises via systemd --user on Linux."
    exit 2
fi
if ! systemd_user_manager_reachable; then
    err "systemd --user manager is not reachable (no XDG_RUNTIME_DIR / no active user session)."
    err "On a headless host, enable lingering first: loginctl enable-linger \"\$USER\""
    exit 2
fi
UNIT_DIR="$(resolve_systemd_unit_dir)"
UNIT_PATH="${UNIT_DIR}/${UNIT}"

if [[ "$ACTION" == "uninstall" ]]; then
    systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
    rm -f "$UNIT_PATH"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    ok "safehoused systemd --user unit removed ($UNIT)"
    exit 0
fi

# install
mkdir -p "$UNIT_DIR" "$(dirname "$RESOLVED_LOG")"
render_systemd_unit "$WORKDIR" "$RESOLVED_LOG" "$RESOLVED_SOCKET" "$RESOLVED_CONFIG" "$EXEC_LINE" > "$UNIT_PATH"
echo "Systemd unit:   $UNIT_PATH"
systemctl --user daemon-reload >/dev/null 2>&1 || true
if ! systemctl --user enable --now "$UNIT" >/dev/null 2>&1; then
    err "systemctl --user enable --now failed for $UNIT."
    exit 1
fi
ok "safehoused installed + started under systemd --user ($UNIT)."
warn "Reboot survival on a headless host requires: loginctl enable-linger \"\$USER\""
echo "Verify with:    ./.loom/scripts/cli/safehoused-service.sh status"
echo "Then confirm:   loom-daemon status   (Safehouse: line should read 'connected')"
exit 0
