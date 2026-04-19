#!/usr/bin/env bash
# methodology-inject.sh - UserPromptSubmit hook for project-specific context injection
#
# Claude Code UserPromptSubmit hook that injects domain-specific context from
# .loom/context/ files into agent sessions as additionalContext.
#
# Receives JSON on stdin with { "prompt": "...", "session_id": "...", "cwd": "..." }
#
# Behavior:
#   1. Check for .loom/context/ directory — exit silently if absent (opt-in)
#   2. Always inject universal.md if it exists
#   3. Inject roles/<LOOM_ROLE>.md if LOOM_ROLE env var is set
#   4. Inject topics/<name>.md when prompt matches filename or sidecar .pattern file
#   5. Cap total output at configurable max (default 8000 chars)
#
# Output format (Claude Code hooks spec):
#   { "hookSpecificOutput": { "hookEventName": "UserPromptSubmit", "additionalContext": "..." } }
#
# Opt-in: Only activates when .loom/context/ directory exists.
# If the directory is missing, the hook exits silently (no context injected).
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
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [methodology-inject] $msg" >> "$HOOK_ERROR_LOG" 2>/dev/null || true
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
# OPT-IN CHECK
# =============================================================================

CONTEXT_DIR="${MAIN_ROOT}/.loom/context"

# Exit silently if context directory does not exist
if [[ ! -d "$CONTEXT_DIR" ]]; then
    exit 0
fi

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG_FILE="${CONTEXT_DIR}/config.json"
MAX_CONTEXT_CHARS=8000
INJECT_UNIVERSAL=true
INJECT_ROLE=true
INJECT_TOPICS=true

# Read config if it exists
if [[ -f "$CONFIG_FILE" ]] && jq empty "$CONFIG_FILE" 2>/dev/null; then
    # Check enabled flag (jq // is alternative-on-null, not default-on-missing,
    # so we use if/then/else to handle explicit false correctly)
    ENABLED=$(jq -r 'if .enabled == false then "false" else "true" end' "$CONFIG_FILE" 2>/dev/null) || ENABLED=true
    if [[ "$ENABLED" == "false" ]]; then
        exit 0
    fi

    MAX_CONTEXT_CHARS=$(jq -r '.max_context_chars // 8000' "$CONFIG_FILE" 2>/dev/null) || MAX_CONTEXT_CHARS=8000
    INJECT_UNIVERSAL=$(jq -r 'if .inject_universal == false then "false" else "true" end' "$CONFIG_FILE" 2>/dev/null) || INJECT_UNIVERSAL=true
    INJECT_ROLE=$(jq -r 'if .inject_role == false then "false" else "true" end' "$CONFIG_FILE" 2>/dev/null) || INJECT_ROLE=true
    INJECT_TOPICS=$(jq -r 'if .inject_topics == false then "false" else "true" end' "$CONFIG_FILE" 2>/dev/null) || INJECT_TOPICS=true
fi

# =============================================================================
# CONTEXT COLLECTION
# =============================================================================

COLLECTED_CONTEXT=""

