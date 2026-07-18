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

# =============================================================================
# SQL DDL/DML guard toggle — default ON.
#
# The SQL DDL/DML blocks (DROP DATABASE/TABLE/SCHEMA, TRUNCATE TABLE, and
# DELETE FROM without WHERE) are a category error for repos that are themselves
# database engines, where those statements are the product's own dev/test
# vocabulary. Such repos opt out; everyone else keeps the guard on.
#
# Resolution order (highest precedence first):
#   1. LOOM_GUARD_SQL env var (0/false/no disables, 1/true/yes forces on)
#   2. .loom/config.json  ->  guards.sqlDdl  (default true when absent)
#   3. Default: true (guard on)
#
# The resolution runs LAZILY — sql_guard_enabled() is only invoked once a
# command has already matched a SQL DDL/DML pattern, so the jq config read never
# touches the hot path for the ~99% of commands that are not SQL. The result is
# cached so a command matching multiple SQL patterns pays for at most one read.
#
# The config read is best-effort: any parse failure falls through to guard-ON
# and never trips the ERR trap or produces a non-zero exit.
# =============================================================================
_SQL_GUARD_CACHE=""
sql_guard_enabled() {
    if [[ -z "$_SQL_GUARD_CACHE" ]]; then
        local enabled=true
        if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.loom/config.json" ]]; then
            # jq // is alternative-on-null, not default-on-missing, so use
            # if/then/else to treat only an explicit `false` as disabled (a
            # missing guards.sqlDdl key stays on). On malformed JSON jq exits
            # non-zero and the `||` fallback restores the guard-ON default.
            enabled=$(jq -r 'if .guards.sqlDdl == false then "false" else "true" end' "$REPO_ROOT/.loom/config.json" 2>/dev/null) || enabled=true
            [[ -n "$enabled" ]] || enabled=true
        fi
        # Env override wins over config.
        case "${LOOM_GUARD_SQL:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _SQL_GUARD_CACHE="$enabled"
    fi
    [[ "$_SQL_GUARD_CACHE" == "true" ]]
}

