#!/usr/bin/env bash
# guard-loom-workflow.sh - PreToolUse hook for Loom-workflow-specific Bash guards
#
# Claude Code PreToolUse hook that intercepts Bash commands before execution.
# Receives JSON on stdin with tool_input.command and cwd fields.
#
# This hook carries ONLY the two Loom-workflow-specific guards that were
# extracted from guard-destructive.sh (issue #3604):
#
#   1. LOOM: Prefer merge-pr.sh over 'gh pr merge'
#   2. LOOM: Block 'pip install -e' inside worktrees (issue #2495)
#
# The generic repository-hygiene guards (catastrophic denies, SQL/cloud toggles,
# ASK patterns) live in guard-destructive.sh and are being migrated toward Repo
# Skills (rjwalters/repo#13). This file stays Loom-owned because both guards are
# specific to the Loom worktree/merge workflow.
#
# IMPORTANT: This hook only fires when Claude Code is invoked with:
#   --dangerously-skip-permissions  ← hooks FIRE (used by Loom agents)
#
# It does NOT fire with:
#   --permission-mode bypassPermissions  ← hooks SKIPPED entirely
#
# Output format (Claude Code hooks spec):
#   { "hookSpecificOutput": { "hookEventName": "PreToolUse", "permissionDecision": "deny|ask", "permissionDecisionReason": "..." } }
#
# NOTE: The "hookEventName": "PreToolUse" field is REQUIRED by Claude Code's
# PreToolUse hook schema. Without it, Claude Code silently discards the
# decision and the guard becomes inert (see issue #3550).
#
# Error handling: This script MUST never exit with a non-zero code or produce
# invalid output. Any internal error is caught by the trap, logged for
# diagnostics, and results in an "allow" decision to prevent infinite retry
# loops in Claude Code.

# Determine log directory relative to this script's location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo ".")"
HOOK_ERROR_LOG="${SCRIPT_DIR}/../logs/hook-errors.log"

# Decision telemetry log (issue #3771 / #3898) — a SEPARATE JSONL file from
# HOOK_ERROR_LOG, sharing the SAME schema + stable rule tags as
# guard-destructive.sh so a single reader (#3772 / the standing per-trigger
# review policy) aggregates BOTH guards' fires. At runtime SCRIPT_DIR is the
# installed hook's own dir (.loom/hooks/), so this resolves to
# .loom/logs/guard-decisions.log. LOOM_GUARD_DECISION_LOG_FILE overrides the
# path (test seam / operator override). Off by default — see
# decision_log_enabled() below.
DECISION_LOG="${LOOM_GUARD_DECISION_LOG_FILE:-${SCRIPT_DIR}/../logs/guard-decisions.log}"

# Log a diagnostic error message (best-effort, never fails the script)
log_hook_error() {
    local msg="$1"
    # Ensure log directory exists
    mkdir -p "$(dirname "$HOOK_ERROR_LOG")" 2>/dev/null || true
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [guard-loom-workflow] $msg" >> "$HOOK_ERROR_LOG" 2>/dev/null || true
}

# Redact the quoted VALUES of known text-carrying flags (--body, -m/--message,
# --title, --notes, --comment). A trimmed-down mirror of guard-destructive.sh's
# strip_literal_text() so the decision log persists no raw --body/-m secret
# value. Multi-line quoted spans are handled by slurping the whole command
# first (#3898). Best-effort: any failure falls back to the raw command.
strip_literal_text() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)   # single quote
        DQ = sprintf("%c", 34)   # double quote
        re = "(^|[ \t\n])(--message|--body|--notes|--title|--comment|-m)[ \t]*=?[ \t]*(" \
             DQ "[^" DQ "]*" DQ "|" SQ "[^" SQ "]*" SQ ")"
        buf = ""
    }
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        s = buf
        out = ""
        while (match(s, re)) {
            pre     = substr(s, 1, RSTART - 1)
            matched = substr(s, RSTART, RLENGTH)
            s       = substr(s, RSTART + RLENGTH)
            qpos = 0
            for (i = 1; i <= length(matched); i++) {
                c = substr(matched, i, 1)
                if (c == DQ || c == SQ) { qpos = i; break }
            }
            head  = substr(matched, 1, qpos)
            qchar = substr(matched, qpos, 1)
            inner = substr(matched, qpos + 1, length(matched) - qpos - 1)
            if (index(inner, "$(") == 0 && index(inner, "`") == 0) {
                gsub(/./, "X", inner)
            }
            out = out pre head inner qchar
        }
        out = out s
        printf "%s", out
    }'
}

