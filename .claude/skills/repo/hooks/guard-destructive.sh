#!/usr/bin/env bash
# guard-destructive.sh - PreToolUse hook to block destructive agent commands
#
# Part of Repo Skills (https://github.com/rjwalters/repo), and the CANONICAL
# generic destructive-command guard (rjwalters/repo#30): Loom and other tooling
# defer to this copy rather than shipping their own. It is installed by
# install.sh into .claude/skills/repo/hooks/ and wired into the consumer repo's
# .claude/settings.json PreToolUse -> Bash matcher.
#
# Provenance: the precision work in this file (segment parsing, quote-aware
# splitting, literal-text redaction, the read-only fast path, toggles, decision
# telemetry) was developed in rjwalters/loom and consolidated here. Bare issue
# refs like (#3553)/(#3771) refer to rjwalters/loom issues; refs written
# repo#NN refer to rjwalters/repo.
#
# =============================================================================
# STABLE INTERFACE (the contract downstream tools — e.g. Loom — rely on)
# =============================================================================
#
# Input:  JSON on stdin with .tool_input.command and .cwd (Claude Code
#         PreToolUse hook payload). An empty/absent command allows.
# Output: silence + exit 0  => allow;  otherwise a single JSON object:
#   { "hookSpecificOutput": { "hookEventName": "PreToolUse",
#       "permissionDecision": "deny|ask", "permissionDecisionReason": "..." } }
# Exit:   ALWAYS 0, even on deny/ask/internal error. The "hookEventName" field
#         is REQUIRED by Claude Code's schema — without it the decision is
#         silently discarded and the guard becomes inert.
# Errors: this script MUST never exit non-zero or emit invalid output. Any
#         internal error is caught by the ERR trap, logged to
#         <script-dir>/../logs/hook-errors.log, and resolves to allow (fail
#         open) to prevent infinite retry loops in Claude Code.
#
# Config: guards.* keys are read from BOTH config locations —
#           .claude/skills/repo/config.json   (Repo Skills' own; WINS)
#           .loom/config.json                 (legacy/Loom; fallback)
#         Env vars override config; the REPO_* name wins over the legacy
#         LOOM_* name. Both names are a stable part of this interface.
#
#   Toggle (config key)          REPO_* env                     legacy LOOM_* env              default
#   guards.readOnlyFastPath      REPO_GUARD_READONLY_FASTPATH   LOOM_GUARD_READONLY_FASTPATH   on
#   guards.readOnlyFastPathExtra (config-only extend list)      —                              []
#   guards.positionalMaskAllowlist (config-only extend list)    —                              []
#   guards.sqlDdl                REPO_GUARD_SQL                 LOOM_GUARD_SQL                 on
#   guards.cloudCli              REPO_GUARD_CLOUD               LOOM_GUARD_CLOUD               on
#   guards.reversibleGh          REPO_GUARD_REVERSIBLE_GH       LOOM_GUARD_REVERSIBLE_GH       off (opt-in)
#   guards.decisionLog           REPO_GUARD_DECISION_LOG        LOOM_GUARD_DECISION_LOG        off (opt-in)
#   (decision log path)          REPO_GUARD_DECISION_LOG_FILE   LOOM_GUARD_DECISION_LOG_FILE   <script-dir>/../logs/guard-decisions.log
#   guards.rmScope               REPO_RM_SCOPE                  LOOM_RM_SCOPE                  repo
#   guards.forceScope            REPO_FORCE_SCOPE               LOOM_FORCE_SCOPE               all
#   (default-branch seam)        REPO_DEFAULT_BRANCH            LOOM_DEFAULT_BRANCH            resolved from git
#   worktree.root (config key)   —                              LOOM_WORKTREE_ROOT             <repo>/.loom/worktrees
#
# positionalMaskAllowlist is a config-only array of command names (no single
# env var makes sense for a list, mirroring readOnlyFastPathExtra above): each
# entry masks that command's own quoted POSITIONAL arguments in the ASK-tier
# working copy ONLY (never the catastrophic scan), so a read-only tool's own
# search/dedup text is not misread as an ask-triggering phrase (#195). Absent
# or empty (the default) is a no-op. The command words the two DENY-tier
# consumers of that scan recognize as their subject — grep/egrep/fgrep/rg
# (SQL DDL) and cp/mv/tee/sed (#4178 write confinement) — can never be added,
# regardless of config — see mask_ask_positional_args()'s header comment and
# positional_mask_cmdre()'s consumer audit table.
#
# On/off toggles accept 0/false/no and 1/true/yes. rmScope accepts
# repo|off|permissive; forceScope accepts all|protected|off. Loom-compat
# surfaces (the .loom/config.json fallback, LOOM_* env names, and the
# .loom/worktrees rm allowlist) are permanent parts of this contract, not
# transitional shims — Loom installs no generic guard of its own.
# =============================================================================
#
# IMPORTANT: This hook only fires when Claude Code is invoked with:
#   --dangerously-skip-permissions  <- hooks FIRE
#
# It does NOT fire with:
#   --permission-mode bypassPermissions  <- hooks SKIPPED entirely
#
# If you have a shell alias like 'alias claude="claude --permission-mode bypassPermissions"',
# this safety hook will be silently disabled in interactive sessions.
# Use --dangerously-skip-permissions instead for automation that needs hooks.
#
# Decisions:
#   - Block (deny): Dangerous commands that should never run
#   - Ask: Commands that need human confirmation
#   - Allow: Everything else (exit 0, no output)

# Determine log directory relative to this script's location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo ".")"
HOOK_ERROR_LOG="${SCRIPT_DIR}/../logs/hook-errors.log"

# Decision telemetry log (issue #3771) — a SEPARATE JSONL file from
# HOOK_ERROR_LOG. At runtime SCRIPT_DIR is the installed hook's own directory
# (.claude/skills/repo/hooks/), so this resolves to
# .claude/skills/repo/logs/guard-decisions.log in a real install.
# REPO_GUARD_DECISION_LOG_FILE (or the legacy LOOM_ name) overrides the path (a
# test seam; also lets an operator point the log elsewhere). Off by default —
# see decision_log_enabled() below.
DECISION_LOG="${REPO_GUARD_DECISION_LOG_FILE:-${LOOM_GUARD_DECISION_LOG_FILE:-${SCRIPT_DIR}/../logs/guard-decisions.log}}"

# Log a diagnostic error message (best-effort, never fails the script)
log_hook_error() {
    local msg="$1"
    # Ensure log directory exists
    mkdir -p "$(dirname "$HOOK_ERROR_LOG")" 2>/dev/null || true
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [guard-destructive] $msg" >> "$HOOK_ERROR_LOG" 2>/dev/null || true
}

# =============================================================================
# DECISION TELEMETRY (issue #3771) — one JSONL record per deny/ask decision.
#
# Append a machine-readable record to DECISION_LOG each time the guard denies or
# asks, so false-positive friction becomes measurable (which patterns fire, how
# often, before/after a precision fix). Deliberately does NOT log `allow`: the
# #3687 read-only fast path's zero-overhead silent-allow must stay silent, and
# allow-logging would swamp the log with the ~99% common case.
#
# STABLE SCHEMA (the contract #3772's reader/aggregation tooling stacks on — do
# NOT rename fields without considering that dependency), one JSON object per
# line:
#   {"ts":"<UTC>","decision":"deny"|"ask","pattern":"<tag>",
#    "tier":"catastrophic"|"ask","command":"<redacted>"}
#     ts       — UTC timestamp, same format as log_hook_error's date -u call.
#     decision — "deny" or "ask".
#     pattern  — a short, stable rule tag (NOT the full free-text reason). For
#                the pattern-array loops it is the matched pattern; the non-loop
#                sites pass a static tag (e.g. "sql-ddl", "rm-protected-path").
#     tier     — "catastrophic" for deny, "ask" for ask.
#     command  — the command string, REDACTED via strip_literal_text() so no raw
#                --body/-m/--title/--notes/--comment secret value is persisted.
#
# Best-effort like log_hook_error: gated by the lazy decision_log_enabled()
# toggle, and a log-write failure (permission denied, disk full, missing dir)
# NEVER changes the deny/ask decision and NEVER causes a non-zero exit. Callers
# invoke it as `log_guard_decision ... || true` so it can never trip the ERR
# trap.
#
# One-liner to summarize fires by pattern (AC — full tooling is #3772):
#   jq -r '.pattern' .claude/skills/repo/logs/guard-decisions.log | sort | uniq -c | sort -rn
# =============================================================================
log_guard_decision() {
    # Args: <decision> <tier> <pattern-tag>. The command is read from the global
    # $COMMAND and redacted here. Returns 0 unconditionally.
    decision_log_enabled || return 0
    local decision="$1" tier="$2" tag="${3:-$1}"
    local ts redacted line
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null) || ts=""
    # Redact quoted --body/-m/--title/--notes/--comment values (same redactor the
    # pattern-matching tiers use) so no raw secret text is persisted to a log that
    # aggregates across sessions. Fall back to raw only if redaction produced
    # nothing (awk unavailable) — impossible in practice since jq is required and
    # awk is used throughout.
    redacted=$(strip_literal_text "$COMMAND" 2>/dev/null) || redacted=""
    [[ -n "$redacted" ]] || redacted="$COMMAND"
    # Build the JSONL record with jq so all escaping is correct. If jq fails,
    # skip the write entirely rather than hand-roll a line that might mis-escape.
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
    # Group the append so a FAILED >> redirection (unwritable/nonexistent dir)
    # has its bash-level error caught by the group's stderr redirect too — a bare
    # `>> "$f" 2>/dev/null` does not suppress the redirection-open error itself.
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
#        jq  wc  head  tail        (pure read-only text/JSON filters — none has
#          an in-place-mutation flag, so any args are admitted, #3772)
#        test  [  [[               (boolean file/string test builtins — no
#          mutation surface at all, #3772)
#        find                      (admitted for any args EXCEPT when a dangerous
#          action-primary is present: -delete, -exec, -execdir, -ok, -okdir,
#          -fls, -fprint, -fprint0, -fprintf. Any of those disqualifies eligibility
#          and falls through to the full path — structural, not content-scanned,
#          so a future `find -delete` deny rule is never silently bypassed, #3772)
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
# up from CWD to the nearest guard config (.claude/skills/repo/config.json or
# legacy .loom/config.json) WITHOUT forking git
# (fastpath_config_file). So a fast-pathed command pays: 1 bash-builtin test +
# (only if eligible) 1 stat-walk + 1 jq read — never the git rev-parse, never a
# deny/ask array, never a log write.
#
# Toggle: guards.readOnlyFastPath (default true) / LOOM_GUARD_READONLY_FASTPATH
# env (0/false/no disables, 1/true/yes forces on; env wins). Optional
# guards.readOnlyFastPathExtra is an EXTEND-ONLY array of literal first-word
# commands (each entry is a full-generality bypass for that command word).
# =============================================================================

# Locate the nearest guard config by walking up from CWD, fork-free (no git
# rev-parse). At each level Repo Skills' own config
# (.claude/skills/repo/config.json) wins over the legacy .loom/config.json.
# Cached. Best-effort: empty when none is found.
_FASTPATH_CFG_FILE=""
_FASTPATH_CFG_FILE_DONE=""
fastpath_config_file() {
    if [[ -z "$_FASTPATH_CFG_FILE_DONE" ]]; then
        _FASTPATH_CFG_FILE_DONE=1
        local d="$CWD"
        if [[ -n "$d" && "$d" == /* ]]; then
            while :; do
                if [[ -f "$d/.claude/skills/repo/config.json" ]]; then
                    _FASTPATH_CFG_FILE="$d/.claude/skills/repo/config.json"
                    break
                fi
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
        # Env override wins over config; REPO_* wins over the legacy LOOM_* name.
        case "${LOOM_GUARD_READONLY_FASTPATH:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        case "${REPO_GUARD_READONLY_FASTPATH:-}" in
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
        jq|wc|head|tail)
            # Pure read-only text/JSON filters. None writes files or takes an
            # in-place-mutation flag (jq has no `-i`; wc/head/tail never mutate),
            # so any arguments are admitted with no sub-form check.
            return 0
            ;;
        test|'['|'[[')
            # Boolean file/string test builtins — no mutation surface at all.
            return 0
            ;;
        find)
            # find is read-only UNLESS a dangerous action-primary is present.
            # Structurally exclude the write/delete/exec primaries: if ANY token
            # exactly matches one, decline eligibility (fall through to the full
            # path). Pure bash-builtin string compares, zero forks.
            local i
            for (( i = 1; i < n; i++ )); do
                case "${t[i]}" in
                    -delete|-exec|-execdir|-ok|-okdir|-fls|-fprint|-fprint0|-fprintf)
                        return 1
                        ;;
                esac
            done
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
# REPO_* wins over the legacy LOOM_* name (fastpath_enabled applies the same
# precedence for the enable direction).
_fastpath_env="${REPO_GUARD_READONLY_FASTPATH:-${LOOM_GUARD_READONLY_FASTPATH:-}}"
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
# Shared config reader for the guards.* toggles below.
#
# guard_cfg <key> — echo the raw value of .guards.<key> (via jq tostring, so
# booleans arrive as "true"/"false" and strings as their bare text), or "unset"
# when the key is absent, the file is missing/malformed, or there is no repo
# root. Repo Skills' own config (.claude/skills/repo/config.json) WINS over the
# legacy Loom location (.loom/config.json). Best-effort: any jq failure reads
# as unset and never trips the ERR trap; each caller applies its own default
# and polarity, so a malformed config always falls through to that caller's
# safe default.
# =============================================================================
guard_cfg() {
    local key="$1" cfg val
    for cfg in "$REPO_ROOT/.claude/skills/repo/config.json" "$REPO_ROOT/.loom/config.json"; do
        [[ -n "$REPO_ROOT" && -f "$cfg" ]] || continue
        val=$(jq -r --arg k "$key" '((.guards? // {}) | if has($k) then (.[$k] | tostring) else "unset" end)' "$cfg" 2>/dev/null) || val="unset"
        if [[ -n "$val" && "$val" != "unset" ]]; then
            printf '%s' "$val"
            return 0
        fi
    done
    printf 'unset'
}

# =============================================================================
# Array-valued sibling of guard_cfg() above, for a `guards.<key>` that holds a
# JSON array of strings rather than a scalar. Same dual-location resolution
# and per-file "key ABSENT falls through, key PRESENT (even as []) wins"
# contract as guard_cfg() — the first config file that actually DEFINES the
# key wins outright, matching guard_cfg()'s "unset" sentinel semantics rather
# than a naive "first non-empty array" scan. Each array element is echoed on
# its own line. Best-effort: any jq failure (malformed JSON, non-array value)
# yields no output and never trips the ERR trap.
# =============================================================================
guard_cfg_array() {
    local key="$1" cfg has
    for cfg in "$REPO_ROOT/.claude/skills/repo/config.json" "$REPO_ROOT/.loom/config.json"; do
        [[ -n "$REPO_ROOT" && -f "$cfg" ]] || continue
        has=$(jq -r --arg k "$key" '((.guards? // {}) | has($k))' "$cfg" 2>/dev/null) || has="false"
        if [[ "$has" == "true" ]]; then
            jq -r --arg k "$key" '(.guards[$k] // []) | .[]' "$cfg" 2>/dev/null
            return 0
        fi
    done
    return 0
}

# =============================================================================
# ASK-tier positional-argument masking allowlist (guards.positionalMaskAllowlist,
# #195) — resolved lazily (only invoked once mask_ask_positional_args() below
# is about to run) and cached, mirroring sql_guard_enabled()'s lazy-config-read
# discipline so the jq/array read never touches the hot path for the majority
# of commands that don't reach the ASK-tier working-copy build. Builds an ERE
# alternation of the configured command names, each ERE-metacharacter-escaped
# so a config-supplied name containing a literal `.` (e.g.
# "./.loom/scripts/check-duplicate.sh") can't accidentally widen the anchor
# regex built from it.
#
# MANDATORY EXCLUSION SET (_POSITIONAL_MASK_NEVER) — these command names are
# UNCONDITIONALLY dropped here, even if a repo configures them, because
# COMMAND_ASK_SCAN feeds two DENY-tier consumers as well as the ask-tier ones
# (audit table below). Masking a command that either deny-tier scan treats as
# its SUBJECT would silently downgrade a hard deny to an allow, which no
# operator config may ever do. See mask_ask_positional_args()'s header comment
# (below, near strip_datasink_literals()) for the per-consumer reasoning.
#
# COMMAND_ASK_SCAN consumer audit (#195 review) — every reader of this scan,
# and whether narrowing it is safe:
#
#   Consumer (search this file)          Tier   Narrowing safe?
#   -----------------------------------  -----  --------------------------------
#   ASK_PATTERNS                         ask    yes — the intended target (#195)
#   parse_force_ops (force-op:*)         ask    yes
#   stash-scope                          ask    yes
#   reversible-gh / git-read-tree        ask    yes
#   cloud-cli                            ask    yes
#   SQL_DDL_PATTERN (sql-ddl)            DENY   NO — scans raw quoted text for a
#                                               literal DDL phrase; masking a
#                                               grep/rg pattern argument blinds
#                                               it. => grep|egrep|fgrep|rg
#   extract_write_targets (#4178         DENY   NO — extracts a write idiom's own
#   worktree-write-confinement)                 target PATH from this scan;
#                                               masking that argument blinds the
#                                               confinement deny. => cp|mv|tee|sed
#
# The deny-tier rows are why this set is hardcoded rather than advisory: with
# `positionalMaskAllowlist: ["cp"]` configured and no exclusion,
# `cp "/tmp/src.txt" "<main-checkout>/evil.sh"` issued from a builder worktree
# went from `deny` (worktree-write-confinement) to ALLOW, because
# extract_write_targets() could no longer see the masked destination path.
# cp/mv/tee/sed are exactly the command words extract_write_targets() recognizes
# as write idioms (its `toks[1] == "tee" / "sed" / "cp" / "mv"` scans; `>`/`>>`
# redirection has no command word and is never maskable by construction), so
# excluding them restores the pre-#195 deny in every configuration.
#
# Comparison is on the BASENAME of the configured entry, so a path-qualified
# spelling (`/bin/cp`, `./tee`) cannot smuggle an excluded name past the set.
#
# GENERAL OPERATOR RULE (the invariant this set enforces mechanically for the
# two known deny-tier subjects): only allowlist a command whose positional
# arguments are INERT TEXT it merely reads. A command that ACTS on its
# positional arguments — writes them as paths (cp/mv/tee/sed) or executes them
# as statements (`psql "DROP TABLE …"`, `sh -c "…"`) — must never be
# allowlisted; masking its arguments hides exactly the text a deny-tier scan
# exists to read. Extend _POSITIONAL_MASK_NEVER whenever a new deny-tier
# consumer of COMMAND_ASK_SCAN is added with a recognizable command word.
# =============================================================================
_POSITIONAL_MASK_NEVER='grep egrep fgrep rg cp mv tee sed'
_POSITIONAL_MASK_CMDRE_CACHE=""
_POSITIONAL_MASK_CMDRE_DONE=""
positional_mask_cmdre() {
    if [[ -z "$_POSITIONAL_MASK_CMDRE_DONE" ]]; then
        _POSITIONAL_MASK_CMDRE_DONE=1
        local raw cmd base never
        local -a escaped=()
        raw=$(guard_cfg_array positionalMaskAllowlist)
        if [[ -n "$raw" ]]; then
            while IFS= read -r cmd; do
                [[ -z "$cmd" ]] && continue
                # Basename comparison against the mandatory exclusion set
                # above: `/bin/cp` and `./tee` are dropped exactly like the
                # bare spellings.
                base="${cmd##*/}"
                local excluded=""
                for never in $_POSITIONAL_MASK_NEVER; do
                    [[ "$base" == "$never" ]] && { excluded=1; break; }
                done
                [[ -n "$excluded" ]] && continue
                escaped+=("$(printf '%s' "$cmd" | sed -E 's/[][(){}.*+?^$|\\]/\\&/g')")
            done <<< "$raw"
        fi
        if [[ ${#escaped[@]} -gt 0 ]]; then
            local joined
            joined=$(IFS='|'; printf '%s' "${escaped[*]}")
            _POSITIONAL_MASK_CMDRE_CACHE="$joined"
        fi
    fi
    printf '%s' "$_POSITIONAL_MASK_CMDRE_CACHE"
}

# =============================================================================
# SQL DDL/DML guard toggle — default ON.
#
# The SQL DDL/DML blocks (DROP DATABASE/TABLE/SCHEMA, TRUNCATE TABLE, and
# DELETE FROM without WHERE) are a category error for repos that are themselves
# database engines, where those statements are the product's own dev/test
# vocabulary. Such repos opt out; everyone else keeps the guard on.
#
# Resolution order (highest precedence first):
#   1. REPO_GUARD_SQL env var, then legacy LOOM_GUARD_SQL
#      (0/false/no disables, 1/true/yes forces on)
#   2. guards.sqlDdl via guard_cfg() — repo config wins over legacy .loom
#      (default true when absent)
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
        # Only an explicit `false` disables (a missing guards.sqlDdl key or a
        # malformed config reads as unset and stays on).
        case "$(guard_cfg sqlDdl)" in
            false) enabled=false ;;
            true)  enabled=true ;;
        esac
        # Env override wins over config; REPO_* wins over the legacy LOOM_* name.
        case "${LOOM_GUARD_SQL:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        case "${REPO_GUARD_SQL:-}" in
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
#   1. REPO_GUARD_CLOUD env var, then legacy LOOM_GUARD_CLOUD
#      (0/false/no disables, 1/true/yes forces on)
#   2. guards.cloudCli via guard_cfg() — repo config wins over legacy .loom
#      (default true when absent)
#   3. Default: true (guard on)
#
# Mirrors sql_guard_enabled() exactly: cached in _STASH_SCOPE_CACHE, invoked
# LAZILY only after the stash pattern has already matched.
# =============================================================================
_STASH_SCOPE_CACHE=""
stash_scope_guard_enabled() {
    if [[ -z "$_STASH_SCOPE_CACHE" ]]; then
        local enabled=true
        case "$(guard_cfg stashScope)" in
            false) enabled=false ;;
            true)  enabled=true ;;
        esac
        # Env override wins over config; REPO_* wins over the legacy LOOM_* name.
        case "${LOOM_GUARD_STASH_SCOPE:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        case "${REPO_GUARD_STASH_SCOPE:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _STASH_SCOPE_CACHE="$enabled"
    fi
    [[ "$_STASH_SCOPE_CACHE" == "true" ]]
}

# =============================================================================
# Mirrors sql_guard_enabled() exactly: cached in _CLOUD_GUARD_CACHE, invoked
# LAZILY only after a cloud pattern has already matched so the jq config read
# never touches the hot path for non-cloud commands. The config read is
# best-effort: any parse failure falls through to guard-ON.
# =============================================================================
_CLOUD_GUARD_CACHE=""
cloud_guard_enabled() {
    if [[ -z "$_CLOUD_GUARD_CACHE" ]]; then
        local enabled=true
        # Only an explicit `false` disables (a missing key or malformed config
        # reads as unset and stays on).
        case "$(guard_cfg cloudCli)" in
            false) enabled=false ;;
            true)  enabled=true ;;
        esac
        # Env override wins over config; REPO_* wins over the legacy LOOM_* name.
        case "${LOOM_GUARD_CLOUD:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        case "${REPO_GUARD_CLOUD:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _CLOUD_GUARD_CACHE="$enabled"
    fi
    [[ "$_CLOUD_GUARD_CACHE" == "true" ]]
}

# =============================================================================
# Reversible-GitHub ask toggle — default OFF (opt-IN; inverse polarity, #3757).
#
# `gh pr close`, `gh issue close`, and `gh label delete` change shared state but
# are trivially reversible — `gh pr reopen`, `gh issue reopen`, and recreating a
# label (a repo with labels.yml restores in one `gh label sync`). A guard whose
# purpose is preventing irreversible loss should not add confirmation friction to
# these: an autonomous agent that closes its own issue/PR as part of a normal
# lifecycle would otherwise stall on a prompt (or, headless, block entirely). So
# they are NO LONGER in the ungated ASK_PATTERNS array; a repo that still wants
# the confirmation can opt IN here. The genuinely hard-to-reverse ops
# (`gh release delete` — published artifacts/tags; `git clean -fd` / `git
# checkout .` / `git restore .` — untracked/uncommitted loss) STAY in the ungated
# ask tier and are unaffected by this toggle.
#
# This is the INVERSE polarity of sql_guard_enabled()/cloud_guard_enabled():
# those default ON (guard active) and are opted OUT; this one defaults OFF (no
# ask) and is opted IN — because enabling it ADDS friction rather than removing
# it. So the default and the absent-key resolution are `false`, not `true`.
#
# Resolution order (highest precedence first):
#   1. REPO_GUARD_REVERSIBLE_GH env var, then legacy LOOM_GUARD_REVERSIBLE_GH
#      (1/true/yes enables the ask, 0/false/no forces it off)
#   2. guards.reversibleGh via guard_cfg() — repo config wins over legacy .loom
#      (default false when absent)
#   3. Default: false (no ask)
#
# Mirrors cloud_guard_enabled()'s lazy/cached shape: cached in
# _REVERSIBLE_GH_GUARD_CACHE, invoked LAZILY only after a reversible-gh pattern
# has already matched so the jq config read never touches the hot path for the
# common (non-matching) case. The config read is best-effort: any parse failure
# falls through to guard-OFF (the default), never blocking.
# =============================================================================
_REVERSIBLE_GH_GUARD_CACHE=""
reversible_gh_guard_enabled() {
    if [[ -z "$_REVERSIBLE_GH_GUARD_CACHE" ]]; then
        local enabled=false
        # Only an explicit `true` enables (a missing guards.reversibleGh key or
        # a malformed config reads as unset and stays off — inverse polarity).
        case "$(guard_cfg reversibleGh)" in
            true)  enabled=true ;;
            false) enabled=false ;;
        esac
        # Env override wins over config; REPO_* wins over the legacy LOOM_* name.
        case "${LOOM_GUARD_REVERSIBLE_GH:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        case "${REPO_GUARD_REVERSIBLE_GH:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _REVERSIBLE_GH_GUARD_CACHE="$enabled"
    fi
    [[ "$_REVERSIBLE_GH_GUARD_CACHE" == "true" ]]
}

