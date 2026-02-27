#!/usr/bin/env bash
# guard-destructive.sh - PreToolUse hook to block destructive agent commands
#
# Claude Code PreToolUse hook that intercepts Bash commands before execution.
# Receives JSON on stdin with tool_input.command and cwd fields.
#
# IMPORTANT: This hook only fires when Claude Code is invoked with:
#   --dangerously-skip-permissions  ← hooks FIRE (used by Loom agents)
#
# It does NOT fire with:
#   --permission-mode bypassPermissions  ← hooks SKIPPED entirely
#
# If you have a shell alias like 'alias claude="claude --permission-mode bypassPermissions"',
# this safety hook will be silently disabled in interactive sessions.
# Use --dangerously-skip-permissions instead for automation that needs hooks.
#
# Decisions:
#   - Block (deny): Dangerous commands that should never run
#   - Ask: Commands that need human confirmation
#   - Allow: Everything else (exit 0, no output)
#
# Output format (Claude Code hooks spec):
#   { "hookSpecificOutput": { "permissionDecision": "deny|ask", "permissionDecisionReason": "..." } }
#
# Error handling: This script MUST never exit with a non-zero code or produce
# invalid output. Any internal error is caught by the trap, logged for
# diagnostics, and results in an "allow" decision to prevent infinite retry
# loops in Claude Code.

# Determine log directory relative to this script's location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo ".")"
HOOK_ERROR_LOG="${SCRIPT_DIR}/../logs/hook-errors.log"

