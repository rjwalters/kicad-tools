#!/usr/bin/env bash
# skill-router.sh - UserPromptSubmit hook for agent routing suggestions
#
# Claude Code UserPromptSubmit hook that injects agent routing context.
# Receives JSON on stdin with { "prompt": "...", "session_id": "...", "cwd": "..." }
#
# Behavior:
#   1. Always injects a compact agent routing table as additionalContext
#   2. Optionally emits an AGENT_ROUTE directive when prompt strongly matches
#      a domain pattern (first match wins)
#
# Output format (Claude Code hooks spec):
#   { "hookSpecificOutput": { "hookEventName": "UserPromptSubmit", "additionalContext": "..." } }
#
# Opt-in: Only activates when .loom/config/skill-routes.json exists.
# If the config file is missing, the hook exits silently (no context injected).
#
# Error handling: This script MUST never exit with a non-zero code or produce
# invalid output. Any internal error results in a silent exit 0.

set -o pipefail

# Determine main repo root via git-common-dir (works from worktrees)
MAIN_ROOT="$(cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd)" || \
MAIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd 2>/dev/null || echo ".")"

HOOK_ERROR_LOG="${MAIN_ROOT}/.loom/logs/hook-errors.log"

# Log a diagnostic error message (best-effort, never fails the script)
log_hook_error() {
    local msg="$1"
    mkdir -p "$(dirname "$HOOK_ERROR_LOG")" 2>/dev/null || true
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [skill-router] $msg" >> "$HOOK_ERROR_LOG" 2>/dev/null || true
}

# Top-level error trap: on ANY unexpected error, exit silently
trap 'log_hook_error "Unexpected error on line ${LINENO}: ${BASH_COMMAND:-unknown} (exit=$?)"; exit 0' ERR

# Read stdin safely
INPUT=$(cat 2>/dev/null) || INPUT=""

# Verify jq is available
if ! command -v jq &>/dev/null; then
    log_hook_error "jq not found in PATH"
    exit 0
fi

# Extract prompt
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null) || PROMPT=""

# If no prompt, nothing to do
if [[ -z "$PROMPT" ]]; then
    exit 0
fi

# Skip orchestrator pulse prompts (start with /self)
if [[ "$PROMPT" == /self* ]]; then
    exit 0
fi

# =============================================================================
# ROUTING CONFIG
# =============================================================================

ROUTES_FILE="${MAIN_ROOT}/.loom/config/skill-routes.json"
ROUTES_LOCAL="${MAIN_ROOT}/.loom/config/skill-routes.local.json"

# Opt-in check: if no config file exists, exit silently
if [[ ! -f "$ROUTES_FILE" ]]; then
    exit 0
fi

# Validate config file is valid JSON
if ! jq empty "$ROUTES_FILE" 2>/dev/null; then
    log_hook_error "Invalid JSON in $ROUTES_FILE"
    exit 0
fi

# Merge routes: local routes first (higher priority), then main routes
# Local routes file is optional
ROUTES_JSON=""
if [[ -f "$ROUTES_LOCAL" ]] && jq empty "$ROUTES_LOCAL" 2>/dev/null; then
    # Merge: local routes prepended to main routes
    ROUTES_JSON=$(jq -s '.[0].routes + .[1].routes' "$ROUTES_LOCAL" "$ROUTES_FILE" 2>/dev/null) || ROUTES_JSON=""
fi

if [[ -z "$ROUTES_JSON" ]]; then
    ROUTES_JSON=$(jq '.routes' "$ROUTES_FILE" 2>/dev/null) || ROUTES_JSON=""
fi

if [[ -z "$ROUTES_JSON" ]] || [[ "$ROUTES_JSON" == "null" ]]; then
    log_hook_error "No routes found in config"
    exit 0
fi

# =============================================================================
# BUILD AGENT TABLE
# =============================================================================

# Build compact agent routing table from config
AGENT_TABLE=$(echo "$ROUTES_JSON" | jq -r '.[] | "\(.agent) — \(.description)"' 2>/dev/null) || AGENT_TABLE=""

if [[ -z "$AGENT_TABLE" ]]; then
    log_hook_error "Failed to build agent table"
    exit 0
fi

# =============================================================================
# PATTERN MATCHING
# =============================================================================

PROMPT_LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]')
MATCHED_AGENT=""
MATCHED_DESC=""

# Iterate routes in order (first match wins)
ROUTE_COUNT=$(echo "$ROUTES_JSON" | jq 'length' 2>/dev/null) || ROUTE_COUNT=0

for (( i=0; i<ROUTE_COUNT; i++ )); do
    PATTERN=$(echo "$ROUTES_JSON" | jq -r ".[$i].pattern // empty" 2>/dev/null) || continue
    AGENT=$(echo "$ROUTES_JSON" | jq -r ".[$i].agent // empty" 2>/dev/null) || continue
    DESC=$(echo "$ROUTES_JSON" | jq -r ".[$i].description // empty" 2>/dev/null) || continue

    if [[ -z "$PATTERN" ]] || [[ -z "$AGENT" ]]; then
        continue
    fi

    # Match pattern case-insensitively against prompt
    if echo "$PROMPT_LOWER" | grep -qiE "$PATTERN" 2>/dev/null; then
        MATCHED_AGENT="$AGENT"
        MATCHED_DESC="$DESC"
        break
    fi
done

# =============================================================================
# OUTPUT
# =============================================================================

# Build the additionalContext string
CONTEXT="Available Loom agents:
${AGENT_TABLE}"

if [[ -n "$MATCHED_AGENT" ]]; then
    CONTEXT="${CONTEXT}

AGENT_ROUTE: ${MATCHED_AGENT} — ${MATCHED_DESC}
(This is a suggestion based on prompt keywords. Use the Skill tool to invoke if appropriate.)"
fi

# Output valid JSON
jq -n --arg context "$CONTEXT" '{
    hookSpecificOutput: {
        hookEventName: "UserPromptSubmit",
        additionalContext: $context
    }
}' 2>/dev/null || {
    log_hook_error "Failed to produce JSON output"
    exit 0
}

exit 0
