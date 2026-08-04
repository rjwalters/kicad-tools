#!/usr/bin/env bash
# loom status - Display agent pool state
#
# Usage:
#   loom status                   Show running agents + configured-but-stopped agents
#   loom status --json            Machine-readable JSON output
#   loom status --help            Show help
#
# Reports the tmux agent pool spawned by `loom start`:
#   - Running `loom-*` tmux sessions on the `loom` socket (with pane PID and
#     uptime derived from tmux #{session_created}).
#   - Cross-references each session against .loom/config.json .terminals[]
#     to report terminal id, name, and role file.
#   - Prints a `tmux -L loom kill-session` recovery command for any unmanaged
#     session (present on the socket but not in config).
#   - Flags agents configured in .loom/config.json that are NOT running (in red),
#     so a crashed / never-started agent is easy to spot.
#   - Shows a work-queue summary (open loom:issue / loom:review-requested /
#     loom:pr / loom:architect / loom:hermit / loom:curated / loom:auditor
#     counts) when `gh` is available on a GitHub forge.
#   - Surfaces MACHINE-DAEMON state for this workspace (#4793): this script
#     only ever inspected the LOCAL tmux agent pool, so a repo actively
#     managed by a separate machine-level `loom-daemon` fleet (autonomous
#     work finder / role runner dispatching sweeps with zero local tmux
#     sessions) used to read as "No agents running" -- misleadingly implying
#     nothing would pick up open issues. When a `loom-daemon` binary can be
#     located, this queries `loom-daemon status --json` (the same IPC surface
#     `./.loom/bin/loom health` delegates to) for daemon liveness, whether
#     this repo is a registered/managed workspace, its dispatch-gate state,
#     and its in-flight sweep count -- and the empty-agent-pool message names
#     its own scope explicitly ("local agent pool: none") instead of reading
#     as a bare, fleet-wide "No agents running".
#
# Exits 0 whether or not any agents are running (an empty pool is not an error).

set -euo pipefail

_LOOM_STATUS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/config-resolver.sh
source "$_LOOM_STATUS_SCRIPT_DIR/../lib/config-resolver.sh"
# shellcheck source=../lib/locate-daemon-bin.sh
source "$_LOOM_STATUS_SCRIPT_DIR/../lib/locate-daemon-bin.sh"

# Find repository root
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then
            echo "$dir"
            return 0
        fi
        if [[ -f "$dir/.git" ]]; then
            local gitdir
            gitdir=$(sed 's/^gitdir: //' "$dir/.git")
            local main_repo
            main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
            if [[ -d "$main_repo/.loom" ]]; then
                echo "$main_repo"
                return 0
            fi
        fi
        dir="$(dirname "$dir")"
    done
    echo ""
}

REPO_ROOT=$(find_repo_root)
if [[ -z "$REPO_ROOT" ]]; then
    echo "Error: Not in a Loom workspace (.loom directory not found)" >&2
    exit 1
fi

# Canonicalized (symlink-resolved) repo root, used ONLY to match this
# workspace against the `root` paths `loom-daemon status --json` / the
# `~/.loom/workspaces.json` registry report -- both normalize via Rust's
# `std::fs::canonicalize` (`workspace_registry::normalize_path`), so a
# non-canonical `REPO_ROOT` (e.g. a `/tmp` symlinked to `/private/tmp` on
# macOS) would otherwise silently fail to match a repo the daemon DOES
# manage. Falls back to the plain `REPO_ROOT` if canonicalization fails.
REPO_ROOT_CANON="$(cd "$REPO_ROOT" 2>/dev/null && pwd -P 2>/dev/null)"
[[ -n "$REPO_ROOT_CANON" ]] || REPO_ROOT_CANON="$REPO_ROOT"

TMUX_SOCKET="loom"

