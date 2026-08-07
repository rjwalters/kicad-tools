#!/usr/bin/env bash
# mcp-config.sh — shared resolution for the optional safehouse MCP server
# injected into worker sessions (issue #3999, phase 2 of the safehouse
# fleet-comms layer #3997).
#
# Source this file (do not exec). It defines resolvers that mirror the daemon's
# `safehouse` config-block precedence (env > config > default) from
# loom-daemon/src/safehouse.rs, plus a per-worker persona picker and a
# `.mcp.json` emitter that always lists the `loom` server FIRST.
#
# The daemon narrates as ONE static operator-provisioned persona
# (`safehouse.persona`, default `loom_daemon`). safehoused reads its persona
# allowlist once at boot with no runtime registration and no prefix matching,
# so literal per-issue names (`loom_builder_42`) cannot be registered at
# dispatch time. Instead, workers draw from a BOUNDED, PRE-REGISTERED pool
# (`safehouse.workerPersonas`) an operator adds to safehoused's allowlist
# ahead of time; each concurrent worker is assigned one pool entry (round-robin
# by its issue/worktree slot). When no pool is configured, every worker falls
# back to the scalar `safehouse.persona` (workspace-wide, no per-worker
# attribution) — the feature degrades, it never fails.
#
# Every resolver soft-degrades: a missing/malformed config, a missing `jq`, or
# a disabled block yields the safe default and never aborts the caller.

_LOOM_MCP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./config-resolver.sh
source "$_LOOM_MCP_LIB_DIR/config-resolver.sh"

# _loom_mcp_truthy <value> -> exit 0 when truthy, 1 otherwise.
# Matches loom-daemon/src/safehouse.rs `env_bool`: 1/true/yes/on ⇒ true.
_loom_mcp_truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
        1 | true | yes | on) return 0 ;;
        *) return 1 ;;
    esac
}

# loom_mcp_safehouse_enabled <repo_root> -> echoes "true" or "false".
# Precedence: LOOM_SAFEHOUSE_ENABLED (env) > safehouse.enabled (config) >
# false (default). An explicitly-empty env value ("") disables (matches the
# daemon's env_bool, which maps "" ⇒ false).
loom_mcp_safehouse_enabled() {
    local repo_root="$1"
    if [[ -n "${LOOM_SAFEHOUSE_ENABLED+set}" ]]; then
        if _loom_mcp_truthy "$LOOM_SAFEHOUSE_ENABLED"; then echo true; else echo false; fi
        return 0
    fi
    local v
    v="$(loom_config_get "$repo_root" "safehouse.enabled" "false")"
    if _loom_mcp_truthy "$v"; then echo true; else echo false; fi
}

# loom_mcp_safehouse_socket <repo_root> -> echoes the resolved socket path, or
# empty when none resolves. Precedence mirrors safehouse.rs `resolve_socket`:
# LOOM_SAFEHOUSE_SOCKET (env) > SAFEHOUSED_SOCKET (env) > safehouse.socket
# (config). Env wins here so a host-specific env override always takes effect
# over a committed/shared config value.
#
# DELIBERATELY no conventional-path fallback (issue #5523). #5457 removed a
# hardcoded macOS socket path that had been committed to the SHARED
# .loom/config.json — every host that `git pull`ed main inherited a path to a
# socket that did not exist on it, with no env override able to win while
# that stale committed value stayed in place. Adding a code-level default here
# would not reintroduce that exact failure mode (a code default can't go
# stale via `git pull`), but it does reintroduce the underlying risk this
# function's callers must not paper over: "resolves to *something*" silently
# stops meaning "actually reaches a live safehoused". #5523's incident was
# caused by the RESULTING silence being invisible for 11 hours, not by the
# absence of a default — so the fix there is to make "enabled but
# unresolved/unreachable" LOUD (see spawn-claude.sh's safehouse warning and
# check-safehouse-socket.sh), not to guess a path here. See
# defaults/docs/safehouse.md "Socket resolution" for the full writeup.
loom_mcp_safehouse_socket() {
    local repo_root="$1"
    if [[ -n "${LOOM_SAFEHOUSE_SOCKET:-}" ]]; then
        printf '%s\n' "$LOOM_SAFEHOUSE_SOCKET"
        return 0
    fi
    if [[ -n "${SAFEHOUSED_SOCKET:-}" ]]; then
        printf '%s\n' "$SAFEHOUSED_SOCKET"
        return 0
    fi
    local cfg
    cfg="$(loom_config_get "$repo_root" "safehouse.socket" "")"
    if [[ -n "$cfg" && "$cfg" != "null" ]]; then
        printf '%s\n' "$cfg"
        return 0
    fi
    printf '\n'
}