# =============================================================================
# Decision-telemetry toggle — default OFF (opt-IN; inverse polarity, #3771).
#
# The deny/ask decision log (log_guard_decision() near the top of this file) is
# OFF by default: it writes a new persistent, cross-session artifact of redacted
# commands, so — mirroring the other opt-in data-collection features in Loom
# (transcript archival #3726, the model-cost experiment #3725) — a zero-config
# install sees NO new file and NO behaviour change. An operator enables it to
# measure guard-hook friction.
#
# Same INVERSE polarity as reversible_gh_guard_enabled(): defaults false, the
# absent-key resolution is false, and only an explicit `true` (config) or a
# truthy env value enables it.
#
# Resolution order (highest precedence first):
#   1. REPO_GUARD_DECISION_LOG env var, then legacy LOOM_GUARD_DECISION_LOG
#      (1/true/yes/on enables; 0/false/no/off disables). Overrides config.
#   2. guards.decisionLog via guard_cfg() — repo config wins over legacy .loom
#      (default false when absent).
#   3. Default: false (no decision log written).
#
# Resolved LAZILY and cached in _DECISION_LOG_CACHE, invoked only from inside
# log_guard_decision() (i.e. only once a deny/ask is about to fire), exactly like
# the other toggles — so the config read NEVER touches the hot path for the ~99%
# of commands that neither deny nor ask, and in particular never runs on the
# #3687 read-only fast path (which exits before any deny/ask). The config read is
# best-effort: any parse failure falls through to guard-OFF (the default).
# =============================================================================
_DECISION_LOG_CACHE=""
decision_log_enabled() {
    if [[ -z "$_DECISION_LOG_CACHE" ]]; then
        local enabled=false
        # Only an explicit `true` enables; a missing key or malformed config
        # reads as unset and stays OFF — inverse of sql_guard_enabled().
        case "$(guard_cfg decisionLog)" in
            true)  enabled=true ;;
            false) enabled=false ;;
        esac
        # Env override wins over config; REPO_* wins over the legacy LOOM_* name.
        case "${LOOM_GUARD_DECISION_LOG:-}" in
            0|false|no|off)   enabled=false ;;
            1|true|yes|on)    enabled=true ;;
        esac
        case "${REPO_GUARD_DECISION_LOG:-}" in
            0|false|no|off)   enabled=false ;;
            1|true|yes|on)    enabled=true ;;
        esac
        _DECISION_LOG_CACHE="$enabled"
    fi
    [[ "$_DECISION_LOG_CACHE" == "true" ]]
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
#   1. REPO_RM_SCOPE env var, then legacy LOOM_RM_SCOPE (repo enables;
#      off/0/no/permissive disables). Overrides config. Absent → falls through
#      to config/default.
#   2. guards.rmScope via guard_cfg() — repo config wins over legacy .loom:
#      "off"/"permissive" => off; absent key / any other value / malformed
#      JSON => repo (the default).
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
        # Only an explicit guards.rmScope of "off" or "permissive" opts out to
        # the legacy permissive behaviour; any other value, a missing key, or a
        # malformed config (reads as unset) resolves to "repo" (the safe
        # default).
        case "$(guard_cfg rmScope)" in
            off|permissive) mode=off ;;
        esac
        # Env override wins over config; REPO_* wins over the legacy LOOM_* name.
        case "${LOOM_RM_SCOPE:-}" in
            repo)                  mode=repo ;;
            off|0|no|permissive)   mode=off ;;
        esac
        case "${REPO_RM_SCOPE:-}" in
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
    # 1. Env override (highest priority); must be absolute. LOOM_WORKTREE_ROOT
    #    is kept as the only env name — it is a Loom concept and part of the
    #    Loom-compat contract.
    if [[ -n "${LOOM_WORKTREE_ROOT:-}" && "$LOOM_WORKTREE_ROOT" == /* ]]; then
        printf '%s/%s' "${LOOM_WORKTREE_ROOT%/}" "$(basename "$repo_root")"
        return 0
    fi
    # 2. Config key worktree.root (absolute only), read from both config
    #    locations — repo config wins over legacy .loom.
    local config_file
    for config_file in "$repo_root/.claude/skills/repo/config.json" "$repo_root/.loom/config.json"; do
        [[ -f "$config_file" ]] || continue
        local cfg_root
        cfg_root=$(jq -r '.worktree.root? // empty' "$config_file" 2>/dev/null) || cfg_root=""
        if [[ -n "$cfg_root" && "$cfg_root" == /* ]]; then
            printf '%s/%s' "${cfg_root%/}" "$(basename "$repo_root")"
            return 0
        fi
    done
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
#   1. REPO_FORCE_SCOPE env var, then legacy LOOM_FORCE_SCOPE
#      (all/protected/off). Overrides config.
#   2. guards.forceScope via guard_cfg() — repo config wins over legacy .loom:
#      "protected"/"off"; absent key / any other value / malformed JSON =>
#      "all" (the current-behaviour default).
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
        # Only "protected"/"off" opt away from the default; a missing key, any
        # other value, or a malformed config (reads as unset) resolves to "all".
        case "$(guard_cfg forceScope)" in
            protected) mode=protected ;;
            off)       mode=off ;;
        esac
        # Env override wins over config; REPO_* wins over the legacy LOOM_* name.
        case "${LOOM_FORCE_SCOPE:-}" in
            all)         mode=all ;;
            protected)   mode=protected ;;
            off)         mode=off ;;
        esac
        case "${REPO_FORCE_SCOPE:-}" in
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
    #    REPO_* wins over the legacy LOOM_* name.
    if [[ -n "${REPO_DEFAULT_BRANCH:-}" ]]; then
        printf '%s' "$REPO_DEFAULT_BRANCH"
        return 0
    fi
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

# =============================================================================
# BACKSLASH-ESCAPE HELPERS (shared by BOTH lexers)
#
# The character at position i is BACKSLASH-ESCAPED when it is preceded by an ODD
# number of backslashes (`\x` is an escape; `\\x` is a literal backslash followed
# by an unescaped `x`). Call sites depend on this parity:
#   - a NEWLINE: an escaped newline is a LINE CONTINUATION — the shell removes it
#     and the logical line continues, so pending heredoc BODIES must not start
#     there;
#   - the leading `<` of a `<<`: an escaped `\<` is a literal `<` inside a word,
#     NOT a redirection operator, so `\<<WORD` never opens a heredoc and must not
#     be probed as one (#108);
#   - a QUOTE character: `\"` is literal text, so it never OPENS a quoted span
#     and is never accepted as the authoritative CLOSE of an active one (#113).
#
# `trusted_close()` resolves that last case for the ACTIVE-span bookkeeping the
# two lexers share. Starting from the naive "next quote of the same kind" index
# it:
#   - SKIPS backslash-escaped quotes (`\"` is literal text, never a real close);
#   - returns 0 — meaning "do not record a close, walk the span the legacy way" —
#     when no unescaped candidate exists, or when the candidate directly follows
#     a backslash run (`\\"`). There the escaped BACKSLASH makes the pairing
#     genuinely ambiguous (the quote is real, but the shell re-parses quoting
#     inside `$( )`), and recording it would end the span at a position the
#     legacy walk pairs differently — the one direction that can lose a segment
#     boundary. Returning 0 reproduces the pre-#113 walk for that span exactly.
# Every use of THESE HELPERS is narrowing: treating something as
# escaped/ambiguous only falls back to (or stays in) separator-ACTIVE
# segmentation. That is a property of the helpers, not an unconditional property
# of the lexers — see the KNOWN LIMIT note on the inert-span branch (#130) for
# the one shape where correcting the active-span pairing lets a STRAY unmatched
# quote pair differently than it did before #113.
#
# Lives in its own awk source string, prepended to BOTH lexer sources, so
# qsplit() and ml_segment() share ONE definition and cannot drift (#113).
# =============================================================================
_ESCAPE_AWK='
function bs_escaped(s, i,   bs, p) {
    bs = 0
    for (p = i - 1; p >= 1 && substr(s, p, 1) == "\\"; p--) bs++
    return (bs % 2)
}
function trusted_close(s, n, ci, qc,   j) {
    while (ci > 0 && bs_escaped(s, ci)) {
        j = ci + 1
        ci = 0
        for (; j <= n; j++) if (substr(s, j, 1) == qc) { ci = j; break }
    }
    if (ci > 1 && substr(s, ci - 1, 1) == "\\") return 0
    return ci
}
'

# =============================================================================
# QUOTE-AWARE COMMAND SEGMENTATION (#3755)
#
# The three segment parsers below (parse_force_ops, lifecycle_or_cloud_reason,
# extract_rm_targets) split a command string on the shell separators ; | & && ||
# to find each simple command's command word. The historical split was a naive
#   gsub(/&&|\|\||[;|&]/, "\n")
# over the raw string, which has NO lexer — so a `|`-alternation INSIDE a quoted
# argument (e.g. `grep -E "lifecycle|halt|poweroff"`) was split as if it were a
# real pipe, manufacturing a phantom segment whose command word is the bare word
# `halt` and hard-denying a completely read-only command.
#
# `qsplit()` replaces that gsub: it walks the string tracking single-/double-quote
# state and emits a newline for a separator ONLY when it is OUTSIDE a quoted span.
# A quoted span is treated as inert (its separators are preserved as literal
# text) ONLY when it carries no command substitution — no `$(` and no backtick —
# mirroring strip_literal_text()'s #3679 safety floor: a smuggled
# `"$(a|halt)"` keeps its separators ACTIVE so the genuine protection is intact.
# Such an ACTIVE span still records where it really ENDS, so the character walk
# does not mistake its own closing quote for the opener of a new span (#113) —
# without that, a later unrelated quote paired with the re-opened one and every
# real separator between them (a genuine `; <destructive cmd>` after the span)
# was swallowed as bogus inert text.
# The token VALUES are preserved verbatim (unlike a redaction approach), so
# extract_rm_targets still sees the real `rm` targets. Best-effort like
# strip_literal_text(): where a BACKSLASH makes the quote pairing ambiguous both
# lexers resolve it in the one direction that keeps separators ACTIVE, so the
# result can only gain segment boundaries, never lose them (#113):
#   - a backslash-escaped quote (`\"`) never OPENS a span — it is literal text —
#     so it can no longer start a bogus inert run that swallows real separators;
#   - the close index an ACTIVE span records is resolved by trusted_close():
#     escaped candidates are skipped, and an ambiguous one (`\\"`) records
#     nothing at all so that span is walked exactly the legacy way;
#   - an unterminated quote advances ONE character with separators still active
#     (both lexers) instead of swallowing the remainder of the buffer.
# For every command the shell can actually PARSE, none of these widens a deny
# into an allow; the escaped-quote cases in the #113 test block pin the shapes
# that did. For input with an UNBALANCED quote count the guarantee is weaker —
# see the KNOWN LIMIT note on the inert-span branch (#130).
#
# Shared as a single awk source string so the three parsers cannot drift.
# =============================================================================
_QSPLIT_AWK='
function qsplit(s,   out, n, i, c, j, qc, ci, tc, inner, SQ, DQ, acs, acn) {
    SQ = sprintf("%c", 39)   # single quote
    DQ = sprintf("%c", 34)   # double quote
    out = ""
    n = length(s)
    split("", acs)           # stack of pending active-span CLOSING quote indexes
    acn = 0
    i = 1
    while (i <= n) {
        c = substr(s, i, 1)
        # A quote character at a position ALREADY KNOWN to be the closing quote of
        # an open active (command-substitution-bearing) span TERMINATES that span —
        # it is not the opener of a new one (#113). See the ml_segment() copy of
        # this guard for the full rationale; both lexers share the defect and
        # therefore share the fix so they cannot drift.
        while (acn > 0 && acs[acn] < i) acn--   # spans a jump already skipped past
        if (acn > 0 && i == acs[acn]) {
            out = out c
            acn--
            i++
            continue
        }
        # A BACKSLASH-ESCAPED quote (`\"`) is literal text, not a span opener
        # (#113). Opening a span there let an escaped quote AFTER an active span
        # pair with a much later quote and copy every real separator between
        # them as bogus inert text. Refusing to open is strictly NARROWING (the
        # separators stay ACTIVE), so it can only add segment boundaries.
        if ((c == DQ || c == SQ) && !bs_escaped(s, i)) {
            qc = c
            ci = 0
            for (j = i + 1; j <= n; j++) {
                if (substr(s, j, 1) == qc) { ci = j; break }
            }
            if (ci == 0) {
                # Unterminated quote: fall back to separator-active processing so
                # a stray quote never suppresses a real split (never widen a deny).
                out = out c
                i++
                continue
            }
            inner = substr(s, i + 1, ci - i - 1)
            if (index(inner, "$(") == 0 && index(inner, "`") == 0) {
                # Inert quoted span: copy verbatim, separators inside are literal.
                # KNOWN LIMIT (#130): this is the one branch that can LOSE segment
                # boundaries, because it consumes whatever the forward scan paired
                # with. When the opener is a STRAY unmatched quote, that partner
                # can be an unrelated later quote and the real separators between
                # them are swallowed. Two routes reach it: an opener sitting AFTER
                # an active span has closed, and an opener INSIDE an active span
                # whose partner lies past that span-s close. Both are confined to
                # odd-quote-count input the shell will not parse — see the KNOWN
                # LIMIT block in ml_segment().
                out = out substr(s, i, ci - i + 1)
                i = ci + 1
                continue
            }
            # Span carries command substitution: keep separators ACTIVE (copy the
            # opening quote and keep walking char-by-char so a `|` inside splits).
            # REMEMBER where the span really ENDS (#113) so the char-walk does not
            # re-read that quote as a NEW opener — which used to swallow
            # everything after the span. `trusted_close()` resolves the real close
            # (skipping backslash-escaped quotes, refusing an ambiguous one); see
            # the ml_segment() copy of this guard for the full rationale — both
            # lexers share the defect and therefore share the fix so they cannot
            # drift.
            out = out c
            tc = trusted_close(s, n, ci, qc)
            if (tc > 0) acs[++acn] = tc
            i++
            continue
        }
        if (c == ";") { out = out "\n"; i++; continue }
        if (c == "&") {
            if (i < n && substr(s, i + 1, 1) == "&") { out = out "\n"; i += 2; continue }
            out = out "\n"; i++; continue
        }
        if (c == "|") {
            if (i < n && substr(s, i + 1, 1) == "|") { out = out "\n"; i += 2; continue }
            out = out "\n"; i++; continue
        }
        out = out c
        i++
    }
    return out
}
'

# =============================================================================
# MULTI-LINE QUOTE-AWARE SEGMENTATION (#71)
#
# The three segment parsers (parse_force_ops, lifecycle_or_cloud_reason,
# extract_rm_targets) originally segmented per awk INPUT RECORD with
# `$0 = qsplit($0); split($0, segs, "\n")`. Because awk's default RS="\n" splits
# a multi-line command into separate records BEFORE the pattern block runs,
# qsplit()'s quote-tracking state — scoped to a single call — reset at every
# embedded newline. An interior line of an otherwise-inert multi-line quoted
# DATA literal (echo/printf/--body) was therefore lexed as its own top-level
# segment with no memory that it is still inside an open quote from a prior line,
# so a quoted `git push --force origin main` false-`ask`ed (parse_force_ops) and
# a quoted `halt` false-`deny`ed (lifecycle_or_cloud_reason). PR #69 fixed the
# identical defect in extract_rm_targets() with a whole-buffer slurp-then-segment
# lexer; this shared helper generalizes that lexer so all three parsers segment
# ONCE over the full command and cannot drift (the same rationale the _QSPLIT_AWK
# header gives for sharing qsplit()).
#
# `ml_segment(buf, segs)` fills the caller's `segs[]` out-array (AWK passes arrays
# by reference) with one entry per top-level segment and returns the count. It
# deliberately does NOT reuse qsplit()+split("\n"): qsplit() emits a `\n` for each
# REAL separator while ALSO leaving a literal newline that lived inside an inert
# quoted span untouched, so the two become indistinguishable to a downstream
# `split(s, segs, "\n")`. Segmentation is therefore done inline here, so an inert
# quoted span's embedded newlines never become segment boundaries.
#
# Segmentation contract (identical to qsplit()'s single-line behaviour, now
# correctly carried ACROSS embedded newlines):
#   - Separators `;` `&` (`&&`) `|` (`||`) OUTSIDE any quote split segments.
#   - A raw newline OUTSIDE any quote ALSO splits — so a GENUINE multi-line
#     command still yields a real later-line segment and still denies (safety
#     floor preserved; matches the old per-record behaviour where each input line
#     was its own record).
#   - An INERT quoted span (no `$(` and no backtick) is copied VERBATIM, so its
#     embedded newlines/separators stay literal and never manufacture a phantom
#     segment out of quoted documentation prose (the false positive).
#   - A quoted span carrying command substitution (`$(` or a backtick) keeps its
#     separators ACTIVE (walked char-by-char, exactly like qsplit()), so a
#     smuggled payload is never hidden behind an opening quote. Its
#     already-computed CLOSING quote index is remembered (#113) so the walk that
#     reaches it recognises the span TERMINATOR instead of re-opening a phantom
#     span there — the mis-read that used to swallow the whole rest of the
#     command (and with it any `; <destructive cmd>` following the span).
#   - An unterminated quote copies the remainder verbatim (best-effort; never
#     widens a deny into an allow).
#
# HEREDOC AWARENESS (#84)
#
# The lexer above tracked quote state only — it had no concept of heredoc syntax
# (`<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`). A composite like
#   gh issue create --body "$(cat <<'EOF'
#   shutdown Iq is specified at ...
#   EOF
#   )"
# therefore fell through to the default "a raw newline splits" rule (the outer
# double-quoted span carries `$(`, so by the #3679 safety floor its separators
# stay ACTIVE), and every heredoc BODY line became its own phantom top-level
# segment. lifecycle_or_cloud_reason() then read `toks[1]` of that phantom
# segment and hard-denied on the word `shutdown`; the same phantom segments
# reach parse_force_ops() and extract_rm_targets(). Heredoc body lines are DATA,
# never command boundaries, so:
#   - A heredoc opener seen OUTSIDE any inert quoted span records its terminator
#     word (quotes/backslash stripped) and the `<<-` leading-TAB-strip flag. The
#     REST of the opener line is segmented normally, so `cat <<EOF | grep x`
#     still splits at the pipe.
#   - The newline that ends the opener line closes that segment (it IS a real
#     command boundary), and the body lines through the terminator are then
#     SKIPPED entirely — they are data, so they contribute no segment and no
#     tokens. Normal segmentation resumes on the line after the terminator, so a
#     real command following the terminator still gets its own segment and still
#     denies.
#   - Multiple heredocs on one line (`cmd <<A <<B`) consume body A in full before
#     body B, matching real shell semantics.
#   - An UNTERMINATED heredoc skips the remainder of the buffer — which is
#     exactly what a real shell does with it (the rest of the input IS the body
#     and never executes), and mirrors the unterminated-quote fallback above.
#     Anything BEFORE the opener is still segmented normally.
#   - SAFETY CARVE-OUT: a body attached to a BARE (unquoted) delimiter is still
#     parameter/command-expanded by the shell, so if such a body carries `$(` or
#     a backtick the whole pending-heredoc region reverts to the legacy
#     separator-active treatment — the same #3679 floor the quoted-span branch
#     applies. A quoted/escaped delimiter (`<<'EOF'`, `<<"EOF"`, `<<\EOF`)
#     suppresses expansion, so its body is unconditionally inert.
#   - `<<<` is a here-STRING, not a heredoc, and is deliberately not matched.
#   - A `<<` inside a shell COMMENT (`echo hi # <<EOF`) is not an operator at
#     all, so the opener probe is SUPPRESSED from a word-initial unquoted `#`
#     through the end of that physical line. Without this, the phantom opener's
#     terminator never appears and the unterminated-heredoc rule would skip the
#     rest of the buffer — hiding a real command on the NEXT line from
#     extract_rm_targets(), which parses raw $COMMAND rather than
#     $COMMAND_NO_COMMENT. Only the probe is suppressed (the characters still
#     flow through normal separator handling), because skipping to the newline
#     would in turn hide a trailing `; <cmd>` after a `#` that sits inside a
#     command-substitution-bearing quoted span.
#   - A newline preceded by an ODD number of backslashes is a LINE CONTINUATION,
#     not the end of the logical line, so the pending bodies do NOT start there
#     (`cat <<EOF \` + newline + `&& <cmd>` really does run `<cmd>`). Such a
#     newline falls through to the legacy separator handling, so the continued
#     line is segmented as the real commands it is; the bodies then start at the
#     first NON-continued newline, matching the shell.
#   - A `<<` whose leading `<` is itself preceded by an ODD number of backslashes
#     (`\<<WORD`) is NOT an operator — `\<` is a literal `<` inside a word — so it
#     is not probed at all (#108). Without this the phantom opener's terminator
#     never appeared and the unterminated-heredoc rule skipped the rest of the
#     buffer, hiding every later real command. The ESCAPED-DELIMITER form
#     `<<\WORD` is a different (and legitimate) thing and is unaffected.
# Safety floor unchanged: the raw ALWAYS_BLOCK_PATTERNS catastrophic scan reads
# the command string directly (never through ml_segment), so a `$(...)`-smuggled
# payload inside a heredoc body still denies, and a GENUINE multi-line command
# whose later real line is dangerous still yields a real segment and still denies.
# =============================================================================
_ML_QSPLIT_AWK='
# Probe for a heredoc redirection operator at position i (the caller guarantees
# substr(s, i, 2) is "<<"). On success fills out["delim"] (terminator word, with
# any surrounding quotes/backslash stripped), out["strip"] (1 for the `<<-` form,
# which strips leading TABs from the terminator line) and out["quoted"] (1 when
# the delimiter was quoted or backslash-escaped, i.e. the body is NOT expanded)
# and returns the index of the first character AFTER the operator. Returns 0 when
# this is NOT a heredoc opener: a `<<<` here-string, a `<<` with no delimiter
# word, or a delimiter whose opening quote is never closed.
function hd_opener(s, n, i, out,   j, c, q, w, SQ, DQ) {
    SQ = sprintf("%c", 39)   # single quote
    DQ = sprintf("%c", 34)   # double quote
    j = i + 2
    if (substr(s, j, 1) == "<") return 0        # `<<<` here-string, not a heredoc
    out["strip"] = 0
    out["quoted"] = 0
    if (substr(s, j, 1) == "-") { out["strip"] = 1; j++ }
    while (j <= n && (substr(s, j, 1) == " " || substr(s, j, 1) == "\t")) j++
    w = ""
    c = substr(s, j, 1)
    if (c == SQ || c == DQ) {
        q = c
        out["quoted"] = 1                       # no expansion inside the body
        j++
        while (j <= n && substr(s, j, 1) != q) { w = w substr(s, j, 1); j++ }
        if (j > n) return 0                     # unterminated delimiter quote
        j++                                     # consume the closing quote
    } else {
        if (c == "\\") { out["quoted"] = 1; j++ }   # `<<\EOF` (escaped, unexpanded)
        while (j <= n) {
            c = substr(s, j, 1)
            if (c ~ /[A-Za-z0-9_.:@%+=\/-]/) { w = w c; j++ } else break
        }
    }
    if (w == "") return 0
    # Reject shapes that are far more likely to be an arithmetic left-shift than
    # a heredoc, so `$((1 << 3))` / `(( x << 2 ))` are not misread as openers that
    # would swallow the rest of the buffer:
    #   - an all-digit BARE delimiter (`<< 3`) — real delimiters are words;
    #   - a delimiter not followed by a redirection/separator/whitespace boundary
    #     (`<< 3))` — a genuine opener is always followed by more redirections,
    #     a separator, or end of line.
    if (!out["quoted"] && w ~ /^[0-9]+$/) return 0
    c = substr(s, j, 1)
    if (j <= n && c != " " && c != "\t" && c != "\n" && c != ";" && c != "&" &&
        c != "|" && c != "<" && c != ">") return 0
    out["delim"] = w
    return j
}
# bs_escaped() (the backslash-parity helper every escape-sensitive branch below
# calls) lives in the shared _ESCAPE_AWK source string, which is prepended to
# BOTH this lexer and qsplit() so the two cannot drift (#113).
function ml_segment(buf, segs,   SQ, DQ, s, n, seg, segc, i, c, qc, ci, tc, j, inner,
                    hdc, hddelim, hdstrip, hdquoted, hdo, hdnext, h, k, unsafe,
                    arO, arC, eol, nexti, line, t, incmt, pc, acs, acn) {
    SQ = sprintf("%c", 39)   # single quote
    DQ = sprintf("%c", 34)   # double quote
    split("", segs)          # clear the caller-supplied out-array
    split("", acs)           # stack of pending active-span CLOSING quote indexes
    acn = 0
    s = buf
    n = length(s)
    seg = ""
    segc = 0
    hdc = 0                  # heredoc openers pending a body on the next line
    incmt = 0                # a shell COMMENT is open on the current line
    i = 1
    while (i <= n) {
        c = substr(s, i, 1)
        # ACTIVE-SPAN CLOSE (#113). When the span below carries a command
        # substitution its separators stay ACTIVE, so the walk continues
        # character-by-character INTO the span — and eventually reaches the REAL
        # closing quote of that same span. Without this guard the top-of-loop quote
        # detector below re-read that closing quote as the OPENER of a brand-new
        # span and re-scanned forward for a partner: with no later quote the
        # unterminated-quote fallback swallowed the ENTIRE remainder of the buffer
        # into the current segment, and with a later quote everything up to it
        # (including real `;` `|` `&` separators) was copied as a bogus inert span.
        # Either way a genuinely destructive command AFTER a quoted `$(...)` span
        # never became its own segment, so parse_force_ops(),
        # lifecycle_or_cloud_reason() and extract_rm_targets() never saw its
        # command word and fell through to ALLOW.
        #
        # This was a fixable FALSE NEGATIVE, not an intentional safety floor: the
        # #3679/#3755 "keep separators active inside a substitution-bearing span"
        # rule exists so smuggled content INSIDE the span is not masked — it never
        # implied disabling matching for the text AFTER the span. Remembering the
        # already-computed close index (a stack, so NESTED active spans each pop
        # their own close) resumes correct segmentation right after the real close
        # while leaving in-span matching exactly as it was.
        #
        # The close index is only authoritative when a BACKSLASH did not make the
        # pairing ambiguous — see the escaped-quote rules in the shared header
        # above and the three escape-aware branches below. With those in place
        # this produces MORE segment boundaries than before for every command the
        # shell can PARSE, so it cannot turn an existing deny into an allow there.
        #
        # KNOWN LIMIT (#130) — it is NOT an unconditional guarantee. Correcting
        # the pairing also frees a STRAY unmatched quote sitting after the span to
        # pair with a LATER quote instead of with the close of this span. (No
        # apostrophes in this block: it lives inside a single-quoted awk source
        # string, where one would terminate the string.) When the text
        # between them carries no substitution, the inert branch below copies it
        # verbatim and swallows the real separators inside it — separators the
        # pre-#113 mis-pairing happened to leave ACTIVE, so a few shapes go
        # deny -> allow:
        #     echo "$(id)" " ; <destructive> ; echo "trailing"
        # The same branch is reached by a second route, where the stray opener
        # sits INSIDE an active span and its partner lies past that span-s close
        # (the #130 reproduction; written here with S for the single quote this
        # single-quoted awk string cannot contain):
        #     echo "$( S )" ; <destructive> S
        # Every member of the family needs an ODD quote count, so the shell
        # rejects the command outright and nothing executes; the swallowed text is
        # text the shell also treats as quoted. Both routes are pinned by the
        # KNOWN LIMIT cases in the #113 test block, each paired with an
        # assert_shell_rejects so the unparseability half is mechanical rather
        # than a claim in this comment. The underlying weakness is the inert
        # branch itself, tracked in #130 — not this close-index bookkeeping.
        while (acn > 0 && acs[acn] < i) acn--   # spans a jump already skipped past
        if (acn > 0 && i == acs[acn]) {
            seg = seg c
            acn--
            i++
            continue
        }
        # A BACKSLASH-ESCAPED quote (`\"`) is literal text and never OPENS a span
        # (#113): opening one there let an escaped quote sitting AFTER an active
        # span pair with a much later quote and copy every real separator between
        # them as bogus inert text (`echo "$(id)" \" ; <destructive>`). Refusing
        # to open keeps the separators ACTIVE, which is the narrowing direction.
        # The forward close scan below still ACCEPTS an escaped quote as the
        # INERT-span boundary (ending a literal span earlier is also the active
        # direction, and it is what the pre-#113 walk did), but the ACTIVE-span
        # bookkeeping resolves a real close through trusted_close() (see below).
        if ((c == DQ || c == SQ) && !bs_escaped(s, i)) {
            qc = c
            ci = 0
            for (j = i + 1; j <= n; j++) if (substr(s, j, 1) == qc) { ci = j; break }
            if (ci == 0) {
                # Unterminated quote: advance ONE character with separators still
                # ACTIVE, exactly like qsplit() (#113). Copying the whole rest of
                # the buffer verbatim — the pre-#113 behaviour here — swallowed
                # every remaining separator, so a stray or escaped quote anywhere
                # ahead of a real `; <destructive>` hid it from all three parsers.
                # One-character advance can only ADD segment boundaries.
                seg = seg c
                i++
                continue
            }
            inner = substr(s, i + 1, ci - i - 1)
            if (index(inner, "$(") == 0 && index(inner, "`") == 0) {
                seg = seg substr(s, i, ci - i + 1)   # inert span: verbatim (newlines stay literal)
                # KNOWN LIMIT (#130): the only branch that can LOSE a boundary —
                # it consumes whatever the forward scan paired with, which for a
                # STRAY unmatched opener may be an unrelated later quote. That
                # opener may sit after a closed active span OR inside one with its
                # partner past the close. See the KNOWN LIMIT block at the
                # active-span-close guard above.
                # NOTE: `incmt` is deliberately NOT cleared here even when the
                # span carries a newline. A real comment ends at that newline, so
                # leaving the flag set can only suppress the opener probe for
                # LONGER than the shell would — and over-suppression is strictly
                # narrowing (it just restores the legacy pre-#84 treatment).
                # Clearing it here would re-enable the probe on text the shell may
                # still regard as commented, which is the widening direction.
                i = ci + 1
                continue
            }
            seg = seg c        # command substitution present: keep separators ACTIVE
            # ...but remember where the span really ENDS (#113). The naive "next
            # quote of the same kind" index is NOT usable here: a backslash-escaped
            # `\"` inside the span is literal text, so ending the span there would
            # leave the REAL close to open a bogus span (`echo "$(a \" b)" ;
            # <destructive>` — a shape the pre-#113 walk denied). trusted_close()
            # skips escaped candidates and returns 0 when the pairing is ambiguous,
            # in which case NO close is recorded and this span is walked exactly
            # the legacy way.
            tc = trusted_close(s, n, ci, qc)
            if (tc > 0) acs[++acn] = tc
            i++
            continue
        }
        # A `#` that STARTS a word opens a shell COMMENT that runs to the end of
        # the physical line, so any `<<WORD` after it is TEXT, not an operator.
        # A word starts at the beginning of the buffer or right after an unquoted
        # blank or shell METACHARACTER (` ` \t \n ; & | ( ) < >) — the full set
        # bash uses to delimit words, so `;#`, `&&#`, `|#` and `(cmd)#` are all
        # comment starts. A `#` in any OTHER position is part of a word
        # (`http://x#y`, `${#arr}`, `ab#cd`) and is correctly NOT a comment.
        #
        # Only the heredoc-opener PROBE is suppressed while the flag is set — the
        # characters still flow through the normal separator handling below,
        # because skipping to the newline here would hide a trailing `; <cmd>` in
        # a shape like `echo "$(id) # x" ; <cmd>` (a `#` inside a
        # command-substitution-bearing quoted span is walked by this loop, and
        # bash really does run that command). Probe suppression is strictly
        # NARROWING: it can only restore the legacy pre-#84 treatment, never
        # widen a deny into an allow — which is why the metacharacter set is
        # chosen as a SUPERSET of the shapes bash actually treats as comments.
        if (c == "#" && !incmt) {
            pc = (i == 1) ? "" : substr(s, i - 1, 1)
            if (i == 1 || pc == " " || pc == "\t" || pc == "\n" || pc == ";" ||
                pc == "&" || pc == "|" || pc == "(" || pc == ")" ||
                pc == "<" || pc == ">") incmt = 1
        }
        # A BACKSLASH-ESCAPED leading `<` (`\<<WORD`) is a literal `<` inside a
        # word, not a redirection operator, so the shell opens no heredoc there
        # (#108). Probing it anyway manufactured a phantom opener whose
        # terminator never appears, and the unterminated-heredoc rule then
        # skipped the REST OF THE BUFFER — hiding every later real command from
        # all three parsers. Note this checks only the FIRST `<`: when the SECOND
        # one is escaped (`<\<`) the `substr()` guard below already fails, and an
        # escaped DELIMITER (`<<\EOF`) is a legitimate opener that hd_opener()
        # handles by marking the body unexpanded.
        if (c == "<" && i < n && substr(s, i + 1, 1) == "<" && !incmt &&
            !bs_escaped(s, i)) {
            if (substr(s, i + 2, 1) == "<") {
                # `<<<` is a here-STRING: consume the whole operator so its third
                # `<` cannot be re-probed as the start of a `<< WORD` heredoc.
                seg = seg substr(s, i, 3)
                i += 3
                continue
            }
            # Inside an unclosed arithmetic expansion (`$((` / `((`) a `<<` is a
            # left-shift operator, never a heredoc — count the unbalanced opens
            # in the segment so far and skip the probe when we are inside one.
            t = seg; arO = gsub(/\(\(/, "", t)
            t = seg; arC = gsub(/\)\)/, "", t)
            hdnext = (arO > arC) ? 0 : hd_opener(s, n, i, hdo)
            if (hdnext > 0) {
                # Record the pending heredoc and copy the operator verbatim (this
                # also consumes a quoted delimiter, so its quotes never open a
                # phantom quoted span). The REST of this line segments normally.
                seg = seg substr(s, i, hdnext - i)
                hdc++
                hddelim[hdc] = hdo["delim"]
                hdstrip[hdc] = hdo["strip"]
                hdquoted[hdc] = hdo["quoted"]
                i = hdnext
                continue
            }
        }
        if (c == "\n" && hdc > 0 && !bs_escaped(s, i)) {
            # End of the opener line: the pending heredoc BODIES follow. Look
            # ahead across every pending body, in opener order, to find where the
            # last one ends — and whether any EXPANSION-CAPABLE body (bare
            # delimiter) carries a command substitution, which a real shell WOULD
            # execute. That case keeps the legacy separator-active treatment
            # (same #3679 floor the quoted-span branch above applies), so a
            # smuggled payload is never hidden behind a heredoc opener.
            k = i + 1
            unsafe = 0
            for (h = 1; h <= hdc; h++) {
                while (k <= n) {
                    eol = index(substr(s, k), "\n")
                    nexti = (eol == 0) ? n + 1 : k + eol
                    line = substr(s, k, nexti - k)
                    t = line
                    sub(/\n$/, "", t)
                    if (hdstrip[h]) sub(/^\t+/, "", t)
                    k = nexti
                    if (t == hddelim[h]) break        # terminator line, body done
                    if (!hdquoted[h] && (index(line, "$(") > 0 || index(line, "`") > 0)) unsafe = 1
                }
            }
            hdc = 0
            if (!unsafe) {
                # The opener line is a complete simple command; the body (through
                # its terminator, or through end-of-buffer when unterminated) is
                # inert DATA and contributes no segment at all.
                segs[++segc] = seg
                seg = ""
                incmt = 0
                i = k
                continue
            }
            # else: fall through to the ordinary separator handling below.
        }
        if (c == ";" || c == "&" || c == "|" || c == "\n") {
            if (c == "\n") incmt = 0   # a comment ends at the physical newline
            segs[++segc] = seg; seg = ""; i++; continue
        }
        seg = seg c
        i++
    }
    segs[++segc] = seg
    return segc
}
'

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
    printf '%s' "$1" | awk "$_ESCAPE_AWK$_ML_QSPLIT_AWK"'
    BEGIN {
        SEP = sprintf("%c", 31)  # US (unit separator) — non-whitespace so bash
                                 # read does not trim an empty cpath.
        buf = ""
    }
    # Slurp the whole (possibly multi-line) command, then segment ONCE with the
    # shared quote-aware lexer (#71) so a multi-line quoted DATA literal whose
    # interior line is a force-push phrase is no longer mis-read as a real
    # segment (the pre-#71 per-record `qsplit()` reset quote state at each
    # embedded newline).
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        n = ml_segment(buf, segs)
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
# --title, --notes, --comment) so a dangerous-looking phrase quoted INSIDE such a
# value no longer trips the raw ALWAYS_BLOCK_PATTERNS substring scan (catastrophic
# tier) or the ASK_PATTERNS scan (ask tier, #3756). Used ONLY to build the
# literal-redacted working copies for those two loops (mirrors the
# COMMAND_NO_COMMENT precedent); every other scan keeps reading the raw command.
# This kills the #3679 false positive where `gh pr comment --body "…git push
# --force origin main…"` / `git commit -m "…"` hard-denied even though nothing
# executes, and (#3756) the analogous ask-tier false ask where an ask-phrase like
# `gh issue close` quoted inside a `--comment`/`--body` value prompted for
# confirmation despite no such command actually being run.
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
# =============================================================================
# dequote_inert_spans (repo#197) — remove the quote CHARACTERS around inert
# quoted spans, leaving their contents in place.
#
# Turns `rm -rf "/"` into `rm -rf /` so the literal catastrophic patterns match
# what the shell would actually run. Callers MUST pass a copy that has already
# been through the sink-aware literal-text redaction, so that prose quoted in a
# --body/-m/--title/--notes/--comment value is already blanked and cannot be
# resurrected into an apparent command by dequoting.
#
# A span containing a command substitution is left completely intact, quotes and
# all, so smuggling still reaches the raw scan unchanged.
#
# Unterminated quotes are emitted verbatim: an unbalanced quote is ambiguous,
# and the raw copy is still scanned, so failing to dequote can only ever keep
# the existing verdict, never widen it.
#
# NOTE: the awk program below is SINGLE-QUOTED. An apostrophe anywhere inside
# it, including in a comment, terminates the string and breaks the guard for
# every command in the repo. Keep comments here apostrophe-free.
# =============================================================================
dequote_inert_spans() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)
        DQ = sprintf("%c", 34)
        buf = ""
    }
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        s = buf
        out = ""
        n = length(s)
        i = 1
        while (i <= n) {
            c = substr(s, i, 1)
            if (c == DQ || c == SQ) {
                # Find the matching close quote.
                endpos = 0
                for (j = i + 1; j <= n; j++) {
                    if (substr(s, j, 1) == c) { endpos = j; break }
                }
                if (endpos == 0) {
                    # Unterminated: emit the rest verbatim and stop.
                    out = out substr(s, i)
                    i = n + 1
                    continue
                }
                inner = substr(s, i + 1, endpos - i - 1)
                if (index(inner, "$(") == 0 && index(inner, "`") == 0) {
                    out = out inner
                } else {
                    out = out substr(s, i, endpos - i + 1)
                }
                i = endpos + 1
                continue
            }
            out = out c
            i++
        }
        printf "%s", out
    }'
}

strip_literal_text() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)   # single quote
        DQ = sprintf("%c", 34)   # double quote
        # boundary + text-carrying flag + optional (ws / = / ws) + quoted span.
        # The leading boundary class includes a newline so a `--body` that begins
        # a continuation line is still recognized; the quoted-span classes
        # ([^"]* / [^'"'"']*) already match a newline, so a MULTI-LINE quoted
        # value is captured as one span once the whole command is slurped below.
        re = "(^|[ \t\n])(--message|--body|--notes|--title|--comment|-m)[ \t]*=?[ \t]*(" \
             DQ "[^" DQ "]*" DQ "|" SQ "[^" SQ "]*" SQ ")"
        buf = ""
    }
    # MULTI-LINE REDACTION (#3898): slurp the whole (possibly multi-line) command
    # into one buffer, preserving embedded newlines, then redact ONCE in END so a
    # quoted flag value that spans several lines is treated as a single inert
    # span. The old per-line ($0) processing split a multi-line `gh issue create
    # --body "…"` body at each newline, leaving a dangerous phrase quoted on an
    # interior line un-redacted — which then tripped the catastrophic scan on
    # documentation text that merely MENTIONS a dangerous command (the meta
    # false-positive that blocked filing #3898). Single-line input is
    # byte-for-byte identical to the previous behaviour.
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        s = buf
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
            # gsub(/./) leaves embedded newlines untouched (awk `.` never matches a
            # newline), so a multi-line span stays SAME-LENGTH and byte offsets of
            # the surrounding command are preserved.
            if (index(inner, "$(") == 0 && index(inner, "`") == 0) {
                gsub(/./, "X", inner)
            }
            out = out pre head inner qchar
        }
        out = out s
        printf "%s", out
    }'
}

# Redact the quoted argument(s) of a non-executing "data sink" command word
# (echo, printf) so a dangerous-looking string that appears ONLY as quoted DATA
# handed to echo/printf no longer trips the raw ALWAYS_BLOCK_PATTERNS scan
# (catastrophic tier) or the ASK_PATTERNS scan (ask tier) (#53). echo/printf
# print their arguments verbatim; they never EXECUTE them, so a quoted argument
# is inert text — exactly like a --body/-m value — yet strip_literal_text()'s
# flag allowlist never covered it. This is the meta false-positive that blocked
# a guard self-test (`echo '{"…":"<dangerous cmd>"}' | guard-destructive.sh`)
# and blocked filing this very issue's heredoc body.
#
# Command-word anchored (mirrors fastpath_builtin_admits() and the segment
# parsers): the quoted args are redacted ONLY for a simple command whose FIRST
# token is exactly `echo` or `printf` (optionally behind a bare `sudo`/`env`
# wrapper). A wrapper that actually EXECUTES its argument — `bash -c '<payload>'`,
# `sh -c`, `eval`, `xargs` — is never a data sink and is never redacted here.
#
# Safety floor, identical to strip_literal_text()/qsplit():
#   - A quoted span is redacted ONLY when it carries no command substitution /
#     backtick opener (`$(` or a backtick), so a smuggled `echo "$(<payload>)"`
#     keeps its payload intact and still hard-denies.
#   - The `echo '<payload>' | sh` shape (data PIPED into a shell that WOULD
#     execute it) is handled by the command_has_shell_segment() gate at the call
#     site, which skips this redaction entirely whenever any pipeline segment's
#     command word is a shell — so the raw scan still sees and blocks the payload.
#
# Single-pass quote-aware lexer. It deliberately does NOT reuse qsplit(), whose
# `\n`-per-separator contract would conflate a real newline inside a multi-line
# quoted span with a shell separator; here a multi-line span is redacted as one
# inert unit (`.` never matches a newline, so each line stays SAME-LENGTH and the
# surrounding byte offsets are preserved). Best-effort like strip_literal_text():
# an unterminated quote copies the remainder verbatim (never redacts), and the
# result feeds only the NARROWING scans, so the worst case is a raw substring
# surviving (a false block) — never a catastrophic block being skipped.
strip_datasink_literals() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)   # single quote
        DQ = sprintf("%c", 34)   # double quote
        buf = ""
    }
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        s = buf
        n = length(s)
        out = ""
        i = 1
        atcmd = 1     # at the start of a simple command (command-word position)
        sink = 0      # inside an echo/printf data-sink command
        redir = 0     # the previous token was a redirection operator (repo#197)
        while (i <= n) {
            c = substr(s, i, 1)
            # A shell separator resets to command-word position.
            if (c == ";" || c == "&" || c == "|" || c == "\n") {
                out = out c; i++; atcmd = 1; sink = 0; redir = 0; continue
            }
            # Leading whitespace is copied without leaving command-word position.
            # It also does NOT clear redir, so the space between the operator
            # and its target is transparent.
            if (c == " " || c == "\t") { out = out c; i++; continue }
            # A redirection operator. What follows is a FILENAME handed to the
            # redirection by the shell, never an argument to echo/printf, so it
            # must not be redacted as data. Without this, a quoted redirect
            # target after echo was blanked and Bash-tool write confinement went
            # blind to it -- deny for a bare target, allow for the identical
            # quoted one (repo#197). Only echo/printf were affected; cat, tee,
            # cp, mv and sed -i confine quoted targets correctly because they
            # are not data sinks.
            if (c == ">") { out = out c; i++; redir = 1; continue }
            # Command-word position: read the first token and classify it.
            if (atcmd) {
                atcmd = 0
                tok = ""
                j = i
                while (j <= n) {
                    cc = substr(s, j, 1)
                    if (cc == " " || cc == "\t" || cc == ";" || cc == "&" || cc == "|" || cc == "\n") break
                    tok = tok cc
                    j++
                }
                # A bare sudo/env wrapper: emit it and stay in command-word
                # position so the NEXT token is classified as the command word.
                if (tok == "sudo" || tok == "env") {
                    out = out tok; i = j; atcmd = 1; continue
                }
                if (tok == "echo" || tok == "printf") { sink = 1 }
                out = out tok; i = j; continue
            }
            # Mid-command: a quoted span is redacted only inside a data sink.
            if (c == DQ || c == SQ) {
                qc = c
                ci = 0
                for (j = i + 1; j <= n; j++) {
                    if (substr(s, j, 1) == qc) { ci = j; break }
                }
                if (ci == 0) {
                    # Unterminated quote: copy the rest verbatim, never redact.
                    out = out substr(s, i); i = n + 1; continue
                }
                inner = substr(s, i + 1, ci - i - 1)
                if (sink && !redir && index(inner, "$(") == 0 && index(inner, "`") == 0) {
                    gsub(/./, "X", inner)   # . never matches \n: multi-line stays same-length
                }
                out = out qc inner qc; i = ci + 1; redir = 0; continue
            }
            out = out c; i++; redir = 0
        }
        printf "%s", out
    }'
}

# Mask quoted POSITIONAL arguments (no preceding flag name) to a repo-
# configurable allowlist of known non-executing commands/scripts (#195). Used
# to build the ASK-tier working copy (COMMAND_ASK_SCAN) ONLY — see the call
# site below strip_datasink_literals()'s invocation for that copy. This is
# strip_literal_text()'s counterpart for POSITIONAL text: strip_literal_text()
# only recognizes text following a NAMED flag (--body/-m/--title/--notes/
# --comment); it has no effect on a script whose free-text arguments are
# purely positional, e.g. `./scripts/check-duplicate.sh "TITLE"
# "DESCRIPTION"` where DESCRIPTION happens to quote an ask-phrase. Such a
# script never EXECUTES a positional argument — it only reads it as inert
# search/dedup text — so masking a quoted argument immediately following the
# configured command (optionally after short/long flags, e.g. `check-
# duplicate.sh --include-merged-prs "..."`) can never blind ASK_PATTERNS (or
# any other COMMAND_ASK_SCAN consumer) to a REAL invocation: a wrapper that
# WRAPS the phrase and then executes it — `sh -c "git stash pop"`, `bash -c
# '...'`, `eval "..."` — is never in the allowlist and stays fully visible.
#
# DELIBERATELY EXCLUDES grep/egrep/fgrep/rg AND cp/mv/tee/sed — enforced by
# the caller (positional_mask_cmdre()'s _POSITIONAL_MASK_NEVER set above drops
# them even when configured), not by this function, which simply masks
# whatever command-name alternation it is given. The reasons live with the
# caller (see its full COMMAND_ASK_SCAN consumer audit table); in short, this
# scan feeds TWO deny-tier consumers besides the ask-tier ones:
#
#   - the SQL DDL/DML check (SQL_DDL_PATTERN, below), which intentionally
#     scans a `grep '<pattern>' file` invocation's own quoted positional
#     pattern for a literal DDL phrase like "DROP TABLE" and DENIES, by
#     design — masking grep's own quoted argument here would blind that scan
#     to text it is specifically meant to catch. Adding grep/rg to the
#     allowlist was tried and directly regresses the "Fast path security" /
#     SQL-DDL test coverage in hooks/repo/tests/test-guard-destructive.sh.
#   - the #4178 Bash-tool WRITE CONFINEMENT block, which passes this exact
#     scan to extract_write_targets() and DENIES a write landing in the main
#     checkout from a builder worktree. cp/mv/tee/sed are the command words
#     that extractor recognizes as write idioms, and their target PATH is a
#     positional argument — masking it made `cp "/tmp/src.txt"
#     "<main-checkout>/evil.sh"` fall through from deny to ALLOW under
#     `positionalMaskAllowlist: ["cp"]` (#195 review finding).
#
# Extend the exclusion set only for another read-only positional-arg consumer
# with NO competing raw-text consumer elsewhere in this file (mirrors the
# vendored guard's own extend-only convention for this allowlist), and extend
# _POSITIONAL_MASK_NEVER whenever a NEW deny-tier consumer of
# COMMAND_ASK_SCAN with a recognizable command word is added.
#
# Only feeds COMMAND_ASK_SCAN, never the catastrophic scan (which keeps
# reading raw $COMMAND/$COMMAND_NO_LITERAL_TEXT). Within COMMAND_ASK_SCAN it
# narrows ask-tier matching only — the two deny-tier consumers above stay
# intact because their subject command words can never enter the allowlist.
#
# Masks EVERY quoted argument that directly, consecutively follows the
# command+flags (separated only by whitespace) — not just the first — so a
# multi-positional-arg script's whole argument list gets masked. Masking
# stops at the first token that is not a quoted string (a bare filename,
# `&&`, `|`, etc.), leaving anything after that boundary — including a real
# ask-triggering invocation chained onto the same line — fully visible.
#
# $1 = command string to mask. $2 = '|'-joined, ERE-escaped allowlist of
# command names (already filtered by positional_mask_cmdre()). The caller
# only invokes this when $2 is non-empty; an absent/empty allowlist is a
# no-op by construction (the anchor regex then never matches), matching the
# "absent config is a no-op" default (#195 AC).
mask_ask_positional_args() {
    # cmdre is threaded through ENVIRON, NOT -v: gawk's -v assignment runs the
    # value through the same C-style backslash-escape decoding as a string
    # constant (so a caller-supplied "\." — the ERE-escaped literal dot
    # positional_mask_cmdre() produces for a name like "check-duplicate.sh" —
    # would be silently decoded back to a bare "." before the regex engine
    # ever sees it, defeating the escaping and emitting a spurious "unknown
    # escape sequence" warning). ENVIRON values are passed through verbatim.
    printf '%s' "$1" | CMDRE_FOR_AWK="$2" awk '
    BEGIN {
        SQ = sprintf("%c", 39)
        DQ = sprintf("%c", 34)
        cmdre = ENVIRON["CMDRE_FOR_AWK"]
        # Zero or more short/long flags between the command name and the
        # first quoted positional argument (e.g.
        # `check-duplicate.sh --include-merged-prs --issue 195`).
        flagre = "([ \t]+-[A-Za-z0-9_-]+)*"
        anchor = "(^|[ \t\n;&|`(])(" cmdre ")" flagre "[ \t]+"
        buf = ""
    }
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        s = buf
        out = ""
        while (match(s, anchor)) {
            pre     = substr(s, 1, RSTART - 1)
            matched = substr(s, RSTART, RLENGTH)
            rest    = substr(s, RSTART + RLENGTH)
            out = out pre matched
            # Mask every consecutive quoted positional argument immediately
            # following the anchor (whitespace-separated). Stops at the first
            # non-quote-starting token, so anything after the argument list
            # (a pipe, &&, an unrelated command) is left fully visible.
            while (1) {
                qc = substr(rest, 1, 1)
                if (qc != DQ && qc != SQ) break
                endpos = 0
                for (i = 2; i <= length(rest); i++) {
                    if (substr(rest, i, 1) == qc) { endpos = i; break }
                }
                if (endpos == 0) break
                inner = substr(rest, 2, endpos - 2)
                if (index(inner, "$(") == 0 && index(inner, "`") == 0) {
                    gsub(/./, "X", inner)
                }
                out = out qc inner qc
                rest = substr(rest, endpos + 1)
                while (substr(rest, 1, 1) == " " || substr(rest, 1, 1) == "\t") {
                    out = out substr(rest, 1, 1)
                    rest = substr(rest, 2)
                }
            }
            s = rest
        }
        out = out s
        printf "%s", out
    }'
}

# Return 0 (success) if ANY quote-aware segment's command word is a shell binary
# (sh/bash/dash/zsh/ksh/csh/tcsh/fish/pwsh, with or without a leading path).
# GATES strip_datasink_literals(): when a shell could consume the command's data
# (e.g. `echo '<payload>' | sh`), the data-sink redaction is skipped so the raw
# catastrophic scan still sees — and blocks — the payload. Conservative by
# construction: a shell ANYWHERE in the command disables the (narrowing)
# redaction, so the worst case is a preserved false BLOCK, never a skipped one.
# `guard-destructive.sh` (basename is not a bare shell word) is deliberately NOT
# matched, so the guard's own `echo '<json>' | guard-destructive.sh` self-test
# still redacts and no longer false-blocks (#53). Emits "yes"/"no".
command_has_shell_segment() {
    printf '%s' "$1" | awk "$_ESCAPE_AWK$_QSPLIT_AWK"'
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        found = 0
        s = qsplit(buf)   # quote-aware segmentation (#3755); separators -> \n
        n = split(s, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            # Strip any run of leading VAR=val assignments, then a sudo/env wrapper,
            # so the REAL command word is classified. The required trailing [ \t]+
            # in the assignment pattern guarantees the loop makes progress.
            while (match(seg, /^[A-Za-z_][A-Za-z0-9_]*=[^ \t]*[ \t]+/)) { seg = substr(seg, RLENGTH + 1) }
            sub(/^sudo[ \t]+/, "", seg)
            sub(/^env[ \t]+/, "", seg)
            sub(/^[ \t]+/, "", seg)
            m = split(seg, toks, /[ \t]+/)
            if (m == 0) continue
            w = toks[1]
            sub(/.*\//, "", w)   # basename only
            if (w == "sh" || w == "bash" || w == "dash" || w == "zsh" || \
                w == "ksh" || w == "csh" || w == "tcsh" || w == "fish" || w == "pwsh") { found = 1 }
        }
        print (found ? "yes" : "no")
    }'
}

# Helper: output a deny decision and exit
#
# Optional second arg is a short, STABLE rule tag (issue #3771) recorded as the
# decision log's `pattern` field; it defaults to "deny" (a function-name-derived
# fallback) so this stays backward-compatible with call sites that don't pass
# one. Telemetry is emitted BEFORE the JSON decision so a logging hiccup can
# never suppress the deny, and the `|| true` guarantees it never trips the ERR
# trap. Deny is always the "catastrophic" tier.
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
#
# Same optional rule-tag convention as deny() (issue #3771); defaults to "ask".
# Ask is always the "ask" tier. Telemetry is best-effort and emitted before the
# JSON decision.
ask() {
    local reason="$1"
    local tag="${2:-ask}"
    log_guard_decision "ask" "ask" "$tag" || true
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
    # NOTE (#72): these three patterns require `rm` to be immediately followed by
    # whitespace, so a command-word substitution like `$(which rm) -rf /` (where
    # `rm` is followed by `)`) does NOT match here. That shape is instead caught
    # by the extract_rm_targets() -> rm-protected-path path below, whose deny
    # covers root, $HOME, AND every top-level dir — a superset of these three.
    # For that superset claim to actually hold for the substitution shape, the
    # extract path must recognize the SAME home/root targets these literal
    # patterns do: extract_rm_targets() strips a leading `env` (and VAR=val
    # assignments) as well as `sudo`, and the protected-path loop expands a bare
    # `~`/`$HOME` target before the check — so `env $(which rm) -rf /`,
    # `$(which rm) -rf ~`, and `$(which rm) -rf $HOME` all deny just like their
    # literal counterparts. No parallel regex is needed for the substitution case.
    'rm[[:space:]]+-[a-zA-Z]*[rf][a-zA-Z]*[[:space:]]+/([^[:alnum:]._~/-]|$)'
    'rm[[:space:]]+-[a-zA-Z]*[rf][a-zA-Z]*[[:space:]]+~([^[:alnum:]._~/-]|$)'
    'rm[[:space:]]+-[a-zA-Z]*[rf][a-zA-Z]*[[:space:]]+\$HOME([^[:alnum:]._~/-]|$)'

    # Fork bombs
    ':\(\)\{ :\|:& \};:'

    # Pipe to shell (supply chain risk) — the piped-to COMMAND must itself be
    # a shell (repo#29). The old shapes ('curl .* \| .*sh', 'curl .* \| bash',
    # 'wget .* \| .*sh', 'wget .* -O- \| sh') matched "sh" anywhere after the
    # pipe, so piping a download to `tee /usr/share/...`, `shasum`, or any
    # path containing "sh" false-positived (and quoting such a pipeline in an
    # issue body blocked the bug report about it). The single fixed pattern
    # anchors on the command position immediately after a pipe: optional
    # sudo (with flags), an optional path prefix, then a shell word
    # (sh/bash/dash/zsh/ksh/csh/tcsh/fish/pwsh) followed by a non-word
    # character. `[^;&]*` spans pipes but not command separators, so a
    # multi-stage pipeline (`curl … | gunzip | sh`) still denies while a
    # neighbouring command after `&&`/`;` is never mis-joined. Known accepted
    # misses: a wrapper consuming the command position (`| sudo -u user sh`,
    # `| env sh`); `bash -c 'curl … | sh'` still denies (raw scan, `-c` is
    # never redacted).
    '(^|[;&|[:space:](])(curl|wget)[^;&]*\|[[:space:]]*(sudo[[:space:]]+(-[^[:space:]]+[[:space:]]+)*)?([^[:space:]|;&]*/)?(ba|da|z|k|c|tc|fi|pw)?sh([[:space:]]|$|[;&|)])'

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
# below, so a force-push-to-main phrase quoted inside a
# --body/-m/--title/--notes/--comment value no longer false-positives (#3679,
# --comment added #3756). The awk only runs when one of those flags is actually
# present, keeping it off the hot path (mirrors the COMMAND_NO_COMMENT
# `#`-present guard). `-c` is intentionally excluded so `bash -c '<payload>'`
# payloads still reach the raw scan; spans carrying `$(` / backtick are left
# intact so command-substitution smuggling still hard-denies.
COMMAND_NO_LITERAL_TEXT="$COMMAND"
if [[ "$COMMAND" == *"--body"* || "$COMMAND" == *"--message"* || \
      "$COMMAND" == *"--title"* || "$COMMAND" == *"--notes"* || \
      "$COMMAND" == *"--comment"* || "$COMMAND" == *"-m"* ]]; then
    COMMAND_NO_LITERAL_TEXT=$(strip_literal_text "$COMMAND")