# Helper: append content with a separator, respecting max chars
append_context() {
    local label="$1"
    local content="$2"

    if [[ -z "$content" ]]; then
        return
    fi

    local new_section
    if [[ -n "$COLLECTED_CONTEXT" ]]; then
        new_section=$'\n\n---\n\n'"[${label}]"$'\n'"${content}"
    else
        new_section="[${label}]"$'\n'"${content}"
    fi

    local current_len=${#COLLECTED_CONTEXT}
    local new_len=${#new_section}

    if (( current_len + new_len > MAX_CONTEXT_CHARS )); then
        # Truncate to fit within budget
        local remaining=$(( MAX_CONTEXT_CHARS - current_len ))
        if (( remaining > 50 )); then
            COLLECTED_CONTEXT="${COLLECTED_CONTEXT}${new_section:0:$remaining}... [truncated]"
        fi
        return
    fi

    COLLECTED_CONTEXT="${COLLECTED_CONTEXT}${new_section}"
}

# --- Universal context ---
if [[ "$INJECT_UNIVERSAL" == "true" ]] && [[ -f "${CONTEXT_DIR}/universal.md" ]]; then
    UNIVERSAL_CONTENT=$(cat "${CONTEXT_DIR}/universal.md" 2>/dev/null) || UNIVERSAL_CONTENT=""
    append_context "Project Context" "$UNIVERSAL_CONTENT"
fi

# --- Role-specific context ---
if [[ "$INJECT_ROLE" == "true" ]]; then
    ROLE="${LOOM_ROLE:-}"

    # Fallback: detect role from prompt preamble (slash commands)
    if [[ -z "$ROLE" ]]; then
        case "$PROMPT" in
            /builder*) ROLE="builder" ;;
            /judge*)   ROLE="judge" ;;
            /curator*) ROLE="curator" ;;
            /doctor*)  ROLE="doctor" ;;
            /architect*) ROLE="architect" ;;
            /hermit*)  ROLE="hermit" ;;
            /champion*) ROLE="champion" ;;
            /guide*)   ROLE="guide" ;;
            /auditor*) ROLE="auditor" ;;
            /shepherd*) ROLE="shepherd" ;;
            /loom*)    ROLE="loom" ;;
        esac
    fi

    if [[ -n "$ROLE" ]]; then
        # Normalize to lowercase
        ROLE_LOWER=$(echo "$ROLE" | tr '[:upper:]' '[:lower:]')
        ROLE_FILE="${CONTEXT_DIR}/roles/${ROLE_LOWER}.md"

        if [[ -f "$ROLE_FILE" ]]; then
            ROLE_CONTENT=$(cat "$ROLE_FILE" 2>/dev/null) || ROLE_CONTENT=""
            append_context "Role Context: ${ROLE_LOWER}" "$ROLE_CONTENT"
        fi
    fi
fi

# --- Topic-specific context ---
if [[ "$INJECT_TOPICS" == "true" ]] && [[ -d "${CONTEXT_DIR}/topics" ]]; then
    PROMPT_LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]')

    for topic_file in "${CONTEXT_DIR}/topics/"*.md; do
        # Skip if glob didn't match anything
        [[ -f "$topic_file" ]] || continue

        # Check if we've already hit the max
        if (( ${#COLLECTED_CONTEXT} >= MAX_CONTEXT_CHARS )); then
            break
        fi

        TOPIC_NAME=$(basename "$topic_file" .md)
        PATTERN=""

        # Check for sidecar .pattern file first
        PATTERN_FILE="${CONTEXT_DIR}/topics/${TOPIC_NAME}.pattern"
        if [[ -f "$PATTERN_FILE" ]]; then
            PATTERN=$(cat "$PATTERN_FILE" 2>/dev/null) || PATTERN=""
        fi

        # Fall back to using filename as the regex pattern
        if [[ -z "$PATTERN" ]]; then
            PATTERN="$TOPIC_NAME"
        fi

        # Match pattern case-insensitively against prompt
        if echo "$PROMPT_LOWER" | grep -qiE "$PATTERN" 2>/dev/null; then
            TOPIC_CONTENT=$(cat "$topic_file" 2>/dev/null) || TOPIC_CONTENT=""
            append_context "Topic: ${TOPIC_NAME}" "$TOPIC_CONTENT"
        fi
    done
fi

# =============================================================================
# OUTPUT
# =============================================================================

# If no context was collected, exit silently
if [[ -z "$COLLECTED_CONTEXT" ]]; then
    exit 0
fi

# Output valid JSON
jq -n --arg context "$COLLECTED_CONTEXT" '{
    hookSpecificOutput: {
        hookEventName: "UserPromptSubmit",
        additionalContext: $context
    }
}' 2>/dev/null || {
    log_hook_error "Failed to produce JSON output"
    exit 0
}

exit 0