# List of config tier paths that exist on disk (lowest to highest precedence).
# Used only for the human "Config:" display line -- naming every present tier
# instead of a single legacy path (#4062).
_loom_status_config_tiers_present() {
    local dp
    dp="$(_loom_config_private_defaults_path)"
    [[ -n "$dp" && -f "$dp" ]] && echo "$dp"
    [[ -f "$REPO_ROOT/$LOOM_CONFIG_LEGACY_REL" ]] && echo "$REPO_ROOT/$LOOM_CONFIG_LEGACY_REL"
    [[ -f "$REPO_ROOT/$LOOM_CONFIG_PROJECT_REL" ]] && echo "$REPO_ROOT/$LOOM_CONFIG_PROJECT_REL"
    [[ -f "$REPO_ROOT/$LOOM_CONFIG_LOCAL_REL" ]] && echo "$REPO_ROOT/$LOOM_CONFIG_LOCAL_REL"
    # Explicit trailing success: under `set -o pipefail`, this function's exit
    # status is whatever its last statement returned. The common case (no
    # local/private-defaults tier present) makes the last `[[ -f ]] && echo`
    # above false -- without this, callers piping this function's output
    # (e.g. `_loom_status_config_tiers_present | paste ...`) would see the
    # pipeline fail and, combined with `set -e`, abort the whole script
    # (#4062 regression).
    return 0
}

# Resolve the effective config ONCE per invocation (config-resolver, #4062)
# and memoize it -- read_terminals() may be called from both emit_json and
# emit_human, and must not re-merge the tier chain on every call.
_LOOM_STATUS_EFFECTIVE_CONFIG=""
_loom_status_effective_config() {
    if [[ -z "$_LOOM_STATUS_EFFECTIVE_CONFIG" ]]; then
        _LOOM_STATUS_EFFECTIVE_CONFIG="$(loom_resolve_config "$REPO_ROOT")"
    fi
    echo "$_LOOM_STATUS_EFFECTIVE_CONFIG"
}

# ANSI colors
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    GRAY='\033[0;90m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    CYAN=''
    GRAY=''
    BOLD=''
    NC=''
fi

# Show help
show_help() {
    cat <<EOF
${BOLD}loom status - Display agent pool state${NC}

${YELLOW}USAGE:${NC}
    loom status                   Show running + configured-but-stopped agents
    loom status --json            Machine-readable JSON output
    loom status --help            Show this help

${YELLOW}OUTPUT:${NC}
    Running agents are the ${CYAN}loom-*${NC} tmux sessions on the ${CYAN}loom${NC} socket
    spawned by 'loom start'. Each is cross-referenced against
    .loom/config.json .terminals[] to show its id, name, role file, and
    uptime. Unmanaged sessions (on the socket but absent from config) are
    printed with their exact 'tmux -L loom kill-session' recovery command.
    Agents present in the config but not currently running are listed
    separately in ${RED}red${NC} so a crashed or never-started agent is easy to spot.
    A Machine Daemon section reports whether a separate machine-level
    'loom-daemon' fleet manages this repo (liveness, registry membership,
    dispatch-gate state, in-flight sweep count) -- since that daemon can be
    actively dispatching work here even when the local agent pool above is
    empty, shown when a 'loom-daemon' binary is resolvable (PATH or an
    in-repo build). A Work Queue summary (open ${CYAN}loom:issue${NC} /
    ${CYAN}loom:review-requested${NC} / ${CYAN}loom:pr${NC} counts, plus
    pending-proposal counts for ${CYAN}loom:architect${NC} / ${CYAN}loom:hermit${NC} /
    ${CYAN}loom:curated${NC} / ${CYAN}loom:auditor${NC}) is shown when 'gh' is
    available on a GitHub forge.

${YELLOW}EXIT STATUS:${NC}
    Always 0 when the workspace resolves — an empty pool is not an error.

${YELLOW}RELATED COMMANDS:${NC}
    loom start       Spawn the agent pool from config
    loom attach <id> Attach to a running agent's tmux session
    loom logs <id>   Tail an agent's output
    loom stop        Graceful shutdown of the agent pool
EOF
}

# List running loom-* sessions on the loom socket (one per line, may be empty)
get_running_sessions() {
    command -v tmux &>/dev/null || return 0
    tmux -L "$TMUX_SOCKET" list-sessions -F "#{session_name}" 2>/dev/null \
        | grep "^loom-" || true
}

# Pane PID for a session (first pane); empty if unavailable
session_pid() {
    local session_name="$1"
    tmux -L "$TMUX_SOCKET" list-panes -t "$session_name" -F "#{pane_pid}" 2>/dev/null \
        | head -1 || true
}