# Helper: output a deny decision and exit
deny() {
    local reason="$1"
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
# ALWAYS BLOCK - Catastrophic commands that should never execute
# =============================================================================

ALWAYS_BLOCK_PATTERNS=(
    # GitHub destructive operations — command-position anchored (start-of-line
    # or a shell separator must precede the verb) so the phrase inside a flag
    # value no longer trips. NOTE: the catastrophic scan still runs over the
    # full raw command, including quoted/heredoc text, so a `gh repo delete`
    # that a shell would actually execute (leading, sudo-prefixed, or after a
    # separator) still denies (#3553).
    '(^|[;&|[:space:]])gh repo delete'
    '(^|[;&|[:space:]])gh repo archive'

    # Force push to main/master (various flag forms)
    'git push --force origin main'
    'git push --force origin master'
    'git push -f origin main'
    'git push -f origin master'
    'git push --force-with-lease origin main'
    'git push --force-with-lease origin master'

    # Filesystem destruction — anchored to a *real* root/home target so that a
    # scoped path like `rm -rf /tmp/x` no longer trips the catastrophic rule,
    # while root / home obliteration still denies. The left side of `rm` is
    # deliberately NOT anchored, so a quoted payload such as `bash -c 'rm -rf /'`
    # (root followed by a closing quote) still matches (#3553). The trailing
    # class matches anything that is not a path-continuation character (so `/`,
    # `/ `, `/*`, `/;`, `/'` all count as "root itself" but `/tmp` does not).
    'rm[[:space:]]+-[a-zA-Z]*[rf][a-zA-Z]*[[:space:]]+/([^[:alnum:]._~/-]|$)'
    'rm[[:space:]]+-[a-zA-Z]*[rf][a-zA-Z]*[[:space:]]+~([^[:alnum:]._~/-]|$)'
    'rm[[:space:]]+-[a-zA-Z]*[rf][a-zA-Z]*[[:space:]]+\$HOME([^[:alnum:]._~/-]|$)'

    # Fork bombs
    ':\(\)\{ :\|:& \};:'

    # Pipe to shell (supply chain risk)
    'curl .* \| .*sh'
    'curl .* \| bash'
    'wget .* \| .*sh'
    'wget .* -O- \| sh'

    # Cloud infrastructure destruction. The aws forms below are specific
    # multi-token phrases, so they stay in this raw substring scan. The az/gcloud
    # CLIs, by contrast, need command-word anchoring — an unanchored `az.*delete`
    # matches "h·az·ard … delete" across unrelated prose tokens (#3584) — so they
    # are handled by the segment-parsed lifecycle/cloud check further below, NOT
    # here.
    'aws s3 rm.*--recursive'
    'aws s3 rb'
    'aws ec2 terminate'
    'aws iam delete'
    'aws cloudformation delete-stack'

    # Docker mass destruction
    'docker system prune'

    # NOTE: system-lifecycle commands (halt/reboot/poweroff/shutdown/init 0/
    # init 6) are deliberately NOT in this raw substring scan. Even the
    # whitespace-inclusive boundary anchor they used to carry still fired inside
    # ordinary prose ("...the box will halt", "...after a reboot event"), and a
    # pure regex tweak can't separate `sudo halt` from `will halt` (both are
    # "<word> halt"). They are handled by the segment-parsed check below, which
    # denies only when a segment's *command word* is exactly the lifecycle word
    # (#3584).
)

for pattern in "${ALWAYS_BLOCK_PATTERNS[@]}"; do
    if echo "$COMMAND" | grep -qiE "$pattern"; then
        deny "BLOCKED: Command matches dangerous pattern: $pattern"
    fi
done

# =============================================================================
# COMMENT-STRIPPED WORKING COPY - used ONLY for the ASK-word and SQL DDL/DML
# matches below, never for the catastrophic ALWAYS_BLOCK scan.
#
# Strips a `#…EOL` shell comment when the `#` is at start-of-line or preceded
# by whitespace (the common comment shape), so a pattern word that appears only
# in a trailing comment ("# drop database first", "# git push --force") no
# longer trips the ASK/DDL gates. This is best-effort: a `#` inside a quoted
# string that happens to be whitespace-preceded is also stripped, but since the
# stripped copy is used only for the *narrowing* ASK/DDL matches (never the
# catastrophic scan) the worst case is a missed ask on quoted data, never a
# missed catastrophic block. The sed only runs when a `#` is actually present,
# keeping it off the hot path (#3553).
# =============================================================================
if [[ "$COMMAND" == *"#"* ]]; then
    COMMAND_NO_COMMENT=$(printf '%s\n' "$COMMAND" | sed -E 's/(^|[[:space:]])#.*$//')
else
    COMMAND_NO_COMMENT="$COMMAND"
fi

# =============================================================================
# SYSTEM-LIFECYCLE + CLOUD-CLI DELETE (segment-parsed, command-word anchored)
#
# The system-lifecycle commands (halt/reboot/poweroff/shutdown/init 0/init 6)
# and the az/gcloud cloud-delete CLIs are far too common as ordinary prose,
# identifiers, and flag names to scan as unanchored substrings — and even a
# whitespace-inclusive boundary anchor still fired inside comments and commit
# messages ("...the box will halt", "...after a reboot event"). A pure regex
# tweak cannot separate `sudo halt` (a real command) from `will halt` (prose)
# because both are "<word> halt".
#
# So we segment-parse instead, mirroring extract_rm_targets(): split the command
# on ; | & && || and newline, strip a leading sudo/env wrapper from each segment,
# and deny only when a segment's *command word* (first token) is exactly a
# lifecycle word — or is `az`/`gcloud` with a `delete` subcommand token. This
# distinguishes `sudo halt` (command word = halt) from `will halt` (command word
# = echo/other) and from `--instance-initiated-shutdown-behavior` (not a command
# word at all). The scan runs against COMMAND_NO_COMMENT so a lifecycle/cloud
# word sitting in a trailing comment is already gone. The catastrophic
# ALWAYS_BLOCK scan above still reads the raw string for the symbolic patterns
# (rm -rf /, the fork bomb, curl|sh) that are not prose-prone (#3584).
# =============================================================================
lifecycle_or_cloud_reason() {
    # Emit a deny reason (one per line) for every segment whose command word is a
    # system-lifecycle command or an az/gcloud delete. Portable awk only.
    printf '%s' "$1" | awk '
    {
        gsub(/&&|\|\||[;|&]/, "\n")
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/^sudo[ \t]+/, "", seg)
            # Strip a leading `env` wrapper, then loop-strip the env flags and
            # NAME=value assignments a shell resolves past before the command
            # word, so `env FOO=bar halt` resolves to command word `halt` (not
            # `FOO=bar`) and still denies. `env -i FOO=bar halt` and `env -u
            # NAME halt` likewise resolve to `halt`. A bare `env halt` (no
            # assignment) is unaffected — the loop matches nothing and leaves
            # `halt` as the command word. Portable awk only (no GNU/BSD-specific
            # escapes), consistent with extract_rm_targets(). (#3586)
            if (sub(/^env([ \t]+|$)/, "", seg)) {
                sub(/^[ \t]+/, "", seg)
                stripped = 1
                while (stripped) {
                    stripped = 0
                    if (sub(/^-u[ \t]+[^ \t]+([ \t]+|$)/, "", seg)) { stripped = 1; continue }
                    if (sub(/^-i([ \t]+|$)/, "", seg))              { stripped = 1; continue }
                    if (sub(/^--([ \t]+|$)/, "", seg))              { break }
                    if (sub(/^[A-Za-z_][A-Za-z0-9_]*=[^ \t]*([ \t]+|$)/, "", seg)) { stripped = 1; continue }
                }
            }
            sub(/^[ \t]+/, "", seg)
            m = split(seg, toks, /[ \t]+/)
            if (m == 0) continue
            cmd = toks[1]
            if (cmd == "halt" || cmd == "reboot" || cmd == "poweroff" || cmd == "shutdown") {
                print "system lifecycle command: " cmd
                continue
            }
            if (cmd == "init" && (toks[2] == "0" || toks[2] == "6")) {
                print "system lifecycle command: init " toks[2]
                continue
            }
            if (cmd == "az" || cmd == "gcloud") {
                for (j = 2; j <= m; j++) {
                    if (toks[j] == "delete") {
                        print "cloud resource deletion: " cmd " delete"
                        break
                    }
                }
            }
        }
    }'
}