fi
# ALSO redact the quoted args of a data-sink command word (echo/printf), so a
# dangerous string handed to echo/printf as inert DATA no longer trips the raw
# scan (#53) — the meta false-positive that blocked guard self-tests and filing
# this issue's heredoc body. Gated on echo/printf being present (off the hot
# path otherwise) AND on NO shell segment existing: when data is piped into a
# shell that would execute it (`echo '<payload>' | sh`), the redaction is skipped
# so the raw scan still blocks the payload. `-c` wrappers (bash -c/sh -c) are
# never data sinks, and `$(`/backtick spans are never redacted, so smuggling
# still hard-denies.
if [[ "$COMMAND" == *"echo"* || "$COMMAND" == *"printf"* ]] && \
   [[ "$(command_has_shell_segment "$COMMAND")" == "no" ]]; then
    COMMAND_NO_LITERAL_TEXT=$(strip_datasink_literals "$COMMAND_NO_LITERAL_TEXT")
fi

# =============================================================================
# DEQUOTED CATASTROPHIC COPY (repo#197)
#
# The patterns above are literal command text, so quoting an argument used to
# defeat them outright: `rm -rf "/"` was ALLOWED while `rm -rf /` denied, and
# `git push --force origin "main"` fell through to a mere ask. Those are the
# same commands to the shell — the guard was enforcing a spelling, not a
# policy, and quoting a path is the ordinary thing to do.
#
# The fix is ORDER, not less redaction. This copy is derived from
# COMMAND_NO_LITERAL_TEXT, i.e. AFTER the sink-aware redaction above has already
# blanked the quoted values of --body/-m/--title/--notes/--comment and of
# echo/printf data sinks. So prose that merely quotes a dangerous command
# ("document rm -rf / hazard") is already inert before dequoting can see it,
# and stays inert. What dequoting exposes is only the quoting of an OPERATIVE
# argument, which is exactly what should be scanned.
#
# Spans containing $( or a backtick are left untouched, so command-substitution
# smuggling keeps hard-denying via the raw copy.
#
# Scanned IN ADDITION to the raw copy, never instead of it — dequoting changes
# byte offsets, so this copy is only ever fed to these pattern greps, never to
# target extraction.
# =============================================================================
COMMAND_DEQUOTED="$COMMAND_NO_LITERAL_TEXT"
if [[ "$COMMAND_NO_LITERAL_TEXT" == *'"'* || "$COMMAND_NO_LITERAL_TEXT" == *"'"* ]]; then
    COMMAND_DEQUOTED=$(dequote_inert_spans "$COMMAND_NO_LITERAL_TEXT")
fi