# Session creation epoch (seconds); empty if unavailable
session_created() {
    local session_name="$1"
    tmux -L "$TMUX_SOCKET" display-message -t "$session_name" -p "#{session_created}" 2>/dev/null \
        | head -1 || true
}

# Uptime in whole seconds for a session; empty if creation time unavailable
session_uptime_seconds() {
    local session_name="$1"
    local created now
    created=$(session_created "$session_name")
    [[ "$created" =~ ^[0-9]+$ ]] || return 0
    now=$(date +%s)
    local secs=$(( now - created ))
    (( secs < 0 )) && secs=0
    echo "$secs"
}

# Format a whole-second duration compactly (e.g. 4h32m, 3d4h, 45s)
format_duration() {
    local secs="$1"
    [[ "$secs" =~ ^[0-9]+$ ]] || { echo "unknown"; return 0; }
    local d=$(( secs / 86400 ))
    local h=$(( (secs % 86400) / 3600 ))
    local m=$(( (secs % 3600) / 60 ))
    if (( d > 0 )); then
        echo "${d}d${h}h"
    elif (( h > 0 )); then
        echo "${h}h${m}m"
    elif (( m > 0 )); then
        echo "${m}m"
    else
        echo "${secs}s"
    fi
}

# Count open issues/PRs carrying a label. Echoes an integer on success;
# returns non-zero (and echoes nothing) when gh is unavailable, unauthenticated,
# or the forge is not GitHub — callers omit the work-queue rather than error.
# $1 = issue|pr, $2 = label
count_label() {
    local kind="$1" label="$2" n
    command -v gh &>/dev/null || return 1
    n=$(gh "$kind" list --label "$label" --state open --json number --jq 'length' 2>/dev/null) || return 1
    [[ "$n" =~ ^[0-9]+$ ]] || return 1
    echo "$n"
}

# Build the work-queue object as compact JSON, or "null" when gh/label counts
# are unavailable (gh missing, unauthenticated, or a non-GitHub forge). Never
# errors — a missing forge just yields null so the section is omitted.
#
# Includes the pending-proposal labels (loom:architect / loom:hermit /
# loom:curated / loom:auditor, #4793) alongside the original three -- without
# these a non-empty proposal pipeline still rendered as an all-zero work
# queue, which read as "nothing pending" even when several proposals were
# awaiting Champion review.
work_queue_json() {
    command -v jq &>/dev/null || { echo "null"; return 0; }
    local issue rr pr architect hermit curated auditor
    issue=$(count_label issue "loom:issue") || { echo "null"; return 0; }
    rr=$(count_label pr "loom:review-requested") || { echo "null"; return 0; }
    pr=$(count_label pr "loom:pr") || { echo "null"; return 0; }
    architect=$(count_label issue "loom:architect") || { echo "null"; return 0; }
    hermit=$(count_label issue "loom:hermit") || { echo "null"; return 0; }
    curated=$(count_label issue "loom:curated") || { echo "null"; return 0; }
    auditor=$(count_label issue "loom:auditor") || { echo "null"; return 0; }
    jq -nc \
        --argjson issue "$issue" \
        --argjson review_requested "$rr" \
        --argjson pr "$pr" \
        --argjson architect "$architect" \
        --argjson hermit "$hermit" \
        --argjson curated "$curated" \
        --argjson auditor "$auditor" \
        '{"loom:issue":$issue, "loom:review-requested":$review_requested, "loom:pr":$pr,
          "loom:architect":$architect, "loom:hermit":$hermit, "loom:curated":$curated,
          "loom:auditor":$auditor}'
}

# Read the configured terminals as compact JSON array (or "[]"), resolved
# through the config-resolver tier chain (#4062) rather than a single-tier
# .loom/config.json read -- a workspace may supply terminals entirely from
# .loom-project/project.json.
read_terminals() {
    if command -v jq &>/dev/null; then
        _loom_status_effective_config | jq -c '.terminals // []' 2>/dev/null || echo "[]"
    else
        echo "[]"
    fi
}

