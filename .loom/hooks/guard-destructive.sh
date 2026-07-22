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

# =============================================================================
# READ-ONLY FAST PATH (issue #3687) — default ON.
#
# guard-destructive.sh is a PreToolUse/Bash hook, so it fires before EVERY Bash
# tool call. In Bash-dense sessions (remote ops, benchmark drivers) the vast
# majority of those calls are obviously read-only — `git status`, `ls`, `grep`,
# `aws … describe*`, `gh … list` — yet each one still runs the full deny/ask
# gauntlet (~37 grep/awk/sed forks + a git rev-parse, ~179ms measured). This
# block short-circuits that overwhelmingly-common case to a silent `allow` with
# a single bash-builtin structural test (zero forks) plus, only when that test
# passes, one lazy `jq` config read.
#
# SECURITY: a fast path is a guard bypass by construction, so admission is
# purely STRUCTURAL and conservative — never content-sensitive:
#   1. Reject fast-path eligibility if the raw command contains ANY of
#      ;  &  |  <  >  backtick  $(  or a newline. This kills chaining, piping,
#      redirection, and command substitution (so `git status && <force-push>`,
#      `git status; rm -rf /`, `git status $(rm -rf /)`, `git status > /etc/x`
#      all fall through to the full path unchanged).
#   2. Exact first-token (command-word) allowlist — never a wrapper. Because the
#      allowlist is keyed on the literal first token, wrapper forms (`bash -c`,
#      `sh -c`, `eval`, `xargs`, `env … git status`, `sudo git status`) are
#      excluded automatically: their first token isn't allowlisted.
#   3. Verb/subcommand exactness for multi-word tools, chosen to be provably
#      disjoint from every existing deny/ask pattern:
#        git status|log|diff|show  (bare — `git -C /p status` is NOT admitted)
#        ls  grep  rg
#        gh <noun> view|list       (never delete/close/archive/…)
#        aws <service> describe*|get*|list*  and  aws s3 ls   (mirrors the
#          verb-anchoring already in CLOUD_ASK_PATTERNS: those verbs are never
#          mutating, so this only skips greps that were going to allow anyway)
#   cat and ssh are DELIBERATELY EXCLUDED from the built-in list:
#     - cat has a narrow existing ASK carve-out (cat …/.ssh/, cat …/.aws/
#       credentials); a blanket cat fast-path would silently skip it.
#     - ssh wraps an OPAQUE remote command string that the raw ALWAYS_BLOCK scan
#       still covers today; fast-pathing any `ssh …` would drop that coverage.
#
# False NEGATIVES (declining eligibility) are always safe — they just fall
# through to the correct, slower existing behavior. False POSITIVES are the only
# danger, so the eligibility test stays maximally conservative.
#
# CONFIG-ORDERING CHOICE: this block runs BEFORE REPO_ROOT is resolved (the git
# rev-parse subprocess below), on purpose — the structural test never needs the
# repo root. Only the toggle/extra-list config read needs a config file, and it
# is resolved LAZILY (only after structural admission already passed) by walking
# up from CWD to the nearest .loom/config.json WITHOUT forking git
# (fastpath_config_file). So a fast-pathed command pays: 1 bash-builtin test +
# (only if eligible) 1 stat-walk + 1 jq read — never the git rev-parse, never a
# deny/ask array, never a log write.
#
# Toggle: guards.readOnlyFastPath (default true) / LOOM_GUARD_READONLY_FASTPATH
# env (0/false/no disables, 1/true/yes forces on; env wins). Optional
# guards.readOnlyFastPathExtra is an EXTEND-ONLY array of literal first-word
# commands (each entry is a full-generality bypass for that command word).
# =============================================================================