for pattern in "${ALWAYS_BLOCK_PATTERNS[@]}"; do
    if echo "$COMMAND_NO_LITERAL_TEXT" | grep -qiE "$pattern"; then
        deny "BLOCKED: Command matches dangerous pattern: $pattern" "catastrophic:$pattern"
    fi
    if [[ "$COMMAND_DEQUOTED" != "$COMMAND_NO_LITERAL_TEXT" ]] && \
       echo "$COMMAND_DEQUOTED" | grep -qiE "$pattern"; then
        deny "BLOCKED: Command matches dangerous pattern: $pattern (quoting an argument does not change what the shell runs)" "catastrophic-dequoted:$pattern"
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
# ASK-TIER WORKING COPY (#3756) — comment-stripped AND literal-text redacted.
#
# The ASK_PATTERNS loop below needs BOTH narrowings the catastrophic tier's two
# copies provide separately: COMMAND_NO_COMMENT's `#`-comment stripping AND
# strip_literal_text()'s quoted-flag-value redaction (the #3679 fix the ask tier
# never received). Building the ask copy from COMMAND_NO_COMMENT (not raw
# $COMMAND) preserves the comment-stripping the ask tier already relied on, then
# redacts --body/-m/--title/--notes/--comment values so an ask-phrase quoted
# inside such a value (e.g. `gh pr comment --body "…gh issue close…"`) no longer
# false-asks. The strip only runs when a text-carrying flag is present, keeping
# it off the hot path. Never feeds the catastrophic scan (that keeps reading the
# raw command), so this can only NARROW an ask, never miss a hard deny.
# =============================================================================
COMMAND_ASK_SCAN="$COMMAND_NO_COMMENT"
if [[ "$COMMAND_NO_COMMENT" == *"--body"* || "$COMMAND_NO_COMMENT" == *"--message"* || \
      "$COMMAND_NO_COMMENT" == *"--title"* || "$COMMAND_NO_COMMENT" == *"--notes"* || \
      "$COMMAND_NO_COMMENT" == *"--comment"* || "$COMMAND_NO_COMMENT" == *"-m"* ]]; then
    COMMAND_ASK_SCAN=$(strip_literal_text "$COMMAND_NO_COMMENT")
fi
# Mirror the catastrophic tier's data-sink redaction (#53): an ask-phrase quoted
# as inert echo/printf data (e.g. `echo 'run gh issue close 5 to clean up'`)
# should not false-ask. Same shell-segment gate keeps `echo '<phrase>' | sh`
# reaching the raw ask scan. Never feeds the catastrophic scan, so it can only
# NARROW an ask, never miss a hard deny.
if [[ "$COMMAND_NO_COMMENT" == *"echo"* || "$COMMAND_NO_COMMENT" == *"printf"* ]] && \
   [[ "$(command_has_shell_segment "$COMMAND_NO_COMMENT")" == "no" ]]; then
    COMMAND_ASK_SCAN=$(strip_datasink_literals "$COMMAND_ASK_SCAN")
fi
# Third narrowing: mask quoted POSITIONAL arguments of a repo-configured
# command allowlist (guards.positionalMaskAllowlist, #195) — the ASK-tier
# analog of the two named-flag/data-sink narrowings above, for tools whose
# free-text arguments are purely positional rather than behind --body/-m/
# echo (see mask_ask_positional_args()'s header comment, near
# strip_datasink_literals() above). Gated on a quote character being present
# at all (positional masking can only ever matter when there is a quoted
# argument to mask), which keeps the config read off the hot path for the
# many full-path commands that carry no quotes. positional_mask_cmdre() is
# itself cached and resolves to an empty string on the (default) absent/
# empty config, so this step is a true no-op on every repo that hasn't opted
# in.
#
# It never feeds the catastrophic scan. Note that NOT feeding the catastrophic
# scan is by itself NOT enough to guarantee "can only narrow an ask" (#195
# review): COMMAND_ASK_SCAN also feeds two DENY-tier consumers — the SQL DDL
# check below and the #4178 Bash-tool write-confinement block, which passes
# this very variable to extract_write_targets(). What actually preserves both
# denies is positional_mask_cmdre()'s mandatory _POSITIONAL_MASK_NEVER
# exclusion set (grep/egrep/fgrep/rg + cp/mv/tee/sed), which no operator
# config can override — see its consumer audit table.
if [[ "$COMMAND_NO_COMMENT" == *'"'* || "$COMMAND_NO_COMMENT" == *"'"* ]]; then
    _POSITIONAL_MASK_CMDRE="$(positional_mask_cmdre)"
    if [[ -n "$_POSITIONAL_MASK_CMDRE" ]]; then
        COMMAND_ASK_SCAN=$(mask_ask_positional_args "$COMMAND_ASK_SCAN" "$_POSITIONAL_MASK_CMDRE")
    fi
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
    printf '%s' "$1" | awk "$_ESCAPE_AWK$_ML_QSPLIT_AWK"'
    BEGIN { buf = "" }
    # Slurp the whole (possibly multi-line) command, then segment ONCE with the
    # shared quote-aware lexer (#71) so a multi-line quoted DATA literal whose
    # interior line is a lifecycle/cloud word (e.g. `halt`) is no longer mis-read
    # as a real segment (the pre-#71 per-record `qsplit()` reset quote state at
    # each embedded newline, hard-denying inert quoted prose).
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        n = ml_segment(buf, segs)
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

# Lifecycle denies are unconditional. The az/gcloud delete denies are gated by
# the cloud-CLI toggle (Repo Skills refinement): for a repo whose job IS
# managing cloud infra, `az`/`gcloud … delete` is first-class teardown, so
# guards.cloudCli:false / REPO_GUARD_CLOUD=0 downgrades those denies to allow.
# Every emitted reason is inspected (not just the first) so a skipped cloud
# reason can never mask a lifecycle deny later in the same command.
while IFS= read -r _lifecycle_reason; do
    [[ -z "$_lifecycle_reason" ]] && continue
    if [[ "$_lifecycle_reason" == "cloud resource deletion:"* ]]; then
        cloud_guard_enabled && deny "BLOCKED: $_lifecycle_reason" "lifecycle-or-cloud-delete"
    else
        deny "BLOCKED: $_lifecycle_reason" "lifecycle-or-cloud-delete"
    fi
done < <(lifecycle_or_cloud_reason "$COMMAND_NO_COMMENT")

# =============================================================================
# DATABASE DESTRUCTION - Gated by the SQL DDL/DML guard toggle
#
# Kept separate from ALWAYS_BLOCK_PATTERNS so DB-engine repos can opt out
# (guards.sqlDdl:false / LOOM_GUARD_SQL=0). A single alternation grep matches
# all four DDL statements in one pass (cheaper than a per-pattern loop), and
# sql_guard_enabled() is consulted only after a match, so the config read stays
# off the hot path.
# =============================================================================
#
# Scanned against COMMAND_ASK_SCAN — the comment-stripped, literal-text-redacted
# working copy — NOT the raw COMMAND_NO_COMMENT (repo#188 parity fix). A DDL
# phrase quoted inside a `--body`/`-m`/`--title` value is prose *about* a
# destructive statement, not a destructive statement, and denying it blocks
# ordinary work: filing the issue that describes the hazard, or committing the
# migration note that mentions it. This guard's own repository trips it — a
# `grep` for the phrase, and this very comment, both used to deny. Loom's
# vendored copy has always scanned the redacted copy here; the raw scan was the
# single largest source of behavioral divergence between the two guards.
SQL_DDL_PATTERN='DROP DATABASE|DROP TABLE|DROP SCHEMA|TRUNCATE TABLE'
if echo "$COMMAND_ASK_SCAN" | grep -qiE "$SQL_DDL_PATTERN" && sql_guard_enabled; then
    matched=$(echo "$COMMAND_ASK_SCAN" | grep -oiE "$SQL_DDL_PATTERN" | head -1)
    deny "BLOCKED: Command matches dangerous pattern: ${matched:-SQL DDL statement}" "sql-ddl"
fi

# =============================================================================
# rm -rf SCOPE CHECK - Block rm with recursive/force flags on protected paths
#
# Only *actual local* `rm` command words are inspected. `extract_rm_targets`
# splits the command on ; | & && || and, for each simple-command segment whose
# command word is `rm` (optionally sudo-prefixed) — OR a command-word
# *substitution* `$(...)`/backtick in executable position (#72) — AND which
# carries a recursive/force flag, emits the non-flag argument tokens.
# Consequences (#3553):
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
    # Portable awk only (no GNU/BSD-specific escapes).
    #
    # MULTI-LINE QUOTE AWARENESS (#60): slurp the whole (possibly multi-line)
    # command into ONE buffer, then segment ONCE with a quote-aware walk — instead
    # of the old per-awk-record `$0 = qsplit($0)`, whose quote-tracking reset at
    # every input newline because awk's default RS split the command into separate
    # records. That per-record form false-blocked a multi-line quoted DATA literal
    # (echo/printf/--body) whose interior line merely BEGINS with `rm -rf /`: the
    # interior line was scanned as its own top-level segment with no memory that it
    # is still inside an open quote from a prior line, so its command word resolved
    # to a real `rm` and its lone `/` token hit the protected-root deny. Mirrors
    # the buffer-slurp the catastrophic/ask redactors (strip_datasink_literals /
    # strip_literal_text) and command_has_shell_segment() already use.
    #
    # Segmentation is delegated to the shared ml_segment() lexer (#71,
    # _ML_QSPLIT_AWK) rather than reusing qsplit()+split("\n"), because qsplit()
    # emits a `\n` for each real separator while ALSO leaving a literal newline
    # that lived inside an inert quoted span untouched — the two are then
    # indistinguishable to a downstream `split(s, segs, "\n")`, which is exactly
    # why the naive slurp-then-qsplit would still re-split the quoted `rm -rf /`
    # line into its own segment. ml_segment() walks the buffer once so an inert
    # quoted span's embedded newlines never become segment boundaries. PR #69
    # introduced this lexer inline here; #71 extracted it into the shared helper
    # so parse_force_ops()/lifecycle_or_cloud_reason() reuse the SAME algorithm
    # instead of duplicating it (see the _ML_QSPLIT_AWK header for the full
    # segmentation contract).
    printf '%s' "$1" | awk "$_ESCAPE_AWK$_ML_QSPLIT_AWK"'
    BEGIN { buf = "" }
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        segc = ml_segment(buf, segs)
        for (si = 1; si <= segc; si++) {
            seg = segs[si]
            sub(/^[ \t]+/, "", seg)
            # Strip a leading run of VAR=val assignments and sudo/env wrappers
            # (in any order/repetition) so the REAL command word — a literal `rm`
            # OR a command-word substitution — is what we classify. `env` is
            # stripped alongside `sudo` (#72): `env $(which rm) -rf /` and
            # `env rm -rf /` must be seen as an rm command word, not shielded
            # behind the wrapper. The required trailing [ \t]+ in each sub()
            # guarantees the while loop makes progress and terminates.
            while (sub(/^[A-Za-z_][A-Za-z0-9_]*=[^ \t]*[ \t]+/, "", seg) || \
                   sub(/^sudo[ \t]+/, "", seg) || \
                   sub(/^env[ \t]+/, "", seg)) { }
            sub(/^[ \t]+/, "", seg)
            # Determine the argument tail and the token index to start scanning
            # from. Two command-word shapes emit targets:
            #   (a) a literal `rm` command word — scan from toks[2] (skip "rm").
            #   (b) a command-word *substitution* — `$(...)` or a backtick pair —
            #       in executable position (#72). The substitution result becomes
            #       the command word at run time, so `$(which rm) -rf /` never
            #       presents a literal `rm` token yet is exactly as dangerous. We
            #       deliberately do NOT try to resolve what the substitution names
            #       (`which rm`, `command -v rm`, an alias, a PATH-relative rm, … —
            #       unbounded and trivially bypassable); we key on the SHAPE
            #       (substitution in command-word position + a recursive/force
            #       flag + a protected-path target) and let the downstream
            #       protected-path check decide. A benign `$(which ls) -la /tmp`
            #       carries no recursive/force flag, so it emits no target and
            #       stays allowed — no blanket deny on command-word substitutions.
            tail = ""
            start = 0
            if (seg ~ /^rm([ \t]|$)/) {
                tail = seg
                start = 2
            } else if (substr(seg, 1, 2) == "$(") {
                # Balanced-paren skip past the substitution so an internal space
                # (e.g. `$(command -v rm)`) is NOT mis-split into a bogus
                # flag/target token. Start depth at 1 for the opening `(`.
                depth = 1
                p = 3
                L = length(seg)
                while (p <= L && depth > 0) {
                    ch = substr(seg, p, 1)
                    if (ch == "(") depth++
                    else if (ch == ")") depth--
                    p++
                }
                if (depth != 0) continue   # unterminated: emit nothing (conservative)
                tail = substr(seg, p)
                sub(/^[ \t]+/, "", tail)
                start = 1
            } else if (substr(seg, 1, 1) == "`") {
                # Backtick command word: skip to the closing backtick.
                p = 2
                L = length(seg)
                while (p <= L && substr(seg, p, 1) != "`") p++
                if (p > L) continue        # unterminated: emit nothing
                p++                         # step past the closing backtick
                tail = substr(seg, p)
                sub(/^[ \t]+/, "", tail)
                start = 1
            } else {
                continue
            }
            m = split(tail, toks, /[ \t]+/)
            has_rf = 0
            for (j = start; j <= m; j++)
                if (toks[j] ~ /^-/ && toks[j] ~ /[rRfF]/) has_rf = 1
            if (!has_rf) continue
            for (j = start; j <= m; j++) {
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


# =============================================================================
# Worktree-isolation guard toggle — default ON (rjwalters/repo#188, porting
# the BASH-TOOL WRITE CONFINEMENT category below from Loom's vendored
# guard-destructive-generic.sh, itself gated by worktree_isolation_guard_enabled()
# / guards.worktreeIsolation there). Kept as ONE switch so a repo/session that
# already opted out of Edit/Write-tool worktree confinement (a host tool layered
# on top of this guard, e.g. Loom's own guard-worktree-paths.sh) gets the
# identical decision from this guard's Bash-tool confinement below, and the
# documented escape hatch (a human/driver session that must edit the main
# checkout while worktrees exist) keeps working here too.
#
# Resolution order (highest precedence first), mirroring every other guard
# toggle in this file:
#   1. REPO_GUARD_WORKTREE_ISOLATION env var, then legacy
#      LOOM_GUARD_WORKTREE_ISOLATION (0/false/no disables, 1/true/yes forces on)
#   2. guards.worktreeIsolation via guard_cfg() — repo config wins over legacy
#      .loom (default true when absent)
#   3. Default: true (guard on)
#
# Mirrors sql_guard_enabled() / rm_scope_repo_enabled(): cached in
# _WORKTREE_ISOLATION_CACHE, invoked LAZILY — only once the cheap substring
# pre-check on the write-confinement block below has already matched — so the
# jq config read never touches the hot path for the vast majority of Bash calls
# that contain none of the recognized write idioms at all. The config read is
# best-effort: any parse failure falls through to guard-ON and never trips the
# ERR trap.
# =============================================================================
_WORKTREE_ISOLATION_CACHE=""
worktree_isolation_guard_enabled() {
    if [[ -z "$_WORKTREE_ISOLATION_CACHE" ]]; then
        local enabled=true
        # Only an explicit `false` disables (a missing guards.worktreeIsolation
        # key or a malformed config reads as unset and stays on).
        case "$(guard_cfg worktreeIsolation)" in
            false) enabled=false ;;
            true)  enabled=true ;;
        esac
        # Env override wins over config; REPO_* wins over the legacy LOOM_* name.
        case "${LOOM_GUARD_WORKTREE_ISOLATION:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        case "${REPO_GUARD_WORKTREE_ISOLATION:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _WORKTREE_ISOLATION_CACHE="$enabled"
    fi
    [[ "$_WORKTREE_ISOLATION_CACHE" == "true" ]]
}

# True if $1 (an absolute, lexically-normalized path) sits inside ANY managed
# worktree — walks up looking for the `.loom-managed` sentinel worktree.sh
# writes at every worktree root. Inline copy of walk_up_for_sentinel() in
# guard-worktree-paths.sh: kept separate rather than sourced, same rationale
# as resolve_worktree_root() mirroring worktree-root.sh above — this hook is a
# distinct process with its own self-contained fail-open contract.
_in_any_managed_worktree() {
    local dir="$1"
    [[ -n "$dir" ]] || return 1
    if [[ ! -d "$dir" ]]; then
        dir="${dir%/*}"
        [[ -z "$dir" ]] && dir="/"
    fi
    local i=0
    while [[ $i -lt 64 ]]; do
        [[ -f "$dir/.loom-managed" ]] && return 0
        [[ "$dir" == "/" ]] && break
        dir="${dir%/*}"
        [[ -z "$dir" ]] && dir="/"
        i=$((i + 1))
    done
    return 1
}

# True if at least one managed worktree currently exists under $1
# (<base>/<name>/.loom-managed, depth 2 — matches worktree.sh's layout).
# Mirrors any_managed_worktree_exists() in guard-worktree-paths.sh.
_any_managed_worktree_exists() {
    local base="$1"
    [[ -n "$base" && -d "$base" ]] || return 1
    local hit
    hit=$(find "$base" -mindepth 2 -maxdepth 2 -name '.loom-managed' -print -quit 2>/dev/null) || hit=""
    [[ -n "$hit" ]]
}

# =============================================================================
# mark_expandable_dollars() — rewrite a raw write-target token into its
# "effective path shape" (#4921).
#
# extract_write_targets() is a TOKENIZER, not a shell evaluator: a token is
# emitted with its quote characters copied verbatim (qsplit's contract) and
# with every `$…` reference unexpanded. Before the write-confinement block can
# reason about WHERE a token lands, it needs to know which `$` characters the
# real shell would actually expand — a `$` inside a SINGLE-quoted span, or one
# preceded by a backslash, is literal data (a file really named `$X`), while a
# bare or DOUBLE-quoted `$` is an expansion the guard cannot resolve.
#
# Emits (in the global _MARKED_TOKEN) the token with:
#   - quote characters removed (so `"$A"/x`, `"$A/x"` and `$A/x` all normalize
#     to the same shape and quoting cannot be used to dodge the shape tests),
#   - backslash escapes applied (the backslash dropped, the escaped character
#     kept literal),
#   - every EXPANDABLE `$` replaced by SOH (0x01) — a character no real path
#     produced by this tokenizer contains — while a LITERAL `$` stays a `$`.
#
# A global rather than a subshell echo: this runs per write target and the
# callers are already in a `while read` loop.
#
# Implemented on top of the shared scanner below so the write-confinement
# block has exactly ONE definition of "what the shell would do to these
# quotes" — a second, hand-copied quote parser is precisely how the two
# consumers would drift apart, and a drift in this grammar IS a guard bypass.
# =============================================================================
_MARKED_TOKEN=""
mark_expandable_dollars() {
    _scan_token_quoting "$1" $'\001'
    _MARKED_TOKEN="$_SCANNED_TOKEN"
}

# =============================================================================
# strip_target_quoting() — shell-accurate quote removal + backslash
# unescaping for the write-confinement absolute/relative classification
# (#4926).
#
# extract_write_targets() emits tokens with their quote characters preserved
# VERBATIM (qsplit's contract, #3755) — extract_rm_targets() and
# parse_force_ops() depend on that raw form and MUST keep receiving it
# unchanged, so this is called ONLY from the write-confinement classification
# below, never from qsplit()/extract_write_targets() themselves. Without it a
# quoted absolute path (`'/main/evil'`, `"/main/evil"`) starts with a quote
# character rather than `/`, so the `[[ … == /* ]]` check misclassifies it as
# RELATIVE and cwd-prefixes it into a location the write will never have.
# From a LINKED-WORKTREE cwd — the canonical builder setup — that fabrication
# walks straight back into the acting worktree's own `.loom-managed` sentinel
# and is silently ALLOWED, defeating the #4178 confinement check by simply
# quoting the target (the same masked-allow shape as the unresolved-`$`
# bypass fixed by #4921/#4927, reached here through quoting instead).
#
# Emits (in the global _UNQUOTED_TARGET) the token with quote characters
# removed and backslash escapes applied, and every other character —
# INCLUDING `$` — copied through unchanged: any expandable-`$` shape was
# already judged by the dedicated unresolved-`$` block above, so a file
# genuinely named `$X` or `~` (single-quoted or backslash-escaped) unquotes
# to the literal `$X`/`~` and still resolves as a plain relative path,
# exactly as today (#4382 / #4921 contracts preserved).
#
# Returns 0 when every quote in the token is balanced (the caller may use
# _UNQUOTED_TARGET). Returns 1 on an unterminated quote — the caller MUST
# then fall back to the raw, quote-preserved token, so an unbalanced quote
# can only ever keep today's verdict, never widen a deny into an allow.
# =============================================================================
# =============================================================================
# resolve_stash_cwd — the effective cwd a `git stash pop/drop/clear` runs in.
#
# Transplanted from Loom's vendored copy (loom#5173) as part of the repo#188
# parity reconciliation. Mirrors parse_force_ops' cd-tracking: threads a
# `cd <dir> &&` prefix earlier in the SAME compound command through to the
# stash invocation, so `cd <worktree> && git stash pop` — hook session cwd
# still the main repo root, the common worktree shape — resolves scope
# against the cd TARGET rather than the hook's raw session cwd.
#
# Classification uses strip_cd_quoting() so a fully or partially quoted
# absolute argument is not misclassified as relative; curcwd is still built
# from the RAW cd argument (loom#5372), because the caller unquotes a COPY
# before touching the filesystem.
#
# `git -C <path>` threading (repo#194): a `-C`/`-c` run before `stash` is
# resolved the same way `parse_force_ops` already resolves it for force ops,
# so `git -C <main-checkout> stash pop` run from a worktree cwd is caught
# against the -C target rather than the worktree cwd. See the `toks[idx] ==
# "git"` block below.
#
# `--git-dir=`/`--work-tree=` and a leading `GIT_DIR=`/`GIT_WORK_TREE=`
# assignment run (repo#202): two further shapes reach the MAIN checkout's
# stash stack undetected. `git --git-dir=<main>/.git --work-tree=<main> stash
# pop` matches the pre-check but this parser only recognised -C/-c, so it fell
# back to the raw session cwd (the worktree) and the caller saw no reason to
# ask. `GIT_DIR=<main>/.git GIT_WORK_TREE=<main> git stash pop` does not even
# start with `cd`/`git` -- an assignment token precedes it -- so `toks[1] ==
# "git"` above never fired at all. Fixed by (1) skipping a leading run of
# `VAR=val` tokens before classifying a segment, capturing GIT_DIR/
# GIT_WORK_TREE along the way, and (2) recognising --git-dir/--work-tree (both
# `=`-joined and space-separated) in the same loop that already threads -C/-c.
#
# Output contract changed from one line to three (cwd / git-dir override /
# work-tree override) so the caller can resolve --git-dir scope via
# --git-common-dir rather than reusing the -C cd-and-rev-parse path verbatim --
# --git-dir takes a .git directory, not a worktree path, and cd-ing into it
# then asking git to "rev-parse --show-toplevel" is not the same operation git
# itself performs when --git-dir/--work-tree are passed explicitly. The only
# caller of this function is the stash pre-check block below; no other
# consumer or test calls it directly, so widening the contract here is safe.
#
# repo#204 review: -C and --git-dir/GIT_DIR COMPOSE, and git applies -C first
# no matter where it sits in the argument order -- a relative --git-dir /
# GIT_DIR / --work-tree / GIT_WORK_TREE value is interpreted against the
# post--C directory even when the flag precedes -C, and even when it arrives
# as an env prefix. The first cut resolved the env-prefix pair against curcwd
# BEFORE the -C loop ran, pinning it to the pre--C directory. Raw values are
# now recorded during the loop and resolved once, after it, against the final
# process cwd. (The matching caller-side gap -- probing git for the toplevel
# with no -C at all -- is fixed in the pre-check block below.)
# =============================================================================
resolve_stash_cwd() {
    printf '%s' "$1" | awk -v startcwd="$2" -v home="$HOME" "$_ESCAPE_AWK""$_QSPLIT_AWK""$_CDEXPAND_AWK""$_CDQUOTE_AWK""$_MASKWS_AWK"'
    BEGIN { curcwd = startcwd; found = 0 }
    {
        $0 = qsplit($0)   # quote-aware segmentation
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/^sudo[ \t]+/, "", seg)
            sub(/^[ \t]+/, "", seg)
            if (seg == "") continue
            # Mask whitespace INSIDE quoted spans before tokenizing (repo#194
            # review). Splitting on raw whitespace shreds a quoted path that
            # contains a space, so a -C or cd argument like "/main dir" became
            # two tokens and resolution collapsed -- a silent allow for exactly
            # the shape this parser exists to catch. mask_ws/unmask_ws come
            # from _MASKWS_AWK; do NOT redefine them here, awk rejects a
            # duplicate function definition and the whole parser then fails
            # open.
            seg = mask_ws(seg)
            m = split(seg, toks, /[ \t]+/)
            if (m == 0) continue
            # Skip a leading run of `VAR=val` assignment tokens (repo#202) so
            # an env-prefixed invocation like `GIT_DIR=x GIT_WORK_TREE=y git
            # stash pop` still classifies past the assignments to "git" below,
            # instead of never matching toks[1] at all. Capture GIT_DIR/
            # GIT_WORK_TREE while skipping -- a later command-line
            # --git-dir/--work-tree flag on the same segment overrides these,
            # mirroring git own env-vs-flag precedence.
            envgitdir_raw = ""
            envworktree_raw = ""
            idx = 1
            while (idx <= m && toks[idx] ~ /^[A-Za-z_][A-Za-z0-9_]*=/) {
                eqpos = index(toks[idx], "=")
                vname = substr(toks[idx], 1, eqpos - 1)
                vval = substr(toks[idx], eqpos + 1)
                if (vname == "GIT_DIR") envgitdir_raw = vval
                else if (vname == "GIT_WORK_TREE") envworktree_raw = vval
                idx++
            }
            if (idx > m) continue   # nothing left but assignments
            if (toks[idx] == "cd") {
                if (idx + 1 <= m && toks[idx + 1] != "" && toks[idx + 1] != "-") {
                    cdarg = expand_cd_arg(unmask_ws(toks[idx + 1]), home)
                    cdclass = strip_cd_quoting(cdarg)
                    if (cdclass ~ /^\//) {
                        curcwd = cdarg
                    } else if (curcwd != "") {
                        curcwd = curcwd "/" cdarg
                    }
                }
                continue
            }
            # `[VAR=val ...] git [-C <path>] [-c k=v] [--git-dir(=)<path>]
            # [--work-tree(=)<path>] … stash pop|drop|clear`.
            #
            # The -C threading is repo#194: git resolves -C against the process
            # cwd and then operates there, so `git -C <main-checkout> stash pop`
            # issued from a linked worktree touches the MAIN checkout stash
            # stack while a cwd-only check sees only the worktree and allows it.
            # refs/stash is one stack shared across every linked worktree, so
            # that is a live path to destroying the WIP of another agent. This
            # mirrors the -C handling parse_force_ops already had; the asymmetry
            # was inherited from the vendored copy and documented there as a
            # known limitation rather than fixed.
            #
            # NOTE: this whole block sits inside a SINGLE-QUOTED awk program.
            # An apostrophe in a comment here terminates that string and breaks
            # the guard for every command in the repo (it happened while
            # writing this). Keep comments apostrophe-free.
            #
            # Multiple -C options compose in git (each resolved relative to the
            # previous), which is why this loops rather than reading only the
            # first. -c takes a key=value token and is skipped, not applied.
            if (toks[idx] == "git") {
                gi = idx + 1
                # proccwd is the PROCESS cwd git actually runs in: only -C
                # moves it, and -C chdirs immediately during option parsing.
                # A relative --git-dir/--work-tree (or GIT_DIR/GIT_WORK_TREE)
                # is therefore interpreted against the FINAL post--C cwd no
                # matter where it sits in the argument order (repo#204 review;
                # verified against git 2.43 for all three orders: env prefix,
                # flag-before--C, flag-after--C). So the raw values are only
                # RECORDED in this loop and resolved once the loop ends -- the
                # earlier version resolved the env-prefix pair against curcwd
                # before the -C loop ran, which pinned a relative GIT_DIR to
                # the pre--C directory.
                proccwd = curcwd
                gitdir_raw = envgitdir_raw
                worktree_raw = envworktree_raw
                have_gitdir = (envgitdir_raw != "")
                have_worktree = (envworktree_raw != "")
                while (gi <= m) {
                    if (toks[gi] == "-C" && gi + 1 <= m) {
                        gcarg = expand_cd_arg(unmask_ws(toks[gi + 1]), home)
                        gcclass = strip_cd_quoting(gcarg)
                        if (gcclass ~ /^\//) {
                            proccwd = gcarg
                        } else if (proccwd != "") {
                            proccwd = proccwd "/" gcarg
                        }
                        gi += 2
                        continue
                    }
                    if (toks[gi] == "-c" && gi + 1 <= m) { gi += 2; continue }
                    # A command-line flag overrides the env prefix, mirroring
                    # git own precedence; a later flag overrides an earlier one.
                    if (toks[gi] == "--git-dir" && gi + 1 <= m) {
                        gitdir_raw = toks[gi + 1]; have_gitdir = 1
                        gi += 2
                        continue
                    }
                    if (toks[gi] ~ /^--git-dir=/) {
                        gitdir_raw = substr(toks[gi], 11); have_gitdir = 1
                        gi += 1
                        continue
                    }
                    if (toks[gi] == "--work-tree" && gi + 1 <= m) {
                        worktree_raw = toks[gi + 1]; have_worktree = 1
                        gi += 2
                        continue
                    }
                    if (toks[gi] ~ /^--work-tree=/) {
                        worktree_raw = substr(toks[gi], 13); have_worktree = 1
                        gi += 1
                        continue
                    }
                    break
                }
                if (gi + 1 <= m && toks[gi] == "stash" && \
                    (toks[gi + 1] == "pop" || toks[gi + 1] == "drop" || toks[gi + 1] == "clear")) {
                    gitdirarg = ""
                    worktreearg = ""
                    if (have_gitdir) {
                        gdarg = expand_cd_arg(unmask_ws(gitdir_raw), home)
                        gdclass = strip_cd_quoting(gdarg)
                        gitdirarg = (gdclass ~ /^\//) ? gdarg : (proccwd != "" ? proccwd "/" gdarg : gdarg)
                    }
                    if (have_worktree) {
                        wtarg = expand_cd_arg(unmask_ws(worktree_raw), home)
                        wtclass = strip_cd_quoting(wtarg)
                        worktreearg = (wtclass ~ /^\//) ? wtarg : (proccwd != "" ? proccwd "/" wtarg : wtarg)
                    }
                    # An explicit work tree IS the directory the operation acts
                    # on, so it wins over the process cwd for the effective-cwd
                    # line; otherwise the post--C process cwd is what git infers
                    # the work tree from.
                    gitcwd = (worktreearg != "") ? worktreearg : proccwd
                    print gitcwd
                    print gitdirarg
                    print worktreearg
                    found = 1
                    exit
                }
            }
        }
    }
    END { if (!found) { print curcwd; print ""; print "" } }'
}

_UNQUOTED_TARGET=""
strip_target_quoting() {
    local rc=0
    _scan_token_quoting "$1" "" || rc=1
    _UNQUOTED_TARGET="$_SCANNED_TOKEN"
    return "$rc"
}

# =============================================================================
# _scan_token_quoting() — the single shell-accurate quote-removal /
# backslash-unescaping pass shared by the two helpers above.
#
#   $1  raw token (quote characters preserved verbatim, per qsplit's contract)
#   $2  text substituted for each EXPANDABLE `$`; empty keeps the `$` literal
#
# Sets _SCANNED_TOKEN. Returns 0 when every quote was closed, 1 when the token
# ended inside an unterminated quote (callers decide the fallback; no caller
# may treat an unterminated quote as license to widen an allow).
# =============================================================================
_SCANNED_TOKEN=""
_scan_token_quoting() {
    local tok="$1" dollar="$2"
    # Named _stq_out (not "out") to avoid colliding, in shellcheck's
    # cross-function SC2178/SC2179 heuristic, with the unrelated `local -a
    # out=()` ARRAY in normalize_abs_path() elsewhere in this file — two
    # different `local` variables in two different functions, but shellcheck
    # does not scope-isolate that particular check across sibling functions.
    local _stq_out="" c
    local n=${#tok}
    local i=0 in_s=0 in_d=0
    while [[ $i -lt $n ]]; do
        c="${tok:i:1}"
        if [[ $in_s -eq 1 ]]; then
            # Inside '…': nothing expands; only the closing quote is special.
            if [[ "$c" == "'" ]]; then in_s=0; else _stq_out+="$c"; fi
            i=$((i + 1))
            continue
        fi
        case "$c" in
            "'")
                if [[ $in_d -eq 1 ]]; then _stq_out+="$c"; else in_s=1; fi ;;
            '"')
                if [[ $in_d -eq 1 ]]; then in_d=0; else in_d=1; fi ;;
            '\')
                # Escapes the NEXT character (a trailing backslash is dropped).
                i=$((i + 1))
                [[ $i -lt $n ]] && _stq_out+="${tok:i:1}" ;;
            '$')
                if [[ -n "$dollar" ]]; then _stq_out+="$dollar"; else _stq_out+="$c"; fi ;;
            *)
                _stq_out+="$c" ;;
        esac
        i=$((i + 1))
    done
    _SCANNED_TOKEN="$_stq_out"
    [[ $in_s -eq 0 && $in_d -eq 0 ]]
}