# loom_mcp_safehouse_persona_fallback <repo_root> -> echoes the scalar persona.
# Precedence: LOOM_SAFEHOUSE_PERSONA (env) > safehouse.persona (config) >
# loom_daemon (default). Used when no worker pool is configured.
loom_mcp_safehouse_persona_fallback() {
    local repo_root="$1"
    if [[ -n "${LOOM_SAFEHOUSE_PERSONA:-}" ]]; then
        printf '%s\n' "$LOOM_SAFEHOUSE_PERSONA"
        return 0
    fi
    loom_config_get "$repo_root" "safehouse.persona" "loom_daemon"
}

# loom_mcp_worker_personas <repo_root> -> echoes a comma-separated pool, or
# empty when none is configured. Precedence: LOOM_SAFEHOUSE_WORKER_PERSONAS
# (env, comma-separated) > safehouse.workerPersonas (config array).
loom_mcp_worker_personas() {
    local repo_root="$1"
    if [[ -n "${LOOM_SAFEHOUSE_WORKER_PERSONAS:-}" ]]; then
        printf '%s\n' "$LOOM_SAFEHOUSE_WORKER_PERSONAS"
        return 0
    fi
    if ! command -v jq >/dev/null 2>&1; then
        printf '\n'
        return 0
    fi
    local effective
    effective="$(loom_resolve_config "$repo_root")"
    printf '%s' "$effective" | jq -r '
        (.safehouse.workerPersonas // [])
        | if type == "array"
          then (map(select(type == "string" and (. | length) > 0)) | join(","))
          else "" end
    ' 2>/dev/null || printf '\n'
}

# loom_mcp_safehouse_command <repo_root> -> echoes the safehouse-mcp launcher
# command. Precedence: LOOM_SAFEHOUSE_MCP_COMMAND (env) > safehouse.mcpCommand
# (config) > safehouse-mcp (default). The exact binary lives in the external
# rjwalters/safehouse repo and cannot be verified here, so it is configurable.
loom_mcp_safehouse_command() {
    local repo_root="$1"
    if [[ -n "${LOOM_SAFEHOUSE_MCP_COMMAND:-}" ]]; then
        printf '%s\n' "$LOOM_SAFEHOUSE_MCP_COMMAND"
        return 0
    fi
    loom_config_get "$repo_root" "safehouse.mcpCommand" "safehouse-mcp"
}

# loom_mcp_pick_persona <issue> <pool_csv> <fallback> -> echoes the persona for
# this worker. Round-robins the pool by `issue % pool_size` so two concurrent
# workers (distinct issue numbers) get distinct personas whenever the pool is
# at least as large as the concurrent-worker count and their numbers do not
# collide mod N. Falls back to `fallback` when the pool is empty; falls back to
# the first pool entry when the issue slot is unknown (deterministic).
loom_mcp_pick_persona() {
    local issue="$1" pool_csv="$2" fallback="$3"
    if [[ -z "$pool_csv" ]]; then
        printf '%s\n' "$fallback"
        return 0
    fi
    local -a pool
    IFS=',' read -r -a pool <<<"$pool_csv"
    local n=${#pool[@]}
    if [[ "$n" -eq 0 ]]; then
        printf '%s\n' "$fallback"
        return 0
    fi
    if [[ -z "$issue" || ! "$issue" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "${pool[0]}"
        return 0
    fi
    local idx=$((issue % n))
    printf '%s\n' "${pool[$idx]}"
}

# loom_mcp_emit_config <workspace_root> [socket] [persona] [command]
# Echoes a full .mcp.json document. The `loom` server is ALWAYS emitted first
# (claude-wrapper.sh's MCP pre-flight extracts its staleness entry point from
# the first server with args, so order is load-bearing). A `safehouse` server
# is appended SECOND only when socket, persona, AND command are all non-empty.
# The socket path is the only credential-adjacent value written — no token or
# key is ever emitted.
loom_mcp_emit_config() {
    LOOM_MCP_WS="$1" \
        LOOM_MCP_SH_SOCKET="${2:-}" \
        LOOM_MCP_SH_PERSONA="${3:-}" \
        LOOM_MCP_SH_COMMAND="${4:-}" \
        "${LOOM_PYTHON:-python3}" - <<'PY'
import json, os, collections

ws = os.environ["LOOM_MCP_WS"]
socket = os.environ.get("LOOM_MCP_SH_SOCKET", "").strip()
persona = os.environ.get("LOOM_MCP_SH_PERSONA", "").strip()
command = os.environ.get("LOOM_MCP_SH_COMMAND", "").strip()

servers = collections.OrderedDict()
# loom MUST be first — claude-wrapper.sh's pre-flight keys off the first
# server with args.
servers["loom"] = {
    "command": "node",
    "args": [os.path.join(ws, "mcp-loom", "dist", "index.js")],
    "env": {"LOOM_WORKSPACE": ws},
}
if socket and persona and command:
    servers["safehouse"] = {
        "command": command,
        "args": [],
        "env": {
            "SAFEHOUSED_SOCKET": socket,
            "SAFEHOUSE_PERSONA": persona,
        },
    }
print(json.dumps({"mcpServers": servers}, indent=2))
PY
}
