#!/usr/bin/env bash
# guard-destructive-generic.sh - VENDORED copy of Repo Skills' generic guard.
#
# ============================================================================
# VENDORED COPY — the canonical home of this generic destructive-command guard
# is Repo Skills (https://github.com/rjwalters/repo →
# hooks/repo/guard-destructive.sh, installed into consumer repos at
# .claude/skills/repo/hooks/guard-destructive.sh). This file is a clearly-marked
# vendored copy shipped by Loom ONLY so that standalone-Loom repos (no Repo
# Skills installed) keep full destructive-command coverage. See issue #4041.
#
# It is NEVER run directly in a repo that has the canonical Repo Skills guard:
# the guard-destructive.sh DISPATCHER (in this same directory) prefers the
# canonical guard when it is present and carries the rjwalters/repo#29 fix, and
# only falls back to this vendored copy otherwise. Loom's installer / resync
# likewise skips installing this file entirely when the canonical guard is
# present.
#
# DO NOT hand-edit generic pattern behavior here — send fixes upstream to
# Repo Skills so the canonical copy (and every consumer of it) benefits. Loom
# re-vendors this file from the upstream canonical guard at release time.
# ============================================================================
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

# Decision telemetry log (issue #3771) — a SEPARATE JSONL file from
# HOOK_ERROR_LOG. At runtime SCRIPT_DIR is the installed hook's own directory
# (.loom/hooks/), so this resolves to .loom/logs/guard-decisions.log in a real
# install. LOOM_GUARD_DECISION_LOG_FILE overrides the path (a test seam; also
# lets an operator point the log elsewhere). Off by default — see
# decision_log_enabled() below.
DECISION_LOG="${LOOM_GUARD_DECISION_LOG_FILE:-${SCRIPT_DIR}/../logs/guard-decisions.log}"

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
#   jq -r '.pattern' .loom/logs/guard-decisions.log | sort | uniq -c | sort -rn
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
# up from CWD to the nearest Loom config root WITHOUT forking git
# (fastpath_config_root). So a fast-pathed command pays: 1 bash-builtin test +
# (only if eligible) 1 stat-walk + up to 2 bounded jq reads (project tier, then
# legacy tier only if the key is absent from project) — never the git
# rev-parse, never a deny/ask array, never a log write.
#
# Toggle: guards.readOnlyFastPath (default true) / LOOM_GUARD_READONLY_FASTPATH
# env (0/false/no disables, 1/true/yes forces on; env wins). Optional
# guards.readOnlyFastPathExtra is an EXTEND-ONLY array of literal first-word
# commands (each entry is a full-generality bypass for that command word), minus
# the reserved words the escape hatch may not claim (#4791 — see
# _fastpath_extra_reserved below: no denial-floor command word, no shell/exec
# wrapper, so no .loom/config.json can fast-path past the ungated floor).
# =============================================================================

