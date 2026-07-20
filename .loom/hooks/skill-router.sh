#!/usr/bin/env bash
# skill-router.sh - UserPromptSubmit hook for agent routing suggestions
#
# Claude Code UserPromptSubmit hook that injects agent routing context.
# Receives JSON on stdin with { "prompt": "...", "session_id": "...", "cwd": "..." }
#
# Behavior:
#   1. Emits nothing unless the prompt strongly matches a domain route pattern
#      (first match wins). Non-matching turns produce NO additionalContext —
#      the agent table is no longer injected on every prompt (issue #3609).
#   2. On a match, emits an AGENT_ROUTE directive. A compact agent routing
#      table is appended at most ONCE per session, deduped via the session_id
#      present on stdin (a missing session_id degrades to per-match inclusion).
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

# Extract session_id (used for once-per-session table dedup; optional — a
# missing/empty value degrades gracefully, see the PER-SESSION TABLE DEDUP block)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null) || SESSION_ID=""

# If no prompt, nothing to do
if [[ -z "$PROMPT" ]]; then
    exit 0
fi

# Skip orchestrator pulse prompts (start with /self)
if [[ "$PROMPT" == /self* ]]; then
    exit 0
fi

# Skip harness-generated task-notification turns. These are not human input —
# the harness re-runs UserPromptSubmit hooks on every background-task completion,
# and re-injecting the agent table / re-routing on them is pure noise.
# Match against the raw prompt with literal prefix/substring (no regex) so this
# guard cannot itself false-positive on human text.
case "$PROMPT" in
    "[SYSTEM NOTIFICATION"*) exit 0 ;;
esac
if [[ "$PROMPT" == *"<task-notification>"* ]]; then
    exit 0
fi

# =============================================================================
# MINIMUM-SIGNAL GATE (#3609)
# =============================================================================
# Skip turns that are structurally unlikely to be a routing request:
#   - prompts that begin with '/' (slash-command turns, e.g. /builder,
#     /loom:sweep — the user has already chosen a command)
#   - very short prompts (< 3 words), which carry too little signal to route
# These skips keep near-zero-signal chatter from producing routing noise.
if [[ "$PROMPT" == /* ]]; then
    exit 0
fi

WORD_COUNT=$(printf '%s' "$PROMPT" | wc -w | tr -d '[:space:]')
if [[ -z "$WORD_COUNT" ]]; then
    WORD_COUNT=0
fi
if [[ "$WORD_COUNT" -lt 3 ]]; then
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

# No route matched: emit nothing at all (issue #3609). The agent table used to
# ride along on EVERY prompt; now it only accompanies a genuine route match, so
# non-matching turns are a silent exit — no additionalContext, no token cost.
if [[ -z "$MATCHED_AGENT" ]]; then
    exit 0
fi

# =============================================================================
# PER-SESSION TABLE DEDUP (#3609)
# =============================================================================
# The agent table is verbatim repetition of the roles list already shipped in
# CLAUDE.md, so a session needs it at most once. We key an "already sent" marker
# on the session_id already present on stdin. A missing/empty session_id
# degrades gracefully: we cannot dedup, so the table is included on each match
# (the pre-#3609 per-turn behavior, but now only on matching turns).

INCLUDE_TABLE=1
if [[ -n "$SESSION_ID" ]]; then
    # Sanitize to filename-safe characters so the marker is a single predictable
    # file (never a path traversal, never a nested directory).
    SESSION_KEY=$(printf '%s' "$SESSION_ID" | tr -c 'A-Za-z0-9._-' '_')
    SEEN_DIR="${MAIN_ROOT}/.loom/logs/skill-router-seen"
    SEEN_MARKER="${SEEN_DIR}/${SESSION_KEY}"
    if [[ -f "$SEEN_MARKER" ]]; then
        INCLUDE_TABLE=0
    else
        # Best-effort marker creation; a failure here never fails the hook and
        # simply means the table may be re-sent on a later matching turn.
        mkdir -p "$SEEN_DIR" 2>/dev/null || true
        : > "$SEEN_MARKER" 2>/dev/null || true
    fi
fi

# =============================================================================
# BUILD OUTPUT
# =============================================================================

CONTEXT=""
if [[ "$INCLUDE_TABLE" -eq 1 ]]; then
    # Build the compact agent routing table only when we will actually emit it.
    AGENT_TABLE=$(echo "$ROUTES_JSON" | jq -r '.[] | "\(.agent) — \(.description)"' 2>/dev/null) || AGENT_TABLE=""
    if [[ -n "$AGENT_TABLE" ]]; then
        CONTEXT="Available Loom agents:
${AGENT_TABLE}

"
    fi
fi

CONTEXT="${CONTEXT}AGENT_ROUTE: ${MATCHED_AGENT} — ${MATCHED_DESC}
(This is a suggestion based on prompt keywords. Use the Skill tool to invoke if appropriate.)"

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