# ---- Machine daemon integration (#4793) ----------------------------------
# This script only ever inspected the LOCAL tmux agent pool. The functions
# below layer in the machine-level `loom-daemon` picture -- daemon liveness,
# whether THIS repo is a registered/managed workspace, its dispatch-gate
# state, and its in-flight sweep count -- by reusing the daemon's OWN
# reporting surfaces rather than re-deriving any of that logic in bash:
#   - `loom-daemon status --json` (the same IPC call `./.loom/bin/loom
#     health` delegates to) is the primary source: when the daemon answers,
#     its `install_state`/`per_repo` fields are authoritative.
#   - `~/.loom/workspaces.json` (the on-disk registry `loom-daemon
#     workspace add|remove|list` and the daemon itself read/write, override
#     `LOOM_WORKSPACES_PATH`) is consulted ONLY as a fallback membership
#     check when the daemon itself is unreachable -- a static file read
#     still answers "is this repo registered?" even while the daemon is down.

# Resolve the loom-daemon binary via the shared loom_locate_daemon_bin()
# (lib/locate-daemon-bin.sh, #4875) — includes the machine-level
# $LOOM_DAEMON_BIN_DIR (default ~/.local/bin) fallback, so this script,
# loom-daemon-start.sh, loom-daemon-watchdog.sh, loom-daemon-update.sh, and
# `./.loom/bin/loom health` all agree on which binary is "the" daemon CLI.
# Preserves this function's original contract (thin wrapper): echoes the
# resolved path and returns 0, or returns 1 with nothing echoed -- INCLUDING
# the pre-#4875 behavior that an explicitly-set-but-non-executable
# LOOM_DAEMON_BIN is a hard "not found" (unlike the other four call sites,
# which fall through to PATH/machine-level/in-repo candidates in that case).
# This script's own test suite uses LOOM_DAEMON_BIN=<bogus path> as a
# deterministic "force no binary resolvable" sentinel that must NOT then pick
# up a real installed binary via PATH or ~/.local/bin, so that contract is
# preserved here explicitly rather than delegated wholesale.
locate_daemon_bin() {
    if [[ -n "${LOOM_DAEMON_BIN:-}" ]]; then
        [[ -x "${LOOM_DAEMON_BIN}" ]] && { echo "${LOOM_DAEMON_BIN}"; return 0; }
        return 1
    fi
    local bin
    bin="$(unset LOOM_DAEMON_BIN; loom_locate_daemon_bin "$REPO_ROOT")"
    [[ -n "$bin" ]] || return 1
    echo "$bin"
}

# Query `loom-daemon status --json`. Echoes the JSON payload on success --
# either a full DaemonStatusReport (daemon reachable) or the #4069
# unreachable-error object (daemon not reachable; still carries an
# `install_state` liveness classification: NotExpected / ExpectedButDead /
# AliveStarting / AliveButUnresponsive) -- and echoes nothing when no
# loom-daemon binary can be located at all, or `jq` is unavailable to parse
# the result. Never propagates the daemon CLI's own exit code (it exits
# non-zero on the unreachable path by design) so callers never abort under
# `set -e`.
daemon_status_json() {
    command -v jq &>/dev/null || return 0
    local bin
    bin="$(locate_daemon_bin)" || return 0
    "$bin" status --json 2>/dev/null || true
}

# Best-effort machine-registry membership check via a direct read of
# ~/.loom/workspaces.json (or $LOOM_WORKSPACES_PATH), used ONLY as a
# fallback when the daemon itself is unreachable -- a live daemon's own
# `per_repo` array (from daemon_status_json) is authoritative and takes
# precedence over this. Returns 0 (repo present) or 1 (absent / no registry
# / no jq) -- never errors under `set -e`.
registry_has_repo() {
    command -v jq &>/dev/null || return 1
    local registry_path="${LOOM_WORKSPACES_PATH:-$HOME/.loom/workspaces.json}"
    [[ -f "$registry_path" ]] || return 1
    jq -e --arg root "$REPO_ROOT_CANON" \
        '(.workspaces // []) | any(.root == $root)' \
        "$registry_path" >/dev/null 2>&1
}