_LIFECYCLE_REASON=$(lifecycle_or_cloud_reason "$COMMAND_NO_COMMENT" | head -1)
if [[ -n "$_LIFECYCLE_REASON" ]]; then
    deny "BLOCKED: $_LIFECYCLE_REASON"
fi

# =============================================================================
# DATABASE DESTRUCTION - Gated by the SQL DDL/DML guard toggle
#
# Kept separate from ALWAYS_BLOCK_PATTERNS so DB-engine repos can opt out
# (guards.sqlDdl:false / LOOM_GUARD_SQL=0). A single alternation grep matches
# all four DDL statements in one pass (cheaper than a per-pattern loop), and
# sql_guard_enabled() is consulted only after a match, so the config read stays
# off the hot path.
# =============================================================================
SQL_DDL_PATTERN='DROP DATABASE|DROP TABLE|DROP SCHEMA|TRUNCATE TABLE'
if echo "$COMMAND_NO_COMMENT" | grep -qiE "$SQL_DDL_PATTERN" && sql_guard_enabled; then
    matched=$(echo "$COMMAND_NO_COMMENT" | grep -oiE "$SQL_DDL_PATTERN" | head -1)
    deny "BLOCKED: Command matches dangerous pattern: ${matched:-SQL DDL statement}"
fi

# =============================================================================
# rm -rf SCOPE CHECK - Block rm with recursive/force flags on protected paths
#
# Only *actual local* `rm` command words are inspected. `extract_rm_targets`
# splits the command on ; | & && || and, for each simple-command segment whose
# command word is `rm` (optionally sudo-prefixed) AND which carries a
# recursive/force flag, emits the non-flag argument tokens. Consequences (#3553):
#   - A token from an earlier command in the same line (e.g. the `host-ip.txt`
#     in `HOST=$(cat host-ip.txt); ssh $HOST rm -rf …`) is never mis-read as an
#     rm target — only tokens of a real `rm` segment are considered.
#   - An `rm` inside a remote payload (`ssh host 'rm -rf /home/ubuntu/foo'`) is
#     NOT treated as a local rm: the wrapper's command word is `ssh`/`scp`, not
#     `rm`, so no local target is emitted and the local scope check is skipped.
#     The ALWAYS_BLOCK catastrophic patterns above still scan the whole string,
#     so a remote or quoted `rm -rf /` still denies.
#   - Only root, the user's $HOME, and *top-level* directories (/tmp, /var, /etc,
#     /usr, /home, /opt, /bin, …) are blocked. A scoped subpath such as
#     `rm -rf /tmp/whatever` or `rm -rf /var/foo` is allowed — the guard stops
#     obliteration of a whole system/root directory, not cleanup of a subpath.
# =============================================================================

extract_rm_targets() {
    # Emit one rm-target token per line for every local `rm -r/-f` invocation.
    # Portable awk only (no GNU/BSD-specific escapes); replaces the shell
    # separators with newlines, then inspects each simple command.
    printf '%s' "$1" | awk '
    {
        gsub(/&&|\|\||[;|&]/, "\n")
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/^sudo[ \t]+/, "", seg)
            sub(/^[ \t]+/, "", seg)
            if (seg !~ /^rm([ \t]|$)/) continue
            m = split(seg, toks, /[ \t]+/)
            has_rf = 0
            for (j = 2; j <= m; j++)
                if (toks[j] ~ /^-/ && toks[j] ~ /[rRfF]/) has_rf = 1
            if (!has_rf) continue
            for (j = 2; j <= m; j++) {
                if (toks[j] == "") continue
                if (toks[j] ~ /^-/) continue
                print toks[j]
            }
        }
    }'
}