# =============================================================================
# DECISION TELEMETRY (issue #3771 / #3898) — one JSONL record per deny decision,
# identical schema + toggle semantics to guard-destructive.sh so both guards'
# fires land in the SAME .loom/logs/guard-decisions.log for the standing
# per-trigger review policy. Off by default (guards.decisionLog /
# LOOM_GUARD_DECISION_LOG, inverse polarity — only an explicit true/1 enables).
# `allow` is never logged. Fail-open: a write failure never changes the decision
# and never causes a non-zero exit.
#
# Schema (STABLE — matches guard-destructive.sh):
#   {"ts","decision":"deny","pattern":"<tag>","tier":"catastrophic","command":"<redacted>"}
# =============================================================================
_DECISION_LOG_CACHE=""
decision_log_enabled() {
    if [[ -z "$_DECISION_LOG_CACHE" ]]; then
        local enabled=false
        if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.loom/config.json" ]]; then
            # Only an explicit `true` enables; a missing key or malformed JSON
            # (jq non-zero, caught by ||) stays OFF.
            enabled=$(jq -r 'if .guards.decisionLog == true then "true" else "false" end' "$REPO_ROOT/.loom/config.json" 2>/dev/null) || enabled=false
            [[ -n "$enabled" ]] || enabled=false
        fi
        # Env override wins over config.
        case "${LOOM_GUARD_DECISION_LOG:-}" in
            0|false|no|off)   enabled=false ;;
            1|true|yes|on)    enabled=true ;;
        esac
        _DECISION_LOG_CACHE="$enabled"
    fi
    [[ "$_DECISION_LOG_CACHE" == "true" ]]
}

log_guard_decision() {
    # Args: <decision> <tier> <pattern-tag>. Command read from global $COMMAND
    # and redacted here. Returns 0 unconditionally.
    decision_log_enabled || return 0
    local decision="$1" tier="$2" tag="${3:-$1}"
    local ts redacted line
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null) || ts=""
    redacted=$(strip_literal_text "$COMMAND" 2>/dev/null) || redacted=""
    [[ -n "$redacted" ]] || redacted="$COMMAND"
    line=$(jq -cn \
        --arg ts "$ts" \
        --arg decision "$decision" \
        --arg pattern "$tag" \
        --arg tier "$tier" \
        --arg command "$redacted" \
        '{ts:$ts, decision:$decision, pattern:$pattern, tier:$tier, command:$command}' \
        2>/dev/null) || return 0
    [[ -n "$line" ]] || return 0
    mkdir -p "$(dirname "$DECISION_LOG")" 2>/dev/null || true
    { printf '%s\n' "$line" >> "$DECISION_LOG"; } 2>/dev/null || true
    return 0
}

# Top-level error trap: on ANY unexpected error, output valid JSON "allow"
# and log the failure for debugging. This prevents Claude Code from showing
# "PreToolUse:Bash hook error" which causes infinite retry loops.
trap 'log_hook_error "Unexpected error on line ${LINENO}: ${BASH_COMMAND:-unknown} (exit=$?)"; exit 0' ERR

# Read stdin safely — if cat or jq fails, the ERR trap fires and we allow
INPUT=$(cat 2>/dev/null) || INPUT=""

# Verify jq is available before attempting to parse
if ! command -v jq &>/dev/null; then
    log_hook_error "jq not found in PATH — allowing command (cannot parse input)"
    exit 0
fi

COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null) || COMMAND=""
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null) || CWD=""

# If no command to check, allow
if [[ -z "$COMMAND" ]]; then
    exit 0
fi

# Resolve repo root from cwd (handles worktree paths safely)
REPO_ROOT=""
if [[ -n "$CWD" ]] && [[ -d "$CWD" ]]; then
    REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || true)
elif [[ -n "$CWD" ]]; then
    # CWD doesn't exist (e.g., deleted worktree) — log but continue without repo root
    log_hook_error "cwd does not exist: $CWD — skipping repo root resolution"