# Locate the nearest .loom/config.json by walking up from CWD, fork-free (no
# git rev-parse). Cached. Best-effort: empty when none is found.
_FASTPATH_CFG_FILE=""
_FASTPATH_CFG_FILE_DONE=""
fastpath_config_file() {
    if [[ -z "$_FASTPATH_CFG_FILE_DONE" ]]; then
        _FASTPATH_CFG_FILE_DONE=1
        local d="$CWD"
        if [[ -n "$d" && "$d" == /* ]]; then
            while :; do
                if [[ -f "$d/.loom/config.json" ]]; then
                    _FASTPATH_CFG_FILE="$d/.loom/config.json"
                    break
                fi
                [[ "$d" == "/" ]] && break
                local parent="${d%/*}"
                [[ -z "$parent" ]] && parent="/"
                d="$parent"
            done
        fi
    fi
    printf '%s' "$_FASTPATH_CFG_FILE"
}

# Resolve the fast-path toggle (config + env), cached. Default true. Only ever
# called after structural admission has already passed, so the jq read stays off
# the hot path for commands that don't structurally qualify.
_FASTPATH_ENABLED_CACHE=""
fastpath_enabled() {
    if [[ -z "$_FASTPATH_ENABLED_CACHE" ]]; then
        local enabled=true cfg
        cfg=$(fastpath_config_file)
        if [[ -n "$cfg" ]]; then
            # Only an explicit `false` disables; a missing key or malformed JSON
            # (jq non-zero, caught by ||) stays ON — mirrors sql_guard_enabled().
            enabled=$(jq -r 'if .guards.readOnlyFastPath == false then "false" else "true" end' "$cfg" 2>/dev/null) || enabled=true
            [[ -n "$enabled" ]] || enabled=true
        fi
        # Env override wins over config.
        case "${LOOM_GUARD_READONLY_FASTPATH:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _FASTPATH_ENABLED_CACHE="$enabled"
    fi
    [[ "$_FASTPATH_ENABLED_CACHE" == "true" ]]
}

# Shared structural pre-check: reject any chaining/piping/redirection/
# substitution/newline. Pure bash builtins, zero forks.
fastpath_structural_ok() {
    case "$1" in
        *';'*|*'&'*|*'|'*|*'<'*|*'>'*|*'`'*|*'$('*) return 1 ;;
    esac
    [[ "$1" == *$'\n'* ]] && return 1
    return 0
}

# Built-in allowlist admission — bash-builtin regex/case only, zero forks.
fastpath_builtin_admits() {
    local cmd="$1"
    fastpath_structural_ok "$cmd" || return 1
    local -a t
    read -ra t <<< "$cmd"
    local n=${#t[@]}
    (( n >= 1 )) || return 1
    case "${t[0]}" in
        ls|grep|rg)
            return 0
            ;;
        git)
            (( n >= 2 )) || return 1
            case "${t[1]}" in
                status|log|diff|show) return 0 ;;
            esac
            return 1
            ;;
        gh)
            (( n >= 3 )) || return 1
            case "${t[2]}" in
                view|list) return 0 ;;
            esac
            return 1
            ;;
        aws)
            (( n >= 3 )) || return 1
            [[ "${t[1]}" == "s3" && "${t[2]}" == "ls" ]] && return 0
            case "${t[2]}" in
                describe*|get*|list*) return 0 ;;
            esac
            return 1
            ;;
    esac
    return 1
}

# Optional extend-only escape hatch: guards.readOnlyFastPathExtra is an array of
# literal first-word commands. Read lazily (only when the built-in list did not
# admit) and cached. Each entry is a full-generality bypass for that word.
_FASTPATH_EXTRA_CACHE=""
_FASTPATH_EXTRA_DONE=""
fastpath_extra_admits() {
    local cmd="$1"
    fastpath_structural_ok "$cmd" || return 1
    local -a t
    read -ra t <<< "$cmd"
    (( ${#t[@]} >= 1 )) || return 1
    local first="${t[0]}"
    if [[ -z "$_FASTPATH_EXTRA_DONE" ]]; then
        _FASTPATH_EXTRA_DONE=1
        local cfg
        cfg=$(fastpath_config_file)
        if [[ -n "$cfg" ]]; then
            _FASTPATH_EXTRA_CACHE=$(jq -r '(.guards.readOnlyFastPathExtra // []) | .[]' "$cfg" 2>/dev/null) || _FASTPATH_EXTRA_CACHE=""
        fi
    fi
    [[ -n "$_FASTPATH_EXTRA_CACHE" ]] || return 1
    local w
    while IFS= read -r w; do
        [[ -n "$w" && "$first" == "$w" ]] && return 0
    done <<< "$_FASTPATH_EXTRA_CACHE"
    return 1
}

# Fast-path dispatch. The env fast-disable check is first so a fully-disabled
# feature stays entirely off the hot path (no structural test, no config read).
_fastpath_env="${LOOM_GUARD_READONLY_FASTPATH:-}"
if [[ "$_fastpath_env" != "0" && "$_fastpath_env" != "false" && "$_fastpath_env" != "no" ]]; then
    if fastpath_builtin_admits "$COMMAND"; then
        # Silent allow: no stdout/stderr, no log_hook_error, before REPO_ROOT.
        fastpath_enabled && exit 0
    elif fastpath_extra_admits "$COMMAND"; then
        fastpath_enabled && exit 0
    fi
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

# =============================================================================
# Cloud CLI guard toggle — default ON.
#
# The cloud/docker ASK patterns (mutating aws ec2/lambda/s3/... subcommands and
# docker rm/rmi/stop/kill/restart) prompt for confirmation on every match. For a
# repo whose *purpose* is managing cloud infrastructure (launch/stop/terminate
# dev VMs, build/tear-down containers), that friction is a category error — the
# mutating calls are the product's own dev/test vocabulary. Such repos opt out;
# everyone else keeps the guard on. The genuinely catastrophic aws/docker denies
# in ALWAYS_BLOCK_PATTERNS are NOT gated by this toggle and stay active.
#
# Resolution order (highest precedence first):
#   1. LOOM_GUARD_CLOUD env var (0/false/no disables, 1/true/yes forces on)
#   2. .loom/config.json  ->  guards.cloudCli  (default true when absent)
#   3. Default: true (guard on)
#
# Mirrors sql_guard_enabled() exactly: cached in _CLOUD_GUARD_CACHE, invoked
# LAZILY only after a cloud pattern has already matched so the jq config read
# never touches the hot path for non-cloud commands. The config read is
# best-effort: any parse failure falls through to guard-ON.
# =============================================================================
_CLOUD_GUARD_CACHE=""
cloud_guard_enabled() {
    if [[ -z "$_CLOUD_GUARD_CACHE" ]]; then
        local enabled=true
        if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.loom/config.json" ]]; then
            # jq // is alternative-on-null, not default-on-missing, so use
            # if/then/else to treat only an explicit `false` as disabled (a
            # missing guards.cloudCli key stays on). On malformed JSON jq exits
            # non-zero and the `||` fallback restores the guard-ON default.
            enabled=$(jq -r 'if .guards.cloudCli == false then "false" else "true" end' "$REPO_ROOT/.loom/config.json" 2>/dev/null) || enabled=true
            [[ -n "$enabled" ]] || enabled=true
        fi
        # Env override wins over config.
        case "${LOOM_GUARD_CLOUD:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _CLOUD_GUARD_CACHE="$enabled"
    fi
    [[ "$_CLOUD_GUARD_CACHE" == "true" ]]
}

# =============================================================================
# rm-scope repo mode toggle — default REPO (safe-by-default; opt out to off).
#
# As of issue #3628 (ADR Option B) this guard defaults to `repo` mode: it
# DENIES any rm target that is neither under the repo / worktree areas nor on a
# built-in ephemeral allowlist (system temp dirs + the Claude scratchpad), in
# addition to the catastrophic top-level deny. A zero-config install therefore
# gets outside-repo rm protection out of the box (e.g. `rm -rf
# /Users/someone/important` is DENIED).
#
# The legacy permissive behaviour — block only catastrophic rm targets (root,
# $HOME, bare top-level dirs) and ALLOW every deeper subpath including subpaths
# OUTSIDE the repo — is now an explicit opt-out: guards.rmScope:"off" (or the
# synonym "permissive") / LOOM_RM_SCOPE=off. Consumers who relied on the old
# permissive default must set one of those to restore it.
#
# The catastrophic top-level deny stays unconditional in BOTH modes, so bare
# /tmp and / are still blocked regardless of rmScope.
#
# Resolution order (highest precedence first):
#   1. LOOM_RM_SCOPE env var (repo enables; off/0/no/permissive disables).
#      Overrides config. Absent → falls through to config/default.
#   2. .loom/config.json  ->  guards.rmScope: "off"/"permissive" => off;
#      absent key / any other value / malformed JSON => repo (the new default).
#   3. Default: repo (safe-by-default, current behaviour after #3628)
#
# Mirrors sql_guard_enabled() / cloud_guard_enabled(): cached in
# _RM_SCOPE_CACHE, invoked LAZILY only after a candidate rm target survives the
# catastrophic check, so the jq config read never touches the hot path for
# non-rm commands. The config read is best-effort: any parse failure falls
# through to REPO (the safe default) and never trips the ERR trap.
# =============================================================================
_RM_SCOPE_CACHE=""
rm_scope_repo_enabled() {
    if [[ -z "$_RM_SCOPE_CACHE" ]]; then
        local mode=repo
        if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.loom/config.json" ]]; then
            # Only an explicit guards.rmScope of "off" or "permissive" opts out
            # to the legacy permissive behaviour; any other value, a missing
            # key, or malformed JSON resolves to "repo" (the safe default — the
            # jq non-zero exit on malformed JSON is caught by the `||`
            # fallback, which also resolves to repo).
            mode=$(jq -r 'if (.guards.rmScope == "off" or .guards.rmScope == "permissive") then "off" else "repo" end' "$REPO_ROOT/.loom/config.json" 2>/dev/null) || mode=repo
            [[ -n "$mode" ]] || mode=repo
        fi
        # Env override wins over config.
        case "${LOOM_RM_SCOPE:-}" in
            repo)                  mode=repo ;;
            off|0|no|permissive)   mode=off ;;
        esac
        _RM_SCOPE_CACHE="$mode"
    fi
    [[ "$_RM_SCOPE_CACHE" == "repo" ]]
}

# Resolve the Loom worktree base dir for repo-scope checks. Mirrors the
# precedence of loom_worktree_root() in defaults/scripts/lib/worktree-root.sh
# (env -> config -> default), replicated inline so the hook stays
# self-contained and best-effort: any failure falls back to the default in-repo
# path and never fails the hook. Only called in repo mode, once per rm scan.
resolve_worktree_root() {
    local repo_root="$1"
    [[ -z "$repo_root" ]] && return 0
    # 1. Env override (highest priority); must be absolute.
    if [[ -n "${LOOM_WORKTREE_ROOT:-}" && "$LOOM_WORKTREE_ROOT" == /* ]]; then
        printf '%s/%s' "${LOOM_WORKTREE_ROOT%/}" "$(basename "$repo_root")"
        return 0
    fi
    # 2. Config key .loom/config.json -> worktree.root (absolute only).
    local config_file="$repo_root/.loom/config.json"
    if [[ -f "$config_file" ]]; then
        local cfg_root
        cfg_root=$(jq -r '.worktree.root? // empty' "$config_file" 2>/dev/null) || cfg_root=""
        if [[ -n "$cfg_root" && "$cfg_root" == /* ]]; then
            printf '%s/%s' "${cfg_root%/}" "$(basename "$repo_root")"
            return 0
        fi
    fi
    # 3. Default — in-repo worktrees dir.
    printf '%s/.loom/worktrees' "$repo_root"
}

# =============================================================================
# force-op branch-scope toggle — default ALL (preserve current behaviour).
#
# The three generic force-op ASK patterns (git push --force / -f /
# --force-with-lease and git reset --hard) prompt on EVERY match regardless of
# which branch is targeted. For an autonomous/background agent that cannot answer
# an interactive prompt, that stalls the agent on routine own-branch rebase /
# amend / reset work. The genuinely dangerous case is a force op against a
# PROTECTED branch (the repo default plus main/master), which stays a hard deny
# via ALWAYS_BLOCK_PATTERNS for the explicit main/master forms.
#
# guards.forceScope selects the behaviour:
#   "all"       (default) — ask on every force op, exactly as before (#3674).
#   "protected"           — ask only when the resolved target is a protected
#                           branch (repo default / main / master) or the branch
#                           identity is ambiguous (detached HEAD); allow force
#                           ops on the agent's own working branches.
#   "off"                 — never ask/deny on force ops. The unconditional
#                           main/master hard-denies in ALWAYS_BLOCK_PATTERNS
#                           STILL apply in every mode, including "off".
#
# Resolution order (highest precedence first):
#   1. LOOM_FORCE_SCOPE env var (all/protected/off). Overrides config.
#   2. .loom/config.json  ->  guards.forceScope: "protected"/"off"; absent key /
#      any other value / malformed JSON => "all" (the current-behaviour default).
#   3. Default: all (preserve current behaviour byte-for-byte)
#
# Mirrors sql_guard_enabled() / rm_scope_repo_enabled(): cached in
# _FORCE_SCOPE_CACHE, invoked LAZILY only after a command plausibly carries a
# force op, so the jq config read never touches the hot path for the ~99% of
# commands that are not force ops. The config read is best-effort: any parse
# failure falls through to "all" (the safe default) and never trips the ERR trap.
# =============================================================================
_FORCE_SCOPE_CACHE=""
force_scope_mode() {
    if [[ -z "$_FORCE_SCOPE_CACHE" ]]; then
        local mode=all
        if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.loom/config.json" ]]; then
            # jq // is alternative-on-null, not default-on-missing, so use an
            # explicit if/elif/else: only "protected"/"off" opt away from the
            # default. A missing key, any other value, or malformed JSON (jq
            # exits non-zero, caught by ||) resolves to "all".
            mode=$(jq -r 'if (.guards.forceScope == "protected") then "protected" elif (.guards.forceScope == "off") then "off" else "all" end' "$REPO_ROOT/.loom/config.json" 2>/dev/null) || mode=all
            [[ -n "$mode" ]] || mode=all
        fi
        # Env override wins over config.
        case "${LOOM_FORCE_SCOPE:-}" in
            all)         mode=all ;;
            protected)   mode=protected ;;
            off)         mode=off ;;
        esac
        _FORCE_SCOPE_CACHE="$mode"
    fi
    printf '%s' "$_FORCE_SCOPE_CACHE"
}

# Resolve the repository's default branch name for the protected-branch set.
# Inlined, offline-first detection mirroring loom_default_branch() in
# defaults/scripts/lib/default-branch.sh, replicated here so the hook stays
# self-contained (same rationale as resolve_worktree_root() mirroring
# loom_worktree_root() rather than sourcing it). Deliberately OMITS the network
# `git ls-remote` fallback — a PreToolUse hook must never touch the network — so
# resolution is env-var / local-ref only; the main/master literals in the
# protected set below cover the common case when local detection yields nothing.
# Best-effort: echoes the branch name or nothing on failure. Only invoked in
# "protected" mode after a force op has already matched.
resolve_default_branch() {
    local dir="$1"
    # 1. Env var override — highest priority (escape hatch + test seam).
    if [[ -n "${LOOM_DEFAULT_BRANCH:-}" ]]; then
        printf '%s' "$LOOM_DEFAULT_BRANCH"
        return 0
    fi
    [[ -z "$dir" ]] && return 0
    # 2. Local symbolic ref for origin/HEAD — offline, no network.
    local sref
    sref=$(git -C "$dir" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true)
    if [[ -n "$sref" ]]; then
        printf '%s' "${sref#origin/}"
        return 0
    fi
    # 3. Local probe: prefer main, then master, whichever remote ref exists.
    local candidate
    for candidate in main master; do
        if git -C "$dir" show-ref --verify --quiet "refs/remotes/origin/$candidate" 2>/dev/null; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    # 4. No local answer — echo nothing (caller's main/master literals cover it).
    return 0
}

# Parse force-op segments out of a command, emitting one TAB-separated
# "<cpath>\t<target>" line per genuine git force-push / hard-reset. Portable awk
# only (mirrors extract_rm_targets / lifecycle_or_cloud_reason segment parsing):
#   - split on ; | & && || and newline, strip a leading sudo wrapper.
#   - only a segment whose command word is `git` is considered.
#   - `git -C <path> ...` sets <cpath>; other pre-subcommand global options are
#     skipped (`-c <k=v>` consumes its argument).
#   - push: emitted only when a --force/-f/--force-with-lease flag is present.
#     ONE line is emitted per positional refspec (pos[2], pos[3], …) after the
#     remote — a multi-refspec push like `git push --force origin a b` emits a
#     line for `a` AND `b`, so a protected branch in any refspec position (not
#     just the first) reaches the caller's per-line check (#3674 follow-up).
#     <target> is the destination branch parsed from each refspec —
#       * `<src>:<dst>` form => <dst>
#       * a bare ref        => the ref with a leading `+` stripped
#       * `HEAD`, or no ref => the literal "@HEAD@" (resolve checked-out branch)
#   - reset --hard: always emitted with <target> = "@HEAD@".
# The caller resolves "@HEAD@" to the checked-out branch and applies the mode.
parse_force_ops() {
    printf '%s' "$1" | awk '
    BEGIN { SEP = sprintf("%c", 31) }  # US (unit separator) — non-whitespace so
                                       # bash read does not trim an empty cpath.
    {
        gsub(/&&|\|\||[;|&]/, "\n")
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/^sudo[ \t]+/, "", seg)
            sub(/^[ \t]+/, "", seg)
            m = split(seg, toks, /[ \t]+/)
            if (m == 0) continue
            if (toks[1] != "git") continue
            # Walk global options between `git` and the subcommand.
            cpath = ""
            k = 2
            while (k <= m) {
                t = toks[k]
                if (t == "-C") { cpath = toks[k+1]; k += 2; continue }
                if (t == "-c") { k += 2; continue }
                if (t ~ /^-/)  { k += 1; continue }
                break
            }
            if (k > m) continue
            subcmd = toks[k]
            if (subcmd == "push") {
                force = 0
                np = 0
                # pos is a file-global awk array; clear it per segment
                # (portable — split with an empty string empties the array) so
                # refspecs from a prior segment cannot leak into this one now
                # that we read every positional slot, not just pos[2].
                split("", pos)
                for (j = k+1; j <= m; j++) {
                    t = toks[j]
                    if (t == "--force" || t == "-f" || t == "--force-with-lease" || t ~ /^--force-with-lease=/) { force = 1; continue }
                    if (t ~ /^-/) continue
                    np++
                    pos[np] = t
                }
                if (!force) continue
                # pos[1] is the remote; pos[2..np] are refspecs. Emit ONE line per
                # positional refspec so a protected branch in ANY refspec position
                # (not just the first) reaches the per-line check in the caller. A
                # bare push with no refspec (np < 2) resolves the checked-out branch.
                if (np < 2) {
                    print cpath SEP "@HEAD@"
                } else {
                    for (p = 2; p <= np; p++) {
                        rs = pos[p]
                        sub(/^\+/, "", rs)
                        ci = index(rs, ":")
                        if (ci > 0) rs = substr(rs, ci + 1)
                        target = "@HEAD@"
                        if (rs != "HEAD" && rs != "") target = rs
                        print cpath SEP target
                    }
                }
            } else if (subcmd == "reset") {
                hard = 0
                for (j = k+1; j <= m; j++) if (toks[j] == "--hard") hard = 1
                if (hard) print cpath SEP "@HEAD@"
            }
        }
    }'
}

# Redact the quoted VALUES of known text-carrying flags (--body, -m/--message,
# --title, --notes) so a dangerous-looking phrase quoted INSIDE such a value no
# longer trips the raw ALWAYS_BLOCK_PATTERNS substring scan. Used ONLY to build
# COMMAND_NO_LITERAL_TEXT for that one loop (mirrors the COMMAND_NO_COMMENT
# precedent); every other scan keeps reading the raw command. This kills the
# #3679 false positive where `gh pr comment --body "…git push --force origin
# main…"` / `git commit -m "…"` hard-denied even though nothing executes.
#
# Safety floor preserved two ways:
#   - `-c` is deliberately NOT a text-carrying flag, so `bash -c '<payload>'`
#     is never redacted and its payload stays caught by the raw scan.
#   - a quoted span is redacted ONLY when it carries no command-substitution or
#     backtick opener (`$(` — which also subsumes the arithmetic `$((` — or a
#     backtick). So a smuggling attempt like `git commit -m "$(git push --force
#     origin main)"` is left intact and still hard-denies.
# Each redacted span is replaced by a SAME-LENGTH placeholder so byte offsets of
# the surrounding command are unchanged. Best-effort like COMMAND_NO_COMMENT:
# it does not model backslash-escaped quotes, but since the result feeds only
# the narrowing (never widening) catastrophic scan, the worst case is a raw
# substring surviving — never a catastrophic block being skipped incorrectly.
strip_literal_text() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)   # single quote
        DQ = sprintf("%c", 34)   # double quote
        # boundary + text-carrying flag + optional (ws / = / ws) + quoted span
        re = "(^|[ \t])(--message|--body|--notes|--title|-m)[ \t]*=?[ \t]*(" \
             DQ "[^" DQ "]*" DQ "|" SQ "[^" SQ "]*" SQ ")"
    }
    {
        s = $0
        out = ""
        while (match(s, re)) {
            pre     = substr(s, 1, RSTART - 1)
            matched = substr(s, RSTART, RLENGTH)
            s       = substr(s, RSTART + RLENGTH)
            # Locate the opening quote inside the matched span.
            qpos = 0
            for (i = 1; i <= length(matched); i++) {
                c = substr(matched, i, 1)
                if (c == DQ || c == SQ) { qpos = i; break }
            }
            head  = substr(matched, 1, qpos)                              # up to & incl. opening quote
            qchar = substr(matched, qpos, 1)
            inner = substr(matched, qpos + 1, length(matched) - qpos - 1) # between the quotes
            # Redact ONLY provably inert text (no command substitution / backtick).
            if (index(inner, "$(") == 0 && index(inner, "`") == 0) {
                gsub(/./, "X", inner)
            }
            out = out pre head inner qchar
        }
        out = out s
        printf "%s", out
    }'
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
    # NOTE: `aws ec2 terminate` is deliberately NOT in this raw catastrophic
    # scan. For a repo whose job is standing up and tearing down dev VMs the
    # teardown path (`terminate-instances`) is a first-class workflow, so it is
    # downgraded to an ask via the toggle-gated CLOUD_ASK_PATTERNS below (and
    # fully bypassed when LOOM_GUARD_CLOUD=0 / guards.cloudCli:false). The other
    # aws forms here stay ungated — they remain a hard safety floor (#3593).
    'aws s3 rm.*--recursive'
    'aws s3 rb'
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

# Build a literal-text-redacted working copy ONLY for the catastrophic scan
# below, so a force-push-to-main phrase quoted inside a --body/-m/--title/--notes
# value no longer false-positives (#3679). The awk only runs when one of those
# flags is actually present, keeping it off the hot path (mirrors the
# COMMAND_NO_COMMENT `#`-present guard). `-c` is intentionally excluded so
# `bash -c '<payload>'` payloads still reach the raw scan; spans carrying `$(` /
# backtick are left intact so command-substitution smuggling still hard-denies.
COMMAND_NO_LITERAL_TEXT="$COMMAND"
if [[ "$COMMAND" == *"--body"* || "$COMMAND" == *"--message"* || \
      "$COMMAND" == *"--title"* || "$COMMAND" == *"--notes"* || \
      "$COMMAND" == *"-m"* ]]; then
    COMMAND_NO_LITERAL_TEXT=$(strip_literal_text "$COMMAND")
fi

for pattern in "${ALWAYS_BLOCK_PATTERNS[@]}"; do
    if echo "$COMMAND_NO_LITERAL_TEXT" | grep -qiE "$pattern"; then
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

            # Opt-in repo-scoped strict mode (guards.rmScope:"repo" /
            # LOOM_RM_SCOPE=repo). The catastrophic top-level deny above stays
            # unconditional; here we additionally DENY any target that is
            # neither under the repo / worktree areas nor on the built-in
            # ephemeral allowlist. Default OFF preserves the permissive
            # behaviour byte-for-byte (rm_scope_repo_enabled() returns false).
            if rm_scope_repo_enabled; then
                IN_SCOPE=false

                # Repo + worktree areas. Prefix matches carry a trailing slash
                # (or match the dir itself) so a sibling dir sharing a name
                # prefix — e.g. "<repo>-sibling" vs "<repo>" — is NOT admitted.
                if [[ -n "$REPO_ROOT" ]]; then
                    if [[ "$ABS_PATH" == "$REPO_ROOT" || "$ABS_PATH" == "$REPO_ROOT"/* ]]; then
                        IN_SCOPE=true
                    fi
                    # The default in-repo worktrees dir is always in scope, even
                    # when an external worktree.root / LOOM_WORKTREE_ROOT is set.
                    if [[ "$IN_SCOPE" == false ]] && \
                       { [[ "$ABS_PATH" == "$REPO_ROOT/.loom/worktrees" || "$ABS_PATH" == "$REPO_ROOT/.loom/worktrees"/* ]]; }; then
                        IN_SCOPE=true
                    fi
                    # Configured/overridden worktree root (external volumes).
                    if [[ "$IN_SCOPE" == false ]]; then
                        if [[ -z "${_WT_ROOT+x}" ]]; then
                            _WT_ROOT=$(resolve_worktree_root "$REPO_ROOT")
                        fi
                        if [[ -n "$_WT_ROOT" ]] && \
                           { [[ "$ABS_PATH" == "$_WT_ROOT" || "$ABS_PATH" == "$_WT_ROOT"/* ]]; }; then
                            IN_SCOPE=true
                        fi
                    fi
                fi

                # Built-in ephemeral allowlist: system temp roots + the Claude
                # scratchpad. normalize_abs_path() is LEXICAL — it does NOT
                # resolve symlinks — so on macOS both the symlink form (/tmp,
                # /var/tmp, /var/folders) AND its /private target must be listed.
                # A bare temp root (/tmp, /private/tmp, …) is NOT matched here:
                # those have no trailing component, so the catastrophic
                # top-level deny above already handled bare /tmp, and a bare
                # /private/tmp falls through to the out-of-scope deny.
                if [[ "$IN_SCOPE" == false ]]; then
                    case "$ABS_PATH" in
                        /tmp/*|/private/tmp/*|\
                        /var/tmp/*|/private/var/tmp/*|\
                        /var/folders/*|/private/var/folders/*|\
                        */claude-*/*/scratchpad/*)
                            IN_SCOPE=true ;;
                    esac
                fi

                if [[ "$IN_SCOPE" == false ]]; then
                    deny "BLOCKED: rm target outside repo scope (LOOM_RM_SCOPE=repo): $ABS_PATH"
                fi
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
# FORCE-OP BRANCH SCOPE - branch-aware git push --force / git reset --hard
#
# Gated by guards.forceScope / LOOM_FORCE_SCOPE (see force_scope_mode() above).
#   - "all"       (default): every force op asks — byte-for-byte the pre-#3674
#                            behaviour, so existing tests still see an ask.
#   - "protected"          : ask only when the resolved target is a protected
#                            branch (repo default / main / master) or the branch
#                            identity is ambiguous (detached HEAD / unresolved);
#                            own working branches pass straight through.
#   - "off"                : never ask/deny here.
#
# The explicit main/master force-push hard-denies in ALWAYS_BLOCK_PATTERNS above
# already fired for those forms and are NOT reachable here in ANY mode — this
# block only ever downgrades to ask/allow, never weakens a hard deny.
#
# A cheap pre-check keeps the config read + segment parser off the hot path for
# the ~99% of commands with no force flag at all.
# =============================================================================
if [[ "$COMMAND_NO_COMMENT" == *git* ]] && \
   echo "$COMMAND_NO_COMMENT" | grep -qE '(--force|--force-with-lease|(^|[[:space:]])-f([[:space:]]|$)|--hard)'; then
    _FORCE_MODE=$(force_scope_mode)
    if [[ "$_FORCE_MODE" != "off" ]]; then
        _FORCE_OPS=$(parse_force_ops "$COMMAND_NO_COMMENT")
        if [[ -n "$_FORCE_OPS" ]]; then
            if [[ "$_FORCE_MODE" == "all" ]]; then
                # Preserve pre-#3674 behaviour byte-for-byte: any force op asks.
                ask "Command requires confirmation: $COMMAND"
            fi
            # "protected" mode: ask only for protected-branch or ambiguous
            # targets; allow own working branches. resolve_default_branch() plus
            # the main/master literals form the protected set.
            while IFS=$'\037' read -r _fcpath _ftarget; do
                [[ -z "$_ftarget" ]] && _ftarget="@HEAD@"
                _fcwd="$_fcpath"
                [[ -z "$_fcwd" ]] && _fcwd="$CWD"
                if [[ "$_ftarget" == "@HEAD@" ]]; then
                    _fbranch=""
                    if [[ -n "$_fcwd" ]]; then
                        _fbranch=$(git -C "$_fcwd" symbolic-ref --short HEAD 2>/dev/null || true)
                    fi
                    if [[ -z "$_fbranch" ]]; then
                        # Detached HEAD / unresolved identity is ambiguous — ask,
                        # never silently allow (fail toward asking).
                        ask "Command requires confirmation: $COMMAND (force operation on a detached or unresolved branch)"
                    fi
                    _ftarget="$_fbranch"
                fi
                _fdefault=$(resolve_default_branch "$_fcwd")
                if [[ "$_ftarget" == "main" || "$_ftarget" == "master" ]] || \
                   { [[ -n "$_fdefault" && "$_ftarget" == "$_fdefault" ]]; }; then
                    ask "Command requires confirmation: $COMMAND (force operation targets protected branch '$_ftarget')"
                fi
            done <<< "$_FORCE_OPS"
            # No protected/ambiguous target matched — fall through to allow.
        fi
    fi
fi

# =============================================================================
# REQUIRE CONFIRMATION - Potentially dangerous but sometimes legitimate
# =============================================================================

ASK_PATTERNS=(
    # NOTE: the force-op patterns (git push --force / -f / --force-with-lease and
    # git reset --hard) are NOT in this ungated array. They are handled by the
    # branch-aware FORCE-OP BRANCH SCOPE block above, gated by
    # force_scope_mode() (guards.forceScope / LOOM_FORCE_SCOPE, #3674), so an
    # autonomous agent can force-push / hard-reset its own working branch without
    # a stall while protected-branch force ops still ask. git clean / checkout .
    # / restore . stay here — they are not force ops and have no branch scope.
    'git clean -fd'
    'git checkout \.'
    'git restore \.'

    # GitHub operations that modify shared state
    'gh pr close'
    'gh issue close'
    'gh release delete'
    'gh label delete'

    # NOTE: cloud CLI (aws) + docker ASK patterns are NOT in this ungated array.
    # They live in CLOUD_ASK_PATTERNS below, gated by cloud_guard_enabled() so
    # cloud-dev repos can opt down (LOOM_GUARD_CLOUD=0 / guards.cloudCli:false).

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
# git read-tree WITHOUT an isolating GIT_INDEX_FILE assignment
#
# A bare `git read-tree` (no tree-ish, no isolated index) is equivalent to
# `git read-tree --empty`: it clobbers the repository's REAL staging index,
# turning every tracked file into a phantom staged deletion. The working tree
# and HEAD are left untouched and NO reflog entry is written, so the corruption
# is silent and near-invisible (issue #3637 — a judge ran one against the main
# checkout during a merge simulation and emptied the live index).
#
# This is an ASK (not a deny) because it is generic git hygiene, not a Loom
# workflow rule, and an isolated form is legitimate. It is kept narrow: the
# safe, index-free path is `git merge-tree --write-tree <base> <branch>` for a
# merge preview, or `GIT_INDEX_FILE=$(mktemp) git read-tree <tree>` when a
# temporary index really is needed. Any command that carries a `GIT_INDEX_FILE=`
# assignment is treated as isolated and passes through untouched.
#
# `git commit-tree` is intentionally NOT guarded here — it writes a commit
# object from an existing tree and does not mutate the index.
# =============================================================================
if echo "$COMMAND_NO_COMMENT" | grep -qE '(^|[;&|(]|[[:space:]])git[[:space:]]+read-tree'; then
    # Isolated form (GIT_INDEX_FILE=... git read-tree ...) is allowed.
    if ! echo "$COMMAND_NO_COMMENT" | grep -qE 'GIT_INDEX_FILE='; then
        ask "Command requires confirmation: $COMMAND (a bare 'git read-tree' empties the real staging index with no reflog trace; use 'git merge-tree --write-tree <base> <branch>' for a merge preview, or isolate with GIT_INDEX_FILE=\$(mktemp))"
    fi
fi

# =============================================================================
# CLOUD CLI ASK patterns — gated by the cloud CLI guard toggle
#
# Kept separate from ASK_PATTERNS so cloud-dev repos can opt out
# (guards.cloudCli:false / LOOM_GUARD_CLOUD=0). cloud_guard_enabled() is
# consulted only AFTER a cloud pattern matches, so the config read stays off the
# hot path for non-cloud commands (mirrors the SQL DDL block above).
#
# The aws entries are VERB-ANCHORED (case-sensitive ERE against the
# comment-stripped command): only mutating subcommands match, never read-only
# describe*/get*/list*/ls. So `aws ec2 describe-instances`, `aws s3 ls`, and
# `aws lambda list-functions` no longer prompt, while `run-instances`,
# `create-*`, `terminate-instances`, `stop-instances`, `lambda invoke`,
# `lambda publish*`, `sns publish`, etc. still ask.
#
# The docker entries already name only mutating verbs (rm/rmi/stop/kill/restart)
# and never match read-only `docker ps`/`docker logs`, so they are unchanged —
# they only move under this toggle.
# =============================================================================
CLOUD_ASK_PATTERNS=(
    # aws mutating subcommands (verb-anchored). The service list covers the
    # common infra-mutating namespaces; the verb list is the mutating vocabulary
    # (never describe*/get*/list*/ls). terminate lands here — an ask, not a deny.
    # invoke/publish are mutating (lambda invoke runs arbitrary code with side
    # effects; lambda publish-version / publish-layer-version and sns publish
    # mutate state) — there is no read-only `aws <svc> invoke|publish`, so they
    # cannot introduce describe/get/list false-positives. copy (ec2
    # copy-image/copy-snapshot) and assign (ec2 assign-*-addresses) are likewise
    # mutating-only. All were caught by the pre-#3593 bare `aws ec2|lambda`
    # prefixes and must stay asks (#3595).
    'aws (ec2|lambda|s3api|rds|iam|autoscaling|cloudformation|eks|ecs|elb|elbv2|route53|dynamodb|sns|sqs) (run|create|delete|terminate|stop|start|modify|update|put|reboot|authorize|revoke|attach|detach|associate|disassociate|register|deregister|enable|disable|add|remove|set|import|restore|reset|cancel|scale|invoke|publish|copy|assign)'
    # aws s3 (high-level) mutating verbs. `ls` is intentionally excluded. `mb`
    # (make-bucket) is mutating and was caught by the old bare `aws s3` prefix.
    'aws s3 (rm|rb|cp|mv|sync|mb)'

    # Docker operations (already mutating-verb only; does not match docker ps/logs)
    'docker rm'
    'docker rmi'
    'docker stop'
    'docker kill'
    'docker restart'
)

for pattern in "${CLOUD_ASK_PATTERNS[@]}"; do
    if echo "$COMMAND_NO_COMMENT" | grep -qE "$pattern" && cloud_guard_enabled; then
        ask "Command requires confirmation: $COMMAND (set guards.cloudCli:false in .loom/config.json if this repo manages cloud infra as a first-class workflow)"
    fi
done

# =============================================================================
# NOTE: The two Loom-workflow-specific guards (the 'gh pr merge' → merge-pr.sh
# redirect, and the 'pip install -e' worktree block keyed on LOOM_WORKTREE_PATH)
# were extracted into guard-loom-workflow.sh (issue #3604). They are registered
# as a separate PreToolUse/Bash hook and fire independently of this guard. This
# file is the generic repository-hygiene guard, on its way to Repo Skills
# (rjwalters/repo#13); the Loom-specific pair stays Loom-owned.
# =============================================================================

# =============================================================================
# ALLOW - Everything else passes through
# =============================================================================

exit 0