# Populate the daemon_* globals this section's callers (emit_human /
# emit_json) branch on, from a single daemon_status_json() call:
#   daemon_bin_found     true|false  -- a loom-daemon binary was located
#   daemon_reachable     true|false  -- the IPC round-trip succeeded
#   daemon_manages_repo  true|false  -- this repo is a managed/registered
#                                       workspace (per_repo match, or the
#                                       registry-file fallback)
#   daemon_in_flight     count of non-terminal sweeps for this repo (per_repo
#                        match only; "0" when reachable but unmatched, empty
#                        when unreachable)
#   daemon_gate_halted   true|false|"" -- per-repo main-health gate state
#                        (per_repo match only; empty when unreachable/unmatched)
#   daemon_install_state NotExpected|ExpectedButDead|AliveStarting|
#                        AliveButUnresponsive|"" (unreachable path only)
#   daemon_liveness_detail  human-readable detail string (unreachable path only)
#   daemon_roles         space-separated distinct role names this repo's
#                        role-runner has ticked recently ("" when none/unreachable)
collect_daemon_state() {
    daemon_bin_found=false
    daemon_reachable=false
    daemon_manages_repo=false
    daemon_in_flight=""
    daemon_gate_halted=""
    daemon_install_state=""
    daemon_liveness_detail=""
    daemon_roles=""

    command -v jq &>/dev/null || return 0
    locate_daemon_bin >/dev/null 2>&1 && daemon_bin_found=true
    [[ "$daemon_bin_found" == "true" ]] || return 0

    local json
    json="$(daemon_status_json)"
    [[ -n "$json" ]] || return 0

    if echo "$json" | jq -e 'has("error") | not' >/dev/null 2>&1; then
        daemon_reachable=true
        local repo_entry
        repo_entry="$(echo "$json" | jq -c --arg root "$REPO_ROOT_CANON" \
            '[(.per_repo // [])[] | select(.root == $root)] | .[0] // empty' 2>/dev/null)"
        if [[ -n "$repo_entry" && "$repo_entry" != "null" ]]; then
            daemon_manages_repo=true
            daemon_in_flight="$(echo "$repo_entry" | jq -r '.in_flight_count // 0' 2>/dev/null)"
            daemon_gate_halted="$(echo "$repo_entry" | jq -r '.health_gate_halted // false' 2>/dev/null)"
        fi
        daemon_roles="$(echo "$json" | jq -r --arg root "$REPO_ROOT_CANON" \
            '[(.role_tick_records // [])[] | select(.root == $root) | .role] | unique | join(" ")' 2>/dev/null)"
    else
        daemon_install_state="$(echo "$json" | jq -r '.install_state.state // empty' 2>/dev/null)"
        daemon_liveness_detail="$(echo "$json" | jq -r '.install_state.liveness_detail // empty' 2>/dev/null)"
    fi

    if [[ "$daemon_manages_repo" != "true" ]] && registry_has_repo; then
        daemon_manages_repo=true
    fi
}

# Compact JSON rendering of the daemon_* globals collect_daemon_state()
# populates, for `emit_json`'s `daemon` key. Must be called AFTER
# collect_daemon_state.
daemon_state_json_obj() {
    command -v jq &>/dev/null || { echo "null"; return 0; }
    local roles_json="[]"
    if [[ -n "$daemon_roles" ]]; then
        roles_json="$(printf '%s\n' "$daemon_roles" | tr ' ' '\n' | jq -R . | jq -sc 'map(select(. != ""))')"
    fi
    jq -nc \
        --argjson binary_found "$daemon_bin_found" \
        --argjson reachable "$daemon_reachable" \
        --argjson manages_repo "$daemon_manages_repo" \
        --arg in_flight "$daemon_in_flight" \
        --arg gate_halted "$daemon_gate_halted" \
        --arg install_state "$daemon_install_state" \
        --arg liveness_detail "$daemon_liveness_detail" \
        --argjson roles "$roles_json" \
        '{
            binary_found: $binary_found,
            reachable: $reachable,
            manages_repo: $manages_repo,
            in_flight: (if $in_flight == "" then null else ($in_flight | tonumber?) end),
            gate_halted: (if $gate_halted == "" then null elif $gate_halted == "true" then true else false end),
            install_state: (if $install_state == "" then null else $install_state end),
            liveness_detail: (if $liveness_detail == "" then null else $liveness_detail end),
            roles: $roles
          }'
}