# =============================================================================
# CD-ARGUMENT TILDE / $HOME EXPANSION (#5315)
#
# The three `cd`-tracking blocks below (extract_write_targets, parse_force_ops,
# resolve_stash_cwd) thread a `cd <dir> &&` prefix through the later segments of
# a compound command by joining <dir> onto a tracked `curcwd`. That join is a
# plain string concatenation with NO word expansion — so a `cd ~/GitHub/loom`
# prefix was joined VERBATIM, embedding a literal `~` mid-path
# (`.../loom/~/GitHub/loom/...`) and mis-resolving every later relative write /
# force-op / stash target of that command (the false positive reported in
# #5315). Only a leading `/` (already-absolute) was handled specially; a leading
# `~` or `$HOME` fell through to the plain repo-relative join.
#
# expand_cd_arg() performs the SAME narrow, unambiguous slice of shell word
# expansion the bash-side expand_leading_tilde() (#4382) already applies to
# write TARGETS, but for the cd ARGUMENT and inside awk (which the write-target
# helper runs too late to reach). `home` is the guard process's own $HOME,
# passed in via `-v home=...` exactly like expand_leading_tilde() reads the
# guard's process $HOME — a same-line `HOME=<x> cmd` prefix in the analyzed
# command text can never redefine it. Handled here:
#   ~            -> home
#   ~/rest       -> home "/rest"
#   $HOME        -> home
#   $HOME/rest   -> home "/rest"
# An expanded value starts with `/`, so the caller's existing `~ /^\//` branch
# then treats it as an ABSOLUTE curcwd (correct — `cd ~` replaces the cwd, it is
# not appended to it).
#
# Left DELIBERATELY UNEXPANDED (returned unchanged, so the caller joins it
# repo-relative — the fail-CLOSED direction this file always biases toward, and
# the same convention already used for `cd -` / a bare `cd`):
#   ~user, ~user/rest   awk cannot safely resolve another user's home (no
#                       getent/dscl without a shell-injection surface); leaving
#                       it repo-relative keeps a genuinely-out-of-tree write
#                       classified as in-tree (denied) rather than guessing it
#                       safe. See the #5315 DECISION note at the head of
#                       extract_write_targets() for the ~user/EPHEMERAL rationale.
# Because qsplit() copies a quoted span VERBATIM (including its quote chars) and
# leaves a literal backslash untouched, a token the real shell would NOT expand
# does not start with a bare `~`/`$HOME` here and falls through unchanged —
#   '~/x' / "~/x"  -> starts with a quote char (shell never tilde-expands it)
#   \~/x           -> starts with a backslash (shell never tilde-expands it)
#   foo~/x         -> tilde is not leading (not an expansion position)
# mirroring expand_leading_tilde()'s quoted-tilde treatment exactly. If `home`
# is empty (HOME unset) every case falls through unchanged, matching that
# helper's `[[ -n "$HOME" ]]` guard.
#
# Shared as a single awk source string (like _QSPLIT_AWK) so the three
# cd-tracking blocks cannot drift.
# =============================================================================
_CDEXPAND_AWK='
function expand_cd_arg(tok, home) {
    if (home == "") return tok
    if (tok == "~") return home
    if (tok == "$HOME") return home
    if (substr(tok, 1, 2) == "~/") return home substr(tok, 2)
    if (substr(tok, 1, 6) == "$HOME/") return home substr(tok, 6)
    return tok
}
'

# =============================================================================
# strip_cd_quoting() (#5363) — full quote-removal absolute/relative
# CLASSIFICATION helper for a tracked `cd` argument. Used by the three
# `cd`-tracking awk blocks in this file — extract_write_targets() (the
# write-confinement hard-deny path), parse_force_ops(), and
# resolve_stash_cwd() (the latter two feed the ask-gate for
# force-push/reset-hard branch identity and stash-scope cwd resolution,
# wired up in #5372) — NEVER on the RAW cdarg threaded into curcwd itself
# (see each call site's own comment for why).
#
# The #4933/#4941 fix (cdqc/cdlen leading-and-matching-trailing-quote strip)
# only recognizes a FULLY quoted argument ('/abs/path', "/abs/path"): it peels
# one leading quote character and, if the LAST character of the token is the
# SAME quote character, one trailing one. A PARTIALLY quoted absolute
# argument -- the quote closes mid-token, e.g. '<main>'/defaults -- still
# starts with a quote character, so it fails that narrow test and falls
# through unchanged, still starting with a quote rather than `/`, and is
# misclassified as RELATIVE -- the same masked-allow shape as #4933/#4926,
# reached through a partially-quoted `cd` argument instead of a fully-quoted
# or unquoted one (#5363).
#
# strip_cd_quoting() instead walks the ENTIRE token character-by-character,
# stripping every quote character (both single- and double-quoted spans, with
# ordinary shell nesting: a `"` is literal data inside a `'...'` span and vice
# versa) rather than only a leading/trailing pair -- so '<main>'/defaults
# correctly unquotes to <main>/defaults, which DOES start with `/`, and
# classifies as absolute. This mirrors (but, being pure awk, cannot literally
# share code with) the shell layer's _scan_token_quoting() used by
# strip_target_quoting() for the write-TARGET side (#4926) -- that scanner is
# unreachable from here because this decision is made entirely inside awk,
# before the shell layer ever sees a token. Backslash-escapes and `$` are
# deliberately left untouched (out of scope for a leading-`/` classification
# test, and the existing unresolved-`$` detector downstream,
# mark_expandable_dollars()/#4921, still needs the RAW curcwd this function
# never touches).
#
# Returns the token UNCHANGED whenever a quote is left open at end-of-token
# (in_s or in_d still true) -- an unbalanced/unterminated quote can therefore
# only ever KEEP today's classification, never flip a relative-looking token
# into an absolute one it never proved (same fallback contract as
# strip_target_quoting()/#4926 and the #4933 leading/trailing strip it
# replaces here).
# =============================================================================
_CDQUOTE_AWK='
function strip_cd_quoting(tok,   out, n, i, c, in_s, in_d, sq, dq) {
    sq = sprintf("%c", 39)
    dq = sprintf("%c", 34)
    out = ""
    n = length(tok)
    in_s = 0
    in_d = 0
    for (i = 1; i <= n; i++) {
        c = substr(tok, i, 1)
        if (in_s) {
            if (c == sq) { in_s = 0 } else { out = out c }
            continue
        }
        if (c == sq) {
            if (in_d) { out = out c } else { in_s = 1 }
            continue
        }
        if (c == dq) {
            if (in_d) { in_d = 0 } else { in_d = 1 }
            continue
        }
        out = out c
    }
    if (in_s || in_d) return tok
    return out
}
'

# =============================================================================
# QUOTE-AWARE REDIRECTION MASKING (#4245)
#
# extract_write_targets() (below) recognizes `>`/`>>` redirection by splitting
# a qsplit()-segmented command on whitespace and pattern-matching each token —
# but that whitespace split is NOT quote-aware, so a `>` that is DATA inside a
# quoted argument (e.g. `gh issue create --body "... env > config > default
# ..."`) can land in its own whitespace-bounded "token" and be misread as a
# real redirection operator, manufacturing a phantom write target and denying
# a command that writes nothing to the filesystem (#4245; same failure class
# as the #3755 qsplit()/#3679 strip_literal_text() quoting fixes above).
#
# mask_gt() walks the string tracking quote state (single-quoted,
# double-quoted, unquoted) and replaces every `>` found INSIDE a quoted span
# with SOH (0x01, a character that can never appear in a shell command and so
# can never itself be mis-split into a phantom target). It otherwise returns
# the input UNCHANGED byte-for-byte (same length, same whitespace positions),
# so a caller can split both the original and the masked string on whitespace
# and get IDENTICAL token boundaries — the masked tokens are used only to
# DECIDE whether a token is a real (unquoted) redirection operator; the
# ORIGINAL tokens are still used to extract the actual target text.
#
# Deliberately does NOT model backslash-escaped quotes -- same simplification
# qsplit() (above) and strip_literal_text() already accept for this file's
# other quote-tracking scans, and for good reason beyond just consistency: the
# input mask_gt() actually receives (COMMAND_ASK_SCAN, see extract_write_targets()
# below) has typically already been through strip_literal_text()'s OWN
# escape-blind redaction, which can shift a quote's effective position (e.g. an
# escaped `\"` inside a redacted --body value loses its backslash, since the
# redaction's own quote-matching stops at the first bare `"` it finds). Layering
# a stricter, escape-AWARE scan on top of that already-escape-blind text would
# only desynchronize the two passes' quote parity -- worse, in the wrong
# direction (masking too little). Matching qsplit()'s exact toggle-on-every-
# quote-char behavior keeps both passes' parity in agreement. Same accepted
# risk direction as qsplit(): pathological unbalanced-quote input could in
# theory shift parity and mis-mask a genuine unquoted `>`, but that is the same
# best-effort risk this file already accepts for `;|&` segmentation -- never a
# NEW risk introduced here. An unterminated quote (no matching close before
# end-of-string) just runs to the end of the string in that quote state --
# never crashes, never mis-indexes.
# =============================================================================
_MASKGT_AWK='
function mask_gt(s,   out, n, i, c, mode, SQ, DQ, MASK) {
    SQ = sprintf("%c", 39)    # single quote
    DQ = sprintf("%c", 34)    # double quote
    MASK = sprintf("%c", 1)   # SOH -- placeholder for a quoted ">" (never a real char)
    out = ""
    n = length(s)
    i = 1
    mode = 0   # 0 = unquoted, 1 = single-quoted, 2 = double-quoted
    while (i <= n) {
        c = substr(s, i, 1)
        if (mode == 0) {
            if (c == SQ) { mode = 1; out = out c; i++; continue }
            if (c == DQ) { mode = 2; out = out c; i++; continue }
            out = out c
            i++
            continue
        }
        if (mode == 1) {
            # Single-quoted: only the matching quote ends the span.
            if (c == SQ) { mode = 0; out = out c; i++; continue }
            out = out (c == ">" ? MASK : c)
            i++
            continue
        }
        # mode == 2 (double-quoted): only the matching quote ends the span.
        if (c == DQ) { mode = 0; out = out c; i++; continue }
        out = out (c == ">" ? MASK : c)
        i++
    }
    return out
}
'

# =============================================================================
# QUOTE-AWARE WHITESPACE MASKING (#4934)
#
# extract_write_targets() (below) recognizes each write-idiom argument by
# `split(seg, toks, /[ \t]+/)` — plain whitespace splitting, NOT quote-aware
# (documented as a known limitation at extract_write_targets()'s own header).
# A quoted target containing a literal space, e.g.
#   echo x > '/main/checkout/evil file.sh'
# is therefore split into TWO tokens (`'/main/checkout/evil` and `file.sh'`).
# Only the first fragment is ever used as the write target. That fragment
# starts with a quote character (not `/`), so it is misclassified as a
# RELATIVE path (strip_target_quoting() correctly reports the dangling quote
# as unbalanced and falls back to the raw fragment per #4926's "never widen a
# deny into an allow" contract -- but the fallback fragment itself still gets
# cwd-joined and can land INSIDE the acting worktree, turning what should be
# a main-checkout DENY into an ALLOW (#4934).
#
# mask_ws() is the same masking technique as mask_gt() above (#4245), applied
# to whitespace instead of `>`: it walks the string tracking quote state and
# replaces every space/tab found INSIDE a quoted span with a placeholder
# character that can never appear in real shell text (STX 0x02 for a masked
# space, ETX 0x03 for a masked tab), so `split(seg, toks, /[ \t]+/)` never
# splits inside a quoted span -- a quoted path containing spaces now yields
# exactly ONE token. unmask_ws() reverses the substitution so the token's
# TEXT is unchanged (real spaces/tabs restored) once splitting is done; only
# the whitespace bytes INSIDE quotes are ever touched, so mask_ws() (like
# mask_gt()) returns a byte-for-byte-length-identical string with identical
# non-whitespace content, which is what keeps the `>`-detection pass (mtoks[],
# masked via mask_gt() on top of mask_ws()'s output) in lockstep with the
# target-text pass (toks[], unmasked back to real whitespace): the two are
# always split into the SAME number of tokens at the SAME boundaries, because
# mask_gt() only ever changes `>` bytes, never whitespace-ness.
#
# Deliberately does NOT model backslash-escaped quotes or attempt look-ahead
# for a terminating quote — same simplification qsplit()/mask_gt() already
# accept (see mask_gt()'s comment above for the accepted-risk rationale). An
# unterminated quote just runs to the end of the string in that quote state;
# never crashes, never mis-indexes, and never widens a deny into an allow
# (the SAME fallback direction qsplit()'s own unterminated-quote handling
# already uses, #4926).
#
# This is scoped ONLY to extract_write_targets() -- qsplit() itself (and its
# verbatim-quote-preservation contract depended on by extract_rm_targets() /
# parse_force_ops()) is untouched.
# =============================================================================
_MASKWS_AWK='
function mask_ws(s,   out, n, i, c, mode, SQ, DQ, SPMASK, TABMASK) {
    SQ = sprintf("%c", 39)    # single quote
    DQ = sprintf("%c", 34)    # double quote
    SPMASK = sprintf("%c", 2)    # STX -- placeholder for a quoted space
    TABMASK = sprintf("%c", 3)   # ETX -- placeholder for a quoted tab
    out = ""
    n = length(s)
    i = 1
    mode = 0   # 0 = unquoted, 1 = single-quoted, 2 = double-quoted
    while (i <= n) {
        c = substr(s, i, 1)
        if (mode == 0) {
            if (c == SQ) { mode = 1; out = out c; i++; continue }
            if (c == DQ) { mode = 2; out = out c; i++; continue }
            out = out c
            i++
            continue
        }
        if (mode == 1) {
            # Single-quoted: only the matching quote ends the span.
            if (c == SQ) { mode = 0; out = out c; i++; continue }
            if (c == " ") { out = out SPMASK; i++; continue }
            if (c == "\t") { out = out TABMASK; i++; continue }
            out = out c
            i++
            continue
        }
        # mode == 2 (double-quoted): only the matching quote ends the span.
        if (c == DQ) { mode = 0; out = out c; i++; continue }
        if (c == " ") { out = out SPMASK; i++; continue }
        if (c == "\t") { out = out TABMASK; i++; continue }
        out = out c
        i++
    }
    return out
}
function unmask_ws(s) {
    gsub(sprintf("%c", 2), " ", s)
    gsub(sprintf("%c", 3), "\t", s)
    return s
}
'

