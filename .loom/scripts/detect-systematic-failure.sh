#!/bin/bash
# detect-systematic-failure.sh - Detect systematic failure patterns across shepherd failures
#
# Usage:
#   detect-systematic-failure.sh              # Check for systematic failures (dry run)
#   detect-systematic-failure.sh --update     # Check and update daemon-state.json
#   detect-systematic-failure.sh --clear      # Clear systematic failure state
#   detect-systematic-failure.sh --probe-started  # Increment probe count and set new cooldown
#   detect-systematic-failure.sh --probe-success  # Clear systematic failure after successful probe
#   detect-systematic-failure.sh --json       # JSON output
#   detect-systematic-failure.sh --help       # Show help
#
# Analyzes the recent_failures array in daemon-state.json to detect when N consecutive
# shepherd failures share the same error_class. When detected, sets the systematic_failure
# field in daemon-state.json which causes the snapshot system to pause shepherd spawning.
#
# Auto-clear mechanism:
#   After a cooldown period (default 30min), the daemon can spawn a probe shepherd.
#   If the probe succeeds, the systematic failure is cleared.
#   If it fails, the cooldown is extended with exponential backoff.
#
# Configuration:
#   LOOM_SYSTEMATIC_FAILURE_THRESHOLD - Consecutive same-error failures to trigger (default: 3)
#   LOOM_SYSTEMATIC_FAILURE_COOLDOWN - Seconds before first probe attempt (default: 1800)
#   LOOM_SYSTEMATIC_FAILURE_MAX_PROBES - Maximum probe attempts before giving up (default: 3)

set -euo pipefail

SYSTEMATIC_FAILURE_THRESHOLD="${LOOM_SYSTEMATIC_FAILURE_THRESHOLD:-3}"
SYSTEMATIC_FAILURE_COOLDOWN="${LOOM_SYSTEMATIC_FAILURE_COOLDOWN:-1800}"
# shellcheck disable=SC2034  # Documented for configuration, used by Python snapshot
SYSTEMATIC_FAILURE_MAX_PROBES="${LOOM_SYSTEMATIC_FAILURE_MAX_PROBES:-3}"

# Find the repository root
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
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
            if [[ -d "$dir/.loom" ]]; then
                echo "$dir"
                return 0
            fi
        fi
        dir="$(dirname "$dir")"
    done
    echo "Error: Not in a git repository with .loom directory" >&2
    return 1
}

REPO_ROOT=$(find_repo_root)
DAEMON_STATE_FILE="$REPO_ROOT/.loom/daemon-state.json"

# Parse arguments
UPDATE=false
CLEAR=false
JSON_OUTPUT=false
PROBE_STARTED=false
PROBE_SUCCESS=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --update)
            UPDATE=true
            shift
            ;;
        --clear)
            CLEAR=true
            shift
            ;;
        --probe-started)
            PROBE_STARTED=true
            shift
            ;;
        --probe-success)
            PROBE_SUCCESS=true
            shift
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --help|-h)
            echo "Usage: detect-systematic-failure.sh [--update] [--clear] [--probe-started] [--probe-success] [--json]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

if [[ ! -f "$DAEMON_STATE_FILE" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"detected": false, "reason": "no_state_file"}'
    else
        echo "No daemon state file found"
    fi
    exit 0
fi

# Handle --clear
if [[ "$CLEAR" == "true" ]]; then
    temp_file=$(mktemp)
    if jq '.systematic_failure = {} | .recent_failures = []' "$DAEMON_STATE_FILE" > "$temp_file" 2>/dev/null; then
        mv "$temp_file" "$DAEMON_STATE_FILE"
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"cleared": true}'
        else
            echo "Systematic failure state cleared"
        fi
    else
        rm -f "$temp_file"
        echo "Failed to clear state" >&2
        exit 1
    fi
    exit 0
fi

# Handle --probe-success (clear systematic failure after successful probe)
if [[ "$PROBE_SUCCESS" == "true" ]]; then
    temp_file=$(mktemp)
    if jq '.systematic_failure = {} | .recent_failures = []' "$DAEMON_STATE_FILE" > "$temp_file" 2>/dev/null; then
        mv "$temp_file" "$DAEMON_STATE_FILE"
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"cleared": true, "reason": "probe_success"}'
        else
            echo "Systematic failure cleared after successful probe"
        fi
    else
        rm -f "$temp_file"
        echo "Failed to clear state" >&2
        exit 1
    fi
    exit 0
fi