fi

# Helper: output a deny decision and exit
#
# Optional second arg is a short, STABLE rule tag (issue #3771 / #3898) recorded
# as the decision log's `pattern` field; defaults to "deny" for back-compat.
# Telemetry is emitted BEFORE the JSON decision (so a logging hiccup can never
# suppress the deny) and `|| true` guarantees it never trips the ERR trap. Deny
# is always the "catastrophic" tier.
deny() {
    local reason="$1"
    local tag="${2:-deny}"
    log_guard_decision "deny" "catastrophic" "$tag" || true
    if jq -n --arg reason "$reason" '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason: $reason
        }
    }' 2>/dev/null; then
        exit 0
    fi
    # jq failed — emit raw JSON as fallback
    local escaped_reason
    escaped_reason=$(echo "$reason" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g; s/\n/\\n/g')
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"${escaped_reason}\"}}"
    exit 0
}

# Helper: output an ask decision and exit
ask() {
    local reason="$1"
    if jq -n --arg reason "$reason" '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "ask",
            permissionDecisionReason: $reason
        }
    }' 2>/dev/null; then
        exit 0
    fi
    # jq failed — emit raw JSON as fallback
    local escaped_reason
    escaped_reason=$(echo "$reason" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g; s/\n/\\n/g')
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"ask\",\"permissionDecisionReason\":\"${escaped_reason}\"}}"
    exit 0
}

# =============================================================================
# LOOM: Prefer merge-pr.sh over gh pr merge
# =============================================================================

if echo "$COMMAND" | grep -qE 'gh\s+pr\s+merge'; then
    # Resolve the merge-pr.sh path for the current repo context. Prefer an
    # in-repo installed copy (./.loom/scripts/merge-pr.sh); fall back to the
    # loom-checkout copy under defaults/scripts/ (via $LOOM_HOME) when the repo
    # runs scripts directly from the checkout rather than an installed copy.
    MERGE_SCRIPT="./.loom/scripts/merge-pr.sh"
    if [[ -n "$REPO_ROOT" ]] && [[ ! -x "$REPO_ROOT/.loom/scripts/merge-pr.sh" ]]; then
        if [[ -n "${LOOM_HOME:-}" ]] && [[ -x "$LOOM_HOME/defaults/scripts/merge-pr.sh" ]]; then
            MERGE_SCRIPT="$LOOM_HOME/defaults/scripts/merge-pr.sh"
        elif [[ -x "$REPO_ROOT/defaults/scripts/merge-pr.sh" ]]; then
            MERGE_SCRIPT="$REPO_ROOT/defaults/scripts/merge-pr.sh"
        fi
    fi
    deny "Use $MERGE_SCRIPT <PR_NUMBER> instead of 'gh pr merge'. The script merges via the GitHub API without local checkout, which avoids worktree errors." "loom:gh-pr-merge-redirect"
fi

# =============================================================================
# LOOM: Block pip install -e inside worktrees (issue #2495)
#
# Editable pip installs overwrite a global .pth file in site-packages.
# When multiple builders run in parallel worktrees, each 'pip install -e .'
# clobbers the .pth to point at its own worktree, causing all other Python
# processes to import from the wrong source tree.
#
# PYTHONPATH is already set by agent-spawn.sh and _build_worktree_env()
# so editable installs are unnecessary inside worktrees.
# =============================================================================

WORKTREE_PATH="${LOOM_WORKTREE_PATH:-}"
if [[ -n "$WORKTREE_PATH" ]]; then
    if echo "$COMMAND" | grep -qE '(pip|pip3|uv pip)\s+install\s+.*-e\s' || \
       echo "$COMMAND" | grep -qE '(pip|pip3|uv pip)\s+install\s+.*--editable\s'; then
        deny "BLOCKED: 'pip install -e' is not allowed inside worktrees. Editable installs overwrite the global .pth file, breaking parallel builders (see issue #2495). PYTHONPATH is already configured for this worktree — imports resolve correctly without editable installs." "loom:pip-install-editable-worktree"
    fi
fi

# =============================================================================
# ALLOW - Everything else passes through
# =============================================================================

exit 0