# =============================================================================
# HEREDOC-BODY MASKING (#5000)
#
# extract_write_targets() (below) is fed the RAW, un-redacted value of a
# --body/-m/--title/--notes/--comment flag whenever strip_literal_text()'s own
# `$(`/backtick safety floor (#3679) declines to redact it -- the common
# real-world trigger being a heredoc-wrapped value, `--body "$(cat <<'EOF'
# ... EOF)"`, this repo's OWN recommended idiom (see CLAUDE.md/builder role)
# for any multi-line/special-character body text. A `>` (or `;`, `&`, `|`, or
# a write-idiom command word like `tee`) sitting on a heredoc BODY line is
# inert DATA *to the OUTER shell* -- the outer shell never shell-parses a
# heredoc body for redirection/separator syntax, regardless of whether its
# delimiter is quoted (see KNOWN LIMITATIONS below for two narrow cases where
# that is not the end of the story) -- but qsplit()/mask_gt()/mask_ws() are
# (like awk itself) driven one
# PHYSICAL LINE at a time, with no memory of the `"` opened several lines
# earlier once a later heredoc-body line is reached, so a write-idiom-looking
# byte on such a line was misread as real shell syntax, manufacturing a
# phantom write target and denying a command that writes nothing to the
# filesystem (#5000; same failure family as #4245/#3679, one line-boundary
# narrower).
#
# mask_heredoc_bodies() sidesteps that per-line memory gap entirely rather
# than teaching qsplit()/mask_gt()/mask_ws() cross-line state -- those three
# are SHARED with extract_rm_targets()/parse_force_ops()/
# lifecycle_or_cloud_reason(), out of THIS fix's scope (#5000's own Affected
# Files list names only strip_literal_text() and extract_write_targets()/
# mask_gt()). It walks the WHOLE (possibly multi-line) buffer ONCE, looking
# for a `<<`/`<<-` heredoc opener whose delimiter is a bare or single/double
# -quoted identifier (`EOF`, `'EOF'`, `"EOF"`, ... -- the near-universal
# real-world shape), then replaces every byte of the BODY -- every full line
# strictly between the opener line and the first following line that is
# exactly (barring `<<-`'s permitted leading tabs) the bare delimiter -- with
# a neutral placeholder byte (ETB, 0x17: never meaningful shell syntax, never
# matched by any pattern elsewhere in this file). Real newlines, the opener
# line, and the delimiter line itself are all left untouched, so line counts
# and the surrounding qsplit()/strip_literal_text() `$(`-floor logic are
# unaffected -- ONLY the inert body content disappears.
#
# MASK ONLY A *CLOSED* BLOCK (#5087 -- the fail-open regression this two-pass
# structure exists to prevent). The masking decision is made per candidate
# opener with the closing-delimiter line ALREADY LOCATED: for each candidate
# on a line, a forward scan looks for the terminating bare-delimiter line
# FIRST, and only a block that is genuinely closed inside this buffer has its
# body masked. A candidate whose delimiter line never appears masks NOTHING
# and the scan simply moves on to the next candidate -- because the original
# single-pass form (which flipped a sticky `inbody` flag the instant it saw
# `<<` and only ever cleared it on a delimiter line) silently masked
# EVERYTHING from a FALSE opener to the end of the command whenever no such
# line followed, swallowing any real `>`/`tee`/`cp`/`mv` target after it and
# defeating the write-confinement guard (#4178) outright. Two ordinary,
# heredoc-free command shapes hit that: a quoted string that merely CONTAINS
# `<<TOKEN` (`echo "test <<TOKEN"`), and an arithmetic bitshift
# (`x=$((1 << 3))`) -- both followed by a genuine out-of-worktree write, both
# ALLOWed pre-#5087 where `main` DENIed. Masking is a NARROWING operation, so
# it must never be applied speculatively: when in doubt, mask nothing and let
# the text flow through the pre-#5000 per-line scan.
#
# Opener detection is correspondingly tightened so the commonest false
# openers are rejected before the forward scan even runs: a `<<<` herestring
# is never a heredoc opener, and a BARE (unquoted) delimiter starting with a
# DIGIT is read as an arithmetic shift operand (`1 << 3`), not a delimiter --
# `<<'3'`/`<<"3"` stay recognized, since an explicitly quoted delimiter is
# unambiguous heredoc intent. Every `<<` occurrence on a line is considered
# in turn (not just the first), so one rejected candidate never hides a real
# terminated heredoc later on the same line.
#
# Deliberately narrow / best-effort, consistent with every other quote-
# tracking scan in this file: an exotic delimiter (not a bare identifier, or
# using shell metacharacters) is simply not recognized as a heredoc opener --
# fail-open for THIS masking pass only, never a NEW risk, since the text then
# flows through the pre-#5000 per-line scan exactly as it always has. Never
# denies by itself; only ever narrows what extract_write_targets() can find,
# matching that scanner's own documented fail-open contract (a missed target
# is the accepted safe direction there) -- a real write-idiom byte OUTSIDE
# any recognized heredoc body, even in the SAME multi-line command, is
# completely unaffected and still flows through unchanged.
#
# KNOWN LIMITATIONS (#5117 -- surfaced during Judge re-review of #5085, left
# in place deliberately rather than folded into that fix):
#
#   1. Interpreter-fed heredocs -- CLOSED for heredocs (#5351), broader
#      interpreter-mediated writes still open. "Inert to the outer shell"
#      (above) is NOT the same as "inert, full stop." When the heredoc body IS
#      the script handed to an interpreter -- `bash <<'EOF' ... EOF`,
#      `cat <<'EOF' ... EOF | bash`, `sh -s <<'EOF' ... EOF` -- a write-idiom
#      line inside that body is genuinely live code to the INNER interpreter,
#      even though the outer shell never parses it as redirection/separator
#      syntax. Plain mask_heredoc_bodies() masks it anyway, so a write that
#      `origin/main`'s single-pass scan correctly caught would be missed.
#      ORIGINAL DECISION (#5117): deferred -- extract_write_targets() kept
#      calling plain mask_heredoc_bodies() and masked interpreter-fed bodies,
#      an accepted ask-tier tradeoff (missed ASK, worst case), while #5198
#      introduced mask_heredoc_bodies_selective() for the CATASTROPHIC tier
#      only (masking an interpreter-fed body there flips a DENY to an ALLOW on
#      the #4523/#4601/#4685 data-loss shape -- never acceptable).
#      UPDATED DECISION (#5351): the deferral no longer stands. The catastrophic
#      tier proved the approach, so extract_write_targets() now ALSO calls
#      mask_heredoc_bodies_selective() (see its END block below) -- both tiers
#      share the same interpreter-aware masking. _selective() recognizes an
#      interpreter-fed opener and leaves that block's body VISIBLE to the scan
#      (so a write into the main checkout inside a `bash <<'EOF' ... EOF` body
#      now DENYs from a managed worktree), while still masking every
#      non-interpreter-fed heredoc in the same command -- so the #4914/#5000/
#      #5181 false-positive fixes (an inert `cat`-body / `--body "$(cat <<'EOF'
#      ... EOF)"` sink) stay intact.
#      STILL OPEN (its own follow-up, NOT closed here): the BROADER,
#      heredoc-independent class of interpreter-mediated writes -- `bash -c
#      '... > f'`, `printf ... | bash`, `dd of=f`, `install -m ... f` -- which
#      extract_write_targets(), a command-word-based scanner, still does not
#      cover regardless of heredocs. Closing that (spotting an inner interpreter
#      invocation and recursively re-scanning its script/stdin argument) is a
#      materially larger, separate piece of work than the heredoc masking pass
#      and is deliberately out of scope for #5351.
#
#   2. Crafted false opener whose delimiter later appears. Opener detection
#      (heredoc_delim_at()) runs on a single physical line, before qsplit()
#      -- it cannot know a `<<TOKEN` substring actually sits inside a quoted
#      string on that line (e.g. `echo "test <<EOF" > /etc/passwd`). If a
#      later line in the SAME buffer happens to equal the bare delimiter
#      (`EOF`) for unrelated reasons, PASS 1 finds it and PASS 2 masks every
#      line in between -- even though real bash treats the whole `<<EOF`
#      substring as quoted text (no heredoc at all) and executes the write
#      immediately. `origin/main` denies this; this masking pass ALLOWs it.
#      This is explicitly NOT fixed here: doing so would require teaching
#      heredoc_delim_at() the same quote state qsplit() tracks, and
#      qsplit()/mask_gt()/mask_ws() are SHARED with extract_rm_targets()/
#      parse_force_ops()/lifecycle_or_cloud_reason() -- exactly the
#      cross-function coupling #5000 deliberately avoided by giving
#      mask_heredoc_bodies() its own single, whole-buffer pre-pass instead of
#      threading state through the shared per-line scanners. A structural
#      fix belongs in its own issue, scoped against that coupling risk, not
#      folded in here.
# =============================================================================
_MASKHEREDOC_AWK='
# Return the heredoc delimiter opened by the `<<` at byte offset p in line,
# or "" when that `<<` is not a recognized heredoc opener.
function heredoc_delim_at(line, p,   start, qc, c, wordend, d, SQ, DQ) {
    SQ = sprintf("%c", 39)    # single quote
    DQ = sprintf("%c", 34)    # double quote
    start = p + 2
    # `<<<` is a herestring, never a heredoc opener.
    if (substr(line, start, 1) == "<") return ""
    if (substr(line, start, 1) == "-") start++
    while (substr(line, start, 1) == " " || substr(line, start, 1) == "\t") start++
    qc = ""
    c = substr(line, start, 1)
    if (c == SQ || c == DQ) { qc = c; start++ }
    wordend = start
    while (1) {
        c = substr(line, wordend, 1)
        if (c ~ /^[A-Za-z0-9_]$/) { wordend++; continue }
        break
    }
    if (wordend <= start) return ""
    d = substr(line, start, wordend - start)
    # A BARE delimiter starting with a digit is an arithmetic shift operand
    # (`$((1 << 3))`) far more often than a real heredoc delimiter. A quoted
    # one (`<<"3"`) is unambiguous heredoc intent, so it stays recognized.
    if (qc == "" && d ~ /^[0-9]/) return ""
    return d
}
function mask_heredoc_bodies(s,   out, lines, nl, i, j, line, trimmed, body, delim, closeat, p, off, MASKC) {
    MASKC = sprintf("%c", 23) # ETB -- placeholder for inert heredoc-body text
    nl = split(s, lines, "\n")
    if (nl == 0) return ""
    for (i = 1; i <= nl; i++) {
        line = lines[i]
        off = 1
        # Consider every `<<` on this line, left to right, until one is
        # confirmed to open a CLOSED heredoc block.
        while (1) {
            p = index(substr(line, off), "<<")
            if (p == 0) break
            p = off + p - 1        # absolute offset of `<<` within line
            off = p + 2            # where the next candidate search resumes
            delim = heredoc_delim_at(line, p)
            if (delim == "") continue
            # PASS 1 -- locate the terminating delimiter line. A `<<-` opener
            # permits (and strips) leading tabs on the delimiter line; only
            # leading TABS (never spaces) are ever stripped, per real heredoc
            # semantics. Stripping them unconditionally can only terminate the
            # block EARLIER, i.e. mask LESS -- the safe direction here.
            closeat = 0
            for (j = i + 1; j <= nl; j++) {
                trimmed = lines[j]
                sub(/^\t+/, "", trimmed)
                if (trimmed == delim) { closeat = j; break }
            }
            # Unterminated / false opener: mask NOTHING for this candidate and
            # keep looking. Everything after it stays visible to the caller,
            # exactly as in the pre-#5000 per-line scan (#5087).
            if (closeat == 0) continue
            # PASS 2 -- only now that the block is known to be closed, mask
            # the body lines strictly between opener and delimiter line.
            for (j = i + 1; j < closeat; j++) {
                body = lines[j]
                gsub(/./, MASKC, body)
                lines[j] = body
            }
            i = closeat            # resume scanning after the delimiter line
            break
        }
    }
    out = lines[1]
    for (i = 2; i <= nl; i++) out = out "\n" lines[i]
    return out
}
# True when a heredoc OPENER line looks like it feeds an interpreter --
# either the opener command itself (`bash <<EOF`, `sh -s <<EOF`,
# `python3 <<EOF`, `eval <<EOF`, `source <<EOF`, `. <<EOF`) or the opener is
# piped into one on the same line (`cat <<EOF | bash`). Deliberately a
# whole-line, best-effort check (matching the "narrow / best-effort" style
# of heredoc_delim_at() above): the interpreter must be the COMMAND WORD of
# some segment of the line, not an arbitrary substring -- e.g.
# `echo "installs bash" <<EOF` does NOT match, since "bash" there is a bare
# argument, not the command word.
#
# Recognizing the command word robustly (#5205, widened #5226): the
# interpreter word is matched against the path BASENAME of each segment
# command word, after normalizing away the shell decorations that do not
# change what actually executes --
#   * quoting / backslash-escaping of the command word itself
#     (`"bash" <<EOF`, `\bash <<EOF` -- the classic alias-dodge idiom),
#   * a bare `VAR=value` assignment prefix (`LC_ALL=C bash <<EOF`),
#   * a leading wrapper command with its own flags, assignments and
#     numeric/duration operands (env, command, exec, builtin, sudo, doas,
#     nohup, setsid, nice, ionice, stdbuf, timeout, time, xargs, unbuffer).
# So `/bin/bash <<EOF`, `env bash <<EOF`, `LC_ALL=C bash <<EOF`,
# `sudo bash <<EOF`, `cat <<EOF | sudo bash`, `timeout 60 bash <<EOF`,
# `"bash" <<EOF`, `\bash <<EOF` and `/usr/bin/python3 <<EOF` all resolve to
# the same real interpreter and are caught, closing the evasion class where
# those forms slipped past the older first-token-only / unwrapped checks and
# got their live bodies silently masked (i.e. ALLOWed).
#
# FAIL-CLOSED TAIL (#5226): an interpreter allowlist is an unbounded tail --
# there is always one more wrapper. The residual class no allowlist can ever
# enumerate is a command word the guard cannot resolve to a NAME at all:
# `$SHELL <<EOF`, `${INTERP} <<EOF`, `$(which bash) <<EOF`. Those are treated
# as interpreter-fed, so the body stays visible and the catastrophic-tier
# check still sees any live invocation inside it.
#
# Inverting the whole test (mask ONLY for a known-inert SINK allowlist, so
# every unknown opener fails closed) was considered and deliberately
# rejected: the canonical Loom issue-filing idiom is
# `create-issue.sh --title T --body "$(cat <<EOF ... EOF)"`, whose command
# word is an ordinary repo script -- as is every other repo wrapper around a
# forge call. Under a sink allowlist each of those hard-stalls on a
# catastrophic-tier deny the moment the prose it carries quotes the
# anti-pattern, which is precisely the #5181 false positive this masking
# exists to fix (that bug was found when an agent could not file the report
# about it). So the default for a resolvable-but-unknown command word stays
# "mask", and only unresolvable words fail closed.
function _interp_basename(tok,   base, SQ, DQ) {
    # Reduce a (possibly quoted, backslash-escaped, path-qualified) command
    # word to its basename: quotes and backslashes removed, then the text
    # after the last `/`. `/bin/bash` -> `bash`, `./bash` -> `bash`,
    # `/usr/bin/python3` -> `python3`, `"bash"` -> `bash`, `\bash` -> `bash`,
    # `"/bin/bash"` -> `bash`; a bare `bash` or `.` is unchanged. Stripping
    # quotes/backslashes ANYWHERE in the word (not just at its edges) also
    # collapses the `b"a"sh` / `b\ash` splitting idioms, which the shell
    # resolves to that same command.
    SQ = sprintf("%c", 39)    # single quote
    DQ = sprintf("%c", 34)    # double quote
    base = tok
    gsub(SQ, "", base)
    gsub(DQ, "", base)
    gsub(/\\/, "", base)
    sub(/^.*\//, "", base)
    return base
}
function is_interpreter_opener(line,   n, segs, i, seg, m, toks, j, base) {
    # Split into command segments on ; & | (covers && and || too) so a piped
    # or chained interpreter is caught in ANY position, e.g. `cat <<EOF | bash`
    # and `cat <<EOF | sudo bash`.
    n = split(line, segs, /[;&|]+/)
    for (i = 1; i <= n; i++) {
        seg = segs[i]
        sub(/^[ \t]+/, "", seg)
        m = split(seg, toks, /[ \t]+/)
        if (m == 0) continue
        j = 1
        # (1) Strip a BARE `VAR=value` assignment prefix. This is ordinary
        # shell with no `env` in front (`LC_ALL=C bash <<EOF`) and is the
        # most common prefix in practice; before #5226 it fell straight
        # through, because assignments were only skipped AFTER an
        # env/command/exec/builtin token had already been seen.
        while (j <= m && toks[j] ~ /^[A-Za-z_][A-Za-z0-9_]*=/) j++
        # (2) Strip leading wrapper commands that do not change what runs,
        # each followed by its own -flags, `VAR=value` assignments and
        # numeric/duration operands (`timeout 60`, `timeout 1.5h`,
        # `nice -n 10`, `ionice -c 2`), then re-check for another wrapper.
        # The wrapper word goes through _interp_basename() too, so
        # `/usr/bin/sudo` and `\sudo` strip exactly like a bare `sudo`.
        while (j <= m) {
            base = _interp_basename(toks[j])
            if (base ~ /^(env|command|exec|builtin|sudo|doas|nohup|setsid|nice|ionice|stdbuf|timeout|time|xargs|unbuffer)$/) {
                j++
                while (j <= m && (toks[j] ~ /^-/ || toks[j] ~ /^[A-Za-z_][A-Za-z0-9_]*=/ || toks[j] ~ /^[0-9]+([.][0-9]+)?[smhd]?$/)) j++
                continue
            }
            break
        }
        if (j > m) continue
        base = _interp_basename(toks[j])
        if (base ~ /^(bash|sh|zsh|dash|ksh|python[0-9.]*|perl|ruby|node|nodejs|eval|source|\.)$/)
            return 1
        # (3) Fail CLOSED on a command word that resolves to no name at all --
        # a variable / command substitution, or an empty word. See the
        # FAIL-CLOSED TAIL note above: resolvable-but-unknown command words
        # (`cat`, `tee`, a repo script) keep masking, per #5181.
        if (base == "" || base ~ /[$`]/)
            return 1
    }
    return 0
}
# Same closed-block detection as mask_heredoc_bodies(), but SKIPS masking
# (leaves the body visible) for any block whose opener is interpreter-fed
# per is_interpreter_opener() -- see KNOWN LIMITATIONS #1 above. Used by BOTH
# tiers: the gh-api-rawfield-body-literal-at catastrophic check (#5198) and,
# as of #5351, the extract_write_targets() ask-tier write-confinement scan (the
# END-block call below) -- so a write into the main checkout inside an
# interpreter-fed heredoc body is no longer masked out of the confinement
# check. Plain mask_heredoc_bodies() above is retained as the reference
# primitive (identical minus the interpreter carve-out) but now has no
# runtime caller.
function mask_heredoc_bodies_selective(s,   out, lines, nl, i, j, line, trimmed, body, delim, closeat, p, off, MASKC) {
    MASKC = sprintf("%c", 23) # ETB -- placeholder for inert heredoc-body text
    nl = split(s, lines, "\n")
    if (nl == 0) return ""
    for (i = 1; i <= nl; i++) {
        line = lines[i]
        off = 1
        while (1) {
            p = index(substr(line, off), "<<")
            if (p == 0) break
            p = off + p - 1
            off = p + 2
            delim = heredoc_delim_at(line, p)
            if (delim == "") continue
            closeat = 0
            for (j = i + 1; j <= nl; j++) {
                trimmed = lines[j]
                sub(/^\t+/, "", trimmed)
                if (trimmed == delim) { closeat = j; break }
            }
            if (closeat == 0) continue
            if (!is_interpreter_opener(line)) {
                for (j = i + 1; j < closeat; j++) {
                    body = lines[j]
                    gsub(/./, MASKC, body)
                    lines[j] = body
                }
            }
            i = closeat            # resume scanning after the delimiter line
            break
        }
    }
    out = lines[1]
    for (i = 2; i <= nl; i++) out = out "\n" lines[i]
    return out
}
'

# =============================================================================
# extract_write_targets() — Bash-tool write-idiom target extraction (#4178).
#
# Emits one "<cwd>\t<target>" line (TAB-separated, US separator 0x1f — mirrors
# parse_force_ops' SEP convention) per recognized write idiom found in $1:
#   - `>` / `>>` redirection, bare or fd-prefixed (`2>file`), attached
#     (`>file`) or spaced (`> file`). A dup-to-fd form (`>&1`, `2>&1`) is
#     recognized and EXCLUDED — it never writes a file.
#   - `tee <file>...`            — every non-flag argument is a target.
#   - `sed -i ... <script> <file>...` — only when an -i/-i* flag is present;
#     the FIRST non-flag argument is assumed to be the sed script, the rest
#     are file targets. Exactly one non-flag argument is genuinely ambiguous
#     (could be an -f scriptfile with no positional file yet) and is SKIPPED
#     rather than guessed — allow on uncertainty, never deny on uncertainty.
#   - `cp` / `mv ... <dest>`     — the LAST non-flag argument (the common
#     `cp/mv src... dest` shape).
#
# In the three idiom scans above (NOT the `>`/`>>` scan, which has its own
# operator detection), a `<` stdin redirection is recognized and EXCLUDED
# (#5369): neither the operator token (`<`, `0<`, `</path`) nor the file a
# bare `<` reads FROM is a write target. Skipping it fixes both a false DENY
# (phantom `<repo>/<` targets on `tee f < in`) and, for `cp`/`mv` — whose
# destination is the LAST non-flag token — a false ALLOW where a trailing
# `< in` displaced the real destination. See the inline comment at the scan.
#
# $2 seeds the starting cwd. A `cd <path>` segment updates cwd for LATER
# segments of the SAME command (so `cd <worktree> && echo x > f` resolves the
# relative target against the worktree, not the hook's cwd) — global awk
# variable `curcwd`, threaded across the per-line pattern-action block exactly
# like parse_force_ops threads `cpath` via `git -C`.
#
# NOT a full shell parser: like parse_force_ops / extract_rm_targets, splitting
# a segment into tokens starts from plain whitespace splitting. Unlike those
# two, a QUOTED argument containing a literal space is NOT mis-split here: the
# split runs against mask_ws()'s output (#4934), which replaces whitespace
# INSIDE a quoted span with a non-whitespace placeholder before
# `split(seg, toks, /[ \t]+/)` runs, so a quoted path with an embedded space
# (e.g. `echo x > '/main/checkout/evil file.sh'`) yields exactly ONE token —
# unmask_ws() restores the real whitespace bytes in that token afterward. The
# `>`/`>>` redirection scan below is quote-aware in a second, independent way
# (#4245) via mask_gt() — a `>` inside a quoted argument (e.g. `gh issue
# create --body "... env > config > default ..."`) is never treated as a
# redirection operator, regardless of whether the caller's literal-text
# redaction (next paragraph) already removed it. The caller ALSO feeds this
# the ASK-tier working copy (COMMAND_ASK_SCAN — comment-stripped AND
# literal-text-redacted, i.e. --body/-m/--title/--notes/--comment values are
# replaced with same-length placeholder text) as a second, independent
# narrowing so a `>` that merely appears INSIDE such a quoted value (e.g.
# `git commit -m "a > b"`) cannot manufacture a phantom target even in the
# (non-`>`) tee/sed/cp/mv target-extraction paths below. Any remaining false
# positive resolves to, at worst, an extra deny on a target that isn't really
# a write (safe direction) or a missed target (also safe — the fail-open
# contract this file uses everywhere: ambiguity never widens a deny).
#
# The `cd <path>` tracking now tilde/$HOME-expands its argument via
# expand_cd_arg() (#5315, defined with _QSPLIT_AWK above) — see that function's
# header for the exact expansion rules and the quoted/escaped/`~user` fallbacks.
#
# --------------------------------------------------------------------------
# #5315 DECISION (recorded, NOT implemented in this pass) — two deliberate
# scope calls, documented here so a later reader does not mistake either for an
# oversight:
#
#   1. `~user` / `~user/rest` in a tracked `cd` argument is left UNRESOLVED
#      (joined repo-relative, i.e. classified in-tree / denied) rather than
#      resolved to another account's home. awk cannot look a user's home up via
#      getent/dscl without building a shell command string around an
#      attacker-influenced username token — a command-injection surface this
#      guard must not open. The write-TARGET side (expand_leading_tilde, #4382)
#      can afford that lookup because it runs in bash with the username passed
#      as a non-eval'd argv element; the cd-argument side runs inside awk and
#      cannot. Leaving it repo-relative is the fail-CLOSED direction (a
#      genuinely out-of-tree `cd ~alice && …` write stays denied, never
#      silently allowed), matching this file's `cd -` / bare-`cd` convention.
#      The overwhelmingly-common current-user forms (`~`, `~/rest`, `$HOME`,
#      `$HOME/rest` — the actual #5315 report) ARE expanded.
#
#   2. EPHEMERAL_PATTERNS-class runtime state (the daemon's own gitignored
#      files — `.loom/.daemon.pid` et al., authoritative list in
#      loom-daemon/src/init/post_init.rs) is NOT exempted from Bash write
#      confinement. As of this change no such exemption exists in either
#      guard-destructive-generic.sh or guard-worktree-paths.sh; adding one is
#      net-new policy, and CLAUDE.md documents an "ungated denial floor" that no
#      toggle may bypass, so a gitignore-aware carve-out risks widening that
#      floor into an allow if scoped even slightly too broadly. The concrete
#      #5315 report was a false POSITIVE caused entirely by defect (1) above
#      (the literal-`~` mis-join), which this change fixes directly — an
#      operator maintaining `.loom/.daemon.pid` runs from the primary checkout,
#      not a builder worktree, so once the path resolves correctly it is a
#      routine main-checkout write and the confinement question is orthogonal.
#      Deferred to a dedicated follow-up so the exemption's blast radius can be
#      designed against the denial floor deliberately rather than bolted on
#      alongside a canonicalization fix. See #5315 for the deferral rationale.
# --------------------------------------------------------------------------
#
# SAME-COMMAND VARIABLE RESOLUTION (#4881): a write-idiom target whose token
# is `$NAME`/`${NAME}[...]` is not itself a real path — the real shell
# substitutes it from that variable's value before the redirect/tee/sed/cp/mv
# ever runs. This tokenizer previously treated such a token as a literal
# relative path and cwd-prefixed it, manufacturing a phantom repo-relative
# target (e.g. `SCRATCH=/private/tmp/.../scratchpad` on one line, then `... >
# $SCRATCH/out.txt` on the next, was denied as a worktree-isolation bypass
# even though the real target resolves to /private/tmp, far outside the
# repo). `resolve_var()` below performs the ONE unambiguous, narrow piece of
# this: when the SAME command text contains a `NAME=value` assignment (no
# embedded whitespace in `value`, optionally single/double-quoted) earlier in
# the stream, later `$NAME`/`${NAME}` leading a write target is substituted
# with that value. Threaded via the awk global `varmap`, exactly like `curcwd`
# above. The assignment scan recognizes every ordinary shell assignment
# position, not just a segment that is exactly one bare `NAME=value`:
#   NAME=value                       (bare)
#   export/readonly/declare/typeset/local [-flags] NAME=value [NAME2=value2]
#   A=1 B=2                          (several assignments in one segment)
#   A=1 some-command args…           (env-var prefix — A recorded, then the
#                                     REST of the segment is still scanned as
#                                     a command for write idioms)
#
# CONFLICTING ASSIGNMENTS ARE UNRESOLVABLE (#4914 review): the scan is not
# control-flow aware — qsplit() flattens `||`/`&&`/`;` into plain segments — so
# a name assigned two DIFFERENT values in one command
# (`A=<repo>/defaults/hooks || A=/tmp/outside`) is poisoned to the unresolvable
# sentinel rather than resolved to whichever branch happens to come last in the
# token stream. See record_assign() below.
#
# FAIL-CLOSED ON UNRESOLVABLE (#4914 review): a `$NAME` with NO matching
# assignment, or a token starting with `$` that is not a bare variable
# reference at all (`$(...)` command substitution, `${VAR:-default}`, `$1`,
# an inherited/sourced env var, …), is UNRESOLVABLE. It is NEVER guessed —
# and it is NEVER skipped either. It falls back to the PRE-#4881 behavior:
# the raw token is treated as a literal (repo-relative) path, so a write that
# lands inside the main checkout still denies with the ordinary
# `worktree-write-confinement` tag. Skipping an unresolvable target would
# hand every un-parsed assignment shape a free #4178 worktree-isolation
# bypass (`export SNEAK=<repo>/defaults/hooks; echo x > $SNEAK/evil.sh`), so
# this fix only ever RELAXES the one literally-resolvable case it can prove
# lands outside the repo — it never flips the default for anything else. (The
# file's broader "ambiguity never widens a deny" contract is about not
# inventing NEW denies; preserving an EXISTING one is the conservative side.)
# =============================================================================
extract_write_targets() {
    # Reuses THIS file's own _ESCAPE_AWK/_QSPLIT_AWK (defined above, shared
    # with parse_force_ops()/extract_rm_targets()/command_has_shell_segment()
    # via the #113 escaped-quote fix) rather than re-vendoring the vendored
    # guard's separate, older qsplit() copy — the two must not both define a
    # `qsplit()` under the same awk source variable, and this file's version
    # is the more advanced of the two (rjwalters/repo#188).
    printf '%s' "$1" | awk -v startcwd="$2" -v home="$HOME" "$_ESCAPE_AWK""$_QSPLIT_AWK""$_CDEXPAND_AWK""$_CDQUOTE_AWK""$_MASKGT_AWK""$_MASKWS_AWK""$_MASKHEREDOC_AWK"'
    # Unresolvable cases all return tok UNCHANGED, which is exactly the
    # pre-#4881 treatment (literal, cwd-prefixed => still denied when it
    # lands in the main checkout). Fail-closed by construction: this function
    # can only ever REPLACE a token with a value it actually proved, never
    # make one disappear.
    function resolve_var(tok,   vname, rest, vv) {
        if (substr(tok, 1, 1) != "$") return tok
        if (match(tok, /^\$\{[A-Za-z_][A-Za-z0-9_]*\}/)) {
            vname = substr(tok, RSTART + 2, RLENGTH - 3)
            rest = substr(tok, RSTART + RLENGTH)
        } else if (match(tok, /^\$[A-Za-z_][A-Za-z0-9_]*/)) {
            vname = substr(tok, RSTART + 1, RLENGTH - 1)
            rest = substr(tok, RSTART + RLENGTH)
        } else {
            # `$(...)`, `${VAR:-x}`, `$1`, … — not a bare variable reference.
            return tok
        }
        if (!(vname in varmap)) return tok
        vv = varmap[vname]
        # A value that itself still starts with an unresolved "$" (chained
        # assignment this single-pass resolver does not follow) stays
        # unresolved rather than being guessed.
        if (vv == "" || substr(vv, 1, 1) == "$") return tok
        return vv rest
    }
    # Record a single `NAME=value` word into varmap (value optionally wrapped
    # in matching single/double quotes, which qsplit() copies verbatim).
    #
    # CONFLICTING ASSIGNMENTS POISON THE VARIABLE (#4914 review): this scan is
    # NOT control-flow aware -- qsplit() flattens `||`/`&&`/`;` into plain
    # segments, so `A=<in-repo> || A=/tmp/outside` reaches here as two
    # assignments to the same name. A plain last-write-wins store would then
    # resolve `$A` to whichever branch happens to appear LAST in the token
    # stream, which real bash need never take (`||` short-circuits, so `$A` is
    # the in-repo value at runtime) -- silently ALLOWing a write into the main
    # checkout. So when a name is re-assigned a DIFFERENT value within the same
    # command, its entry is replaced with the AMBIG sentinel instead: a
    # `$`-leading value, which resolve_var() already refuses to substitute as
    # an unresolved chain. The token then falls back to the literal
    # (cwd-prefixed) treatment and denies -- the same fail-closed path every
    # other unresolvable shape takes. Poisoning is sticky (any later assignment
    # differs from the sentinel too) and deliberately blunt: it also covers
    # sequential `A=x; A=y` reassignment, where resolving is *possible* in
    # principle but the safe direction is to stop guessing. Re-assigning the
    # SAME value is not a conflict and still resolves normally -- quotes are
    # stripped above, before the comparison, so a bare and a quoted spelling of
    # one value compare equal.
    function record_assign(word,   eqpos, vname, vval, vlen, c1, c2) {
        eqpos = index(word, "=")
        if (eqpos < 2) return
        vname = substr(word, 1, eqpos - 1)
        vval = substr(word, eqpos + 1)
        vlen = length(vval)
        if (vlen >= 2) {
            c1 = substr(vval, 1, 1)
            c2 = substr(vval, vlen, 1)
            if ((c1 == DQ && c2 == DQ) || (c1 == SQ && c2 == SQ)) {
                vval = substr(vval, 2, vlen - 2)
            }
        }
        if ((vname in varmap) && varmap[vname] != vval) {
            varmap[vname] = AMBIG
            return
        }
        varmap[vname] = vval
    }
    BEGIN {
        SEP = sprintf("%c", 31)
        DQ = sprintf("%c", 34)
        SQ = sprintf("%c", 39)
        # Poison value for a name assigned two different values in one command
        # (see record_assign). The leading "$" is load-bearing: it routes into
        # the existing unresolved-chain refusal inside resolve_var().
        AMBIG = "$__LOOM_AMBIGUOUS_ASSIGNMENT__"
        curcwd = startcwd
    }
    # Slurp the whole (possibly multi-line) command into ONE buffer,
    # preserving embedded newlines (mirrors the #3898 multi-line
    # accumulation strip_literal_text() already uses), then do ALL
    # processing ONCE in END rather than once per PHYSICAL LINE -- the
    # default per-record awk behaviour, which is what silently reset
    # qsplit()/mask_gt()/mask_ws() to "unquoted" at every embedded newline
    # before the #5000 fix below.
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        # Heredoc-body masking (#5000) runs BEFORE qsplit(): once a heredoc
        # body write-idiom-looking bytes (`>`, `;`, `tee`, ...) are replaced
        # with inert placeholders, nothing dangerous-looking is left to
        # misread on those lines. A real write-idiom byte OUTSIDE any
        # recognized heredoc body, even later in the SAME multi-line command,
        # is untouched and still flows through the unchanged pipeline below.
        #
        # INTERPRETER-AWARE (#5351): use the _selective() variant, not plain
        # mask_heredoc_bodies(). A write-idiom line inside a body handed to an
        # interpreter (`bash <<'EOF' ... EOF`, `sh -s <<EOF`, `cat <<EOF |
        # bash`, ...) is genuinely LIVE code to that inner interpreter, so
        # masking it would blank a real out-of-worktree write into an ALLOW --
        # exactly what KNOWN LIMITATIONS #1 recorded as an interpreter-fed gap
        # in this ask-tier scan. _selective() leaves an interpreter-fed body
        # VISIBLE (so the write reaches the confinement check) while still
        # masking every INERT sink body (`cat <<'EOF' ... EOF`,
        # `--body "$(cat <<'EOF' ... EOF)"`), preserving the #4914/#5000/#5181
        # false-positive fixes. This gives the confinement tier the SAME
        # interpreter-awareness the catastrophic tier already has (#5198/#5205).
        buf = mask_heredoc_bodies_selective(buf)
        $0 = qsplit(buf)   # quote-aware segmentation (#3755)

        # Whole-BUFFER quote-aware masking (#5157), not per-segment.
        #
        # mask_ws()/mask_gt() themselves track quote state one character at a
        # time and never special-case "\n" -- an embedded newline inside an
        # OPEN quoted span (e.g. a plain multi-line double-quoted string,
        # `msg="line one\necho pwned > /main/checkout/f.sh\nline three"`, no
        # heredoc involved at all) is simply copied through like any other
        # byte while quote mode stays "on". qsplit() above already preserves
        # such an embedded newline as part of the ONE atomic quoted span it
        # copies verbatim (it finds the matching closing quote by index, not
        # by line), so by construction every "\n" surviving in its output
        # already sits OUTSIDE any then-open quote from the perspective of
        # qsplit() itself -- but mask_ws() and mask_gt() do their own independent
        # char-by-char quote tracking, and calling them per-SEGMENT (after
        # `split($0, segs, "\n")`) resets that tracking to "unquoted" at every
        # such newline, discarding the "still inside this quote" context a
        # PRIOR segment established. A `>` sitting on the continuation line of
        # an otherwise-inert multi-line double-quoted string is then
        # misread as a live redirection operator, manufacturing a phantom
        # write target for text that never reaches the shell as anything but
        # quoted data (#5157). This is the direct multi-line analog of the
        # single-line #4245 fix and the heredoc-body #5000 fix above --
        # masking the WHOLE buffer once, before any "\n"-splitting happens,
        # keeps quote state correctly threaded across every embedded newline,
        # heredoc or not.
        wbuf = mask_ws($0)
        gbuf = mask_gt(wbuf)
        n = split($0, segs, "\n")
        nw = split(wbuf, wsegs, "\n")
        ng = split(gbuf, gsegs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            origlen = length(seg)
            sub(/^[ \t]+/, "", seg)
            sub(/^sudo[ \t]+/, "", seg)
            sub(/^[ \t]+/, "", seg)
            if (seg == "") continue

            # `NAME=value` assignments in any ordinary shell assignment
            # position (#4881; keyword/multi-assignment shapes added by the
            # #4914 review). Recorded into varmap for LATER write targets in
            # this same command. A leading declaration keyword
            # (export/readonly/declare/typeset/local) and its flags are
            # stripped first, then EVERY leading `NAME=value` word is
            # consumed. Whatever remains is the real command for the segment,
            # still scanned for write idioms below (the `A=1 cmd …` env-var
            # prefix shape), so recognizing an assignment never causes a
            # command in the same segment to be skipped.
            if (seg ~ /^(export|readonly|declare|typeset|local)[ \t]/) {
                sub(/^(export|readonly|declare|typeset|local)[ \t]+/, "", seg)
                while (seg ~ /^-/) {
                    if (!sub(/^-[^ \t]*[ \t]*/, "", seg)) break
                }
            }
            while (match(seg, /^[A-Za-z_][A-Za-z0-9_]*=[^ \t]*([ \t]+|$)/)) {
                assignword = substr(seg, 1, RLENGTH)
                seg = substr(seg, RLENGTH + 1)
                sub(/[ \t]+$/, "", assignword)
                record_assign(assignword)
            }
            # A segment that was NOTHING but assignments writes nothing.
            # Anything left over keeps flowing into the command scan below
            # (a redirection is honoured even on a declaration statement —
            # `export FOO > f` really does truncate `f`), so consuming an
            # assignment can never make a real write idiom disappear.
            if (seg == "") continue

            # wsegs[i]/gsegs[i] are byte-for-byte identical in LENGTH to the
            # unstripped segs[i] (masking only ever substitutes one byte for
            # one byte, never adds/removes any) -- `stripped` is the number of
            # leading bytes the three sub() calls above just removed from the
            # (unmasked) seg, so re-applying that same byte count via substr()
            # keeps wseg/mseg positionally aligned with the stripped seg
            # regardless of whether those leading bytes were literal
            # whitespace or (on a mid-quote continuation segment) already
            # masked to a placeholder byte.
            stripped = origlen - length(seg)

            # Quote-aware whitespace masking (#4934, threaded whole-buffer
            # per #5157 above): wseg is byte-for-byte identical to seg except
            # a space/tab INSIDE a quoted span is replaced with a
            # non-whitespace placeholder, so splitting on /[ \t]+/ never
            # breaks a quoted argument (e.g. a quoted path containing a
            # literal space) into more than one token. toks[] is then
            # unmasked back to the real whitespace bytes so the target TEXT
            # downstream is unchanged.
            wseg = substr(wsegs[i], stripped + 1)
            m = split(wseg, toks, /[ \t]+/)
            if (m < 1) continue
            for (j = 1; j <= m; j++) toks[j] = unmask_ws(toks[j])

            # Quote-aware parallel tokenization (#4245, threaded whole-buffer
            # per #5157 above): mseg is byte-for-byte identical to wseg except
            # a `>` inside a quoted span is replaced with an SOH placeholder,
            # so whitespace splitting yields the SAME token boundaries
            # (mm == m always) but mtoks[] can be tested for a REAL (unquoted)
            # redirection operator without ever matching a `>` that was only
            # quoted data. The actual target text is still read from the
            # ORIGINAL toks[] (unmasked) once a real operator is confirmed.
            mseg = substr(gsegs[i], stripped + 1)
            mm = split(mseg, mtoks, /[ \t]+/)

            if (toks[1] == "cd") {
                if (m >= 2 && toks[2] != "" && toks[2] != "-") {
                    cdarg = expand_cd_arg(toks[2], home)   # #5315
                    # Quote-aware absolute/relative CLASSIFICATION only
                    # (#4933, widened to a PARTIALLY quoted argument by
                    # #5363 -- see the strip_cd_quoting() header comment
                    # above). qsplit() preserves quote characters VERBATIM in
                    # toks[] (its contract -- extract_rm_targets()/
                    # parse_force_ops() depend on that raw form elsewhere in
                    # this file), so a quoted ABSOLUTE `cd` argument can start
                    # with a quote character rather than `/`, fail the plain
                    # ^/ test below, and fall into the RELATIVE join branch --
                    # fabricating curcwd as "<worktree>/<quoted-abs-path>", a
                    # location the write never has. From a linked-worktree
                    # cwd that fabrication walks straight back into the
                    # acting worktree own .loom-managed sentinel and the
                    # write is silently ALLOWED, i.e. the #4178 confinement
                    # check is defeated by quoting the cd argument -- fully
                    # (#4933) or only PARTIALLY (#5363, e.g. a quoted
                    # <main> segment followed directly by /sub, no space).
                    #
                    # The fully quote-stripped value (cdclass) is used ONLY
                    # to CLASSIFY. curcwd is still built from the RAW,
                    # quote-preserved cdarg because curcwd is emitted
                    # verbatim as the shell layer `_wcwd`, and the
                    # unresolved-`$` detector there (mark_expandable_dollars,
                    # #4921/#4927) needs those quote characters to tell a
                    # LITERAL `$` inside a single-quoted span (a directory
                    # genuinely named $FOO, explicitly a "deliberately NOT
                    # denied" case in the write-confinement block below) from
                    # an EXPANDABLE one (bare or double-quoted, which the
                    # guard cannot resolve and so fails closed on). Stripping
                    # the quotes here would make every `$` in the last cd
                    # segment look expandable and would deny writes that are
                    # allowed today. The shell layer re-strips quoting for
                    # its own cwd join, mirroring the write-target side raw
                    # `_wtarget` vs. stripped `_wclassify` split
                    # (strip_target_quoting(), #4926).
                    #
                    # An unbalanced/unterminated quote leaves cdclass == cdarg
                    # (strip_cd_quoting() own fallback contract), so
                    # ambiguity can only ever keep the existing verdict, never
                    # widen a deny into an allow (same fallback contract as
                    # #4926).
                    cdclass = strip_cd_quoting(cdarg)
                    if (cdclass ~ /^\//) {
                        curcwd = cdarg
                    } else if (curcwd != "") {
                        curcwd = curcwd "/" cdarg
                    }
                }
                continue
            }

            # STDIN-REDIRECTION EXCLUSION (#5369) -- `<` is a redirection
            # operator, never a write-target operand, so neither it nor the
            # file it reads FROM may be scanned as a write target by the
            # tee / sed -i / cp-mv loops below. Two symptoms motivated this,
            # one in each direction:
            #
            #   * false DENY (tee/sed -i): the bare `<` token and its operand
            #     were both scanned, resolving against curcwd into phantom
            #     `<repo>/<` and `<repo>/in` targets -- so a wholly
            #     out-of-tree `tee /tmp/f.md < /tmp/in` was denied as a
            #     confinement bypass.
            #   * false ALLOW (cp/mv) -- the serious one: that branch takes
            #     the LAST non-flag token as the destination, so a trailing
            #     `< /tmp/in` displaced the REAL destination and a
            #     `cp /tmp/a <main-checkout>/p.sh < /tmp/in` was waved
            #     through -- a #4178 worktree-confinement escape.
            #
            # Token-boundary test, exactly like the `>`/`>>` operator loop
            # below (never a mid-token character scan):
            #   `<` / `0<`  (bare, optionally fd-prefixed) consumes the NEXT
            #               non-empty token, which is the file read FROM.
            #   `</tmp/in`  (attached, optionally fd-prefixed) consumes only
            #               itself.
            #
            # QUOTE AWARENESS COMES FREE, no mask_gt()-style parallel
            # tokenization needed: qsplit() preserves quote characters
            # VERBATIM in toks[] and mask_ws() guarantees a quoted span never
            # spans two tokens, so a quoted/escaped literal filename that
            # merely BEGINS with `<` (single-quoted, double-quoted, or
            # backslash-escaped) starts its token with the quote/backslash
            # byte and can never match the anchored patterns here -- it stays
            # a scanned write target, opening no new escape vector, which is
            # the fail-closed direction this file requires.
            # (mask_gt() exists because a `>` can appear
            # MID-token inside a quoted span; these patterns only ever look at
            # the first bytes of a token, so that case cannot arise.)
            #
            # Deliberately NOT matched: `<<`, `<<-`, `<<<`. Those are heredoc
            # /herestring operators handled separately by the pre-tokenization
            # heredoc machinery above (mask_heredoc_bodies_selective) and by
            # #5232/#5233; the `[^<]` guard below keeps this fix strictly
            # disjoint from that one.
            delete stdin_redir
            for (j = 1; j <= m; j++) {
                if (toks[j] == "") continue
                if (toks[j] ~ /^[0-9]*<$/) {
                    stdin_redir[j] = 1
                    for (k = j + 1; k <= m; k++) {
                        if (toks[k] == "") continue
                        stdin_redir[k] = 1
                        break
                    }
                } else if (toks[j] ~ /^[0-9]*<[^<]/) {
                    stdin_redir[j] = 1
                }
            }

            if (toks[1] == "tee") {
                for (j = 2; j <= m; j++) {
                    if (j in stdin_redir) continue
                    if (toks[j] == "" || toks[j] ~ /^-/) continue
                    print curcwd SEP resolve_var(toks[j])
                }
            } else if (toks[1] == "sed") {
                has_i = 0
                nf = 0
                delete nfargs
                for (j = 2; j <= m; j++) {
                    if (j in stdin_redir) continue
                    if (toks[j] ~ /^-i/) has_i = 1
                    if (toks[j] ~ /^-/) continue
                    if (toks[j] == "") continue
                    nf++
                    nfargs[nf] = toks[j]
                }
                if (has_i && nf >= 2) {
                    for (j = 2; j <= nf; j++) print curcwd SEP resolve_var(nfargs[j])
                }
            } else if (toks[1] == "cp" || toks[1] == "mv") {
                nf = 0
                delete nfargs
                for (j = 2; j <= m; j++) {
                    if (j in stdin_redir) continue
                    if (toks[j] ~ /^-/) continue
                    if (toks[j] == "") continue
                    nf++
                    nfargs[nf] = toks[j]
                }
                if (nf >= 2) print curcwd SEP resolve_var(nfargs[nf])
            }

            # >/>>  redirection — token-boundary detection only (never a
            # mid-token char scan), so scanning stays anchored to whitespace
            # boundaries rather than manufacturing a target out of a `>`
            # sitting inside an already-multi-char token. The MATCH test reads
            # mtoks[] (quote-masked, #4245) so a `>` that is only quoted DATA
            # can never match as an operator; the actual target text is still
            # read from the ORIGINAL toks[] (unmasked) once a real operator is
            # confirmed.
            for (j = 1; j <= m; j++) {
                mt = mtoks[j]
                if (mt == "") continue
                if (mt ~ /^[0-9]*>>?$/) {
                    # Bare operator token. Dup-to-fd (`> &1`) is recognized by
                    # the NEXT token starting with `&` and excluded.
                    if (j + 1 <= m && toks[j+1] != "" && mtoks[j+1] !~ /^&/) {
                        print curcwd SEP resolve_var(toks[j+1])
                    }
                    continue
                }
                if (mt ~ /^[0-9]*>>?[^ \t&]/) {
                    # Attached form (`>file`, `2>file`, `>>file`).
                    op = toks[j]
                    sub(/^[0-9]*>>?/, "", op)
                    if (op != "") print curcwd SEP resolve_var(op)
                }
            }
        }
    }'
}

# =============================================================================
# expand_leading_tilde() — shell-accurate tilde expansion for write targets
# (#4382, same fix family as the quote-aware `>` scanning of #4245/#4289).
#
# extract_write_targets() (below) is a plain-whitespace/quote-aware TOKENIZER,
# not a shell evaluator — it never performs word expansions (tilde, variable,
# glob, ...). A raw token like `~/.local/bin/x` was therefore resolved as a
# REPO-RELATIVE path (cwd-prefixed) even though the real shell would expand it
# to "$HOME/.local/bin/x" before `cp`/`mv`/`tee`/`sed -i`/redirection ever see
# it — producing a false-positive worktree-confinement deny on a write that
# actually lands far outside the repo (#4382).
#
# This performs ONLY the narrow, unambiguous piece of shell word-expansion
# tilde-expansion applies to: an UNQUOTED, UNESCAPED tilde as the FIRST
# character of the token, i.e. exactly the shell-eligible positions:
#   ~/rest        -> "$HOME/rest"
#   ~             -> "$HOME"
#   ~user/rest    -> "<user's home>/rest"   (only if that user resolves)
#   ~user         -> "<user's home>"
#
# Because qsplit() (the shared tokenizer, #3755) copies a quoted span
# VERBATIM including its quote characters, and leaves a literal backslash
# untouched, a token whose raw text does not start with a bare `~` was NOT
# eligible for shell tilde-expansion and MUST stay untouched here:
#   '~/x'   -> starts with a quote char, shell never expands it (stays literal)
#   \~/x    -> starts with a literal backslash, shell never expands it either
#   foo~/x  -> tilde is not the leading character -- not an expansion position
# Any of these three cases falls through unchanged (echoed back as-is), which
# preserves the existing (correct) repo-relative/deny behavior for them.
#
# `~user` lookup uses getent (Linux) / dscl (macOS) with the username passed
# as a plain CLI argument (never eval'd/interpolated into a shell string) so a
# hostile username token cannot inject a command. If the user cannot be
# resolved on this host, the token is returned UNCHANGED (falls back to the
# existing repo-relative treatment) -- consistent with this file's fail-open
# contract: uncertainty here biases toward the (safe) deny path, never toward
# a silent new allow.
# =============================================================================
expand_leading_tilde() {
    local tok="$1"
    # shellcheck disable=SC2088 # intentional: these `~`-prefixed case
    # patterns are literal PATTERN matches against an unexpanded leading
    # tilde in $tok (the whole point of this function), not an attempt at
    # shell tilde expansion.
    case "$tok" in
        '~')
            [[ -n "$HOME" ]] && { printf '%s' "$HOME"; return; }
            printf '%s' "$tok"
            return
            ;;
        '~/'*)
            if [[ -n "$HOME" ]]; then
                printf '%s' "$HOME/${tok#\~/}"
            else
                printf '%s' "$tok"
            fi
            return
            ;;
        '~'*)
            local rest="${tok#\~}"
            local user="${rest%%/*}"
            local remainder=""
            if [[ "$rest" == */* ]]; then
                remainder="/${rest#*/}"
            fi
            if [[ -n "$user" ]]; then
                local home=""
                if command -v getent >/dev/null 2>&1; then
                    home=$(getent passwd "$user" 2>/dev/null | cut -d: -f6)
                elif command -v dscl >/dev/null 2>&1; then
                    home=$(dscl . -read "/Users/$user" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
                fi
                if [[ -n "$home" ]]; then
                    printf '%s' "${home}${remainder}"
                    return
                fi
            fi
            # Unresolvable ~user (unknown user / no lookup tool available):
            # leave untouched -- falls back to repo-relative resolution.
            printf '%s' "$tok"
            return
            ;;
        *)
            printf '%s' "$tok"
            return
            ;;
    esac
}

# Cheap pre-check keeps awk off the hot path for the ~99% of commands that have
# no recursive/force rm at all. The first alternative matches a literal `rm`
# command word; the second admits a command-word *substitution* (#72) — a
# closing `)` or backtick immediately followed by a recursive/force flag, as in
# `$(which rm) -rf /` or `` `which rm` -rf / `` — so extract_rm_targets() is
# invoked for that shape too. This gate is only an optimization: a false match
# here is harmless because extract_rm_targets() emits a target only for a real
# rm-flavored (substitution/rm command word + recursive-force flag) segment.
if echo "$COMMAND" | grep -qE 'rm[[:space:]]+-[a-zA-Z]*[rf]|[)`][[:space:]]+-[a-zA-Z]*[rf]'; then
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

        # Expand a leading `~` or `$HOME` token so home-directory targets are
        # recognized the same way the literal-rm ALWAYS_BLOCK `$HOME`/`~`
        # patterns handle them (#72). extract_rm_targets() emits the raw token,
        # so a substitution-path target of `~` or `$HOME` (as in
        # `$(which rm) -rf ~`) would otherwise be treated as a CWD-relative path
        # and never flagged, an asymmetry vs. literal `rm -rf ~`/`rm -rf $HOME`.
        # Only bare-home and home-subpath forms are expanded — this mirrors the
        # literal floor exactly: bare `$HOME`/`~` deny (whole-home wipe) while a
        # home *subpath* expands to a deeper path and stays allowed.
        if [[ -n "$HOME" ]]; then
            # The `~` in the case globs below is a LITERAL match against the
            # emitted target token, not a path we want the shell to expand —
            # SC2088 (tilde-does-not-expand-in-quotes) is exactly the intended
            # behaviour here, so it is suppressed.
            # shellcheck disable=SC2088
            case "$target" in
                '~')          target="$HOME" ;;
                '~/'*)        target="$HOME/${target#\~/}" ;;
                '$HOME')      target="$HOME" ;;
                '$HOME/'*)    target="$HOME/${target#\$HOME/}" ;;
                '${HOME}')    target="$HOME" ;;
                '${HOME}/'*)  target="$HOME/${target#\$\{HOME\}/}" ;;
            esac
        fi

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
                deny "BLOCKED: rm on protected system path: $ABS_PATH" "rm-protected-path"
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
                    deny "BLOCKED: rm target outside repo scope (guards.rmScope=repo; set guards.rmScope:\"off\" in .claude/skills/repo/config.json to opt out): $ABS_PATH" "rm-scope-outside-repo"
                fi
            fi
        fi
    done
