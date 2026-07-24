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
#   2. Inject universal.md if it exists, ONCE PER SESSION by default (mirrors
#      skill-router.sh's #3609 per-session table dedup). The universal_frequency
#      config knob ("session" default / "always" back-compat) controls this.
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

# Extract session_id (used for once-per-session universal.md dedup; optional — a
# missing/empty value degrades gracefully, see the PER-SESSION UNIVERSAL DEDUP
# block below)
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
# and the relevant context was already injected on the originating human turn.
# Match against the raw prompt with literal prefix/substring (no regex) so this
# guard cannot itself false-positive on human text.
case "$PROMPT" in
    "[SYSTEM NOTIFICATION"*) exit 0 ;;
esac
if [[ "$PROMPT" == *"<task-notification>"* ]]; then
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
# Frequency of universal.md injection: "session" (once per session, new default
# as of #3758) or "always" (every matching prompt, legacy back-compat behavior).
UNIVERSAL_FREQUENCY=session

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
    # Only "always" opts back into per-prompt injection; anything else (including
    # a missing key or malformed value) falls through to the "session" default.
    UNIVERSAL_FREQUENCY=$(jq -r 'if .universal_frequency == "always" then "always" else "session" end' "$CONFIG_FILE" 2>/dev/null) || UNIVERSAL_FREQUENCY=session
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
# =============================================================================
# PER-SESSION UNIVERSAL DEDUP (#3758)
# =============================================================================
# universal.md is verbatim project-wide context that a session needs at most
# once. By default (universal_frequency="session") we inject it on the first
# matching prompt of a session and skip it thereafter, keyed on the session_id
# present on stdin — exactly mirroring skill-router.sh's #3609 table dedup, but
# in its own marker namespace (the two hooks are independent opt-ins and must
# not share state). universal_frequency="always" restores the legacy per-prompt
# behavior. A missing/empty session_id degrades gracefully: we cannot dedup, so
# universal.md is included on each matching turn (the legacy behavior).
if [[ "$INJECT_UNIVERSAL" == "true" ]] && [[ -f "${CONTEXT_DIR}/universal.md" ]]; then
    INCLUDE_UNIVERSAL=1
    if [[ "$UNIVERSAL_FREQUENCY" != "always" ]] && [[ -n "$SESSION_ID" ]]; then
        # Sanitize to filename-safe characters so the marker is a single
        # predictable file (never a path traversal, never a nested directory).
        SESSION_KEY=$(printf '%s' "$SESSION_ID" | tr -c 'A-Za-z0-9._-' '_')
        SEEN_DIR="${MAIN_ROOT}/.loom/logs/methodology-inject-seen"
        SEEN_MARKER="${SEEN_DIR}/${SESSION_KEY}"
        # Opportunistic best-effort prune (#3793): every session adds one marker
        # here and nothing else prunes them, so a busy orchestration repo would
        # accumulate stale empty files without bound. Drop markers older than 7
        # days on hook entry. Fail-open — any error (missing dir, permission)
        # never fails the hook and never changes the dedup decision below.
        find "$SEEN_DIR" -type f -mtime +7 -delete 2>/dev/null || true
        if [[ -f "$SEEN_MARKER" ]]; then
            INCLUDE_UNIVERSAL=0
        else
            # Best-effort marker creation; a failure here never fails the hook
            # and simply means universal.md may be re-injected on a later turn.
            mkdir -p "$SEEN_DIR" 2>/dev/null || true
            : > "$SEEN_MARKER" 2>/dev/null || true
        fi
    fi

    if [[ "$INCLUDE_UNIVERSAL" -eq 1 ]]; then
        UNIVERSAL_CONTENT=$(cat "${CONTEXT_DIR}/universal.md" 2>/dev/null) || UNIVERSAL_CONTENT=""
        append_context "Project Context" "$UNIVERSAL_CONTENT"
    fi
fi

# --- Role-specific context ---
if [[ "$INJECT_ROLE" == "true" ]]; then
    ROLE="${LOOM_ROLE:-}"

    # Fallback: detect role from prompt preamble (slash commands)
    if [[ -z "$ROLE" ]]; then
        # Match both the unnamespaced (/builder) and namespaced (/loom:builder)
        # forms for every role (#3793). Claude Code 2.1+ requires the namespaced
        # /loom:<role> form for subdirectory commands (#3345), so an interactive
        # `/loom:builder 123` must inject builder context too. /loom:sweep has no
        # unnamespaced counterpart (sweep.md was always namespace-only).
        case "$PROMPT" in
            /builder*|/loom:builder*)     ROLE="builder" ;;
            /judge*|/loom:judge*)         ROLE="judge" ;;
            /curator*|/loom:curator*)     ROLE="curator" ;;
            /doctor*|/loom:doctor*)       ROLE="doctor" ;;
            /architect*|/loom:architect*) ROLE="architect" ;;
            /hermit*|/loom:hermit*)       ROLE="hermit" ;;
            /champion*|/loom:champion*)   ROLE="champion" ;;
            /guide*|/loom:guide*)         ROLE="guide" ;;
            /auditor*|/loom:auditor*)     ROLE="auditor" ;;
            /loom:sweep*)                 ROLE="sweep" ;;
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

        # Fall back to an anchored match on the filename. A bare substring
        # match (the historical behavior) false-positives on flag-like and
        # path-segment contexts — e.g. a "release" topic would inject on
        # "cargo build --release" or "target/release". Require either the
        # slash-command form (/loom:<topic> or /repo:<topic>) or a
        # word-boundary token that is NOT preceded by "-" or "/" and NOT
        # followed by "/". The sidecar .pattern file remains the escape hatch
        # for topics that need a custom regex.
        if [[ -z "$PATTERN" ]]; then
            PATTERN="/(loom|repo):${TOPIC_NAME}\b|(^|[^-/[:alnum:]])${TOPIC_NAME}([^/[:alnum:]]|$)"
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