# Handle --probe-started (increment probe count and set new cooldown)
if [[ "$PROBE_STARTED" == "true" ]]; then
    # Get current probe count
    CURRENT_PROBE_COUNT=$(jq -r '.systematic_failure.probe_count // 0' "$DAEMON_STATE_FILE" 2>/dev/null || echo "0")
    NEW_PROBE_COUNT=$((CURRENT_PROBE_COUNT + 1))

    # Calculate cooldown with exponential backoff: base * 2^probe_count
    EFFECTIVE_COOLDOWN=$((SYSTEMATIC_FAILURE_COOLDOWN * (1 << NEW_PROBE_COUNT)))

    # Calculate cooldown_until timestamp
    NOW_EPOCH=$(date +%s)
    COOLDOWN_UNTIL_EPOCH=$((NOW_EPOCH + EFFECTIVE_COOLDOWN))
    COOLDOWN_UNTIL_ISO=$(date -u -r "$COOLDOWN_UNTIL_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "@$COOLDOWN_UNTIL_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)

    temp_file=$(mktemp)
    if jq --argjson probe_count "$NEW_PROBE_COUNT" \
          --arg cooldown_until "$COOLDOWN_UNTIL_ISO" '
        .systematic_failure.probe_count = $probe_count |
        .systematic_failure.cooldown_until = $cooldown_until
    ' "$DAEMON_STATE_FILE" > "$temp_file" 2>/dev/null; then
        mv "$temp_file" "$DAEMON_STATE_FILE"
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            jq -n \
                --argjson probe_count "$NEW_PROBE_COUNT" \
                --arg cooldown_until "$COOLDOWN_UNTIL_ISO" \
                --argjson cooldown_seconds "$EFFECTIVE_COOLDOWN" \
                '{probe_started: true, probe_count: $probe_count, cooldown_until: $cooldown_until, cooldown_seconds: $cooldown_seconds}'
        else
            echo "Probe started (count: $NEW_PROBE_COUNT)"
            echo "  Next cooldown: $EFFECTIVE_COOLDOWN seconds"
            echo "  Cooldown until: $COOLDOWN_UNTIL_ISO"
        fi
    else
        rm -f "$temp_file"
        echo "Failed to update probe state" >&2
        exit 1
    fi
    exit 0
fi

# Read recent failures
RECENT_FAILURES=$(jq -r '.recent_failures // []' "$DAEMON_STATE_FILE" 2>/dev/null || echo "[]")
FAILURE_COUNT=$(echo "$RECENT_FAILURES" | jq 'length')

if [[ "$FAILURE_COUNT" -lt "$SYSTEMATIC_FAILURE_THRESHOLD" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo "{\"detected\": false, \"reason\": \"insufficient_failures\", \"failure_count\": $FAILURE_COUNT, \"threshold\": $SYSTEMATIC_FAILURE_THRESHOLD}"
    else
        echo "Not enough failures ($FAILURE_COUNT < $SYSTEMATIC_FAILURE_THRESHOLD)"
    fi
    exit 0
fi

# Check the last N failures for the same error class
LAST_N=$(echo "$RECENT_FAILURES" | jq --argjson n "$SYSTEMATIC_FAILURE_THRESHOLD" '.[-$n:]')
UNIQUE_CLASSES=$(echo "$LAST_N" | jq -r '[.[].error_class] | unique | length')
COMMON_CLASS=$(echo "$LAST_N" | jq -r '[.[].error_class] | unique | if length == 1 then .[0] else "mixed" end')

DETECTED=false
if [[ "$UNIQUE_CLASSES" -eq 1 ]]; then
    DETECTED=true
fi

NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Calculate initial cooldown_until timestamp
NOW_EPOCH=$(date +%s)
COOLDOWN_UNTIL_EPOCH=$((NOW_EPOCH + SYSTEMATIC_FAILURE_COOLDOWN))
COOLDOWN_UNTIL_ISO=$(date -u -r "$COOLDOWN_UNTIL_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "@$COOLDOWN_UNTIL_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)

# Update daemon-state.json if requested
if [[ "$UPDATE" == "true" ]]; then
    temp_file=$(mktemp)
    if [[ "$DETECTED" == "true" ]]; then
        if jq --arg pattern "$COMMON_CLASS" \
               --argjson count "$SYSTEMATIC_FAILURE_THRESHOLD" \
               --arg detected_at "$NOW_ISO" \
               --arg cooldown_until "$COOLDOWN_UNTIL_ISO" '
            .systematic_failure = {
                active: true,
                pattern: $pattern,
                count: $count,
                detected_at: $detected_at,
                cooldown_until: $cooldown_until,
                probe_count: 0
            }
        ' "$DAEMON_STATE_FILE" > "$temp_file" 2>/dev/null; then
            mv "$temp_file" "$DAEMON_STATE_FILE"
        else
            rm -f "$temp_file"
        fi
    else
        # No systematic failure - clear if previously set
        if jq '.systematic_failure = {}' "$DAEMON_STATE_FILE" > "$temp_file" 2>/dev/null; then
            mv "$temp_file" "$DAEMON_STATE_FILE"
        else
            rm -f "$temp_file"
        fi
    fi
fi

# Output results
if [[ "$JSON_OUTPUT" == "true" ]]; then
    jq -n \
        --argjson detected "$DETECTED" \
        --arg pattern "$COMMON_CLASS" \
        --argjson threshold "$SYSTEMATIC_FAILURE_THRESHOLD" \
        --argjson failure_count "$FAILURE_COUNT" \
        --argjson unique_classes "$UNIQUE_CLASSES" \
        '{
            detected: $detected,
            pattern: $pattern,
            threshold: $threshold,
            failure_count: $failure_count,
            unique_error_classes: $unique_classes
        }'
else
    if [[ "$DETECTED" == "true" ]]; then
        echo "WARNING: Systematic failure detected"
        echo "  Pattern: $COMMON_CLASS"
        echo "  Consecutive failures: $SYSTEMATIC_FAILURE_THRESHOLD"
        echo "  Shepherd spawning should be paused"
    else
        echo "No systematic failure detected"
        echo "  Recent failures: $FAILURE_COUNT"
        echo "  Unique error classes: $UNIQUE_CLASSES"
    fi
fi