normalize_abs_path() {
    # Lexically normalize an ABSOLUTE path without touching the filesystem:
    #   - collapse duplicate slashes    (//etc        -> /etc)
    #   - drop "." segments             (/usr/./      -> /usr)
    #   - resolve ".." segments         (/tmp/..      -> /,   /tmp/../etc -> /etc)
    #   - ".." at or above root stays at root (/a/../../../etc -> /etc)
    #   - strip trailing slash except bare root (/tmp/ -> /tmp)
    # Pure-bash and portable: `realpath -m` is GNU-only and silently no-ops on
    # macOS, so this MUST NOT rely on it. Without this normalization any
    # `..`/`//`/`.` traversal (e.g. `rm -rf /tmp/..` -> `/`) would slip past the
    # protected-path check below and wrongly ALLOW root/system-dir deletion.
    local path="$1"
    local seg
    local -a parts=() out=()
    local oldIFS="$IFS"
    IFS='/'
    read -r -a parts <<< "$path"
    IFS="$oldIFS"
    for seg in "${parts[@]}"; do
        case "$seg" in
            ''|'.')
                : ;;                                    # skip empties (// or leading /) and "."
            '..')
                if [[ ${#out[@]} -gt 0 ]]; then
                    out=("${out[@]:0:$(( ${#out[@]} - 1 ))}")   # pop last segment
                fi
                ;;                                       # ".." at/above root: stay at root
            *)
                out+=("$seg") ;;
        esac
    done
    if [[ ${#out[@]} -eq 0 ]]; then
        printf '/'
    else
        printf '/%s' "${out[@]}"
    fi
}

# Cheap pre-check keeps awk off the hot path for the ~99% of commands that have
# no recursive/force rm at all.
if echo "$COMMAND" | grep -qE 'rm[[:space:]]+-[a-zA-Z]*[rf]'; then
    RM_TARGETS=$(extract_rm_targets "$COMMAND" | head -20)

    for target in $RM_TARGETS; do
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

        # Resolve path to absolute (raw — normalization happens next).
        ABS_PATH=""
        if [[ "$target" = /* ]]; then
            ABS_PATH="$target"
        elif [[ -n "$CWD" ]]; then
            ABS_PATH="$CWD/$target"
        fi

        # Lexically normalize the absolute target BEFORE the protected-path
        # check. This collapses //, resolves . and .., and strips trailing
        # slashes, so traversal/normalization tricks cannot smuggle a
        # root/system-dir deletion past the check below:
        #   /tmp/..  -> /        //etc     -> /etc
        #   /usr/./  -> /usr      /a/../../../etc -> /etc
        # Done in pure shell because `realpath -m` is GNU-only (no-ops on macOS).
        if [[ "$ABS_PATH" = /* ]]; then
            ABS_PATH=$(normalize_abs_path "$ABS_PATH")
        fi

        # Block catastrophic targets only: root, the user's home directory, and
        # any top-level directory (^/<one-segment>$ — covers /tmp, /home, /usr,
        # /var, /etc, /opt, /bin, /lib, …). Deeper paths are allowed.
        if [[ -n "$ABS_PATH" ]]; then
            if [[ "$ABS_PATH" == "/" ]] || \
               [[ -n "$HOME" && "$ABS_PATH" == "$HOME" ]] || \
               [[ "$ABS_PATH" =~ ^/[^/]+$ ]]; then
                deny "BLOCKED: rm on protected system path: $ABS_PATH"
            fi
        fi
    done
fi

# =============================================================================
# DELETE without WHERE - Database safety
# =============================================================================

# Gated by the SQL DDL/DML guard toggle. DB-engine repos opt out via
# guards.sqlDdl:false or LOOM_GUARD_SQL=0. sql_guard_enabled() is consulted only
# after the DELETE-FROM-without-WHERE match, keeping the config read off the hot
# path for non-SQL commands.
if echo "$COMMAND_NO_COMMENT" | grep -qiE 'DELETE[[:space:]]+FROM[[:space:]]+' && \
   ! echo "$COMMAND_NO_COMMENT" | grep -qiE 'WHERE[[:space:]]+'; then
    sql_guard_enabled && deny "BLOCKED: DELETE FROM without WHERE clause"
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
    if echo "$COMMAND_NO_COMMENT" | grep -qE "$pattern"; then
        ask "Command requires confirmation: $COMMAND"
    fi
done

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
    deny "Use $MERGE_SCRIPT <PR_NUMBER> instead of 'gh pr merge'. The script merges via the GitHub API without local checkout, which avoids worktree errors."
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