fi

# =============================================================================
# BASH-TOOL WRITE CONFINEMENT — worktree isolation for `>`/`>>`/tee/sed -i/
# cp/mv (issue #4178)
#
# guard-worktree-paths.sh confines Edit/Write tool calls to a builder's issue
# worktree, but the Bash tool has no equivalent confinement — a session denied
# on Edit/Write could fall back to a Bash write and land the same edit in the
# main checkout (the #4178 incident: sweep #4063 escaped this way and edited
# live guard hooks in the main checkout while its own worktree stayed clean).
#
# Gated by the SAME toggle as guard-worktree-paths.sh
# (guards.worktreeIsolation / LOOM_GUARD_WORKTREE_ISOLATION,
# worktree_isolation_guard_enabled() above) and only denies when a managed
# worktree actually exists somewhere for this repo — exactly the
# path_derived_allow() logic in guard-worktree-paths.sh, reimplemented here
# because this is a separate Bash-matcher hook with its own fail-open
# contract. A cheap substring pre-check keeps the segmenter off the hot path
# for the vast majority of Bash calls that contain none of the recognized
# write idioms at all.
# =============================================================================
if worktree_isolation_guard_enabled && \
   { [[ "$COMMAND_ASK_SCAN" == *">"* ]] || [[ "$COMMAND_ASK_SCAN" == *"tee"* ]] || \
     [[ "$COMMAND_ASK_SCAN" == *"sed"* ]] || [[ "$COMMAND_ASK_SCAN" == *"cp "* ]] || \
     [[ "$COMMAND_ASK_SCAN" == *"mv "* ]]; }; then
    _WT_WRITE_BASE=""
    _WT_WRITE_BASE_DONE=""

    # Derive the TRUE main-checkout root — NOT REPO_ROOT. REPO_ROOT is resolved
    # via `git rev-parse --show-toplevel`, which returns the *worktree* root when
    # CWD is a linked worktree (the canonical builder setup: `cd
    # .loom/worktrees/issue-N`). Keying the "resolves inside the main checkout"
    # test below on REPO_ROOT would therefore miss an absolute-path (or
    # `cd $MAIN && …`) Bash write into the main checkout issued from a builder's
    # own worktree — the exact "denied on Edit/Write → retry via Bash" escape
    # this block exists to close (#4178). Mirror the sibling guard
    # guard-worktree-paths.sh: `--git-common-dir/..` is always the main checkout,
    # from a worktree or not. `pwd -P` resolves symlinks so it matches the
    # git-resolved forms consistently (and sidesteps the macOS
    # /tmp -> /private/tmp mismatch vs. normalize_abs_path's lexical-only form).
    # Fail open to REPO_ROOT if the git resolution is unavailable.
    _WT_MAIN_ROOT=""
    _WT_MAIN_ROOT_LOGICAL=""
    if [[ -n "$CWD" && -d "$CWD" ]]; then
        _wt_common=$(cd "$CWD" 2>/dev/null && git rev-parse --git-common-dir 2>/dev/null) || _wt_common=""
        if [[ -n "$_wt_common" ]]; then
            _WT_MAIN_ROOT=$(cd "$CWD" 2>/dev/null && cd "$_wt_common/.." 2>/dev/null && pwd -P) || _WT_MAIN_ROOT=""
            # ...and the LOGICAL spelling of the same root (symlinks intact).
            # `pwd -P` alone was NOT sufficient (#4495): the write targets this
            # block compares against are produced by normalize_abs_path(), which
            # is lexical-only and therefore keeps a symlinked ancestor intact. A
            # repo reached through a symlinked path (a `/tmp` checkout on macOS,
            # a symlinked home, a bind-mounted workspace) produced targets that
            # never string-matched the physical root, so EVERY Bash write into
            # the main checkout was silently allowed there — the exact #4178
            # escape this block exists to close. Both spellings are checked.
            _WT_MAIN_ROOT_LOGICAL=$(cd "$CWD" 2>/dev/null && cd "$_wt_common/.." 2>/dev/null && pwd) || _WT_MAIN_ROOT_LOGICAL=""
        fi
    fi
    [[ -n "$_WT_MAIN_ROOT" ]] || _WT_MAIN_ROOT="$REPO_ROOT"
    [[ -n "$_WT_MAIN_ROOT_LOGICAL" ]] || _WT_MAIN_ROOT_LOGICAL="$_WT_MAIN_ROOT"

    # "Worktree isolation is actually in play for this repo/session" — a
    # managed worktree exists somewhere under the worktree base derived from
    # the SAME main-checkout root the containment tests use. Resolved lazily
    # and cached, so a command with no confinement-relevant target never pays
    # for the find(1). Defined here (inside the block) because it reads the
    # block-local _WT_MAIN_ROOT / _WT_WRITE_BASE* state.
    _wt_isolation_in_play() {
        if [[ -z "$_WT_WRITE_BASE_DONE" ]]; then
            _WT_WRITE_BASE=$(resolve_worktree_root "$_WT_MAIN_ROOT")
            _WT_WRITE_BASE_DONE=1
        fi
        _any_managed_worktree_exists "$_WT_WRITE_BASE"
    }

    # True if $1 (an absolute, normalized path) sits anywhere in the area this
    # guard protects: inside a managed worktree, inside the main checkout
    # (either spelling), or under the configured worktree base (which may live
    # on an external volume, outside the main checkout entirely).
    # Physical (symlink-resolved) spelling of an absolute path that may not
    # exist yet — the write TARGET usually doesn't. normalize_abs_path() is
    # lexical-only, so it keeps a symlinked ancestor intact; walk up to the
    # longest ancestor that does exist, resolve THAT with `pwd -P`, and
    # re-append the remainder. Prints nothing when the path is relative or no
    # ancestor resolves, so callers can treat empty as "no second spelling".
    _wt_physical_form() {
        local _p="$1" _dir _tail="" _resolved
        [[ "$_p" == /* ]] || return 0
        _dir="$_p"
        while [[ -n "$_dir" && "$_dir" != "/" && ! -d "$_dir" ]]; do
            _tail="/${_dir##*/}$_tail"
            _dir="${_dir%/*}"
            [[ -z "$_dir" ]] && _dir="/"
        done
        [[ -d "$_dir" ]] || return 0
        _resolved=$(cd "$_dir" 2>/dev/null && pwd -P) || return 0
        [[ -n "$_resolved" ]] || return 0
        printf '%s%s' "$_resolved" "$_tail"
    }

    # The string comparisons, run against ONE spelling of the target.
    _wt_in_protected_area_spelling() {
        local _p="$1"
        [[ -n "$_p" ]] || return 1
        _in_any_managed_worktree "$_p" && return 0
        if [[ -n "$_WT_MAIN_ROOT" ]]; then
            case "$_p" in
                "$_WT_MAIN_ROOT"|"$_WT_MAIN_ROOT"/*) return 0 ;;
                "$_WT_MAIN_ROOT_LOGICAL"|"$_WT_MAIN_ROOT_LOGICAL"/*) return 0 ;;
            esac
        fi
        if [[ -z "$_WT_WRITE_BASE_DONE" ]]; then
            _WT_WRITE_BASE=$(resolve_worktree_root "$_WT_MAIN_ROOT")
            _WT_WRITE_BASE_DONE=1
        fi
        if [[ -n "$_WT_WRITE_BASE" ]]; then
            case "$_p" in
                "$_WT_WRITE_BASE"|"$_WT_WRITE_BASE"/*) return 0 ;;
            esac
        fi
        return 1
    }

    # True if $1 (an absolute, normalized path) sits anywhere in the protected
    # area, tested against BOTH the target's own spelling and its physical
    # spelling.
    #
    # The second test closes the remaining half of the #4495 class. That fix
    # captured a logical spelling of the ROOTS, which covers a symlink at or
    # below the repo root — but not one in an ANCESTOR of it. Both roots here
    # are derived through git, which reports physical paths, so a target
    # written through a symlinked ancestor (`/var/... -> /private/var/...` on
    # macOS, where every `mktemp` path is exactly that; a symlinked home; a
    # bind-mounted workspace) matched neither root spelling and EVERY Bash
    # write into the main checkout was silently allowed. Loom's vendored copy
    # still has this gap — verified by probing both guards with the two
    # spellings of the same fixture, which is also why this repo's own
    # write-confinement tests were red before this change.
    #
    # Resolved LAZILY, only after the direct comparisons have all failed, so a
    # command whose target is already physical never pays for the subshell.
    _wt_in_protected_area() {
        local _p="$1" _pp
        [[ -n "$_p" ]] || return 1
        _wt_in_protected_area_spelling "$_p" && return 0
        _pp=$(_wt_physical_form "$_p")
        [[ -n "$_pp" && "$_pp" != "$_p" ]] || return 1
        _wt_in_protected_area_spelling "$_pp"
    }

    WRITE_TARGETS=$(extract_write_targets "$COMMAND_ASK_SCAN" "$CWD" | head -20)
    while IFS=$'\037' read -r _wcwd _wtarget; do
        [[ -z "$_wtarget" ]] && continue

        # Same-command $VAR/${VAR} resolution (#4881) happens inside
        # extract_write_targets(): a target whose leading `$NAME`/`${NAME}`
        # matched an assignment earlier in the SAME command arrives here
        # already substituted. A target it could NOT resolve (no matching
        # assignment, or a $-prefixed token that is not a bare variable
        # reference at all — `$(...)`, `${VAR:-x}`, an inherited env var)
        # arrives UNCHANGED and is deliberately still treated as a literal
        # repo-relative path here, exactly as it was before #4881 — an
        # unresolvable target must stay fail-closed, or every assignment
        # shape the scan cannot parse becomes a free #4178 bypass (#4914
        # review).
        #
        # Shell-accurate tilde expansion (#4382): an unquoted/unescaped
        # leading `~/` or `~user/` in the raw token is what the real shell
        # would expand BEFORE cp/mv/tee/sed -i/redirection ever see it, so
        # expand it here before the relative-path resolution below runs.
        # Quoted ('~/x') / escaped (\~/x) tildes are left untouched (see
        # expand_leading_tilde()'s doc comment) — no change to their
        # existing repo-relative treatment.
        _wtarget=$(expand_leading_tilde "$_wtarget")

        # -------------------------------------------------------------
        # Unresolved `$…` write targets must fail CLOSED, in every cwd (#4921)
        #
        # extract_write_targets() never expands variables; a target it cannot
        # resolve is emitted as the RAW token (`$A/evil.sh`). The resolution
        # below then treats that literal as a relative path and cwd-prefixes
        # it — which fabricates a location the write will not actually have.
        # From a MAIN-CHECKOUT cwd that fabrication happened to land inside
        # the main checkout, so the containment test denied and the token was
        # (accidentally) fail-closed. From a LINKED-WORKTREE cwd — the
        # canonical builder setup, `cd .loom/worktrees/issue-N` — the very
        # same fabrication instead walks straight back up into the acting
        # worktree's own `.loom-managed` sentinel, so check (a) below ALLOWED
        # it before the main-root containment test ever ran, no matter what
        # the variable would expand to at runtime (#4921). That silently
        # defeated the fail-closed backstop for every unresolvable `$` shape
        # (`$(...)`, `${VAR:-x}`, an inherited env var, a chained or
        # conflicting same-command assignment) in the ONE operating mode the
        # #4178 guard exists to protect.
        #
        # So: decide on the token's SHAPE before trusting either test. A
        # target is denial-worthy when the unexpanded `$` makes its LOCATION
        # (not merely its filename) unknowable:
        #
        #   (1) the token IS a variable from the root down — it either starts
        #       with an expandable `$` (`> $DEST`, `tee "${OUT}"`,
        #       `> $(mktemp)`) or starts with `/$` (`> /$X`, `> /$X/evil`).
        #       The path root itself is unknown, so the variable may hold (or
        #       complete) an absolute path into the main checkout and the cwd
        #       prefix is pure invention. Denied regardless of where cwd is.
        #   (2) an expandable `$` appears in a DIRECTORY component of the
        #       resolved path (`> $A/evil`, `> ./$A/evil`, `cd $A && > f`)
        #       AND the known prefix — everything before the first `$`, i.e.
        #       the only part that is a real path — is inside the area this
        #       guard protects, or there is no usable known prefix at all
        #       (it is relative, or it normalizes to `/` as in
        #       `> /tmp/../$A/evil`). An unknown directory component under the
        #       repo can resolve into the main checkout (directly, or via
        #       `..`), and neither the sentinel walk-up nor the containment
        #       test can see it.
        #
        # Deliberately NOT denied (no new false positives — these keep their
        # existing treatment):
        #   - a `$` only in the FINAL component (`> out-$STAMP.log`,
        #     `sed -i s/a/b/ src/$f`): the directory is fully known and really
        #     is cwd-relative, so the sentinel check (a) and the main-root
        #     containment test below are meaningful again.
        #   - a known prefix OUTSIDE the protected area (`> /tmp/$D/f.log`):
        #     the write lands where this guard protects nothing.
        #   - a LITERAL `$` the shell would never expand — inside a
        #     single-quoted span or backslash-escaped (`> '$A/evil'`) — which
        #     really is a relative path to a file named `$A` (mirrors the
        #     quoted-tilde treatment in expand_leading_tilde, #4382).
        #
        # Fail-open contract is preserved: like every other deny in this
        # block, it only fires when a managed worktree actually exists for
        # this repo (_wt_isolation_in_play).
        # -------------------------------------------------------------
        if [[ "$_wtarget" == *'$'* || "$_wcwd" == *'$'* ]]; then
            mark_expandable_dollars "$_wtarget"
            _wmarked="$_MARKED_TOKEN"
            # The cwd itself can carry the unexpanded `$` instead of the
            # target (`cd $A && echo x > f.sh` — extract_write_targets threads
            # the unresolved `cd` argument into curcwd), so mark it too and
            # judge the JOINED path. A cwd that is absolute and `$`-free
            # marks to itself, leaving every existing case byte-identical.
            _wmarkedcwd=""
            if [[ -n "$_wcwd" ]]; then
                mark_expandable_dollars "$_wcwd"
                _wmarkedcwd="$_MARKED_TOKEN"
            fi
            if [[ "$_wmarked" == *$'\001'* || ( "$_wmarked" != /* && "$_wmarkedcwd" == *$'\001'* ) ]]; then
                # (1) Root unknown — the token is a variable from the root
                # down (`$DEST`, `$(mktemp)`) or is root + a variable
                # (`/$X`, `/$X/evil`, whose runtime value picks the top-level
                # directory — the main checkout's own included).
                if [[ "$_wmarked" == $'\001'* || "$_wmarked" == /$'\001'* ]]; then
                    if _wt_isolation_in_play; then
                        deny "BLOCKED: Bash-tool write target '${_wtarget}' is an unexpanded shell variable from the path root down, so this guard cannot tell where the write lands — it may resolve to an absolute path inside the main repository checkout ('${_WT_MAIN_ROOT}'), and a Loom-managed worktree exists in this repository. Unresolvable write targets fail closed (#4921). Write to an explicit literal path — inside your issue worktree (.loom/worktrees/issue-<N>) for repo files, or a spelled-out /tmp path for scratch. (#4178)" "worktree-write-confinement-unresolved-var"
                    fi
                    continue
                fi
                # (2) Unknown DIRECTORY component. Build the effective path
                # the same way the resolution below does (cwd-joined when
                # relative), then test only the KNOWN prefix — everything
                # before the first unexpanded `$`, trimmed to its directory.
                _weff=""
                if [[ "$_wmarked" == /* ]]; then
                    _weff="$_wmarked"
                elif [[ -n "$_wmarkedcwd" ]]; then
                    _weff="$_wmarkedcwd/$_wmarked"
                fi
                _wdirpart=""
                [[ "$_weff" == */* ]] && _wdirpart="${_weff%/*}"
                if [[ "$_wdirpart" == *$'\001'* ]]; then
                    _wknown="${_weff%%$'\001'*}"
                    _wknown="${_wknown%/*}"
                    # Normalize BEFORE judging: a `..` traversal in the known
                    # prefix (`> /tmp/../$A/evil`) otherwise hands the test a
                    # prefix that is not where the write actually starts.
                    [[ "$_wknown" == /* ]] && _wknown=$(normalize_abs_path "$_wknown")
                    if [[ "$_wknown" != /* || "$_wknown" == "/" ]]; then
                        # No usable known prefix — either it is relative (no
                        # cwd to join against) or it collapses to `/`, i.e.
                        # the first real path component IS the variable
                        # (`> /$A/evil`, `> /tmp/../$A/evil`), whose runtime
                        # value picks a top-level directory, the main
                        # checkout's own included. Same verdict as (1).
                        if _wt_isolation_in_play; then
                            deny "BLOCKED: Bash-tool write target '${_wtarget}' has an unexpanded shell variable as its first real path component, so this guard cannot tell where the write lands — it may resolve inside the main repository checkout ('${_WT_MAIN_ROOT}'), and a Loom-managed worktree exists in this repository. Unresolvable write targets fail closed (#4921). Write to an explicit literal path — inside your issue worktree (.loom/worktrees/issue-<N>) for repo files, or a spelled-out /tmp path for scratch. (#4178)" "worktree-write-confinement-unresolved-var"
                        fi
                    elif _wt_in_protected_area "$_wknown"; then
                        if _wt_isolation_in_play; then
                            deny "BLOCKED: Bash-tool write target '${_wtarget}' contains an unexpanded shell variable in a directory component, and its known prefix ('${_wknown}') is inside this repository's worktree/checkout area — this guard cannot tell whether the expanded path stays in your worktree or lands in the main repository checkout ('${_WT_MAIN_ROOT}'). Unresolvable write targets fail closed (#4921). Write to an explicit literal path — inside your issue worktree (.loom/worktrees/issue-<N>) for repo files, or a spelled-out /tmp path for scratch. (#4178)" "worktree-write-confinement-unresolved-var"
                        fi
                    fi
                    continue
                fi
            fi
        fi

        # Shell-accurate quote removal, for the classification only (#4926):
        # `'/main/evil'` / `"/main/evil"` reach here with their quote
        # characters intact (qsplit's contract), so they start with a quote
        # rather than `/` and the test below would call an ABSOLUTE path
        # relative and cwd-prefix it into a location the write never has.
        # Unquote a COPY: extract_rm_targets()/parse_force_ops() keep their
        # verbatim tokens, and the deny message below still quotes the raw
        # `$_wtarget` the operator actually typed. An unterminated quote keeps
        # the raw token (today's verdict) rather than risk widening a deny.
        _wclassify="$_wtarget"
        strip_target_quoting "$_wtarget" && _wclassify="$_UNQUOTED_TARGET"

        # Same split for the CWD half of the pair (#4933). A tracked
        # `cd <dir>` argument reaches here with its quote characters intact
        # too — extract_write_targets() deliberately builds curcwd from the
        # RAW, quote-preserved token so the unresolved-`$` block ABOVE can
        # still tell a literal single-quoted `$` from an expandable one
        # (stripping the quotes in awk instead turned every `$` in the last
        # `cd` segment into an "unresolvable" deny). By the time we get here
        # that judgement is already made, so unquote a COPY for the join —
        # otherwise a quoted absolute `cd` argument would be joined with its
        # quote characters embedded and normalize to a path the write never
        # has. Only touched when a quote character is actually present, so a
        # quote-free cwd (every ordinary case) stays byte-identical; an
        # unterminated quote falls back to the raw value, i.e. today's
        # verdict, never widening a deny into an allow.
        _wcwdclassify="$_wcwd"
        if [[ "$_wcwd" == *"'"* || "$_wcwd" == *'"'* ]]; then
            strip_target_quoting "$_wcwd" && _wcwdclassify="$_UNQUOTED_TARGET"
        fi

        # Resolve to absolute; a relative target with no resolvable cwd is
        # ambiguous — skip it (allow on uncertainty, never deny on it).
        _wabs=""
        if [[ "$_wclassify" == /* ]]; then
            _wabs="$_wclassify"
        elif [[ -n "$_wcwdclassify" ]]; then
            _wabs="$_wcwdclassify/$_wclassify"
        else
            continue
        fi
        _wabs=$(normalize_abs_path "$_wabs")

        # Second spelling of the same target: physical, symlink-resolved.
        # normalize_abs_path() is lexical-only, so it keeps a symlinked
        # ancestor intact, while BOTH roots below come from git and are
        # therefore physical. Without this, a target written through a
        # symlinked ancestor matches neither root and every Bash write into
        # the main checkout is silently allowed — the #4495 class, of which
        # the earlier logical-root fix caught only the half where the symlink
        # sits at or below the repo root. On macOS every `mktemp` path is a
        # `/var -> /private/var` symlink, which is why this repo's own
        # write-confinement tests were red. Loom's vendored copy still has
        # this gap. Resolved once per target and only when it differs.
        _wabsp=$(_wt_physical_form "$_wabs")
        [[ "$_wabsp" == "$_wabs" ]] && _wabsp=""

        # (a) Already inside some managed worktree -> allow. This is exactly
        # where a builder is supposed to write. Checked against both spellings
        # so the allow stays as wide as the deny below.
        _in_any_managed_worktree "$_wabs" && continue
        [[ -n "$_wabsp" ]] && _in_any_managed_worktree "$_wabsp" && continue

        # Not under any worktree. If it's also not under the main checkout,
        # there is nothing this guard protects (e.g. /tmp scratch) -> allow.
        [[ -z "$_WT_MAIN_ROOT" ]] && continue
        # Both spellings are tested with a QUOTED `case`, never a `for` over an
        # unquoted expansion: `for x in ${var:+"$var"}` word-splits (and globs)
        # its result despite the inner quotes, so a target containing a space
        # or a glob character would be compared as fragments. The pre-existing
        # single-spelling code was a quoted `case` for that reason, and adding
        # the second spelling must not regress it.
        #
        # Defensive, not a fix for an observed escape: an UNQUOTED spaced
        # redirect target genuinely splits in the shell too (`> /a/b c` really
        # does redirect to `/a/b`), so the guard is right to see the first
        # word there. The exposure would be a quoted target whose expansion
        # reaches this comparison intact.
        _wt_root_hit=""
        case "$_wabs" in
            "$_WT_MAIN_ROOT"|"$_WT_MAIN_ROOT"/*) _wt_root_hit=1 ;;
            "$_WT_MAIN_ROOT_LOGICAL"|"$_WT_MAIN_ROOT_LOGICAL"/*) _wt_root_hit=1 ;;
        esac
        if [[ -z "$_wt_root_hit" && -n "$_wabsp" ]]; then
            case "$_wabsp" in
                "$_WT_MAIN_ROOT"|"$_WT_MAIN_ROOT"/*) _wt_root_hit=1 ;;
                "$_WT_MAIN_ROOT_LOGICAL"|"$_WT_MAIN_ROOT_LOGICAL"/*) _wt_root_hit=1 ;;
            esac
        fi
        [[ -n "$_wt_root_hit" ]] || continue

        # Target resolves inside the main checkout and outside every
        # worktree. Deny only if worktree isolation is actually in play for
        # this repo/session (a managed worktree exists somewhere); otherwise
        # fail open — a repo/session that has never created a worktree is
        # unaffected, mirroring guard-worktree-paths.sh exactly. The worktree
        # base is resolved off the same main-checkout root so the "a managed
        # worktree exists" gate stays consistent with the containment test.
        if _wt_isolation_in_play; then
            deny "BLOCKED: Bash-tool write to '${_wabs}' resolves to the main repository checkout ('${_WT_MAIN_ROOT}'), but a Loom-managed worktree exists elsewhere in this repository (this check cannot verify it belongs to the acting session — see #4245). This is a worktree-isolation bypass via Bash redirection/tee/sed -i/cp/mv — do NOT retry the write through Bash. cd into your issue worktree (.loom/worktrees/issue-<N>) and write there instead. (#4178)" "worktree-write-confinement"
        fi
    done <<< "$WRITE_TARGETS"
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
    sql_guard_enabled && deny "BLOCKED: DELETE FROM without WHERE clause" "sql-delete-no-where"
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

# Pre-check and parse both read COMMAND_ASK_SCAN, not the raw
# COMMAND_NO_COMMENT (repo#188 parity fix) — a `--force` mentioned inside a
# quoted `--body`/`-m` value is prose, and asking on it stalls ordinary issue
# and commit authoring. Matches Loom's vendored copy, which has always scanned
# the redacted copy here.
if [[ "$COMMAND_ASK_SCAN" == *git* ]] && \
   echo "$COMMAND_ASK_SCAN" | grep -qE '(--force|--force-with-lease|(^|[[:space:]])-f([[:space:]]|$)|--hard)'; then
    _FORCE_MODE=$(force_scope_mode)
    if [[ "$_FORCE_MODE" != "off" ]]; then
        _FORCE_OPS=$(parse_force_ops "$COMMAND_ASK_SCAN")
        if [[ -n "$_FORCE_OPS" ]]; then
            if [[ "$_FORCE_MODE" == "all" ]]; then
                # Preserve pre-#3674 behaviour byte-for-byte: any force op asks.
                ask "Command requires confirmation: $COMMAND" "force-op:all"
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
                        ask "Command requires confirmation: $COMMAND (force operation on a detached or unresolved branch)" "force-op:detached"
                    fi
                    _ftarget="$_fbranch"
                fi
                _fdefault=$(resolve_default_branch "$_fcwd")
                if [[ "$_ftarget" == "main" || "$_ftarget" == "master" ]] || \
                   { [[ -n "$_fdefault" && "$_ftarget" == "$_fdefault" ]]; }; then
                    ask "Command requires confirmation: $COMMAND (force operation targets protected branch '$_ftarget')" "force-op:protected"
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
    #
    # COMMAND-POSITION ANCHORING (#3756): every entry is prefixed with
    # `(^|[;&|[:space:]])`, mirroring ALWAYS_BLOCK_PATTERNS's `gh repo delete`
    # anchor (#3553), so the phrase only fires at start-of-command or after a
    # shell separator — an ask-phrase that merely appears inside another
    # command's quoted argument (e.g. `jq -n '{cmd:"gh issue close 123"}'`, the
    # phrase preceded by `"`) no longer false-asks. Entries whose command is a
    # multi-word phrase (`kubectl rollout restart`, `git checkout \.`) are
    # anchored at the FIRST token only — the phrase's leading command word — per
    # the `gh repo delete` precedent. (Like the catastrophic tier, this anchor
    # cannot distinguish a real separator from a whitespace INSIDE a quoted
    # string, so a mid-quote prose mention such as `echo "… gh pr close …"` still
    # matches on its leading space — an accepted limitation shared with the
    # ALWAYS_BLOCK tier; command-word segment classification is #3757's scope.)
    '(^|[;&|[:space:]])git clean -fd'
    '(^|[;&|[:space:]])git checkout \.'
    '(^|[;&|[:space:]])git restore \.'

    # GitHub operations that are genuinely hard to reverse. `gh release delete`
    # removes published artifacts/tags — it STAYS an ungated ask. The reversible
    # GitHub state changes (`gh pr close`, `gh issue close`, `gh label delete`)
    # were REMOVED from this array (#3757): they are trivially undone (gh pr
    # reopen / gh issue reopen / recreate the label) and are only asked for when
    # a repo opts IN via guards.reversibleGh (REVERSIBLE_GH_ASK_PATTERNS below).
    '(^|[;&|[:space:]])gh release delete'

    # NOTE: cloud CLI (aws) + docker ASK patterns are NOT in this ungated array.
    # They live in CLOUD_ASK_PATTERNS below, gated by cloud_guard_enabled() so
    # cloud-dev repos can opt down (LOOM_GUARD_CLOUD=0 / guards.cloudCli:false).

    # Service management
    '(^|[;&|[:space:]])systemctl restart'
    '(^|[;&|[:space:]])systemctl stop'
    '(^|[;&|[:space:]])systemctl disable'

    # Kubernetes operations
    '(^|[;&|[:space:]])kubectl delete'
    '(^|[;&|[:space:]])kubectl rollout restart'
    '(^|[;&|[:space:]])kubectl drain'

    # SkyPilot infrastructure
    '(^|[;&|[:space:]])sky down'
    '(^|[;&|[:space:]])sky stop'

    # Credential exposure
    '(^|[;&|[:space:]])printenv.*SECRET'
    '(^|[;&|[:space:]])printenv.*TOKEN'
    '(^|[;&|[:space:]])printenv.*KEY'
    '(^|[;&|[:space:]])cat.*/\.ssh/'
    '(^|[;&|[:space:]])cat.*/\.aws/credentials'
)

for pattern in "${ASK_PATTERNS[@]}"; do
    if echo "$COMMAND_ASK_SCAN" | grep -qE "$pattern"; then
        ask "Command requires confirmation: $COMMAND" "ask:$pattern"
    fi
done

# =============================================================================
# REVERSIBLE-GITHUB ASK patterns — gated by the reversible-gh guard toggle (#3757)
#
# Kept OUT of the ungated ASK_PATTERNS array (mirroring the CLOUD_ASK_PATTERNS
# split) because these GitHub state changes are trivially reversible and should
# NOT prompt by default — an autonomous agent closing its own issue/PR as part of
# a normal lifecycle would otherwise stall. reversible_gh_guard_enabled() defaults
# OFF and is consulted only AFTER a pattern matches, so the config read stays off
# the hot path for non-matching commands (mirrors the SQL DDL / cloud blocks).
#
# These entries are anchored (#3756) and scanned against COMMAND_ASK_SCAN — the
# comment-stripped, literal-text-redacted ask working copy — exactly as they were
# while living in ASK_PATTERNS, so #3756's redaction still applies when the toggle
# is opted IN (an ask-phrase quoted inside a --body/--comment value does not
# false-ask). `gh release delete` deliberately stays in the ungated ASK_PATTERNS
# above (hard to reverse) and is NOT gated here.
# =============================================================================
REVERSIBLE_GH_ASK_PATTERNS=(
    '(^|[;&|[:space:]])gh pr close'
    '(^|[;&|[:space:]])gh issue close'
    '(^|[;&|[:space:]])gh label delete'
)

for pattern in "${REVERSIBLE_GH_ASK_PATTERNS[@]}"; do
    if echo "$COMMAND_ASK_SCAN" | grep -qE "$pattern" && reversible_gh_guard_enabled; then
        ask "Command requires confirmation: $COMMAND (set guards.reversibleGh:true in .claude/skills/repo/config.json to keep this ask; it is off by default because the op is trivially reversible)" "reversible-gh:$pattern"
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
        ask "Command requires confirmation: $COMMAND (a bare 'git read-tree' empties the real staging index with no reflog trace; use 'git merge-tree --write-tree <base> <branch>' for a merge preview, or isolate with GIT_INDEX_FILE=\$(mktemp))" "git-read-tree"
    fi
fi

# =============================================================================
# GIT STASH SCOPE ASK — gated by the stash-scope guard toggle
#
# Transplanted from Loom's vendored copy (loom#5173/#4821/#5217) as part of the
# repo#188 parity reconciliation. Before this, the canonical guard had ZERO
# coverage of `git stash` — an agent could destroy operator-preserved WIP with
# no confirmation, while the vendored guard asked. That was the single largest
# capability gap between the two.
#
# Two distinct hazards, both real:
#
# 1. MAIN CHECKOUT. The main checkout's stash stack is operator-owned, not
#    scratch space for an integration check. `pop`/`drop`/`clear` there can
#    destroy state a human deliberately preserved.
#
# 2. WORKTREE-TO-WORKTREE COLLISION. `refs/stash` is a SINGLE stack shared by
#    every linked worktree of a repo, not per-worktree. Two agents working in
#    different worktrees can pop or drop each other's WIP, and a main-checkout-
#    only check asks for neither side. A single active worktree has nobody to
#    collide with, so it stays ungated.
#
#    GENERALIZED from Loom's version, which counted `.loom-managed` marker
#    files under `<main>/.loom/worktrees/`. This counts what git itself
#    reports, so the hazard is caught for any tool's worktrees — or none at
#    all — rather than only Loom's. Same reasoning as the tool-agnostic
#    worktree-root detection elsewhere in this file.
#
# A same-chain heuristic ("push and pop appear in the same command, so allow")
# was considered and rejected upstream (loom#5217): push and pop are separate
# guard-approved calls with arbitrary time between them, so another worktree's
# concurrent push can land on the shared stack in that window and the paired
# pop then restores the WRONG entry. A same-chain check cannot see that.
#
# Gated by stash_scope_guard_enabled() (guards.stashScope /
# REPO_GUARD_STASH_SCOPE, legacy LOOM_GUARD_STASH_SCOPE, default on), invoked
# LAZILY only after the pattern matched, mirroring every other cold-path toggle.
# =============================================================================
# The optional `(-C <path>|-c <k=v>)*` run between `git` and `stash` is
# repo#194: without it this pre-check never matches `git -C <path> stash pop`,
# so the parser below never runs and the -C form escapes the ask entirely —
# the pre-check, not the parser, is the actual gate.
#
# The flag value must tolerate whitespace inside quotes, and MIXED forms like
# `-c user.name="John Doe"` where one token is part bare and part quoted. A
# first cut used `[^[:space:]]+`, which silently reintroduced the very bypass
# this closes: any quoted value containing a space failed the positional match,
# so the whole pre-check missed and the ask was skipped — a silent allow, not
# even an ask. Rather than enumerate token shapes, match the flag run
# non-greedily up to `stash`, and let the parser below (which is genuinely
# quote-aware via qsplit/mask_ws) decide scope. This gate only needs to be
# permissive enough not to miss; being over-inclusive here costs a parser call,
# never a wrong verdict.
#
# repo#202: a leading `GIT_DIR=<path> GIT_WORK_TREE=<path>` assignment run
# before `git` is a DIFFERENT parse shape — the command does not start with
# `git` at all — so it needed its own alternative in this same pre-check
# rather than a tweak to the existing one. `([A-Za-z_][A-Za-z0-9_]*=[^;&|[:space:]]*[[:space:]]+)*`
# tolerates zero or more such assignments (any name, not just GIT_DIR/
# GIT_WORK_TREE — resolve_stash_cwd only ACTS on the two it recognises, so
# being permissive here again only costs a parser call, never a wrong verdict).
if echo "$COMMAND_ASK_SCAN" | grep -qE '(^|[;&|(]|[[:space:]])([A-Za-z_][A-Za-z0-9_]*=[^;&|[:space:]]*[[:space:]]+)*git[[:space:]]+([^;&|]*[[:space:]]+)?stash[[:space:]]+(pop|drop|clear)([[:space:]]|$)' \
   && stash_scope_guard_enabled; then
    _stash_effective_cwd="$CWD"
    _stash_effective_gitdir=""
    _stash_effective_worktree=""
    if [[ -n "$CWD" ]]; then
        _stash_resolved=$(resolve_stash_cwd "$COMMAND_NO_COMMENT" "$CWD")
        _stash_effective_cwd=$(printf '%s\n' "$_stash_resolved" | sed -n '1p')
        _stash_effective_gitdir=$(printf '%s\n' "$_stash_resolved" | sed -n '2p')
        _stash_effective_worktree=$(printf '%s\n' "$_stash_resolved" | sed -n '3p')
        [[ -z "$_stash_effective_cwd" ]] && _stash_effective_cwd="$CWD"
    fi
    # Shell-accurate quote removal for cwd/gitdir/worktree RESOLUTION only —
    # resolve_stash_cwd() threads these from the RAW argument (quotes intact),
    # so unquote a COPY of each before resolving against the filesystem. An
    # unterminated quote falls back to the raw value (today's verdict —
    # ambiguous/ask), never widening to allow.
    if [[ "$_stash_effective_cwd" == *"'"* || "$_stash_effective_cwd" == *'"'* ]]; then
        strip_target_quoting "$_stash_effective_cwd" && _stash_effective_cwd="$_UNQUOTED_TARGET"
    fi
    if [[ "$_stash_effective_gitdir" == *"'"* || "$_stash_effective_gitdir" == *'"'* ]]; then
        strip_target_quoting "$_stash_effective_gitdir" && _stash_effective_gitdir="$_UNQUOTED_TARGET"
    fi
    if [[ "$_stash_effective_worktree" == *"'"* || "$_stash_effective_worktree" == *'"'* ]]; then
        strip_target_quoting "$_stash_effective_worktree" && _stash_effective_worktree="$_UNQUOTED_TARGET"
    fi

    _stash_toplevel=""
    _stash_common_parent=""
    # Every `git --git-dir=…` probe below must run from the SAME directory the
    # real command runs from (repo#204 review): git resolves --show-toplevel by
    # cwd-based worktree inference whenever --work-tree/GIT_WORK_TREE is absent,
    # so a bare `git --git-dir=… rev-parse --show-toplevel` answers for the
    # GUARD process cwd, not for the command being judged. That silently
    # allowed `GIT_DIR=<main>/.git git -C <main> stash pop` issued from a linked
    # worktree — the toplevel came back as the guard cwd, never matched the
    # main checkout common-dir parent, and fell through the collision branch.
    # Threading the resolved cwd through -C makes the probe ask git the same
    # question the command asks. A cwd that is not a directory makes `git -C`
    # fail, leaving toplevel/common empty -> the cd-unresolved ask, which is
    # the intended fail-safe (never a widened allow).
    _stash_gitdir_cd=()
    [[ -n "$_stash_effective_cwd" ]] && _stash_gitdir_cd=(-C "$_stash_effective_cwd")
    if [[ -n "$_stash_effective_gitdir" ]]; then
        # --git-dir / GIT_DIR override (repo#202). --git-dir names a .git
        # DIRECTORY, not a worktree path, so this resolves scope by querying
        # git directly through the override (--git-common-dir) instead of
        # cd-ing into it and running the -C-style rev-parse below — cd-ing
        # into a .git directory and asking for --show-toplevel is not the same
        # operation git performs when --git-dir/--work-tree are passed
        # explicitly. An explicit --work-tree/GIT_WORK_TREE (present in both
        # of #202's reproduction shapes) is exactly what git itself would use
        # for the toplevel side, so prefer it over asking git to guess one.
        if [[ -n "$_stash_effective_worktree" && -d "$_stash_effective_worktree" ]]; then
            _stash_toplevel=$(cd "$_stash_effective_worktree" 2>/dev/null && pwd -P) || _stash_toplevel=""
        elif [[ -e "$_stash_effective_gitdir" ]]; then
            _stash_toplevel=$(git "${_stash_gitdir_cd[@]}" --git-dir="$_stash_effective_gitdir" rev-parse --show-toplevel 2>/dev/null) || _stash_toplevel=""
            [[ -n "$_stash_toplevel" && -d "$_stash_toplevel" ]] && \
                _stash_toplevel=$(cd "$_stash_toplevel" 2>/dev/null && pwd -P) || _stash_toplevel=""
        fi

        if [[ -e "$_stash_effective_gitdir" ]]; then
            _stash_common=$(git "${_stash_gitdir_cd[@]}" --git-dir="$_stash_effective_gitdir" rev-parse --git-common-dir 2>/dev/null) || _stash_common=""
            if [[ -n "$_stash_common" ]]; then
                case "$_stash_common" in
                    /*) : ;;
                    *) _stash_common="$_stash_effective_gitdir/$_stash_common" ;;
                esac
                [[ -d "$_stash_common" ]] && \
                    _stash_common_parent=$(cd "$_stash_common/.." 2>/dev/null && pwd -P) || _stash_common_parent=""
            fi
        fi
    elif [[ -n "$_stash_effective_cwd" && -d "$_stash_effective_cwd" ]]; then
        _stash_toplevel=$(cd "$_stash_effective_cwd" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null) || _stash_toplevel=""
        [[ -n "$_stash_toplevel" && -d "$_stash_toplevel" ]] && \
            _stash_toplevel=$(cd "$_stash_toplevel" 2>/dev/null && pwd -P) || _stash_toplevel=""

        _stash_common=$(cd "$_stash_effective_cwd" 2>/dev/null && git rev-parse --git-common-dir 2>/dev/null) || _stash_common=""
        if [[ -n "$_stash_common" ]]; then
            _stash_common_parent=$(cd "$_stash_effective_cwd" 2>/dev/null && cd "$_stash_common/.." 2>/dev/null && pwd -P) || _stash_common_parent=""
        fi
    fi

    if [[ -n "$_stash_toplevel" && -n "$_stash_common_parent" && "$_stash_toplevel" == "$_stash_common_parent" ]]; then
        ask "Command requires confirmation: $COMMAND (git stash pop/drop/clear in the MAIN checkout can destroy operator-preserved state — the main checkout's stash stack is operator-owned, not scratch space for an integration check. Run test-merges in an isolated worktree instead; set guards.stashScope:false / REPO_GUARD_STASH_SCOPE=0 to disable this ask)" "stash-scope:main-checkout"
    elif [[ -n "$_stash_toplevel" && -n "$_stash_common_parent" ]]; then
        # cwd is a linked worktree, not the main checkout. Count the repo's
        # linked worktrees as git reports them — a collision needs at least one
        # other active worktree to race with.
        if [[ -n "$_stash_effective_gitdir" ]]; then
            _stash_worktree_count=$(git "${_stash_gitdir_cd[@]}" --git-dir="$_stash_effective_gitdir" worktree list --porcelain 2>/dev/null | grep -c '^worktree ') || _stash_worktree_count=0
        else
            _stash_worktree_count=$(cd "$_stash_effective_cwd" 2>/dev/null && \
                git worktree list --porcelain 2>/dev/null | grep -c '^worktree ') || _stash_worktree_count=0
        fi
        [[ "$_stash_worktree_count" =~ ^[0-9]+$ ]] || _stash_worktree_count=0

        # >=3 entries = the main checkout plus two or more linked worktrees, so
        # some OTHER worktree exists besides this one to collide with.
        if [[ "$_stash_worktree_count" -ge 3 ]]; then
            ask "Command requires confirmation: $COMMAND (git stash pop/drop/clear from a linked worktree can destroy ANOTHER agent's WIP — refs/stash is a single stack SHARED across every linked worktree of this repo, not per-worktree, and $((_stash_worktree_count - 1)) linked worktrees are currently active. Use a per-worktree WIP ref instead of the shared stash stack; set guards.stashScope:false / REPO_GUARD_STASH_SCOPE=0 to disable this ask)" "stash-scope:worktree-collision"
        fi
    elif [[ "$_stash_effective_cwd" != "$CWD" || -n "$_stash_effective_gitdir" ]]; then
        # A `cd <dir>` prefix, or a --git-dir/GIT_DIR override, resolved to a
        # target that does not exist or is not inside any git checkout —
        # ambiguous. Fail toward asking rather than guessing (mirrors
        # parse_force_ops' detached-HEAD fail-safe).
        ask "Command requires confirmation: $COMMAND (the cd/--git-dir target for this stash operation could not be resolved to a git checkout, so scope cannot be determined — refusing to silently allow an ambiguous stash pop/drop/clear; set guards.stashScope:false / REPO_GUARD_STASH_SCOPE=0 to disable this ask)" "stash-scope:cd-unresolved"
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

# Scanned against COMMAND_ASK_SCAN, not the raw COMMAND_NO_COMMENT (repo#188
# parity fix) — same reasoning as the SQL DDL and force-op blocks above, and
# the same copy the ungated ASK_PATTERNS loop already used. A cloud verb quoted
# inside an issue body is documentation, not an invocation.
for pattern in "${CLOUD_ASK_PATTERNS[@]}"; do
    if echo "$COMMAND_ASK_SCAN" | grep -qE "$pattern" && cloud_guard_enabled; then
        ask "Command requires confirmation: $COMMAND (set guards.cloudCli:false in .claude/skills/repo/config.json if this repo manages cloud infra as a first-class workflow)" "cloud-cli:$pattern"
    fi
done

# =============================================================================
# NOTE: This file is the CANONICAL generic repository-hygiene guard
# (rjwalters/repo#30). Loom-workflow-specific guards (the 'gh pr merge' →
# merge-pr.sh redirect, the 'pip install -e' worktree block) live in Loom's
# guard-loom-workflow.sh, registered as a separate PreToolUse/Bash hook that
# fires independently of this one — orchestration concerns stay with the
# orchestrator, generic protection lives here.
# =============================================================================

# =============================================================================
# ALLOW - Everything else passes through
# =============================================================================

exit 0