# CARVE-OUT (#4063, UPDATED for Epic #3835 Phase 5, #4262): the fast-path
# config readers below — fastpath_config_root, _fastpath_tiered_get,
# _fastpath_tiered_get_array, fastpath_enabled, fastpath_extra_admits — are
# deliberately NOT migrated to the shared config-resolver.sh (loom_config_get /
# loom_resolve_config) that the cold-path toggles use. This is intentional and
# preserves issue #3687's fork budget:
#   * This block can `exit 0` (silent allow) BEFORE REPO_ROOT is resolved — the
#     `git -C "$CWD" rev-parse --show-toplevel` fork lives strictly below the
#     fast-path dispatch. loom_resolve_config REQUIRES a repo_root as its first
#     argument, so routing these through it would force hoisting that git
#     rev-parse above the fast-path exit, directly regressing #3687 (which
#     removed it from the pre-admission path).
#   * fastpath_config_root is a fork-free bash-builtin upward directory walk
#     (now stat-ing for EITHER `.loom-project/project.json` or
#     `.loom/config.json`, so a `.loom-project/`-only repo is still found);
#     fastpath_enabled / fastpath_extra_admits each cost AT MOST two CACHED,
#     file-scoped jq reads (project tier, then legacy tier ONLY when the key
#     is absent from the project tier — #4262's Epic #3835 `.loom-project/`
#     tier reaching this toggle without forking the full merge).
#     loom_resolve_config is uncached and soft-reads every tier file plus a
#     merge (4+ forks today) on EVERY call — a 4x+ regression on a hook that
#     fires before every Bash tool call. Caching the merge (Option B) still
#     needs a repo_root, and teaching the resolver a fork-free upward-walk
#     root discovery (Option C) would break the documented three-language
#     (Rust/Python/Bash) conformance-fixture contract in config-resolver.sh's
#     header. So the fast path keeps its direct, bounded (<=2 fork) reads
#     instead. See #4063 for the original analysis and #4262 for the tier
#     widening.
#
# Locate the nearest Loom config ROOT by walking up from CWD, fork-free (no
# git rev-parse) — a directory holding EITHER the tracked project tier
# (.loom-project/project.json, Epic #3835) OR the legacy tier
# (.loom/config.json). Cached. Best-effort: empty when neither is found.
#
# Epic #3835 Phase 5 (#4262): previously this walked looking for
# .loom/config.json alone. Widening the stat to "either tier file" keeps the
# walk itself fork-free (bash-builtin [[ -f ]] tests only) while letting the
# two readers below (fastpath_enabled / fastpath_extra_admits) consult
# .loom-project/project.json — see the CARVE-OUT comment above for why this
# stays a direct, bounded-fork jq read instead of routing through
# loom_resolve_config().
_FASTPATH_CFG_ROOT=""
_FASTPATH_CFG_ROOT_DONE=""
fastpath_config_root() {
    if [[ -z "$_FASTPATH_CFG_ROOT_DONE" ]]; then
        _FASTPATH_CFG_ROOT_DONE=1
        local d="$CWD"
        if [[ -n "$d" && "$d" == /* ]]; then
            while :; do
                if [[ -f "$d/.loom-project/project.json" || -f "$d/.loom/config.json" ]]; then
                    _FASTPATH_CFG_ROOT="$d"
                    break
                fi
                [[ "$d" == "/" ]] && break
                local parent="${d%/*}"
                [[ -z "$parent" ]] && parent="/"
                d="$parent"
            done
        fi
    fi
    printf '%s' "$_FASTPATH_CFG_ROOT"
}

# Read a dotted-path boolean-ish scalar from the tiered fast-path config: try
# the tracked project tier first (.loom-project/project.json), falling back to
# the legacy tier (.loom/config.json) ONLY when the key is absent from the
# project tier — this mirrors the resolver's whole-value tier precedence (a
# higher tier that sets the key wins outright, it is not merged with a lower
# tier). At most two jq forks, both file-scoped (no directory-file soft-read
# fan-out, no merge) — the bounded-fork budget this CARVE-OUT preserves.
# Echoes the resolved value, or empty when the key is absent from both tiers.
_fastpath_tiered_get() {
    local dotted="$1" root cfg value
    root=$(fastpath_config_root)
    [[ -n "$root" ]] || { printf ''; return 0; }

    cfg="$root/.loom-project/project.json"
    if [[ -f "$cfg" ]]; then
        value=$(jq -r --arg p "$dotted" '
            ($p | split(".")) as $path
            | try getpath($path) catch null
            | if . == null then empty else . end
        ' "$cfg" 2>/dev/null) || value=""
        [[ -n "$value" ]] && { printf '%s' "$value"; return 0; }
    fi

    cfg="$root/.loom/config.json"
    if [[ -f "$cfg" ]]; then
        value=$(jq -r --arg p "$dotted" '
            ($p | split(".")) as $path
            | try getpath($path) catch null
            | if . == null then empty else . end
        ' "$cfg" 2>/dev/null) || value=""
        [[ -n "$value" ]] && { printf '%s' "$value"; return 0; }
    fi

    printf ''
}

# Same tier precedence as _fastpath_tiered_get, but for an array-valued key:
# echoes the elements of guards.<name> one per line, from whichever tier's
# file HAS the key first (project, else legacy) — an array value at a tier is
# a whole-value override, never merged element-wise with a lower tier (this
# matches the resolver's jq `*` deep-merge semantics for non-object values).
# Echoes nothing when the key is absent from both tiers.
_fastpath_tiered_get_array() {
    local dotted="$1" root cfg has
    root=$(fastpath_config_root)
    [[ -n "$root" ]] || return 0

    cfg="$root/.loom-project/project.json"
    if [[ -f "$cfg" ]]; then
        has=$(jq -r --arg p "$dotted" '($p | split(".")) as $path | try (getpath($path) != null) catch false' "$cfg" 2>/dev/null) || has=false
        if [[ "$has" == "true" ]]; then
            jq -r --arg p "$dotted" '($p | split(".")) as $path | getpath($path) | (. // [])[]' "$cfg" 2>/dev/null
            return 0
        fi
    fi

    cfg="$root/.loom/config.json"
    if [[ -f "$cfg" ]]; then
        jq -r --arg p "$dotted" '($p | split(".")) as $path | try (getpath($path) // []) catch [] | .[]' "$cfg" 2>/dev/null
    fi
}

# Resolve the fast-path toggle (config + env), cached. Default true. Only ever
# called after structural admission has already passed, so the jq read stays off
# the hot path for commands that don't structurally qualify.
_FASTPATH_ENABLED_CACHE=""
fastpath_enabled() {
    if [[ -z "$_FASTPATH_ENABLED_CACHE" ]]; then
        local enabled=true raw
        # Only an explicit `false` disables; an absent key on both tiers, or
        # malformed JSON, stays ON — mirrors sql_guard_enabled().
        raw=$(_fastpath_tiered_get "guards.readOnlyFastPath")
        [[ "$raw" == "false" ]] && enabled=false
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

# Read-only search-pipe-to-sink admission (#5263). Pure bash builtins, zero forks.
#
# The shared fastpath_structural_ok() above disqualifies EVERY pipe, which is
# correct for the general case but produces a self-defeating false positive for
# the single most common interactive idiom: piping a read-only search to a pager
# or counter. `grep 'DROP TABLE' schema.sql | head` is 100% read-only — the DDL
# phrase lives only inside grep's quoted search argument, and grep never executes
# what it matches — yet the pipe kicked it to the full path, where SQL_DDL_PATTERN
# (:~2819) substring-matches the literal phrase in grep's own argument and denies
# at the catastrophic tier. The bare `grep 'DROP TABLE' schema.sql` (no pipe) was
# already fast-pathed and allowed; this narrows the gap so the piped form matches.
#
# SECURITY: this is a deliberately NARROW carve-out, not a general "pipes are OK"
# relaxation. It admits ONLY the shape
#     <search> | <read-only-sink>
# where:
#   * exactly ONE pipe, and NO other shell metacharacter (; & < > ` $( newline)
#     anywhere — so wrapper (`bash -c`), substitution (`$(...)`), and compound
#     (`&&`/`;`) forms are untouched and keep denying via the full path exactly
#     as before (satisfying #5263's "obfuscation still caught" requirement);
#   * the UPSTREAM command word is a non-executing search: grep|egrep|fgrep|rg
#     (grep/rg are already fully admitted for any args by the built-in allowlist;
#     egrep/fgrep are the same tool). A real DDL-executing command piped the same
#     way (`mysql -e '…' | cat`, `psql -c '…' | head`) has a non-search first
#     token, so it is NOT admitted and still denies;
#   * the DOWNSTREAM command word is a fixed read-only sink allowlist. head|tail|wc
#     are already fully allowlisted (any args), so they admit with any args. cat,
#     less, and more are NOT in the built-in allowlist — cat has a live `.ssh`/
#     `.aws/credentials` ASK carve-out (:~3764) — so they admit ONLY as pure stdin
#     consumers (no positional file operand). This keeps `grep x | cat ~/.ssh/id_rsa`
#     (which would leak a key past the cat ASK) OUT of the fast path: it falls
#     through to the full path where the cat ASK still fires.
#
# False NEGATIVES (declining) are always safe — they just fall through to the
# existing slower behavior. So anything not matching the exact shape above (a
# second pipe, an unlisted sink, a non-search upstream, a cat/less/more with a
# file operand) declines and is handled by the full path unchanged.
_FASTPATH_PIPE_SINKS_ANYARG=" head tail wc "     # already fully allowlisted → any args
_FASTPATH_PIPE_SINKS_STDIN=" cat less more "     # stdin-only → no positional operand
fastpath_grep_pipe_admits() {
    local cmd="$1"
    # No shell metacharacter other than a single pipe. Reject substitution,
    # redirection, chaining, backticks, and newlines outright.
    case "$cmd" in
        *';'*|*'&'*|*'<'*|*'>'*|*'`'*|*'$('*) return 1 ;;
    esac
    [[ "$cmd" == *$'\n'* ]] && return 1
    [[ "$cmd" == *'|'* ]] || return 1
    local left="${cmd%%|*}"
    local right="${cmd#*|}"
    # Exactly one pipe: a second one (`grep a | grep b | head`) declines here and
    # falls through to the full path (conservative — a false negative, not a hole).
    case "$right" in
        *'|'*) return 1 ;;
    esac
    local -a lt rt
    read -ra lt <<< "$left"
    read -ra rt <<< "$right"
    (( ${#lt[@]} >= 1 && ${#rt[@]} >= 1 )) || return 1
    # Upstream must be a non-executing search command.
    case "${lt[0]}" in
        grep|egrep|fgrep|rg) ;;
        *) return 1 ;;
    esac
    # Downstream must be a read-only sink.
    local sink="${rt[0]}"
    if [[ "$_FASTPATH_PIPE_SINKS_ANYARG" == *" $sink "* ]]; then
        return 0
    fi
    if [[ "$_FASTPATH_PIPE_SINKS_STDIN" == *" $sink "* ]]; then
        # Pure stdin consumer only: reject any positional (non-flag) operand so a
        # credential-file argument (`| cat ~/.ssh/id_rsa`) is NOT fast-pathed and
        # the cat `.ssh`/`.aws` ASK carve-out still fires via the full path.
        local i
        for (( i = 1; i < ${#rt[@]}; i++ )); do
            case "${rt[i]}" in
                -*) ;;          # a flag (e.g. -n, -N) — fine
                *) return 1 ;;  # a positional file operand — decline
            esac
        done
        return 0
    fi
    return 1
}

# Optional extend-only escape hatch: guards.readOnlyFastPathExtra is an array of
# literal first-word commands. Read lazily (only when the built-in list did not
# admit) and cached. Each entry is a full-generality bypass for that word.
#
# RESERVED WORDS (#4791) — the escape hatch may NOT reach past the ungated
# denial floor. The fast path runs before ALWAYS_BLOCK_PATTERNS, so any word
# admitted here skips the floor entirely for every argument shape; the built-in
# allowlist above is safe by construction (its `git`/`gh`/`aws` entries are
# verb-scoped and no floor member survives the structural test under the other
# entries), but a configured entry is not verb-scoped at all. Before #4791,
# {"guards":{"readOnlyFastPathExtra":["rm"]}} silently fast-pathed `rm -rf /` to
# an allow — i.e. a .loom/config.json COULD disable a floor deny, the exact
# premise defaults/docs/guard-hooks.md now documents as false. So a configured
# entry naming a floor command word, or any shell/exec wrapper that can carry an
# arbitrary payload as an argument, is IGNORED and the command falls through to
# the full deny/ask path.
#
# Fail direction is deliberate: rejecting an entry can only make the guard do
# MORE work, never less, so a false positive here costs a few forks on one
# command word and can never open a hole. Kept as a bash `case` (zero forks) and
# checked BEFORE the config read, so a reserved word never even pays for the
# lazy jq/array read. Silent by design — the fast path emits nothing on any
# path, and the operator sees the effect immediately (the command is evaluated
# normally, with the guard's own reason if it denies).
_fastpath_extra_reserved() {
    case "$1" in
        # Denial-floor command words (ALWAYS_BLOCK_PATTERNS + the segment-parsed
        # system-lifecycle deny + the `--body @path` denies).
        rm|git|gh|aws|docker|curl|wget|halt|reboot|poweroff|shutdown|init)
            return 0 ;;
        # Shell / exec wrappers: admitting one of these admits ANY payload it is
        # handed, which would bypass the floor transitively. The built-in
        # allowlist excludes them for the same reason.
        sudo|doas|env|eval|exec|xargs|nohup|timeout|ssh|bash|sh|zsh|ksh|dash|fish|python|python3|perl|ruby|node)
            return 0 ;;
    esac
    return 1
}

_FASTPATH_EXTRA_CACHE=""
_FASTPATH_EXTRA_DONE=""
fastpath_extra_admits() {
    local cmd="$1"
    fastpath_structural_ok "$cmd" || return 1
    local -a t
    read -ra t <<< "$cmd"
    (( ${#t[@]} >= 1 )) || return 1
    local first="${t[0]}"
    _fastpath_extra_reserved "$first" && return 1
    if [[ -z "$_FASTPATH_EXTRA_DONE" ]]; then
        _FASTPATH_EXTRA_DONE=1
        _FASTPATH_EXTRA_CACHE=$(_fastpath_tiered_get_array "guards.readOnlyFastPathExtra" 2>/dev/null) || _FASTPATH_EXTRA_CACHE=""
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
    elif fastpath_grep_pipe_admits "$COMMAND"; then
        # Read-only search piped to a read-only sink (#5263) — same silent allow.
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

# Shared config-tier resolver (#4063). Source defaults/scripts/lib/config-resolver.sh
# — deliberately sourced HERE, strictly below the #3687 fast-path dispatch and
# REPO_ROOT resolution above, so a fast-pathed command pays ZERO added cost (not
# even the `[[ -f ]]` stat below) — this is the cold path only. At runtime
# SCRIPT_DIR is the installed hook dir (.loom/hooks/), and .loom/scripts is a
# symlink to defaults/scripts, so ../scripts/lib resolves; in the test harness
# SCRIPT_DIR is defaults/hooks/ and the sibling path resolves directly. The
# COLD-PATH toggle readers below (sql/cloud/reversibleGh/decisionLog/rmScope/
# forceScope + worktree.root) call loom_config_get through this so a single code
# path reads the full tier chain (legacy .loom/config.json plus the #4039
# project/local tiers) and stays byte-for-byte in lockstep with loom-daemon and
# loom_tools. Best-effort: a missing/unsourceable lib leaves loom_config_get
# undefined, and each reader's `|| <default>` fallback then preserves that
# guard's safe default, so the guard never breaks.
if [[ -f "$SCRIPT_DIR/../scripts/lib/config-resolver.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/../scripts/lib/config-resolver.sh" 2>/dev/null || true
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
        local enabled=true raw
        if [[ -n "$REPO_ROOT" ]]; then
            # Migrated to the shared tier resolver (#4063). loom_config_get
            # collapses null and missing to the default, so we KEEP the exact
            # polarity in bash: only an explicit boolean `false` disables — a
            # missing/null key OR a non-boolean value (e.g. "yes") stays guard-ON,
            # matching the old `.guards.sqlDdl == false` test. `|| raw=true` also
            # covers config-resolver.sh failing to source (loom_config_get unset)
            # and malformed JSON (the resolver soft-reads a bad tier as {} → the
            # key resolves absent → default "true").
            raw=$(loom_config_get "$REPO_ROOT" "guards.sqlDdl" "true" 2>/dev/null) || raw=true
            [[ "$raw" == "false" ]] && enabled=false
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
        local enabled=true raw
        if [[ -n "$REPO_ROOT" ]]; then
            # Migrated to the shared tier resolver (#4063), same polarity as
            # sql_guard_enabled(): only an explicit boolean `false` disables; a
            # missing/null key, a non-boolean value, or malformed JSON stays
            # guard-ON via the "true" default and the `|| raw=true` fallback.
            raw=$(loom_config_get "$REPO_ROOT" "guards.cloudCli" "true" 2>/dev/null) || raw=true
            [[ "$raw" == "false" ]] && enabled=false
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
#   1. LOOM_GUARD_REVERSIBLE_GH env var (1/true/yes enables the ask,
#      0/false/no forces it off)
#   2. .loom/config.json  ->  guards.reversibleGh  (default false when absent)
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
        local enabled=false raw
        if [[ -n "$REPO_ROOT" ]]; then
            # Migrated to the shared tier resolver (#4063). INVERSE polarity of
            # sql/cloud: only an explicit boolean `true` enables the ask; a
            # missing/null key, a non-boolean value, or malformed JSON stays
            # guard-OFF via the "false" default and the `|| raw=false` fallback.
            raw=$(loom_config_get "$REPO_ROOT" "guards.reversibleGh" "false" 2>/dev/null) || raw=false
            [[ "$raw" == "true" ]] && enabled=true
        fi
        # Env override wins over config.
        case "${LOOM_GUARD_REVERSIBLE_GH:-}" in
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
#   1. LOOM_GUARD_DECISION_LOG env var (1/true/yes/on enables; 0/false/no/off
#      disables). Overrides config.
#   2. .loom/config.json  ->  guards.decisionLog  (default false when absent).
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
        local enabled=false raw
        if [[ -n "$REPO_ROOT" ]]; then
            # Migrated to the shared tier resolver (#4063). INVERSE polarity like
            # reversible_gh_guard_enabled(): only an explicit boolean `true`
            # enables; a missing/null key, a non-boolean value, or malformed JSON
            # stays OFF via the "false" default and the `|| raw=false` fallback.
            raw=$(loom_config_get "$REPO_ROOT" "guards.decisionLog" "false" 2>/dev/null) || raw=false
            [[ "$raw" == "true" ]] && enabled=true
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
        local mode=repo raw
        if [[ -n "$REPO_ROOT" ]]; then
            # Migrated to the shared tier resolver (#4063). This is a string
            # value, not a boolean, so we read the raw string and keep the
            # branching in bash: only an explicit "off"/"permissive" opts out;
            # a missing/null key (→ default "repo"), any other string, or
            # malformed JSON (→ `|| raw=repo`) resolves to the safe "repo".
            raw=$(loom_config_get "$REPO_ROOT" "guards.rmScope" "repo" 2>/dev/null) || raw=repo
            case "$raw" in
                off|permissive)  mode=off ;;
                *)               mode=repo ;;
            esac
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
    # 2. Config key worktree.root (absolute only), via the shared tier resolver
    #    (#4063). Only the config READ is routed through loom_config_get — the
    #    env/default precedence and the absolute-path gate stay inline so the
    #    function keeps its self-contained fallback shape. loom_config_get's
    #    default "" collapses a missing/null key to empty (matching the old
    #    `.worktree.root? // empty`), and a non-absolute value fails the `== /*`
    #    gate and falls through to the in-repo default, exactly as before.
    local cfg_root
    cfg_root=$(loom_config_get "$repo_root" "worktree.root" "" 2>/dev/null) || cfg_root=""
    if [[ -n "$cfg_root" && "$cfg_root" == /* ]]; then
        printf '%s/%s' "${cfg_root%/}" "$(basename "$repo_root")"
        return 0
    fi
    # 3. Default — in-repo worktrees dir.
    printf '%s/.loom/worktrees' "$repo_root"
}

# =============================================================================
# worktree-isolation toggle — Bash-tool write confinement (issue #4178).
#
# guard-worktree-paths.sh confines the Edit/Write TOOL matcher to a builder's
# issue worktree, but nothing confined the Bash tool: `>`/`>>` redirection,
# `tee`, `sed -i`, `cp`/`mv` all write files without ever going through
# Edit/Write, so a session denied on Edit/Write could fall back to Bash and
# land the same write in the main checkout. Sweep #4063 used exactly this
# escape to edit live guard hooks in the main checkout while its own worktree
# stayed clean (see the issue's root-cause writeup / hook-error-log timeline).
#
# This reuses the SAME toggle guard-worktree-paths.sh already exposes
# (guards.worktreeIsolation / LOOM_GUARD_WORKTREE_ISOLATION) — one switch, not
# two — so a repo/session that already opted out of Edit/Write confinement
# gets the identical Bash-write confinement decision, and the documented
# escape hatch (a human/driver session that must edit the main checkout while
# worktrees exist) keeps working here too.
#
# Resolution order (highest precedence first), mirroring every other guard
# toggle in this file:
#   1. LOOM_GUARD_WORKTREE_ISOLATION env var (0/false/no disables, 1/true/yes
#      forces on). Overrides config.
#   2. .loom/config.json (or a higher config-resolver tier) -> guards.worktreeIsolation
#      (default true when absent)
#   3. Default: true (guard on)
#
# The config read is best-effort: any parse failure falls through to guard-ON
# and never trips the ERR trap.
#
# Migrated to the shared tier resolver (#4241; this reader postdated #4063's
# migration pass, see issue #4241). Same polarity contract as every other
# cold-path toggle in this file (sql_guard_enabled() et al.): loom_config_get
# collapses null/missing to the "true" default, so only an explicit boolean
# `false` disables — a missing/null key, a non-boolean value, or malformed
# JSON/config-resolver.sh failing to source all stay guard-ON via `|| raw=true`.
# This runs on the Bash write-scope path (after REPO_ROOT is already resolved
# for the cold-path toggles above), not the #3687 read-only fast path, so the
# extra jq forks loom_config_get costs are not a hot-path regression.
# =============================================================================
_WORKTREE_ISOLATION_CACHE=""
worktree_isolation_guard_enabled() {
    if [[ -z "$_WORKTREE_ISOLATION_CACHE" ]]; then
        local enabled=true raw
        if [[ -n "$REPO_ROOT" ]]; then
            raw=$(loom_config_get "$REPO_ROOT" "guards.worktreeIsolation" "true" 2>/dev/null) || raw=true
            [[ "$raw" == "false" ]] && enabled=false
        fi
        case "${LOOM_GUARD_WORKTREE_ISOLATION:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _WORKTREE_ISOLATION_CACHE="$enabled"
    fi
    [[ "$_WORKTREE_ISOLATION_CACHE" == "true" ]]
}

# =============================================================================
# Stash-scope guard toggle — default ON (#4281).
#
# The main checkout's stash stack is operator-owned: preserved diagnostic state
# (contamination evidence, deliberately-parked WIP) can sit there indefinitely,
# and a role subagent doing an ad-hoc integration check has no way to tell
# "this is scratch" from "this is evidence" before popping it. The 2026-07-28
# incident saw a Judge's throwaway main-checkout test-merge run `git stash pop`
# against a deliberately-preserved stash entry; only a merge conflict on the
# pop kept it from being silently dropped with no recovery path.
#
# Resolution order (highest precedence first), mirroring every other guard
# toggle in this file:
#   1. LOOM_GUARD_STASH_SCOPE env var (0/false/no disables, 1/true/yes forces
#      on). Overrides config.
#   2. .loom/config.json (or a higher config-resolver tier) -> guards.stashScope
#      (default true when absent)
#   3. Default: true (guard on)
#
# The config read is best-effort: any parse failure falls through to guard-ON
# and never trips the ERR trap. Invoked LAZILY — only after a stash
# pop/drop/clear pattern has already matched — so the config read never
# touches the hot path for the vast majority of commands that are not stash
# operations.
# =============================================================================
_STASH_SCOPE_CACHE=""
stash_scope_guard_enabled() {
    if [[ -z "$_STASH_SCOPE_CACHE" ]]; then
        local enabled=true raw
        if [[ -n "$REPO_ROOT" ]]; then
            raw=$(loom_config_get "$REPO_ROOT" "guards.stashScope" "true" 2>/dev/null) || raw=true
            [[ "$raw" == "false" ]] && enabled=false
        fi
        case "${LOOM_GUARD_STASH_SCOPE:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _STASH_SCOPE_CACHE="$enabled"
    fi
    [[ "$_STASH_SCOPE_CACHE" == "true" ]]
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
    local out="" c
    local n=${#tok}
    local i=0 in_s=0 in_d=0
    while [[ $i -lt $n ]]; do
        c="${tok:i:1}"
        if [[ $in_s -eq 1 ]]; then
            # Inside '…': nothing expands; only the closing quote is special.
            if [[ "$c" == "'" ]]; then in_s=0; else out+="$c"; fi
            i=$((i + 1))
            continue
        fi
        case "$c" in
            "'")
                if [[ $in_d -eq 1 ]]; then out+="$c"; else in_s=1; fi ;;
            '"')
                if [[ $in_d -eq 1 ]]; then in_d=0; else in_d=1; fi ;;
            '\')
                # Escapes the NEXT character (a trailing backslash is dropped).
                i=$((i + 1))
                [[ $i -lt $n ]] && out+="${tok:i:1}" ;;
            '$')
                if [[ -n "$dollar" ]]; then out+="$dollar"; else out+="$c"; fi ;;
            *)
                out+="$c" ;;
        esac
        i=$((i + 1))
    done
    _SCANNED_TOKEN="$out"
    [[ $in_s -eq 0 && $in_d -eq 0 ]]
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
        local mode=all raw
        if [[ -n "$REPO_ROOT" ]]; then
            # Migrated to the shared tier resolver (#4063). String value like
            # guards.rmScope, so read the raw string and branch in bash: only
            # "protected"/"off" opt away from the default; a missing/null key
            # (→ default "all"), any other value, or malformed JSON (→ `|| raw=all`)
            # resolves to "all".
            raw=$(loom_config_get "$REPO_ROOT" "guards.forceScope" "all" 2>/dev/null) || raw=all
            case "$raw" in
                protected)  mode=protected ;;
                off)        mode=off ;;
                *)          mode=all ;;
            esac
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
# The token VALUES are preserved verbatim (unlike a redaction approach), so
# extract_rm_targets still sees the real `rm` targets. Best-effort like
# strip_literal_text(): backslash-escaped quotes and an unterminated quote fall
# back to the old separator-active behaviour, never widening a deny into an allow.
#
# Shared as a single awk source string so the three parsers cannot drift.
# =============================================================================
_QSPLIT_AWK='
function qsplit(s,   out, n, i, c, j, qc, ci, inner, SQ, DQ) {
    SQ = sprintf("%c", 39)   # single quote
    DQ = sprintf("%c", 34)   # double quote
    out = ""
    n = length(s)
    i = 1
    while (i <= n) {
        c = substr(s, i, 1)
        if (c == DQ || c == SQ) {
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
                out = out substr(s, i, ci - i + 1)
                i = ci + 1
                continue
            }
            # Span carries command substitution: keep separators ACTIVE (copy the
            # opening quote and keep walking char-by-char so a `|` inside splits).
            out = out c
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
#   1. Interpreter-fed heredocs. "Inert to the outer shell" (above) is NOT
#      the same as "inert, full stop." When the heredoc body IS the script
#      handed to an interpreter -- `bash <<'EOF' ... EOF`, `cat <<'EOF'
#      ... EOF | bash`, `sh -s <<'EOF' ... EOF` -- a write-idiom line inside
#      that body is genuinely live code to the INNER interpreter, even
#      though the outer shell never parses it as redirection/separator
#      syntax. mask_heredoc_bodies() masks it anyway, so a write that
#      `origin/main`'s single-pass scan correctly denied is ALLOWed here.
#      DECISION (recorded, not implemented in this pass): extract_write_targets()
#      is a command-word-based scanner, and interpreter-mediated writes are
#      ALREADY a broad, pre-existing uncovered class on `main` independent of
#      heredocs -- `bash -c '... > f'`, `printf ... | bash`, `dd of=f`,
#      `install -m ... f` all ALLOW today. Closing that whole class (spotting
#      an inner interpreter invocation and recursively re-scanning its
#      script/stdin argument) is a materially larger, separate piece of work
#      than this masking pass and is OUT OF SCOPE here; track it as its own
#      follow-up rather than bolting a partial fix onto heredoc masking.
#      This tradeoff (missed ASK, worst case) is unchanged for
#      extract_write_targets() -- it still calls plain mask_heredoc_bodies()
#      below and still masks interpreter-fed bodies. But #5192 later reused
#      this same primitive for a CATASTROPHIC-tier DENY
#      (gh-api-rawfield-body-literal-at, ~line 2234) without re-deriving this
#      tradeoff for that tier: masking an interpreter-fed body there doesn't
#      risk a missed ask, it silently flips a DENY to an ALLOW on the exact
#      #4523/#4601/#4685 data-loss shape the check exists to catch (#5198).
#      That is NOT an acceptable tradeoff for a catastrophic-tier check, so
#      mask_heredoc_bodies_selective() below (used ONLY by that one check)
#      recognizes an interpreter-fed opener and leaves that specific block's
#      body UNMASKED (visible to the scan) instead of masking it -- narrowing
#      still applies to every other (non-interpreter-fed) heredoc in the same
#      command, so the #5181 false-positive fix stays intact.
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
# per is_interpreter_opener() -- see KNOWN LIMITATIONS #1 above. Used ONLY by
# the gh-api-rawfield-body-literal-at catastrophic check (#5198);
# extract_write_targets() keeps calling plain mask_heredoc_bodies() above,
# unchanged.
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
#
# $2 seeds the starting cwd (the hook's own session cwd — callers pass $CWD,
# mirroring extract_write_targets's `startcwd` parameter). A `cd <dir>`
# segment updates that cwd for LATER segments of the SAME command — global awk
# variable `curcwd`, threaded across the per-segment loop exactly like
# extract_write_targets threads it via its own `cd` case (#4933/#4881). This
# closes the false-ask where a command first `cd`s into a worktree (e.g. `cd
# .loom/worktrees/issue-N && git reset --hard origin/feature/issue-N`) while
# the hook's reported session cwd is still the main repo root: without cd
# tracking the "@HEAD@" target resolved against the WRONG checkout (#5156).
#
# The cd-tracked cwd is used ONLY for "@HEAD@"-target lines (reset --hard, and
# a push with no/HEAD refspec) — i.e. only where branch identity actually
# needs resolving against a checkout. A push naming an explicit branch refspec
# keeps deriving its <cpath> from an explicit `-C <path>` ONLY, exactly as
# before: its target is the literal refspec text, not cwd/HEAD-derived, so cd
# tracking must not change its behavior (a still-empty <cpath> there continues
# to fall back to the caller's raw $CWD, unchanged).
parse_force_ops() {
    printf '%s' "$1" | awk -v startcwd="$2" "$_QSPLIT_AWK"'
    BEGIN { SEP = sprintf("%c", 31); curcwd = startcwd }
                                       # SEP is non-whitespace so bash read
                                       # does not trim an empty cpath.
    {
        $0 = qsplit($0)   # quote-aware segmentation (#3755)
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/^sudo[ \t]+/, "", seg)
            sub(/^[ \t]+/, "", seg)
            m = split(seg, toks, /[ \t]+/)
            if (m == 0) continue
            # Thread a `cd <dir>` prefix through LATER segments of this same
            # compound command (mirrors extract_write_targets, #4933/#4881).
            # `cd -` and a bare `cd` are left unresolved (matches the same
            # known limitation there) rather than guessed.
            if (toks[1] == "cd") {
                if (m >= 2 && toks[2] != "" && toks[2] != "-") {
                    if (toks[2] ~ /^\//) {
                        curcwd = toks[2]
                    } else if (curcwd != "") {
                        curcwd = curcwd "/" toks[2]
                    }
                }
                continue
            }
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
            # headcpath is used ONLY for "@HEAD@"-target lines: an explicit
            # -C always wins; otherwise fall back to the cd-tracked curcwd
            # (which starts at startcwd, so a command with no cd prefix
            # resolves identically to the pre-#5156 behaviour).
            headcpath = cpath
            if (headcpath == "") headcpath = curcwd
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
                    print headcpath SEP "@HEAD@"
                } else {
                    for (p = 2; p <= np; p++) {
                        rs = pos[p]
                        sub(/^\+/, "", rs)
                        ci = index(rs, ":")
                        if (ci > 0) rs = substr(rs, ci + 1)
                        if (rs != "HEAD" && rs != "") {
                            print cpath SEP rs
                        } else {
                            print headcpath SEP "@HEAD@"
                        }
                    }
                }
            } else if (subcmd == "reset") {
                hard = 0
                for (j = k+1; j <= m; j++) if (toks[j] == "--hard") hard = 1
                if (hard) print headcpath SEP "@HEAD@"
            }
        }
    }'
}

# resolve_stash_cwd() — thread a `cd <dir> &&` prefix through $CWD resolution
# for the STASH-STACK SCOPE block below, exactly mirroring parse_force_ops'
# cd-tracking above (which itself mirrors extract_write_targets,
# #4933/#4881/#5156/#5161). Without this, a command of the form `cd
# .loom/worktrees/issue-N && git stash pop` — hook session cwd still the main
# repo root, the common shape per this repo's own CLAUDE.md worktree workflow
# — resolved stash scope against the WRONG checkout and asked as if the
# operation targeted the main checkout (#5173).
#
# $1 = command text, $2 = starting cwd (the hook's own session cwd — callers
# pass $CWD). Returns the cwd IN EFFECT once the FIRST `git stash
# pop/drop/clear` segment is reached; if no such segment is found (should not
# happen — callers only invoke this after the same grep match already
# succeeded), returns the fully cd-threaded cwd after the LAST segment as a
# deterministic fallback. `cd -` and a bare `cd` leave curcwd UNCHANGED —
# same known limitation parse_force_ops documents, left unresolved rather
# than guessed.
#
# A `git -C <path> stash ...` prefix is NOT threaded here (matches this
# block's pre-existing KNOWN LIMITATION comment above the caller, a distinct
# false-NEGATIVE direction out of this issue's scope) — only a `cd <dir>`
# prefix is.
resolve_stash_cwd() {
    printf '%s' "$1" | awk -v startcwd="$2" "$_QSPLIT_AWK"'
    BEGIN { curcwd = startcwd; found = 0 }
    {
        $0 = qsplit($0)   # quote-aware segmentation (#3755)
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/^sudo[ \t]+/, "", seg)
            sub(/^[ \t]+/, "", seg)
            if (seg == "") continue
            m = split(seg, toks, /[ \t]+/)
            if (m == 0) continue
            # Thread a `cd <dir>` prefix through LATER segments of this same
            # compound command (mirrors parse_force_ops above).
            if (toks[1] == "cd") {
                if (m >= 2 && toks[2] != "" && toks[2] != "-") {
                    if (toks[2] ~ /^\//) {
                        curcwd = toks[2]
                    } else if (curcwd != "") {
                        curcwd = curcwd "/" toks[2]
                    }
                }
                continue
            }
            if (toks[1] == "git" && m >= 3 && toks[2] == "stash" && (toks[3] == "pop" || toks[3] == "drop" || toks[3] == "clear")) {
                print curcwd
                found = 1
                exit
            }
        }
    }
    END { if (!found) print curcwd }'
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
#
# -----------------------------------------------------------------------------
# HEREDOC-WRAPPED FLAG VALUES (#5216)
#
# The `$(`-floor above is exactly right for a general command substitution, but
# it also declines to redact THIS repo's own pervasive idiom for any multi-line
# comment body (CLAUDE.md / judge.md / doctor.md / builder-pr.md all show it):
#
#     gh pr comment 4357 --body "$(cat <<QUOTED_DELIM
#     …prose that may QUOTE a dangerous command as an example…
#     QUOTED_DELIM
#     )" && gh pr edit 4357 --add-label "loom:pr"
#
# Every value built that way necessarily contains `$(`, so before this pass it
# was NEVER redacted — and a dangerous-command example merely quoted inside the
# body (a Judge documenting the shell-injection payload a PR now rejects, an
# Auditor quoting a guard pattern) hard-denied the whole command on the
# catastrophic tier. Observed live in guard-decisions.log on 2026-07-29 (a Judge
# approval on PR #4357) and reproduced for the #3679 force-push literals too:
# the gap is CONSTRUCTION-specific (heredoc-wrapped value), not pattern-specific.
#
# mask_flag_cat_heredocs() (below) closes it by masking ONLY the BODY of a
# heredoc in this one provably-inert shape, and only when ALL of these hold:
#   1. the opener is the complete tail of its line, immediately preceded by a
#      recognized text-carrying flag, its opening quote, and `$(cat`;
#   2. the heredoc delimiter is QUOTED (single- or double-quoted, `<<-` allowed)
#      — a quoted delimiter is what guarantees the outer shell performs NO
#      expansion on the body, so a `$(…)` sitting IN the body is inert text
#      rather than live code (an UNQUOTED delimiter is rejected outright);
#   3. the block is CLOSED in this same buffer (mirrors #5087's "never mask
#      speculatively" rule for mask_heredoc_bodies);
#   4. the very next line after the delimiter line is `)` + that same opening
#      quote — i.e. the substitution ends immediately, with nothing chained
#      after the heredoc inside it.
# Condition 4 is what keeps a `--body "$(cat <<QUOTED_DELIM … QUOTED_DELIM`
# <newline> `rm -rf /` <newline> `)"` command denying: bash ends the heredoc at
# the delimiter line and then genuinely RUNS the following line inside the
# substitution, so nothing is masked there. Condition 1 is what keeps an
# INTERPRETER-FED heredoc denying — a body consumed by `bash <<DELIM`,
# `sh -s <<DELIM`, or `cat <<DELIM … | sh` is live code
# to the inner shell, and none of those match `<flag> <quote>$(cat`. This is the
# deliberate narrowing versus reusing mask_heredoc_bodies() (#5000) here: that
# helper masks ANY closed heredoc body regardless of its consumer, a documented
# and accepted fail-open for the write-target scanner (#5117 Known Limitation 1)
# that must NOT be inherited by the catastrophic hard-deny floor.
#
# KNOWN LIMITATION (recorded, mirroring the #5117 convention): this recognizes
# only the literal `cat`-consumed shape spelled out above. A semantically
# equivalent variant — `$(command cat <<DELIM …)`, a heredoc opened on a
# continuation line, or a body whose delimiter line is followed by `) "` with a
# space — is simply not recognized and keeps denying exactly as it does today.
# That is the safe direction (a false positive that already exists, never a new
# bypass), and the shape above is the one the repo's own role prompts prescribe.
# -----------------------------------------------------------------------------
strip_literal_text() {
    printf '%s' "$1" | awk '
    # Mask the body of a `<flag> "$(cat <<QUOTED_DELIM … DELIM\n)"` heredoc.
    # See the header comment above for the four conditions and why each is
    # load-bearing. Body bytes are replaced 1:1 with "X" so the buffer keeps
    # its byte offsets and line count; the opener line, the delimiter line and
    # everything outside the body are left untouched.
    function mask_flag_cat_heredocs(s,   lines, nl, i, j, line, pre, oq, delim, dq, closeat, trimmed, body, dashform) {
        if (index(s, "<<") == 0) return s
        nl = split(s, lines, "\n")
        for (i = 1; i <= nl; i++) {
            line = lines[i]
            # (2) opener must END the line and carry a QUOTED delimiter.
            if (match(line, /<<-?["'"'"'][A-Za-z0-9_]+["'"'"'][ \t]*$/) == 0) continue
            dashform = (substr(line, RSTART + 2, 1) == "-")
            delim = substr(line, RSTART, RLENGTH)
            sub(/^<<-?/, "", delim)
            sub(/[ \t]*$/, "", delim)
            dq = substr(delim, 1, 1)
            if (substr(delim, length(delim), 1) != dq) continue   # quotes must match
            delim = substr(delim, 2, length(delim) - 2)
            if (delim == "") continue
            # (1) …immediately preceded by <flag> <openquote>$(cat.
            pre = substr(line, 1, RSTART - 1)
            if (pre !~ /(^|[ \t])(--message|--body|--notes|--title|--comment|-m)[ \t]*=?[ \t]*["'"'"']\$\([ \t]*cat[ \t]+$/) continue
            oq = ""
            for (j = length(pre); j >= 1; j--) {
                if (substr(pre, j, 2) == "$(") { oq = substr(pre, j - 1, 1); break }
            }
            if (oq != DQ && oq != SQ) continue
            # (3) the block must be CLOSED inside this buffer.
            closeat = 0
            for (j = i + 1; j <= nl; j++) {
                trimmed = lines[j]
                if (dashform) sub(/^\t+/, "", trimmed)
                if (trimmed == delim) { closeat = j; break }
            }
            if (closeat == 0) continue
            # (4) the substitution must close IMMEDIATELY after the delimiter
            #     line — `)` + the same opening quote — so nothing chained
            #     after the heredoc inside `$( … )` is masked away.
            if (closeat == nl) continue
            if (substr(lines[closeat + 1], 1, 2) != ")" oq) continue
            for (j = i + 1; j < closeat; j++) {
                body = lines[j]
                gsub(/./, "X", body)
                lines[j] = body
            }
            i = closeat
        }
        s = lines[1]
        for (i = 2; i <= nl; i++) s = s "\n" lines[i]
        return s
    }
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
        # PRE-PASS (#5216): blank the body of a `<flag> "$(cat <<QDELIMQ … )"`
        # heredoc before the quoted-span redaction below runs. It has to happen
        # here rather than inside the loop because `re`'"'"'s quoted-span classes
        # ([^"]* / [^'"'"']*) stop at the first quote character, and a heredoc body
        # is free to contain raw quotes (prose routinely does) — so the span
        # match alone cannot see such a value whole. Masking first also means
        # the `$(`-floor below needs no exception: by the time the loop reads
        # this span, the only text left inside it is `$(cat <<QDELIMQ`, the
        # delimiter, and `)`.
        s = mask_flag_cat_heredocs(buf)
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

# Mask quoted POSITIONAL arguments (no preceding flag name) to a small
# allowlist of known non-executing commands/scripts (issue #5235, the
# ask-tier analog of the #5155/#5160 fix). strip_literal_text() above only
# recognizes text following a named flag (--body/-m/--title/--notes/
# --comment); it has no effect on a script whose free-text arguments are
# purely positional, e.g. `./.loom/scripts/check-duplicate.sh "TITLE"
# "DESCRIPTION"`. check-duplicate.sh never EXECUTES a positional argument —
# it only reads it as dedup text — so masking a quoted argument immediately
# following it (optionally after short flags, e.g.
# `check-duplicate.sh --include-merged-prs "..."`) can never blind the
# ASK_PATTERNS scan (or any other COMMAND_ASK_SCAN consumer, e.g.
# stash-scope:main-checkout / stash-scope:worktree-collision) to a real
# invocation. Deliberately narrow allowlist, same "deliberately narrow"
# convention documented above strip_literal_text()'s
# mask_flag_cat_heredocs(): a command that WRAPS the phrase and then
# executes it — `sh -c "git stash pop"`, `bash -c '...'`, `eval "..."` — is
# NOT in this allowlist and stays fully visible to every ASK_PATTERNS entry,
# exactly as before.
#
# DELIBERATELY EXCLUDES grep/egrep/fgrep/rg, unlike guard-loom-workflow.sh's
# mask_command_positional_args() (#5155/#5160) which this function is
# otherwise modeled on. In THIS file, COMMAND_ASK_SCAN also feeds the SQL
# DDL/DML check (SQL_DDL_PATTERN, below) — which, once a command is
# disqualified from (or has opted out of) the #3687 read-only fast path,
# intentionally scans a `grep <pattern> <file>` invocation's own quoted
# positional pattern for a literal DDL phrase like "DROP TABLE" and denies,
# by design (see the "Fast path security" / "Fast path off" test groups in
# tests/hooks/test-guard-destructive.sh). Adding grep/rg to this allowlist
# was tried and directly regressed those tests: masking grep's own quoted
# argument blinded the full-path SQL-DDL scan to text it is specifically
# tested to still catch. check-duplicate.sh has no such competing consumer
# of its raw argument text anywhere in this file, so it stays the only
# entry. Extend this allowlist only for another read-only positional-arg
# consumer with NO conflicting raw-text scan elsewhere in this file.
#
# This is an intentional NEAR-DUPLICATE of guard-loom-workflow.sh's own
# mask_command_positional_args() (issue #5155/#5160) — kept as a SEPARATE
# function in this file, same convention as strip_literal_text() above being
# a separate copy from that file's strip_literal_text(): the two guards'
# decision-time masking must never be coupled, so a future fix/tuning of one
# cannot silently change the other's behavior.
#
# Masks EVERY quoted argument that directly, consecutively follows the
# command+flags (separated only by whitespace) — not just the first — so
# multi-positional-arg scripts like check-duplicate.sh's `TITLE DESCRIPTION`
# signature get both arguments masked. Masking stops at the first token that
# is not a quoted string (a bare filename, `&&`, `|`, etc.), leaving anything
# after that boundary — including a real ask-triggering invocation chained
# onto the same line — fully visible.
mask_ask_positional_args() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)
        DQ = sprintf("%c", 34)
        # Command-name allowlist: known non-executing commands/scripts whose
        # positional string arguments are search/dedup text, never live shell
        # syntax. grep/egrep/fgrep/rg are deliberately NOT here — see the
        # SQL-DDL conflict documented in the function header comment above.
        # Extend only when another read-only positional-arg consumer causes a
        # real false ask AND has no competing raw-text consumer elsewhere in
        # this file (see #5235, mirroring #5155).
        cmdre = "(\\./\\.loom/scripts/check-duplicate\\.sh)"
        # Zero or more short/long flags between the command name and the
        # first quoted positional argument (e.g.
        # `check-duplicate.sh --include-merged-prs --issue 5235`).
        flagre = "([ \t]+-[A-Za-z0-9_-]+)*"
        anchor = "(^|[ \t\n;&|`(])" cmdre flagre "[ \t]+"
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
    'rm[[:space:]]+-[a-zA-Z]*[rf][a-zA-Z]*[[:space:]]+/([^[:alnum:]._~/-]|$)'
    'rm[[:space:]]+-[a-zA-Z]*[rf][a-zA-Z]*[[:space:]]+~([^[:alnum:]._~/-]|$)'
    'rm[[:space:]]+-[a-zA-Z]*[rf][a-zA-Z]*[[:space:]]+\$HOME([^[:alnum:]._~/-]|$)'

    # Fork bombs
    ':\(\)\{ :\|:& \};:'

    # Pipe to shell (supply chain risk). Anchored on command *position*
    # rather than a bare substring scan, so a pipe target whose path merely
    # contains "sh" (e.g. `curl … | sudo tee /usr/share/keyrings/x.gpg`) no
    # longer false-positives (repo#29). The pattern requires: (1) curl/wget
    # starts right after a command separator/pipe/redirect or at the start of
    # the string — so it does not match "sh" buried inside an unrelated
    # earlier token; (2) an optional `sudo` (with its own flags) directly
    # after the pipe; (3) an optional path prefix before the shell word, so
    # `/bin/sh`, `/usr/bin/env`-style paths, etc. still match; (4) the shell
    # word itself is one of a known set of shell binaries/short names
    # (sh, bash, dash, zsh, ksh, csh, tcsh, fish) optionally preceded by a
    # 1-2 letter shell-family prefix; (5) that shell word must be followed by
    # whitespace, end of string, or another separator — not by more path
    # characters — so `sudo tee /usr/share/…` (which merely *contains* "sh")
    # does not match. Known accepted miss (not chased here, see repo#29): a
    # quoted/nested invocation such as `bash -c 'curl … | sh'` is not caught
    # by the leading-position anchor, because the character immediately
    # before `curl` is a quote, not one of the anchor's separator classes.
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
    # fully bypassed when LOOM_GUARD_CLOUD=0 / guards.cloudCli:false).
    #
    # NOTE: `aws iam delete` was likewise retiered OUT of this catastrophic
    # scan — but to the UNGATED ask tier (ASK_PATTERNS below, alongside
    # `gh release delete`), NOT to CLOUD_ASK_PATTERNS (#4216). Rationale:
    # deleting an IAM credential is a legitimate, often security-POSITIVE step
    # (revoking an exposed key whose replacement is already active) that a
    # supervised operator must be able to run in-session — a hard block left
    # only the undocumented script-file bypass. It stays UNGATED so a repo that
    # set guards.cloudCli:false / LOOM_GUARD_CLOUD=0 for EC2-churn convenience
    # would still ASK (never silently allow) on IAM deletion, and a headless
    # sweep still effectively blocks (an ASK with no human to answer denies;
    # see defaults/docs/guard-hooks.md). The remaining aws forms below stay
    # ungated catastrophic denies — a hard safety floor (#3593): mass object /
    # bucket deletion (`s3 rm --recursive`, `s3 rb`) and stack teardown
    # (`cloudformation delete-stack`) were not part of the rotation incident and
    # are deliberately kept as hard denies.
    'aws s3 rm.*--recursive'
    'aws s3 rb'
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

for pattern in "${ALWAYS_BLOCK_PATTERNS[@]}"; do
    if echo "$COMMAND_NO_LITERAL_TEXT" | grep -qiE "$pattern"; then
        deny "BLOCKED: Command matches dangerous pattern: $pattern" "catastrophic:$pattern"
    fi
done

# =============================================================================
# `gh pr/issue comment --body @path` — literal-@ silent data loss (#4523)
#
# `gh pr comment`/`gh issue comment --body @path` does NOT expand `@path` to
# the file's contents the way `-F body=@path` (gh api) or `gh pr edit
# --body-file path` do — it posts the literal string `@path` as the comment.
# A real incident (PR #4457) lost an entire Judge changes-requested review
# this way. The shape is never intentional (see judge.md's/doctor.md's
# `--body @path` anti-pattern warning), so this is an ungated hard deny, like
# the GitHub destructive ops above.
#
# DELIBERATELY scans the RAW $COMMAND, NOT COMMAND_NO_LITERAL_TEXT. This rule
# is narrowly anchored — it only inspects the character immediately after the
# --body/-b flag's opening quote (if any) — so, unlike the broad dangerous-
# substring scans above, it does not need strip_literal_text()'s quoted-value
# redaction to avoid false-positiving on prose. Scanning the redacted copy
# would in fact silently BREAK this rule: strip_literal_text() replaces a
# quoted value's entire inner text (including a leading `@`) with `X`s, so
# `gh pr comment 123 --body "@/tmp/x"` would come out of redaction as
# `--body "XXXXXXXXX"` — no `@` left to match. The unquoted form
# (`--body @/tmp/x`) is untouched by redaction either way, so a naive
# implementation that only smoke-tests the unquoted shape can look correct
# while silently missing the quoted shape actually seen in the field.
#
# The `@` must be followed by a path-shaped character (`/`, `.`, or `~`) —
# every real incident/test case is an absolute or relative path
# (`@/tmp/...`, `@./relative/path`, `@~/home/path`). A bare `@word` right
# after the opening quote is an @mention addressed to a reviewer (e.g.
# `--body "@reviewer Could you clarify..."`, the shape doctor.md's own
# "Can't Understand Feedback" example uses) and must NOT be treated as the
# `-F body=@path` anti-pattern (#4577).
GH_COMMENT_BODY_AT_PATTERN="(^|[;&|[:space:]])gh[[:space:]]+(pr|issue)[[:space:]]+comment[^;&]*(-b|--body)[[:space:]]*=?[[:space:]]*[\"']?@[/.~]"
if echo "$COMMAND" | grep -qiE "$GH_COMMENT_BODY_AT_PATTERN"; then
    deny "BLOCKED: 'gh pr comment'/'gh issue comment --body @path' does NOT expand the file — it posts the literal string '@path' as the comment (lost the PR #4457 review this way). Use --body \"\$(cat <<'EOF' ... EOF)\", -F/--body-file <path>, or 'gh api ... -F body=@<path>' instead." "gh-comment-body-literal-at"
fi

# =============================================================================
# `gh pr/issue edit --body @path` — same literal-@ silent data loss, different
# subcommand (#4685). The #4523 rule above is hard-anchored to `comment`, so
# `gh issue edit N --body @path` sailed through untouched and posted the
# literal string as the issue/PR BODY (not a comment) — real-world evidence:
# issue #4608's body was corrupted to the literal string
# `@/tmp/issue4608_body_new.txt`. Deliberately a SEPARATE rule/regex, not a
# widened GH_COMMENT_BODY_AT_PATTERN (#4577's additive-not-widened precedent),
# so the two subcommands' patterns can be fixed/tuned independently.
# =============================================================================
GH_EDIT_BODY_AT_PATTERN="(^|[;&|[:space:]])gh[[:space:]]+(pr|issue)[[:space:]]+edit[^;&]*(-b|--body)[[:space:]]*=?[[:space:]]*[\"']?@[/.~]"
if echo "$COMMAND" | grep -qiE "$GH_EDIT_BODY_AT_PATTERN"; then
    deny "BLOCKED: 'gh pr edit'/'gh issue edit --body @path' does NOT expand the file — it writes the literal string '@path' as the issue/PR body (corrupted issue #4608's body this way). Use --body \"\$(cat <<'EOF' ... EOF)\", -F/--body-file <path>, or 'gh api ... -F body=@<path>' instead." "gh-edit-body-literal-at"
fi

# =============================================================================
# The same literal-@ loss reached through SHELL-VARIABLE INDIRECTION (#4601)
#
# The #4523 rule above inspects only the STATIC text immediately following the
# --body/-b flag, so it is blind to an identical `@path` value that arrives via
# a shell variable. This recurred in the field on PR #4600 (~1h45m AFTER the
# #4523 guard was live in the installed copy — so the guard was present and
# simply did not cover the shape):
#
#   REVIEW_FILE="@/tmp/pr4600-review.md"; gh pr comment 4600 --body "$REVIEW_FILE"
#
# ...was ALLOWED and posted the literal path string as the comment again. This
# is also the shape an agent naturally reaches for when the literal form is
# denied — i.e. the deny above actively invites this bypass.
#
# An unconditional deny on `--body "$VAR"` would be far too broad: a legitimate
# `--body "$SUMMARY"` carrying review prose has the identical shape. So this
# rule CORRELATES instead — it denies only when the SAME command both
#   (a) assigns a PATH-SHAPED `@…` value to a shell variable, and
#   (b) passes that same variable as the --body/-b value.
# That combination has no legitimate use, and `--body "$SUMMARY"` (no @path
# assignment anywhere in the command) is untouched.
#
# COORDINATION (#4577): this is deliberately an ADDITIVE, separate check rather
# than a widening of GH_COMMENT_BODY_AT_PATTERN above. #4577 is an open fix to
# that same regex for the OPPOSITE failure direction (bare `@mention` reply
# prose false-positived); keeping the two rules on separate lines means neither
# fix can regress or textually conflict with the other. For the same reason
# GH_AT_PATHISH requires actual path shape (an explicit `/`, `~/`, `./`, `../`
# prefix, or a text-file extension), so bare `@mention` / `@org/team` prose
# never matches it — this rule cannot widen #4577's false-positive surface.
#
# KNOWN LIMIT (by construction): a variable assigned in an EARLIER Bash call is
# invisible in a single PreToolUse payload, and no static inspection can reach
# it. That residual case is covered by the independent second defense layer —
# the "re-fetch the posted comment and confirm it renders your prose, not a
# path" step in judge.md's/doctor.md's pre-approval / pre-completion checklists.
# =============================================================================
# `@` followed by a genuinely path-shaped value. Two alternatives:
#   1. an explicit path prefix — @/…, @~/…, @./…, @../…
#   2. a bare relative path ending in a text-file extension — @review.md,
#      @scratch/review.md
# Deliberately NOT `@\S+`: that would match `@rjwalters`, `@org/team`, and
# `@example.com` prose, i.e. exactly the #4577 false-positive family.
GH_AT_PATHISH="@((/|~/|\.\.?/)[^[:space:]\"';&|]*|[^[:space:]\"';&|]*\.(md|markdown|txt|text|log|json|ya?ml|diff|patch|out))"

# Both rules below require a literal `@` somewhere in the command, so this
# bash-builtin prefilter keeps them entirely off the hot path for the vast
# majority of commands (same pattern as the COMMAND_NO_LITERAL_TEXT /
# COMMAND_NO_COMMENT `#`-present guards above).
if [[ "$COMMAND" == *"@"* ]]; then
    if echo "$COMMAND" | grep -qiE "(^|[;&|[:space:]])gh[[:space:]]+(pr|issue)[[:space:]]+comment"; then
        # Names of variables assigned a path-shaped `@…` value in THIS command.
        _gh_at_path_vars=$(printf '%s\n' "$COMMAND" \
            | grep -oE "(^|[;&|(){}[:space:]])[A-Za-z_][A-Za-z0-9_]*=[\"']?$GH_AT_PATHISH" 2>/dev/null \
            | grep -oE "[A-Za-z_][A-Za-z0-9_]*=" 2>/dev/null \
            | tr -d '=' | sort -u)
        for _gh_at_var in $_gh_at_path_vars; do
            # ...and passed straight through as the --body/-b value ($V, ${V}, "$V").
            if echo "$COMMAND" | grep -qiE "(-b|--body)[[:space:]]*=?[[:space:]]*[\"']?[\$]\{?${_gh_at_var}(\}|[^A-Za-z0-9_]|\$)"; then
                deny "BLOCKED: '\$${_gh_at_var}' is assigned a path-shaped '@<path>' value and passed as --body — 'gh pr comment'/'gh issue comment' does NOT expand '@path' from a variable either; it posts the literal string as the comment (lost the PR #4457 review this way, recurred on PR #4600 through exactly this indirection). Use --body-file <path>, 'gh api ... -F body=@<path>', or --body \"\$(cat <<'EOF' ... EOF)\"." "gh-comment-body-literal-at-var"
            fi
        done
    fi

    # -------------------------------------------------------------------------
    # Same shell-variable-indirection shape, `edit` subcommand (#4685). Kept as
    # a separate parallel `if`/loop rather than folding `edit` into the
    # `comment` subcommand regex above, for the identical #4577-precedent
    # reason cited on GH_EDIT_BODY_AT_PATTERN.
    # -------------------------------------------------------------------------
    if echo "$COMMAND" | grep -qiE "(^|[;&|[:space:]])gh[[:space:]]+(pr|issue)[[:space:]]+edit"; then
        _gh_at_path_vars_edit=$(printf '%s\n' "$COMMAND" \
            | grep -oE "(^|[;&|(){}[:space:]])[A-Za-z_][A-Za-z0-9_]*=[\"']?$GH_AT_PATHISH" 2>/dev/null \
            | grep -oE "[A-Za-z_][A-Za-z0-9_]*=" 2>/dev/null \
            | tr -d '=' | sort -u)
        for _gh_at_var in $_gh_at_path_vars_edit; do
            if echo "$COMMAND" | grep -qiE "(-b|--body)[[:space:]]*=?[[:space:]]*[\"']?[\$]\{?${_gh_at_var}(\}|[^A-Za-z0-9_]|\$)"; then
                deny "BLOCKED: '\$${_gh_at_var}' is assigned a path-shaped '@<path>' value and passed as --body — 'gh pr edit'/'gh issue edit' does NOT expand '@path' from a variable either; it writes the literal string as the issue/PR body (corrupted issue #4608's body this way). Use --body-file <path>, 'gh api ... -F body=@<path>', or --body \"\$(cat <<'EOF' ... EOF)\"." "gh-edit-body-literal-at-var"
            fi
        done
    fi

    # -------------------------------------------------------------------------
    # `gh api … -f/--raw-field body=@path` — the same silent literal-@ loss.
    #
    # On `gh api`, ONLY `-F`/`--field` gives `@<path>` its read-from-file
    # meaning. `-f`/`--raw-field` is a plain string parameter with no file
    # expansion, so `gh api … -f body=@/tmp/review.md` posts the literal string
    # `@/tmp/review.md` as the comment body — byte-for-byte the same silent data
    # loss as the #4523 shape, on a surface that rule never inspected.
    #
    # CASE-SENSITIVE on purpose (grep -qE, NOT -qiE): `-i` would make `-f` match
    # the documented CORRECT alternative `-F body=@path` and deny it. The
    # leading whitespace anchor before the flag is likewise load-bearing —
    # without it, `-f` matches inside `--field` (the long form of `-F`) and
    # denies that too. GH_AT_PATHISH keeps `-f body="@mention …"` prose allowed.
    #
    # ENDPOINT SCOPE (#4685): this pattern was never anchored to a
    # `/comments` endpoint — it matches `gh api <any-endpoint> ... -f
    # body=@path` regardless of path — so it already covers `gh api
    # repos/{o}/{r}/issues/{n}` (issue PATCH) and
    # `repos/{o}/{r}/pulls/{n}` (PR PATCH) exactly as it covers
    # `.../issues/{n}/comments`. Confirmed via the test suite's new
    # non-comments-endpoint case; no widening was needed here.
    #
    # HEREDOC-MASKED SCAN (#5181, refined #5198): this check used to grep raw
    # $COMMAND, so a heredoc BODY line that merely QUOTES the denied phrase as
    # inert prose (e.g. a report `cat > f.md <<'EOF' ... gh api ... -f
    # body=@x ... EOF`, nothing of which executes) tripped the same
    # catastrophic-tier deny as a live invocation. Reuses
    # mask_heredoc_bodies_selective() (#5198, built on the mask_heredoc_bodies()
    # primitive extract_write_targets() already uses, #5000) to build a
    # heredoc-body-blanked working copy and scans THAT instead. Built lazily
    # (only when '<<' is present, mirroring the COMMAND_NO_COMMENT
    # `#`-present hot-path guard below) and scoped to just this one check;
    # every other rule in this file keeps reading raw $COMMAND /
    # COMMAND_NO_COMMENT unchanged. Masking only ever narrows: a REAL
    # (non-heredoc) invocation is untouched and still denies, even sitting in
    # the same multi-line command as an unrelated heredoc (mirrors the #5000
    # "narrows, never widens" test at
    # tests/hooks/test-guard-destructive.sh:2691). UNLIKE plain
    # mask_heredoc_bodies(), the _selective() variant does NOT mask a heredoc
    # body whose opener feeds an interpreter (`bash <<EOF`, `sh -s <<EOF`,
    # `cat <<EOF | bash`, ...) -- that body is genuinely live code to the
    # inner interpreter (KNOWN LIMITATIONS #1, above), so masking it here
    # would silently turn a real `gh api ... -f body=@path` invocation into
    # an ALLOW (#5198's regression). extract_write_targets() is unaffected --
    # it still calls plain mask_heredoc_bodies() and still masks
    # interpreter-fed bodies, an accepted tradeoff for its ask-tier use.
    # -------------------------------------------------------------------------
    if [[ "$COMMAND" == *"<<"* ]]; then
        COMMAND_HEREDOC_MASKED=$(printf '%s' "$COMMAND" | awk "$_MASKHEREDOC_AWK"'
        { buf = buf (NR > 1 ? "\n" : "") $0 }
        END { printf "%s", mask_heredoc_bodies_selective(buf) }')
    else
        COMMAND_HEREDOC_MASKED="$COMMAND"
    fi
    GH_API_RAWFIELD_BODY_AT_PATTERN="(^|[;&|[:space:]])gh[[:space:]]+api[^;&]*[[:space:]](-f|--raw-field)[[:space:]]*=?[[:space:]]*[\"']?body=[\"']?$GH_AT_PATHISH"
    if echo "$COMMAND_HEREDOC_MASKED" | grep -qE "$GH_API_RAWFIELD_BODY_AT_PATTERN"; then
        deny "BLOCKED: 'gh api ... -f/--raw-field body=@<path>' does NOT read the file — only -F/--field gives '@<path>' its read-from-file meaning. As written this posts the literal string '@<path>' as the body (same silent data loss as PR #4457/issue #4608). Use '-F body=@<path>' instead." "gh-api-rawfield-body-literal-at"
    fi
fi

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
#
# POSITIONAL-ARGUMENT MASKING (#5235): strip_literal_text() above is keyed
# ONLY on named-flag presence, so a script with a purely POSITIONAL
# signature — e.g. `./.loom/scripts/check-duplicate.sh TITLE DESCRIPTION`
# (no flags at all) — never triggered it, leaving its free-text TITLE/
# DESCRIPTION arguments scanned unmasked. This is the same class of gap
# #5155/#5160 already fixed for guard-loom-workflow.sh's gh-pr-merge-redirect
# scan. mask_ask_positional_args() (defined above, a deliberate near-
# duplicate of that fix, with a narrower allowlist — see its header comment
# for why grep/rg are deliberately excluded here) masks quoted positional
# arguments to check-duplicate.sh BEFORE the flag-keyed strip above runs, so
# a dedup check whose prose quotes an ask-phrase (e.g. "git stash pop") as
# inert text no longer false-asks on stash-scope:main-checkout,
# force-op:protected, or any other ASK_PATTERNS entry. Gated on the same
# command-name substring the awk allowlist matches, keeping it off the hot
# path for the vast majority of commands that never invoke it.
COMMAND_ASK_SCAN="$COMMAND_NO_COMMENT"
if [[ "$COMMAND_NO_COMMENT" == *"check-duplicate.sh"* ]]; then
    COMMAND_ASK_SCAN=$(mask_ask_positional_args "$COMMAND_ASK_SCAN")
fi
if [[ "$COMMAND_NO_COMMENT" == *"--body"* || "$COMMAND_NO_COMMENT" == *"--message"* || \
      "$COMMAND_NO_COMMENT" == *"--title"* || "$COMMAND_NO_COMMENT" == *"--notes"* || \
      "$COMMAND_NO_COMMENT" == *"--comment"* || "$COMMAND_NO_COMMENT" == *"-m"* ]]; then
    COMMAND_ASK_SCAN=$(strip_literal_text "$COMMAND_ASK_SCAN")
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
    printf '%s' "$1" | awk "$_QSPLIT_AWK"'
    {
        $0 = qsplit($0)   # quote-aware segmentation (#3755)
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

# Split the single deny call this used to share (#4216). System-lifecycle
# commands (halt/reboot/poweroff/shutdown/init 0|6) are a hard safety floor and
# stay DENY. The az/gcloud `… delete` cloud branch was retiered to the UNGATED
# ask tier — mirroring the `aws iam delete` move above — so a supervised
# operator is prompted rather than hard-blocked, while a headless sweep's
# unanswered ASK still blocks (see defaults/docs/guard-hooks.md). A lifecycle
# deny takes precedence over a cloud-delete ask in a compound command (e.g.
# `az group delete …; halt`), so the hard floor is never downgraded.
#
# SCANS COMMAND_ASK_SCAN, NOT COMMAND_NO_COMMENT (#5216). lifecycle_or_cloud_reason()
# segment-parses per physical line, so a heredoc BODY line inside
# `--body "$(cat <<'EOF' … EOF)"` whose first word happens to be a lifecycle verb
# ("halt the deploy if this fails", "shutdown checklist:") was read as a live
# `halt`/`shutdown` command word and hard-denied a comment that runs nothing.
# The literal-redacted copy blanks only that quoted-value text; a real
# `… ; halt` outside a flag value, or a `bash -c 'halt'` payload (never
# redacted), still reaches this check and still denies.
_LIFECYCLE_CLOUD_REASONS=$(lifecycle_or_cloud_reason "$COMMAND_ASK_SCAN")
_LIFECYCLE_DENY=$(printf '%s\n' "$_LIFECYCLE_CLOUD_REASONS" | grep '^system lifecycle command:' | head -1)
if [[ -n "$_LIFECYCLE_DENY" ]]; then
    deny "BLOCKED: $_LIFECYCLE_DENY" "lifecycle"
fi
_CLOUD_DELETE_ASK=$(printf '%s\n' "$_LIFECYCLE_CLOUD_REASONS" | grep '^cloud resource deletion:' | head -1)
if [[ -n "$_CLOUD_DELETE_ASK" ]]; then
    ask "Command requires confirmation: $COMMAND ($_CLOUD_DELETE_ASK — retiered to the ungated ask tier in #4216; an interactive operator confirms, a headless session still blocks)" "cloud-delete-ask"
fi

# =============================================================================
# DATABASE DESTRUCTION - Gated by the SQL DDL/DML guard toggle
#
# Kept separate from ALWAYS_BLOCK_PATTERNS so DB-engine repos can opt out
# (guards.sqlDdl:false / LOOM_GUARD_SQL=0). A single alternation grep matches
# all four DDL statements in one pass (cheaper than a per-pattern loop), and
# sql_guard_enabled() is consulted only after a match, so the config read stays
# off the hot path.
#
# SCANS COMMAND_ASK_SCAN, NOT COMMAND_NO_COMMENT (#5216). This check was the one
# broad-substring deny that never received #3679's quoted-flag-value redaction:
# `gh pr comment --body "example payload: DROP TABLE users"` hard-denied a
# comment that runs no SQL at all, where the equivalent prose about `rm -rf /`
# was already allowed by the catastrophic scan. COMMAND_ASK_SCAN is the
# comment-stripped AND literal-redacted copy (built above; already relied on by
# the deny-tier write-confinement check, so its use here is not an ask-tier-only
# convention), which also carries #5216's heredoc-body masking — so both the
# plain `--body "…"` and the heredoc-wrapped `--body "$(cat <<'EOF' … EOF)"`
# forms of quoted prose stop false-positiving in one step. `-c`/`-e` are NOT
# text-carrying flags, so the real invocations (`psql -c '…'`, `mysql -e '…'`)
# are untouched and still deny.
# =============================================================================
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
    printf '%s' "$1" | awk "$_QSPLIT_AWK"'
    {
        $0 = qsplit($0)   # quote-aware segmentation (#3755)
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
# =============================================================================
extract_write_targets() {
    printf '%s' "$1" | awk -v startcwd="$2" "$_QSPLIT_AWK""$_MASKGT_AWK""$_MASKWS_AWK""$_MASKHEREDOC_AWK"'
    BEGIN {
        SEP = sprintf("%c", 31)
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
        # with inert placeholders, the per-line loop below -- which still
        # resets mask_gt()/mask_ws() per SEGMENT, exactly as before -- has
        # nothing dangerous-looking left to misread on those lines,
        # regardless of that per-line reset. A real write-idiom byte OUTSIDE
        # any recognized heredoc body, even later in the SAME multi-line
        # command, is untouched and still flows through the unchanged
        # pipeline below.
        buf = mask_heredoc_bodies(buf)
        $0 = qsplit(buf)   # quote-aware segmentation (#3755)
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/^sudo[ \t]+/, "", seg)
            sub(/^[ \t]+/, "", seg)
            if (seg == "") continue

            # Quote-aware whitespace masking (#4934): wseg is byte-for-byte
            # identical to seg except a space/tab INSIDE a quoted span is
            # replaced with a non-whitespace placeholder, so splitting on
            # /[ \t]+/ never breaks a quoted argument (e.g. a quoted path
            # containing a literal space) into more than one token. toks[]
            # is then unmasked back to the real whitespace bytes so the
            # target TEXT downstream is unchanged.
            wseg = mask_ws(seg)
            m = split(wseg, toks, /[ \t]+/)
            if (m < 1) continue
            for (j = 1; j <= m; j++) toks[j] = unmask_ws(toks[j])

            # Quote-aware parallel tokenization (#4245): mseg is byte-for-byte
            # identical to wseg except a `>` inside a quoted span is replaced
            # with an SOH placeholder, so whitespace splitting yields the SAME
            # token boundaries (mm == m always) but mtoks[] can be tested for
            # a REAL (unquoted) redirection operator without ever matching a
            # `>` that was only quoted data. Masking `>` on TOP of wseg (not
            # seg) keeps the two passes token boundaries in lockstep, since
            # mask_gt() only ever changes `>` bytes, never whitespace-ness.
            mseg = mask_gt(wseg)
            mm = split(mseg, mtoks, /[ \t]+/)

            if (toks[1] == "cd") {
                if (m >= 2 && toks[2] != "" && toks[2] != "-") {
                    if (toks[2] ~ /^\//) {
                        curcwd = toks[2]
                    } else if (curcwd != "") {
                        curcwd = curcwd "/" toks[2]
                    }
                }
                continue
            }

            if (toks[1] == "tee") {
                for (j = 2; j <= m; j++) {
                    if (toks[j] == "" || toks[j] ~ /^-/) continue
                    print curcwd SEP toks[j]
                }
            } else if (toks[1] == "sed") {
                has_i = 0
                nf = 0
                delete nfargs
                for (j = 2; j <= m; j++) {
                    if (toks[j] ~ /^-i/) has_i = 1
                    if (toks[j] ~ /^-/) continue
                    if (toks[j] == "") continue
                    nf++
                    nfargs[nf] = toks[j]
                }
                if (has_i && nf >= 2) {
                    for (j = 2; j <= nf; j++) print curcwd SEP nfargs[j]
                }
            } else if (toks[1] == "cp" || toks[1] == "mv") {
                nf = 0
                delete nfargs
                for (j = 2; j <= m; j++) {
                    if (toks[j] ~ /^-/) continue
                    if (toks[j] == "") continue
                    nf++
                    nfargs[nf] = toks[j]
                }
                if (nf >= 2) print curcwd SEP nfargs[nf]
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
                        print curcwd SEP toks[j+1]
                    }
                    continue
                }
                if (mt ~ /^[0-9]*>>?[^ \t&]/) {
                    # Attached form (`>file`, `2>file`, `>>file`).
                    op = toks[j]
                    sub(/^[0-9]*>>?/, "", op)
                    if (op != "") print curcwd SEP op
                }
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

# SCANS COMMAND_NO_LITERAL_TEXT, NOT RAW $COMMAND (#5216). extract_rm_targets()
# segments with qsplit(), which — like every quote-tracking scan in this file —
# is driven one PHYSICAL LINE at a time and has no memory of a `"` opened on an
# earlier line. So a heredoc BODY line inside `--body "$(cat <<'EOF' … EOF)"`
# was segmented as if it were live shell: the prose
# `Example payload: \`owner/name; rm -rf /\`` split on its `;` into a segment
# whose command word is `rm`, manufacturing the target ``/` `` and hard-denying a
# Judge comment that deletes nothing (observed on PR #4357). Same failure family
# as #5000's phantom write targets, and the reason fixing only the
# ALWAYS_BLOCK_PATTERNS scan above leaves the reported command still denied.
# The literal-redacted copy blanks exactly the quoted flag-value text (including
# #5216's provably-inert `$(cat <<QDELIMQ … )` heredoc bodies) and nothing else,
# so a REAL `rm -rf /` — bare, sudo-prefixed, after a `&&`, or smuggled through
# `bash -c '…'` / `-m "$(rm -rf /)"` (neither of which is ever redacted) — still
# reaches this check unchanged.
#
# Cheap pre-check keeps awk off the hot path for the ~99% of commands that have
# no recursive/force rm at all.
if echo "$COMMAND_NO_LITERAL_TEXT" | grep -qE 'rm[[:space:]]+-[a-zA-Z]*[rf]'; then
    RM_TARGETS=$(extract_rm_targets "$COMMAND_NO_LITERAL_TEXT" | head -20)

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
                    deny "BLOCKED: rm target outside repo scope (LOOM_RM_SCOPE=repo): $ABS_PATH" "rm-scope-outside-repo"
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
    _wt_in_protected_area() {
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

    WRITE_TARGETS=$(extract_write_targets "$COMMAND_ASK_SCAN" "$CWD" | head -20)
    while IFS=$'\037' read -r _wcwd _wtarget; do
        [[ -z "$_wtarget" ]] && continue

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

        # Resolve to absolute; a relative target with no resolvable cwd is
        # ambiguous — skip it (allow on uncertainty, never deny on it).
        _wabs=""
        if [[ "$_wclassify" == /* ]]; then
            _wabs="$_wclassify"
        elif [[ -n "$_wcwd" ]]; then
            _wabs="$_wcwd/$_wclassify"
        else
            continue
        fi
        _wabs=$(normalize_abs_path "$_wabs")

        # (a) Already inside some managed worktree -> allow. This is exactly
        # where a builder is supposed to write.
        _in_any_managed_worktree "$_wabs" && continue

        # Not under any worktree. If it's also not under the main checkout,
        # there is nothing this guard protects (e.g. /tmp scratch) -> allow.
        [[ -z "$_WT_MAIN_ROOT" ]] && continue
        case "$_wabs" in
            "$_WT_MAIN_ROOT"|"$_WT_MAIN_ROOT"/*) : ;;
            "$_WT_MAIN_ROOT_LOGICAL"|"$_WT_MAIN_ROOT_LOGICAL"/*) : ;;
            *) continue ;;
        esac

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
#
# SCANS COMMAND_ASK_SCAN, NOT COMMAND_NO_COMMENT (#5216). parse_force_ops() is
# another per-physical-line segment parser, so a heredoc BODY line that BEGINS
# with a force op (a Judge quoting `git push --force origin main` at the start of
# a line, the exact prose #3679 exists to allow) was parsed as a live force op
# and asked — and an unanswered ask blocks a headless sweep just like a deny.
# Reading the literal-redacted copy keeps that prose inert while every real force
# op — bare, chained after `&&`, or inside `bash -c '…'` — is untouched.
# =============================================================================
if [[ "$COMMAND_ASK_SCAN" == *git* ]] && \
   echo "$COMMAND_ASK_SCAN" | grep -qE '(--force|--force-with-lease|(^|[[:space:]])-f([[:space:]]|$)|--hard)'; then
    _FORCE_MODE=$(force_scope_mode)
    if [[ "$_FORCE_MODE" != "off" ]]; then
        _FORCE_OPS=$(parse_force_ops "$COMMAND_ASK_SCAN" "$CWD")
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
    #
    # Right-hand anchored (#5260): the old pattern had no boundary after
    # `delete`, so it substring-matched `gh release delete-asset` — a distinct,
    # far-less-destructive subcommand that only removes one uploaded artifact,
    # not the whole release/tag. Requiring the match to end at a shell
    # separator/whitespace or end-of-string (mirroring the existing left-hand
    # `(^|[;&|[:space:]])` anchor) lets `delete-asset`'s immediate hyphen break
    # the match while the bare/argumented `gh release delete` case (and any
    # other `gh release delete-*` subcommand) is unaffected.
    '(^|[;&|[:space:]])gh release delete([;&|[:space:]]|$)'

    # Cloud IAM credential deletion — retiered from the catastrophic ALWAYS_BLOCK
    # list to this UNGATED ask tier (#4216). Kept OUT of CLOUD_ASK_PATTERNS on
    # purpose: that array is gated by cloud_guard_enabled(), so a repo that set
    # guards.cloudCli:false / LOOM_GUARD_CLOUD=0 for EC2-churn convenience would
    # SILENTLY bypass IAM deletion too — an unacceptable weakening for credential
    # deletion. Ungated here means: always prompt an interactive operator, never
    # silently allow, and still block a headless sweep (an ASK with no human to
    # answer denies, per defaults/docs/guard-hooks.md). The az/gcloud `… delete`
    # peer is handled by the segment parser above, which splits its former deny
    # into a lifecycle deny + this same cloud-delete ask.
    '(^|[;&|[:space:]])aws iam delete'

    # NOTE: the remaining cloud CLI (aws ec2/lambda) + docker ASK patterns are
    # NOT in this ungated array. They live in CLOUD_ASK_PATTERNS below, gated by
    # cloud_guard_enabled() so cloud-dev repos can opt down (LOOM_GUARD_CLOUD=0 /
    # guards.cloudCli:false).

    # NOTE: `systemctl restart`/`stop`/`disable` are NOT in this ungated array.
    # They used to be plain '(^|[;&|[:space:]])systemctl <verb>' entries here,
    # but that anchor cannot distinguish a real shell separator from a
    # whitespace character sitting INSIDE a quoted string, so read-only
    # introspection like `grep -n "...|systemctl restart|..." file` or
    # `jq -c 'select(.pattern | contains("systemctl"))' log` false-asked on the
    # phrase's leading space (#5214). They are handled by the segment-parsed,
    # command-word-anchored systemctl_ask_reason() check below instead — see
    # its own comment block for the fix rationale.

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
# SERVICE-MANAGEMENT ASK — systemctl restart/stop/disable, segment-parsed,
# command-word anchored (#5214)
#
# These three verbs used to live in ASK_PATTERNS above as plain substring
# patterns anchored only by '(^|[;&|[:space:]])' (#3756) — a boundary that
# cannot distinguish a real shell separator from a whitespace character sitting
# INSIDE a quoted string literal. So a phrase like `systemctl restart` merely
# being quoted as SEARCH TEXT (a grep pattern, a jq filter, prose) still matched
# on its leading space, even though no such command was ever invoked:
#   grep -n "idle\|systemctl restart\|systemd\|relaunch\|--idle-shutdown" f.sh
#   jq -c 'select(.pattern | contains("systemctl"))' guard-decisions.log
#
# Mirrors lifecycle_or_cloud_reason()'s fix for the analogous halt/reboot/
# az-delete false positive: segment-parse the command with qsplit() (quote-aware,
# #3755) instead of scanning raw substrings, strip a leading sudo/env wrapper
# per segment, and ask ONLY when a segment's actual command word is `systemctl`
# AND its very next token is restart/stop/disable. A quoted `|` inside
# `grep`/`jq` arguments (no `$(`/backtick) is inert to qsplit(), so both example
# commands above stay a single `grep`/`jq` segment — command word never
# resolves to `systemctl` — and no longer false-ask. A genuine invocation
# (bare, after `;`/`&&`/`|`, or with a later quoted argument such as
# `systemctl restart "my service"`) still asks, since toks[1]/toks[2] are
# unaffected by trailing quoted content.
#
# Scoped narrowly to this one "Service management" ASK_PATTERNS block per
# #5214 — #5157/#5158 describe the same false-positive CLASS for other
# patterns but were judged too broad a fix to land autonomously; this is not
# an attempt at a general-purpose fix for the whole ASK_PATTERNS family.
# =============================================================================
systemctl_ask_reason() {
    printf '%s' "$1" | awk "$_QSPLIT_AWK"'
    {
        $0 = qsplit($0)   # quote-aware segmentation (#3755)
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/^sudo[ \t]+/, "", seg)
            # Strip a leading `env` wrapper + its flags/assignments, mirroring
            # lifecycle_or_cloud_reason() (#3586), so `env FOO=bar systemctl
            # restart x` still resolves its command word to `systemctl`.
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
            if (m < 2) continue
            if (toks[1] == "systemctl" && (toks[2] == "restart" || toks[2] == "stop" || toks[2] == "disable")) {
                print "systemctl " toks[2]
            }
        }
    }'
}
_SYSTEMCTL_ASK=$(systemctl_ask_reason "$COMMAND_NO_COMMENT" | head -1)
if [[ -n "$_SYSTEMCTL_ASK" ]]; then
    ask "Command requires confirmation: $COMMAND" "ask:$_SYSTEMCTL_ASK"
fi

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
        ask "Command requires confirmation: $COMMAND (set guards.reversibleGh:true in .loom/config.json to keep this ask; it is off by default because the op is trivially reversible)" "reversible-gh:$pattern"
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
# STASH-STACK SCOPE — ask on git stash pop/drop/clear in the MAIN checkout (#4281)
#
# git stash push/apply/list are NOT gated here — push only adds an entry,
# apply keeps the entry on the stack, and list only reads; none of them can
# remove operator-preserved state. `git stash` with no subcommand defaults to
# `push` and is likewise untouched. Only pop/drop/clear can destroy an entry.
#
# Scoped to the MAIN checkout only, never a linked worktree — a worktree has
# its own working tree, so a stash op run there cannot touch the main
# checkout's stack, and gating it too would add friction with no protective
# value.
#
# Resolution: unlike the Bash-write confinement block above (which compares an
# arbitrary WRITE TARGET path against the main-checkout root by prefix), CWD
# is compared against ITSELF: `git rev-parse --show-toplevel` (the working
# tree root of wherever CWD is) vs. `git rev-parse --git-common-dir/..` (always
# the true main-checkout root, from a worktree or not). They are EQUAL only
# when CWD is the main checkout; they diverge when CWD is a linked worktree. A
# subdirectory-prefix test would be fooled here because Loom's own linked
# worktrees live NESTED inside the main checkout's tree
# (`<main>/.loom/worktrees/issue-N`) — that path is textually "under" the main
# root even though it is a distinct working tree, so a naive prefix match would
# ask inside a builder's own worktree too. The show-toplevel/common-dir
# comparison sidesteps that entirely.
#
# CD-PREFIX THREADING (#5173): unlike the raw $CWD comparison this replaced,
# resolve_stash_cwd() (defined above, mirrors parse_force_ops' cd-tracking
# from #5156/#5161) threads a `cd <dir> &&` prefix earlier in the SAME
# compound $COMMAND through to the stash pop/drop/clear invocation, so a
# command like `cd .loom/worktrees/issue-N && git stash pop` — hook session
# cwd still the main repo root, the common shape per this repo's own
# CLAUDE.md worktree workflow — resolves scope against the cd TARGET, not the
# hook's raw session cwd. Both the main-checkout ask below AND the
# worktree-collision ask (#4821) further down consume the SAME
# _stash_toplevel/_stash_common_parent values, so both get this treatment
# for free.
#
# KNOWN LIMITATION: unlike the force-op parser (parse_force_ops), this check
# does not thread a `git -C <path>` argument — `git -C <main-checkout-path>
# stash pop` run from a worktree cwd is not caught today. Track any observed
# bypass via this path as a follow-up.
#
# WORKTREE-TO-WORKTREE COLLISION (#4821): refs/stash is a single stack shared
# by EVERY linked worktree of the repo, not per-worktree — so two parallel
# Builders each in a *different* linked worktree (neither one the main
# checkout) can pop/drop each other's WIP, and the main-checkout-only check
# above asks for neither side. Observed in production: kicad-tools PRs
# #4524/#4526. Below, when cwd is a linked worktree (not the main checkout)
# AND two or more `.loom-managed` worktrees currently exist under
# `<main>/.loom/worktrees/`, we ask too — a single active worktree has no one
# else's stash entry to collide with, so it stays ungated.
#
# NOTE (#5217): this fires correctly, per the design above, for a legitimate
# `git stash push && <baseline check> && git stash pop` pattern too — used to
# diff a clean baseline against WIP (clippy/shellcheck/test-output
# comparisons) — since in this repo's typical worktree count that pattern is
# gated on nearly every occurrence, with no human to answer in headless mode.
# A same-chain heuristic ("push and pop appear in the same command, so
# allow") was considered and REJECTED during #5217's curation: push and pop
# are two separate guard-approved Bash calls with an arbitrary-duration
# command running between them, so another worktree's concurrent `git stash
# push` can still land on the SHARED stack during that window, and a same-
# chain "pop" then restores the WRONG entry — a same-chain heuristic alone
# cannot see that. The fix is `worktree.sh stash-push`/`stash-pop`
# (`.loom/scripts/worktree.sh`), which never touch `refs/stash` — WIP is
# anchored to a PER-ISSUE ref instead, so there is no shared stack left to
# collide on. This ask (and the main-checkout ask above) stay exactly as
# strict as before for any RAW `git stash pop/drop/clear` — the new commands
# are a guard-transparent replacement path, not a guard exemption.
#
# Gated by stash_scope_guard_enabled() (guards.stashScope /
# LOOM_GUARD_STASH_SCOPE, default on), invoked LAZILY only after the pattern
# already matched, mirroring every other cold-path toggle in this file.
# =============================================================================
if echo "$COMMAND_ASK_SCAN" | grep -qE '(^|[;&|(]|[[:space:]])git[[:space:]]+stash[[:space:]]+(pop|drop|clear)([[:space:]]|$)' \
   && stash_scope_guard_enabled; then
    _stash_effective_cwd="$CWD"
    if [[ -n "$CWD" ]]; then
        _stash_effective_cwd=$(resolve_stash_cwd "$COMMAND_NO_COMMENT" "$CWD")
        [[ -z "$_stash_effective_cwd" ]] && _stash_effective_cwd="$CWD"
    fi

    _stash_toplevel=""
    _stash_common_parent=""
    if [[ -n "$_stash_effective_cwd" && -d "$_stash_effective_cwd" ]]; then
        _stash_toplevel=$(cd "$_stash_effective_cwd" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null) || _stash_toplevel=""
        [[ -n "$_stash_toplevel" && -d "$_stash_toplevel" ]] && \
            _stash_toplevel=$(cd "$_stash_toplevel" 2>/dev/null && pwd -P) || _stash_toplevel=""

        _stash_common=$(cd "$_stash_effective_cwd" 2>/dev/null && git rev-parse --git-common-dir 2>/dev/null) || _stash_common=""
        if [[ -n "$_stash_common" ]]; then
            _stash_common_parent=$(cd "$_stash_effective_cwd" 2>/dev/null && cd "$_stash_common/.." 2>/dev/null && pwd -P) || _stash_common_parent=""
        fi
    fi

    if [[ -n "$_stash_toplevel" && -n "$_stash_common_parent" && "$_stash_toplevel" == "$_stash_common_parent" ]]; then
        ask "Command requires confirmation: $COMMAND (git stash pop/drop/clear in the MAIN checkout can destroy operator-preserved state — the main checkout's stash stack is operator-owned, not scratch space for an integration check. Run test-merges in an isolated worktree instead; set guards.stashScope:false / LOOM_GUARD_STASH_SCOPE=0 to disable this ask)" "stash-scope:main-checkout"
    elif [[ -n "$_stash_toplevel" && -n "$_stash_common_parent" ]]; then
        # cwd is a linked worktree, not the main checkout. Count OTHER
        # `.loom-managed` worktrees under the main checkout's worktree root —
        # a collision needs at least one other active worktree to race with.
        _stash_worktree_count=0
        if [[ -d "$_stash_common_parent/.loom/worktrees" ]]; then
            while IFS= read -r _stash_wt_dir; do
                [[ -f "$_stash_wt_dir/.loom-managed" ]] && \
                    _stash_worktree_count=$((_stash_worktree_count + 1))
            done < <(find "$_stash_common_parent/.loom/worktrees" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
        fi

        if [[ "$_stash_worktree_count" -ge 2 ]]; then
            ask "Command requires confirmation: $COMMAND (git stash pop/drop/clear from a linked worktree can destroy ANOTHER builder's WIP — refs/stash is a single stack SHARED across every linked worktree of this repo, not per-worktree, and $_stash_worktree_count managed worktrees are currently active. Use './.loom/scripts/worktree.sh snapshot <issue-number>' instead of git stash for ad-hoc WIP, or './.loom/scripts/worktree.sh stash-push <issue-number>' + 'stash-pop <issue-number>' for a clean-baseline-vs-diff comparison — neither touches the shared refs/stash stack, so neither needs this ask; set guards.stashScope:false / LOOM_GUARD_STASH_SCOPE=0 to disable this ask)" "stash-scope:worktree-collision"
        fi
    elif [[ "$_stash_effective_cwd" != "$CWD" ]]; then
        # A `cd <dir>` prefix resolved to a target that does not exist or is
        # not inside any git checkout — ambiguous. Never silently widen an
        # ask into an allow (mirrors parse_force_ops' detached-HEAD fail-safe
        # from #5156/#5161): fail toward asking rather than guessing.
        ask "Command requires confirmation: $COMMAND (the cd target for this stash operation could not be resolved to a git checkout, so scope cannot be determined — refusing to silently allow an ambiguous stash pop/drop/clear; set guards.stashScope:false / LOOM_GUARD_STASH_SCOPE=0 to disable this ask)" "stash-scope:cd-unresolved"
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

# SCANS COMMAND_ASK_SCAN, NOT COMMAND_NO_COMMENT (#5216). This was the only ask
# loop still reading the merely comment-stripped copy — ASK_PATTERNS (#3756) and
# REVERSIBLE_GH_PATTERNS above both already scan the literal-redacted copy — so
# an `aws s3 rm`/`rb` phrase quoted as prose inside a `--body`/`-m` value still
# false-asked after the catastrophic scan stopped false-denying it. In a headless
# sweep an unanswered ask blocks just like a deny (see defaults/docs/
# guard-hooks.md), so leaving it here would have left the reported stall half
# fixed for the aws siblings. A real `aws s3 rb s3://bucket` outside a quoted
# flag value is untouched and still asks.
for pattern in "${CLOUD_ASK_PATTERNS[@]}"; do
    if echo "$COMMAND_ASK_SCAN" | grep -qE "$pattern" && cloud_guard_enabled; then
        ask "Command requires confirmation: $COMMAND (set guards.cloudCli:false in .loom/config.json if this repo manages cloud infra as a first-class workflow)" "cloud-cli:$pattern"
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
