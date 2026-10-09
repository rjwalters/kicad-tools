#!/usr/bin/env bash
# host-sleep-config.sh — shared resolver for the `host.preventSleep` /
# `host.sleepMitigationAcknowledged` config knobs (issue #6311).
#
# #3350 shipped `check-host-sleep.sh`, an ADVISORY-ONLY warning printed at the
# start of long-running orchestration. It never mutates anything, so an
# operator re-reads (and re-applies) the same mitigation by hand on every
# host, every run. This file adds the config surface that lets a repo opt
# INTO Loom applying the Linux mitigation itself, and lets an operator record
# an evaluated macOS mitigation so the warning stops being permanent noise.
#
# Source this file (do not exec). Depends on `loom_config_get`
# (lib/config-resolver.sh) — sourced automatically if not already loaded in
# the calling shell.
#
# Precedence (env > config > default), the tier order every other Loom knob
# in this repo uses:
#   $LOOM_HOST_PREVENT_SLEEP                 > host.preventSleep                 > false
#   $LOOM_HOST_SLEEP_MITIGATION_ACKNOWLEDGED > host.sleepMitigationAcknowledged  > ""
#
# `host.preventSleep` resolution NEVER fails a caller: a malformed value at
# ANY tier (anything that is not a recognizable true/false spelling) warns to
# stderr and falls back to disabled ("0") — this knob must never block or
# fail a sweep (#6311 acceptance criterion: "An invalid/malformed value warns
# and falls back to off; it never blocks or fails a sweep").
#
# Absent config (no `host` block at all) resolves to the exact same "0" a
# malformed value falls back to — byte-identical to today's behavior for
# every repo that has not opted in (#6311 acceptance criterion).

# _loom_host_sleep_config_ensure_resolver — sources lib/config-resolver.sh
# (relative to this file) exactly once, only if `loom_config_get` is not
# already defined in the calling shell. Soft-fails silently (leaves
# `loom_config_get` undefined) when config-resolver.sh cannot be found —
# callers below already guard every call with `declare -F`.
_loom_host_sleep_config_ensure_resolver() {
    if declare -F loom_config_get >/dev/null 2>&1; then
        return 0
    fi
    local _lib_dir
    _lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
    if [[ -n "$_lib_dir" && -f "$_lib_dir/config-resolver.sh" ]]; then
        # shellcheck source=./config-resolver.sh
        source "$_lib_dir/config-resolver.sh"
    fi
}

# loom_host_prevent_sleep_enabled <repo_root>
#
# Echoes "1" (enabled) or "0" (disabled — covers default-off, an explicit
# false/off spelling, AND any malformed value). Never fails: on any error
# resolving config (missing jq, unreadable file, etc.) this falls back to
# "0" exactly like an absent config block.
loom_host_prevent_sleep_enabled() {
    local repo_root="$1"
    local raw="" desc="" raw_lower=""

    if [[ -n "${LOOM_HOST_PREVENT_SLEEP+set}" ]]; then
        raw="$LOOM_HOST_PREVENT_SLEEP"
        desc='$LOOM_HOST_PREVENT_SLEEP'
    else
        _loom_host_sleep_config_ensure_resolver
        if declare -F loom_config_get >/dev/null 2>&1; then
            raw="$(loom_config_get "$repo_root" "host.preventSleep" "" 2>/dev/null || true)"
        fi
        desc="host.preventSleep (resolved config)"
    fi

    raw_lower="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
    case "$raw_lower" in
        1 | true | yes | on)
            echo "1"
            ;;
        "" | 0 | false | no | off)
            echo "0"
            ;;
        *)
            echo "[host-sleep-config] WARNING: malformed value for $desc ('$raw'); expected true/false. Falling back to disabled (this knob never blocks a sweep, issue #6311)." >&2
            echo "0"
            ;;
    esac
}

# loom_host_sleep_mitigation_acknowledged <repo_root>
#
# Echoes the operator's recorded macOS mitigation text (e.g. "pmset sleep=0
# set at image build"), or "" when unset at every tier. Freeform — never
# validated, never fails.
loom_host_sleep_mitigation_acknowledged() {
    local repo_root="$1"

    if [[ -n "${LOOM_HOST_SLEEP_MITIGATION_ACKNOWLEDGED+set}" ]]; then
        printf '%s' "$LOOM_HOST_SLEEP_MITIGATION_ACKNOWLEDGED"
        return 0
    fi

    _loom_host_sleep_config_ensure_resolver
    if declare -F loom_config_get >/dev/null 2>&1; then
        loom_config_get "$repo_root" "host.sleepMitigationAcknowledged" "" 2>/dev/null || true
    fi
}