# Log a diagnostic error message (best-effort, never fails the script)
log_hook_error() {
    local msg="$1"
    # Ensure log directory exists
    mkdir -p "$(dirname "$HOOK_ERROR_LOG")" 2>/dev/null || true
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [guard-destructive] $msg" >> "$HOOK_ERROR_LOG" 2>/dev/null || true
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
deny() {
    local reason="$1"
    if jq -n --arg reason "$reason" '{
        hookSpecificOutput: {
            permissionDecision: "deny",
            permissionDecisionReason: $reason
        }
    }' 2>/dev/null; then
        exit 0
    fi
    # jq failed — emit raw JSON as fallback
    local escaped_reason
    escaped_reason=$(echo "$reason" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g; s/\n/\\n/g')
    echo "{\"hookSpecificOutput\":{\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"${escaped_reason}\"}}"
    exit 0
}

# Helper: output an ask decision and exit
ask() {
    local reason="$1"
    if jq -n --arg reason "$reason" '{
        hookSpecificOutput: {
            permissionDecision: "ask",
            permissionDecisionReason: $reason
        }
    }' 2>/dev/null; then
        exit 0
    fi
    # jq failed — emit raw JSON as fallback
    local escaped_reason
    escaped_reason=$(echo "$reason" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g; s/\n/\\n/g')
    echo "{\"hookSpecificOutput\":{\"permissionDecision\":\"ask\",\"permissionDecisionReason\":\"${escaped_reason}\"}}"
    exit 0
}

# =============================================================================
# ALWAYS BLOCK - Catastrophic commands that should never execute
# =============================================================================

ALWAYS_BLOCK_PATTERNS=(
    # GitHub destructive operations
    'gh repo delete'
    'gh repo archive'

    # Force push to main/master (various flag forms)
    'git push --force origin main'
    'git push --force origin master'
    'git push -f origin main'
    'git push -f origin master'
    'git push --force-with-lease origin main'
    'git push --force-with-lease origin master'

    # Filesystem destruction
    'rm -rf /'
    'rm -rf /\*'
    'rm -rf ~'
    'rm -rf \$HOME'

    # Fork bombs
    ':\(\)\{ :\|:& \};:'

    # Pipe to shell (supply chain risk)
    'curl .* \| .*sh'
    'curl .* \| bash'
    'wget .* \| .*sh'
    'wget .* -O- \| sh'

    # Cloud infrastructure destruction
    'aws s3 rm.*--recursive'
    'aws s3 rb'
    'aws ec2 terminate'
    'aws iam delete'
    'aws cloudformation delete-stack'
    'gcloud.*delete'
    'az.*delete'
    'az group delete'

    # Docker mass destruction
    'docker system prune'

    # System reboot/shutdown
    'reboot'
    'shutdown'
    'halt'
    'poweroff'
    'init 0'
    'init 6'

    # Database destruction
    'DROP DATABASE'
    'DROP TABLE'
    'DROP SCHEMA'
    'TRUNCATE TABLE'
)

for pattern in "${ALWAYS_BLOCK_PATTERNS[@]}"; do
    if echo "$COMMAND" | grep -qiE "$pattern"; then
        deny "BLOCKED: Command matches dangerous pattern: $pattern"
    fi
done

# =============================================================================
# rm -rf SCOPE CHECK - Block rm with recursive/force flags outside repo
# =============================================================================

# Match rm commands with -r or -f flags (in any combination: -rf, -r -f, -fr, etc.)
if echo "$COMMAND" | grep -qE 'rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+' || \
   echo "$COMMAND" | grep -qE 'rm\s+-[a-zA-Z]*r[a-zA-Z]*\s' || \
   echo "$COMMAND" | grep -qE 'rm\s+-[a-zA-Z]*f[a-zA-Z]*\s+-[a-zA-Z]*r[a-zA-Z]*\s'; then

    # Extract target paths from the rm command (skip flags)
    TARGETS=$(echo "$COMMAND" | sed 's/rm\s\+//' | tr ' ' '\n' | grep -v '^-' | head -20)

    for target in $TARGETS; do
        # Skip empty targets
        [[ -z "$target" ]] && continue

        # Skip known-safe patterns (allowlist)
        case "$target" in
            node_modules|./node_modules|*/node_modules)
                continue ;;
            target|./target|*/target)
                continue ;;
            dist|./dist|*/dist)
                continue ;;
            build|./build|*/build)
                continue ;;
            .loom/worktrees/*|*/.loom/worktrees/*)
                continue ;;
            .next|./.next|*/.next)
                continue ;;
            __pycache__|./__pycache__|*/__pycache__)
                continue ;;
            .pytest_cache|./.pytest_cache|*/.pytest_cache)
                continue ;;
            *.pyc)
                continue ;;
        esac

        # Resolve path to absolute
        ABS_PATH=""
        if [[ "$target" = /* ]]; then
            ABS_PATH="$target"
        elif [[ -n "$CWD" ]]; then
            ABS_PATH=$(cd "$CWD" 2>/dev/null && realpath -m "$target" 2>/dev/null || echo "$CWD/$target")
        fi

        # Block dangerous absolute paths
        if [[ "$ABS_PATH" == "/" ]] || [[ "$ABS_PATH" == "/home" ]] || \
           [[ "$ABS_PATH" == "$HOME" ]] || [[ "$ABS_PATH" == "/tmp" ]] || \
           [[ "$ABS_PATH" == "/usr" ]] || [[ "$ABS_PATH" == "/var" ]] || \
           [[ "$ABS_PATH" == "/etc" ]] || [[ "$ABS_PATH" == "/opt" ]]; then
            deny "BLOCKED: rm on protected system path: $ABS_PATH"
        fi

        # Block if outside repo root (when we know the repo root)
        if [[ -n "$REPO_ROOT" ]] && [[ -n "$ABS_PATH" ]]; then
            if [[ "$ABS_PATH" != "$REPO_ROOT"* ]]; then
                deny "BLOCKED: rm target outside repository: $ABS_PATH (repo: $REPO_ROOT)"
            fi
        fi
    done
fi

# =============================================================================
# DELETE without WHERE - Database safety
# =============================================================================

if echo "$COMMAND" | grep -qiE 'DELETE\s+FROM\s+' && \
   ! echo "$COMMAND" | grep -qiE 'WHERE\s+'; then
    deny "BLOCKED: DELETE FROM without WHERE clause"
fi

# =============================================================================
# REQUIRE CONFIRMATION - Potentially dangerous but sometimes legitimate
# =============================================================================

ASK_PATTERNS=(
    # Git destructive operations (not on main/master - those are blocked above)
    'git push --force'
    'git push -f '
    'git reset --hard'
    'git clean -fd'
    'git checkout \.'
    'git restore \.'

    # GitHub operations that modify shared state
    'gh pr close'
    'gh issue close'
    'gh release delete'
    'gh label delete'

    # Cloud CLI operations
    'aws s3'
    'aws ec2'
    'aws lambda'

    # Docker operations
    'docker rm'
    'docker rmi'
    'docker stop'
    'docker kill'
    'docker restart'

    # Service management
    'systemctl restart'
    'systemctl stop'
    'systemctl disable'

    # Kubernetes operations
    'kubectl delete'
    'kubectl rollout restart'
    'kubectl drain'

    # SkyPilot infrastructure
    'sky down'
    'sky stop'

    # Credential exposure
    'printenv.*SECRET'
    'printenv.*TOKEN'
    'printenv.*KEY'
    'cat.*/\.ssh/'
    'cat.*/\.aws/credentials'
)

for pattern in "${ASK_PATTERNS[@]}"; do
    if echo "$COMMAND" | grep -qE "$pattern"; then
        ask "Command requires confirmation: $COMMAND"
    fi
done

# =============================================================================
# LOOM: Prefer merge-pr.sh over gh pr merge
# =============================================================================

if echo "$COMMAND" | grep -qE 'gh\s+pr\s+merge'; then
    deny "Use ./.loom/scripts/merge-pr.sh <PR_NUMBER> instead of 'gh pr merge'. The script merges via the GitHub API without local checkout, which avoids worktree errors."
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
        deny "BLOCKED: 'pip install -e' is not allowed inside worktrees. Editable installs overwrite the global .pth file, breaking parallel builders (see issue #2495). PYTHONPATH is already configured for this worktree — imports resolve correctly without editable installs."
    fi
fi

# =============================================================================
# ALLOW - Everything else passes through
# =============================================================================

exit 0