# ---- JSON output ---------------------------------------------------------
emit_json() {
    if ! command -v jq &>/dev/null; then
        echo '{"error":"jq not installed"}'
        return 0
    fi

    local running
    running=$(get_running_sessions)

    # Build a JSON array of running sessions with pids
    local running_json="[]"
    if [[ -n "$running" ]]; then
        local rows=""
        while IFS= read -r session; do
            [[ -z "$session" ]] && continue
            local id="${session#loom-}"
            local pid uptime
            pid=$(session_pid "$session")
            uptime=$(session_uptime_seconds "$session")
            rows+=$(jq -nc --arg session "$session" --arg id "$id" --arg pid "$pid" --arg uptime "$uptime" \
                '{session:$session, id:$id, pid:($pid|select(.!="")|tonumber?), uptime_seconds:($uptime|select(.!="")|tonumber?)}')
            rows+=$'\n'
        done <<< "$running"
        running_json=$(printf '%s' "$rows" | jq -sc '.')
    fi

    local terminals
    terminals=$(read_terminals)

    local work_queue_json
    work_queue_json=$(work_queue_json)

    collect_daemon_state
    local daemon_json
    daemon_json=$(daemon_state_json_obj)

    jq -nc \
        --argjson running "$running_json" \
        --argjson terminals "$terminals" \
        --argjson work_queue "$work_queue_json" \
        --argjson daemon "$daemon_json" \
        '
        ($running | map(.id)) as $running_ids
        | {
            running: ($terminals | map(
                . as $t
                | ($running[] | select(.id == $t.id)) as $r
                | {
                    id: $t.id,
                    name: ($t.name // $t.id),
                    role: ($t.roleConfig.roleFile // null),
                    session: $r.session,
                    pid: $r.pid,
                    uptime_seconds: $r.uptime_seconds,
                    status: "running"
                  }
              )),
            stopped: ($terminals | map(select(.id as $id | ($running_ids | index($id)) | not))
                | map({
                    id: .id,
                    name: (.name // .id),
                    role: (.roleConfig.roleFile // null),
                    status: "stopped"
                  })),
            unmanaged: ($running | map(select(.id as $rid
                | ($terminals | map(.id) | index($rid)) | not))
                | map({session: .session, id: .id, pid: .pid, uptime_seconds: .uptime_seconds, status: "unmanaged"})),
            work_queue: $work_queue,
            daemon: $daemon
          }'
}

# ---- Human-readable output ----------------------------------------------
emit_human() {
    local running running_ids=()
    running=$(get_running_sessions)

    echo -e "${BOLD}Loom Agent Pool${NC}"
    echo ""
    echo -e "  Workspace: ${CYAN}$REPO_ROOT${NC}"
    # Names every config tier present on disk instead of a single legacy path
    # -- naming only .loom/config.json would misreport "(none)" once a higher
    # tier (e.g. .loom-project/project.json alone) supplies the effective
    # config (#4062).
    local config_tiers_present
    config_tiers_present="$(_loom_status_config_tiers_present | paste -sd ', ' - 2>/dev/null)"
    if [[ -n "$config_tiers_present" ]]; then
        echo -e "  Config:    ${CYAN}$config_tiers_present${NC}"
    else
        echo -e "  Config:    ${GRAY}(none found — checked .loom/config.json, .loom-project/project.json, .loom-local/local.json)${NC}"
    fi
    echo -e "  Socket:    ${CYAN}tmux -L $TMUX_SOCKET${NC}"
    echo ""

    if ! command -v tmux &>/dev/null; then
        echo -e "${YELLOW}tmux is not installed — cannot inspect the agent pool.${NC}"
        return 0
    fi

    # Collect running ids
    if [[ -n "$running" ]]; then
        while IFS= read -r session; do
            [[ -z "$session" ]] && continue
            running_ids+=("${session#loom-}")
        done <<< "$running"
    fi

    # Read config terminals for cross-referencing
    local terminals
    terminals=$(read_terminals)
    local terminal_count
    terminal_count=$(echo "$terminals" | jq 'length' 2>/dev/null || echo 0)

    # Machine-daemon state (#4793) -- collected up front so the "no local
    # agents" branch below can name its own scope explicitly instead of
    # reading as a bare, fleet-wide "No agents running".
    collect_daemon_state

    # Running agents section
    if [[ ${#running_ids[@]} -eq 0 ]]; then
        if [[ "$daemon_manages_repo" == "true" ]]; then
            echo -e "${YELLOW}local agent pool: none${NC} — ${CYAN}machine daemon manages this repo${NC} (see Machine Daemon below)"
        else
            echo -e "${YELLOW}No agents running.${NC}"
        fi
    else
        echo -e "${GREEN}Running agents (${#running_ids[@]}):${NC}"
        echo ""
        local session id name role pid uptime uptime_str
        while IFS= read -r session; do
            [[ -z "$session" ]] && continue
            id="${session#loom-}"
            pid=$(session_pid "$session")
            uptime=$(session_uptime_seconds "$session")
            if [[ -n "$uptime" ]]; then
                uptime_str=$(format_duration "$uptime")
            else
                uptime_str="unknown"
            fi
            name=""
            role=""
            if [[ "$terminal_count" -gt 0 ]]; then
                name=$(echo "$terminals" | jq -r --arg id "$id" \
                    '.[] | select(.id == $id) | (.name // .id)' 2>/dev/null | head -1)
                role=$(echo "$terminals" | jq -r --arg id "$id" \
                    '.[] | select(.id == $id) | (.roleConfig.roleFile // "")' 2>/dev/null | head -1)
            fi
            if [[ -n "$name" ]]; then
                echo -e "  ${GREEN}●${NC} ${BOLD}$id${NC} ($name)   ${GRAY}up ${uptime_str}${NC}"
            else
                echo -e "  ${GREEN}●${NC} ${BOLD}$id${NC} ${YELLOW}(not in config — unmanaged)${NC}   ${GRAY}up ${uptime_str}${NC}"
            fi
            echo -e "      session: ${CYAN}$session${NC}   pid: ${CYAN}${pid:-unknown}${NC}"
            [[ -n "$role" ]] && echo -e "      role:    ${CYAN}$role${NC}"
            # For unmanaged sessions (present on the socket but absent from
            # config), print the exact recovery command to tear it down.
            if [[ -z "$name" ]]; then
                echo -e "      ${YELLOW}recover:${NC} ${CYAN}tmux -L $TMUX_SOCKET kill-session -t $session${NC}"
            fi
        done <<< "$running"
    fi

    # Configured-but-not-running section
    if [[ "$terminal_count" -gt 0 ]]; then
        local stopped
        stopped=$(echo "$terminals" | jq -r \
            --argjson running "$(printf '%s\n' "${running_ids[@]:-}" | jq -R . | jq -sc 'map(select(. != ""))')" \
            '.[] | select(.id as $id | ($running | index($id)) | not)
                | "\(.id)\t\((.name // .id))\t\((.roleConfig.roleFile // ""))"' 2>/dev/null || true)
        if [[ -n "$stopped" ]]; then
            echo ""
            # Escalated to RED (was advisory yellow): a configured agent that is
            # not running should be impossible to miss. NOTE: until a
            # supervisory layer ships a `scaled_to_zero` marker (Proposal 3,
            # deferred), this bucket cannot distinguish "crashed" from
            # "intentionally not started" — both render identically here.
            echo -e "${RED}${BOLD}Configured but not running:${NC}"
            echo ""
            while IFS=$'\t' read -r sid sname srole; do
                [[ -z "$sid" ]] && continue
                echo -e "  ${RED}○${NC} ${BOLD}$sid${NC} ($sname)${srole:+   role: $srole}"
            done <<< "$stopped"
        fi
    fi

    # Machine Daemon section (#4793): the local tmux pool above is only ONE
    # of two ways work gets picked up in this repo -- a separate
    # machine-level `loom-daemon` fleet can independently dispatch sweeps
    # here with zero local tmux sessions. Omitted entirely when no
    # loom-daemon binary is resolvable at all (nothing to report, same
    # graceful-degradation shape as the Work Queue section below).
    if [[ "$daemon_bin_found" == "true" ]]; then
        echo ""
        echo -e "${BOLD}Machine Daemon:${NC}"
        if [[ "$daemon_reachable" == "true" ]]; then
            echo -e "  Daemon:    ${GREEN}alive${NC} (reachable over IPC)"
            if [[ "$daemon_manages_repo" == "true" ]]; then
                echo -e "  Registry:  ${GREEN}this repo is managed${NC} (in-flight sweeps: ${CYAN}${daemon_in_flight:-0}${NC})"
                if [[ "$daemon_gate_halted" == "true" ]]; then
                    echo -e "  Gate:      ${RED}HALTED${NC} (main-health gate is blocking dispatch for this repo)"
                else
                    echo -e "  Gate:      ${GREEN}open${NC} (dispatch not gated)"
                fi
                if [[ -n "$daemon_roles" ]]; then
                    echo -e "  Roles:     ${CYAN}${daemon_roles// /, }${NC} (recently ticked by the role runner)"
                fi
            else
                echo -e "  Registry:  ${YELLOW}this repo is NOT a registered/managed workspace${NC}"
            fi
        else
            local state_desc="${daemon_install_state:-not running}"
            echo -e "  Daemon:    ${YELLOW}unreachable${NC} (${state_desc}${daemon_liveness_detail:+: $daemon_liveness_detail})"
            if [[ "$daemon_manages_repo" == "true" ]]; then
                echo -e "  Registry:  ${CYAN}this repo IS registered${NC} in ~/.loom/workspaces.json, but the daemon is not answering — dispatch is currently NOT happening"
            fi
        fi
        echo -e "  ${GRAY}(full detail: ./.loom/bin/loom health   or  loom-daemon status)${NC}"
    fi

    # Work-queue depth section (open issue/PR counts by label). Omitted
    # entirely — never an error — when gh is unavailable or the forge is not
    # GitHub, so the script still exits 0 on any forge.
    local wq
    wq=$(work_queue_json)
    if [[ -n "$wq" && "$wq" != "null" ]]; then
        local wq_issue wq_rr wq_pr wq_architect wq_hermit wq_curated wq_auditor
        wq_issue=$(echo "$wq" | jq -r '."loom:issue"' 2>/dev/null)
        wq_rr=$(echo "$wq" | jq -r '."loom:review-requested"' 2>/dev/null)
        wq_pr=$(echo "$wq" | jq -r '."loom:pr"' 2>/dev/null)
        wq_architect=$(echo "$wq" | jq -r '."loom:architect"' 2>/dev/null)
        wq_hermit=$(echo "$wq" | jq -r '."loom:hermit"' 2>/dev/null)
        wq_curated=$(echo "$wq" | jq -r '."loom:curated"' 2>/dev/null)
        wq_auditor=$(echo "$wq" | jq -r '."loom:auditor"' 2>/dev/null)
        echo ""
        echo -e "${BOLD}Work Queue:${NC}"
        echo -e "  ${CYAN}loom:issue${NC} ${wq_issue}   ${CYAN}loom:review-requested${NC} ${wq_rr}   ${CYAN}loom:pr${NC} ${wq_pr}"
        echo -e "  ${BOLD}Pending proposals:${NC} ${CYAN}loom:architect${NC} ${wq_architect}   ${CYAN}loom:hermit${NC} ${wq_hermit}   ${CYAN}loom:curated${NC} ${wq_curated}   ${CYAN}loom:auditor${NC} ${wq_auditor}"
    fi

    echo ""
}

# Main
main() {
    local json=false
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help|-h)
                show_help
                exit 0
                ;;
            --json)
                json=true
                shift
                ;;
            -*)
                echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
                echo "Use 'loom status --help' for usage" >&2
                exit 1
                ;;
            *)
                echo -e "${RED}Error: Unexpected argument '$1'${NC}" >&2
                exit 1
                ;;
        esac
    done

    if [[ "$json" == "true" ]]; then
        emit_json
    else
        emit_human
    fi
    exit 0
}

main "$@"
