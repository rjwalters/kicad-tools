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
#        echo                      (pure stdout writer — never treats its
#          arguments as executable code, has no mutation flag, and any command
#          it might otherwise smuggle to a downstream interpreter needs a pipe
#          or redirect, both already killed by the structural test above, so
#          `echo "<anything>"` alone is safe to admit unconditionally, #5838)
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
        echo)
            # #5838: echo never executes its arguments as code — it only ever
            # writes them to stdout — so admitting it unconditionally can only
            # smuggle a live command if that stdout is then piped/redirected to
            # an interpreter (`echo "rm -rf /" | sh`) or captured into a
            # substitution and eval'd. Both require a `|`, `>`, `` ` ``, or
            # `$(` on this same line, which fastpath_structural_ok() above has
            # already ruled out before this case statement ever runs — so a
            # bare `echo "<anything>"` is safe to admit with any args, same
            # zero-mutation-surface rationale as jq/wc/head/tail just above.
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

# Quote/escape-aware pipe count (#5673). Pure bash string ops, zero forks —
# same budget as fastpath_grep_pipe_admits() itself.
#
# A `|` that lives inside a single-/double-quoted argument (grep's own BRE
# alternation pattern, e.g. `"DROP TABLE\|SQL_DDL_PATTERN"`) or immediately
# after an unquoted backslash escape is DATA to the shell, not a pipe
# operator. The naive whole-string character count fastpath_grep_pipe_admits()
# used to do (`${cmd%%|*}` / `case "$right" in *'|'*)`) could not tell that
# apart from a real second pipe: it split at the quoted `|` first, then saw
# the genuine trailing `| head` as a "second" pipe and declined — falling
# through to the full pattern-matching path, which then denied on a bare
# substring match (e.g. "DROP TABLE") inside what was actually a read-only
# grep search argument. This mirrors the quote-tracking state machine
# qsplit() (:~1295) already uses for the awk-side segment splitters (#3755),
# ported to bash since this fast path must stay fork-free; by this point in
# fastpath_grep_pipe_admits(), the caller has already rejected any `$(` /
# backtick anywhere in $cmd, so — unlike qsplit() — a quoted span here can
# never smuggle a command substitution and needs no such carve-out.
#
# Sets _FASTPATH_REAL_PIPE_COUNT to the number of real (shell-significant)
# pipes found, and _FASTPATH_REAL_PIPE_POS to the byte offset of the first
# one (meaningful only when the count is exactly 1). An unterminated quote
# (malformed/unparseable input) forces the count to -1 — never trust a
# partial scan — so the caller declines the fast path exactly like any other
# ambiguous shape (a false negative, not a hole).
_fastpath_count_real_pipes() {
    local s="$1"
    local -i i=0 n=${#s} count=0 pos=-1
    local mode=0 c   # 0=unquoted 1=single-quoted 2=double-quoted
    while (( i < n )); do
        c="${s:i:1}"
        case "$mode" in
            0)
                case "$c" in
                    "'") mode=1 ;;
                    '"') mode=2 ;;
                    '\') (( i++ )) ;;   # unquoted backslash escapes the next char
                    '|') (( count++ )); (( pos == -1 )) && pos=$i ;;
                esac
                ;;
            1)
                [[ "$c" == "'" ]] && mode=0   # no backslash escaping inside '...'
                ;;
            2)
                case "$c" in
                    '"') mode=0 ;;
                    '\') (( i++ )) ;;   # \" \\ etc. inside "..."; a `|` is inert either way
                esac
                ;;
        esac
        (( i++ ))
    done
    if (( mode != 0 )); then
        count=-1   # unterminated quote: never trust the partial count
    fi
    _FASTPATH_REAL_PIPE_COUNT=$count
    _FASTPATH_REAL_PIPE_POS=$pos
}

fastpath_grep_pipe_admits() {
    local cmd="$1"
    # No shell metacharacter other than a single pipe. Reject substitution,
    # redirection, chaining, backticks, and newlines outright.
    case "$cmd" in
        *';'*|*'&'*|*'<'*|*'>'*|*'`'*|*'$('*) return 1 ;;
    esac
    [[ "$cmd" == *$'\n'* ]] && return 1
    [[ "$cmd" == *'|'* ]] || return 1
    _fastpath_count_real_pipes "$cmd"
    # Exactly one REAL pipe: a second one (`grep a | grep b | head`) declines
    # here and falls through to the full path (conservative — a false
    # negative, not a hole). A `|` inside a quoted argument no longer counts.
    (( _FASTPATH_REAL_PIPE_COUNT == 1 )) || return 1
    local left="${cmd:0:_FASTPATH_REAL_PIPE_POS}"
    local right="${cmd:_FASTPATH_REAL_PIPE_POS+1}"
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
# Cargo-clean scope guard toggle — default ON (#6684).
#
# `cargo clean` looks purely local, but on a host whose `~/.cargo/config.toml`
# (or a repo/ancestor `.cargo/config.toml`) sets a SHARED `build.target-dir`
# outside any one repo, a bare `cargo clean` deletes every project's build
# output on that host — including whatever sweep is compiling concurrently
# elsewhere. The cost is "only" recompilation (build output is derived state),
# but it lands on unrelated in-flight work with no way to attribute it, and
# the resulting failure ("No such file or directory" mid-build) names nothing
# about the real cause. See cargo_clean_scope_match() / cargo_clean_effective_
# target_dir() below for the detection + resolution this toggle gates.
#
# Resolution order (highest precedence first):
#   1. LOOM_GUARD_CARGO_CLEAN env var (0/false/no disables, 1/true/yes forces on)
#   2. .loom/config.json  ->  guards.cargoCleanScope  (default true when absent)
#   3. Default: true (guard on)
#
# Mirrors sql_guard_enabled()/cloud_guard_enabled(): cached in
# _CARGO_CLEAN_GUARD_CACHE, invoked LAZILY only once a bare (unscoped) `cargo
# clean` segment has already matched, so the config read never touches the hot
# path for the overwhelming majority of commands that never invoke cargo at
# all. The config read is best-effort: any parse failure falls through to
# guard-ON.
# =============================================================================
_CARGO_CLEAN_GUARD_CACHE=""
cargo_clean_guard_enabled() {
    if [[ -z "$_CARGO_CLEAN_GUARD_CACHE" ]]; then
        local enabled=true raw
        if [[ -n "$REPO_ROOT" ]]; then
            raw=$(loom_config_get "$REPO_ROOT" "guards.cargoCleanScope" "true" 2>/dev/null) || raw=true
            [[ "$raw" == "false" ]] && enabled=false
        fi
        # Env override wins over config.
        case "${LOOM_GUARD_CARGO_CLEAN:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _CARGO_CLEAN_GUARD_CACHE="$enabled"
    fi
    [[ "$_CARGO_CLEAN_GUARD_CACHE" == "true" ]]
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

# True if $1 (the ask-scan form of a command) contains at least one stash
# CREATE invocation: bare `git stash`, `git stash push …`, `git stash save …`,
# or an option-prefixed create (`git stash -u`, `git stash --include-untracked`,
# `git stash -m wip`).
#
# Deliberately NOT treated as a create (#5754):
#   - `pop` / `drop` / `clear` — the RECOVERY half, handled by its own ask
#     below. Never escalate those: once WIP is on `refs/stash`, `pop` is the
#     only way to get it back (worktree.sh's stash-pop reads a per-issue ref,
#     not `refs/stash`), so blocking them strands work with no recovery path.
#   - `apply` / `list` / `show` / `branch` — do not remove entries from the
#     shared stack.
#   - `create` / `store` — plumbing. `git stash create` is exactly what
#     worktree.sh's own `stash-push` runs, so matching it would deny the
#     sanctioned replacement path itself.
#   - `-h` / `--help` — not an operation at all.
#
# ERE has no lookahead, and one command can chain several `git stash`
# invocations of different kinds (`git stash && <check>; git stash pop` is the
# exact shape this fires on), so the subcommand token is extracted per
# occurrence and classified in shell rather than encoded in a single pattern.
# The trailing `([[:space:]]|[;&|)]|$)` on the match is what makes `stash` a
# whole token — without it `git stashx` would match the `git stash` prefix and
# be misread as a bare create.
#
# BACKTICK BOUNDARY (#5783): the leading class, the subcommand token's
# excluded-character class, and the trailing class all now also admit a
# backtick — `` `git stash push` `` used to be invisible to the leading
# anchor entirely, and even after that half is fixed, an unfixed subcommand
# class would swallow the closing backtick into the token itself (`push\``)
# and fail the `push` case match below. All three sites need the same
# widening together for a backtick-wrapped create to classify correctly.
stash_create_invoked() {
    local scan="$1" occurrence subcmd
    local -a parts
    while IFS= read -r occurrence; do
        [[ -n "$occurrence" ]] || continue
        IFS=$' \t' read -r -a parts <<< "$occurrence"
        subcmd="${parts[2]:-}"
        # The match may swallow a trailing separator (`git stash push;`), so
        # keep only the token up to the first shell delimiter.
        subcmd="${subcmd%%[;&|)\`]*}"
        case "$subcmd" in
            -h|--help)        ;;
            ""|push|save|-*)  return 0 ;;
            *)                ;;
        esac
    done < <(printf '%s\n' "$scan" \
        | grep -oE '(^|[;&|(`]|[[:space:]])git[[:space:]]+stash([[:space:]]+[^[:space:];&|)`]+)?([[:space:]]|[;&|)`]|$)' \
        | sed -E 's/^.*(git[[:space:]]+stash)/\1/')
    return 1
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
# SAME-COMMAND VARIABLE RESOLUTION (#4881, shared #6152) — resolve_var() /
# record_assign() / varmap.
#
# Originally embedded only inside extract_write_targets() (the write-
# confinement scan): when the SAME command text contains a `NAME=value`
# assignment (no embedded whitespace in `value`, optionally single/double-
# quoted) earlier in the stream, a later `$NAME`/`${NAME}` token is
# substituted with that value. See extract_write_targets()'s own header
# comment (above its body, further down this file) for the full contract,
# the recognized assignment shapes (bare / export / readonly / declare /
# typeset / local / multi-assignment / env-prefix), the CONFLICTING-
# ASSIGNMENTS-POISON-THE-VARIABLE rule, and the FAIL-CLOSED-ON-UNRESOLVABLE
# guarantee (an unresolvable `$NAME` is returned UNCHANGED, never guessed and
# never dropped).
#
# Extracted to a shared awk source string (#6152, same pattern as
# _QSPLIT_AWK/_CDEXPAND_AWK/_CDQUOTE_AWK above) so parse_force_ops() can reuse
# the IDENTICAL resolver for its `-C <path>` / `cd <dir>` cwd-capture points
# instead of drifting a second copy: a `-C "$VAR"`/`cd "$VAR"` argument fed by
# a preceding same-command `VAR=literal` assignment (e.g. the Guide role's own
# `DOCS_WT="..."; git -C "$DOCS_WT" reset --hard HEAD` shape) previously left
# `cpath`/`cdarg` as the literal unexpanded `$VAR` token, so the #5775
# managed-worktree detached-HEAD reset-recovery allowlist could never resolve
# an absolute cwd to check and always fell through to asking. Callers that
# include this snippet must populate `varmap` themselves by calling
# record_assign() on each `NAME=value` word in a segment BEFORE consulting
# resolve_var() on that same command'"'"'s later tokens — extract_write_targets()
# and parse_force_ops() both do this per-segment, in their own main loops.
# =============================================================================
_VARRESOLVE_AWK='
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
# strip_subst_close_parens() -- drop the closing paren(s) of an ENCLOSING
# `$(...)` command substitution that whitespace tokenization left glued onto a
# write-target token (#6940).
#
# extract_write_targets() is a whitespace/quote-aware TOKENIZER, not a shell
# parser: it has no notion of command-substitution nesting, so in the extremely
# common capture-stderr idiom
#
#     ERR_FILE=/tmp/champion_ci_err_6212.txt
#     out=$(gh pr checks "$PR" --json bucket 2>"$ERR_FILE")
#
# the `2>` redirect target arrives at resolve_var_q() as the literal token
#     "$ERR_FILE")
# -- the `)` that actually terminates the SURROUNDING `$(`...`)` is still
# attached, because nothing separated it from the target by whitespace.
# the double-quote-pair test in resolve_var_q() (and the leading-`$` test
# inside resolve_var()) then both miss, so a target whose value the resolver had
# ALREADY recorded from a literal same-command assignment moments earlier fell
# through unresolved and denied with `worktree-write-confinement-unresolved-
# var`. The identical command WITHOUT the `$(...)` wrapper resolved fine, so
# this was a tokenization gap, not an intentional "a substitution is a fresh
# unresolvable scope" rule (#6940; same defect class as the #6444 quote gap
# directly below).
#
# NARROWNESS -- only an UNBALANCED trailing `)` is stripped: the token`s own
# parens are counted, and at most (closes - opens) trailing `)` characters are
# removed. A paren that belongs to the TOKEN ITSELF is therefore never touched,
# which is what keeps the fail-closed contract intact for the cases that matter:
#   - `$(mktemp -d)` / `"$(mktemp)"` as the target is BALANCED -> untouched ->
#     still not a bare variable reference -> still unresolvable -> still denies.
#   - `$(mktemp))` (a dynamic target inside an enclosing substitution) loses
#     exactly ONE paren, leaving `$(mktemp)` -- still unresolvable, still denies.
#   - a process substitution argument (`>(cmd)`) is balanced -> untouched.
# Parens inside a quoted span are counted like any other, so quoted paren DATA
# can only ever SUPPRESS a strip (leaving the pre-#6940 unresolved/deny
# behavior), never manufacture one.
#
# Stripping cannot widen an allow on its own either: the returned token is
# still handed to resolve_var() (unchanged fail-closed semantics -- an
# unresolvable name comes back untouched) and the resolved path is still run
# through the SAME containment check, so this can never grant more than writing
# that literal path outright would already grant (the #6172 argument). Removing
# a trailing `)` only ever SHORTENS a path within the same parent directory, so
# a target that was inside the main checkout stays inside it.
function strip_subst_close_parens(tok,   i, n, c, opens, closes, excess) {
    n = length(tok)
    if (n == 0) return tok
    if (substr(tok, n, 1) != ")") return tok
    opens = 0
    closes = 0
    for (i = 1; i <= n; i++) {
        c = substr(tok, i, 1)
        if (c == "(") { opens++ } else if (c == ")") { closes++ }
    }
    excess = closes - opens
    if (excess <= 0) return tok
    while (excess > 0) {
        n = length(tok)
        if (n == 0 || substr(tok, n, 1) != ")") break
        tok = substr(tok, 1, n - 1)
        excess--
    }
    return tok
}
# resolve_var_q() -- quote-aware wrapper around resolve_var(), used by the
# five write-target print sites inside extract_write_targets() (#6444).
# qsplit() deliberately preserves quote characters verbatim in each token
# (see its own header comment above), so the extremely common, safe
# double-quoted idiom (`"$VAR/path"`) reaches resolve_var() with a leading
# `"` rather than `$`, and the `substr(tok, 1, 1) != "$"` guard at the top of
# resolve_var() bails out immediately -- leaving a fully-known target
# unresolved and producing a false `worktree-write-confinement-unresolved-
# var` deny. Strip ONE matching leading+trailing DOUBLE-quote pair
# (mirroring the DQ/SQ-aware, single-pair-only stripping rule record_assign()
# already applies to assignment VALUES above, narrowed here to double-quote
# only) before handing off to resolve_var(). SINGLE-quote is deliberately
# never stripped here: a literal single-quoted reference like the two-
# character sequence dollar-sign VAR wrapped in single quotes is a shell
# literal (the shell never expands it) and must stay unresolved rather than
# being silently substituted with varmap value -- the fail-closed guarantee
# (#4921/#6172). A single-quoted or unquoted-but-non-dollar-leading token
# falls through to resolve_var() exactly as it did before this wrapper
# existed.
function resolve_var_q(tok,   n, c1, c2) {
    # #6940: peel an enclosing `$(...)`s trailing paren FIRST, so the quote
    # test below (and resolve_var()s leading-$ test after it) sees the real
    # target token rather than one with a stray `)` glued on.
    tok = strip_subst_close_parens(tok)
    n = length(tok)
    if (n >= 2) {
        c1 = substr(tok, 1, 1)
        c2 = substr(tok, n, 1)
        if (c1 == DQ && c2 == DQ) tok = substr(tok, 2, n - 2)
    }
    return resolve_var(tok)
}
BEGIN {
    DQ = sprintf("%c", 34)
    SQ = sprintf("%c", 39)
    # Poison value for a name assigned two different values in one command
    # (see record_assign). The leading "$" is load-bearing: it routes into
    # the existing unresolved-chain refusal inside resolve_var().
    AMBIG = "$__LOOM_AMBIGUOUS_ASSIGNMENT__"
}
'

# =============================================================================
# QUOTE- AND ARITHMETIC/TEST-CONTEXT-AWARE REDIRECTION MASKING (#4245, #5515)
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
# ARITHMETIC/TEST CONTEXT (#5515): an unquoted `>`/`>=`/`<`/`<=` is ALSO not a
# redirection operator when it is a comparison inside a `(( ... ))` arithmetic
# command/expansion or a `[[ ... ]]` conditional expression — a routine bash
# idiom (`if (( x > 0 )); then`, `(( ${#ARR[@]} > 0 ))`, `[[ "$a" > "$b" ]]`).
# Before #5515, mask_gt() left those bytes unmasked (they are unquoted), so
# extract_write_targets()'s bare-operator branch read the token immediately
# after a bare `>` as a write target (`(( x > 0 ))` -> phantom target `0`), and
# its attached-form branch matched the `>=` token shape (never a valid
# redirection operator in POSIX/bash) and stripped its leading `>`, leaving a
# phantom target of literal `=` (`(( x >= y ))` -> phantom target `=`). Both
# resolve inside curcwd and can trigger a worktree-write-confinement DENY on a
# command that writes nothing.
#
# mask_gt() now ALSO tracks `((`/`))` and `[[`/`]]` span depth (adepth/tdepth
# below) the same way it tracks quote state, and masks `>`/`<` bytes found
# while depth > 0 exactly like a quoted `>` — same MASK byte, same
# byte-for-byte-length-identical contract. A span opens only on the LITERAL
# adjacent two-character sequence `((`/`[[` (consumed as one step, so a
# subsequent stray third paren/bracket is not itself misread as another open)
# and closes on the literal adjacent `))`/`]]` while its own depth is > 0 —
# nested single parens inside an arithmetic span (`(( (a) > 0 ))`) do not
# perturb depth, matching the common case this fix targets. A `>` OUTSIDE any
# such span — including one following a closed arithmetic span on the SAME
# segment, e.g. `echo $(( x > 0 )) > file` — is untouched and still flows
# through as a real operator, so the genuinely dangerous cases (`cp`/`mv`/
# `tee`/`sed -i`/actual redirection writing into the main checkout) keep
# denying. This span tracking only ever runs in unquoted mode (mode == 0
# below): a literal `((`/`[[` appearing as DATA inside a quoted string is
# never treated as entering a span, matching how such a string's `>` is
# already unconditionally masked as quoted data regardless of context.
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
# never crashes, never mis-indexes. Same acceptance for an unbalanced
# `((`/`[[` span: depth simply never returns to 0 for the rest of the buffer,
# masking any LATER unquoted `>`/`<` too -- the same "never widen a deny into
# an allow" fallback direction every other best-effort scan in this file takes
# (a masked-away `>` can only DROP a write target, never invent a new deny).
# =============================================================================
_MASKGT_AWK='
function mask_gt(s,   out, n, i, c, mode, SQ, DQ, MASK, adepth, tdepth) {
    SQ = sprintf("%c", 39)    # single quote
    DQ = sprintf("%c", 34)    # double quote
    MASK = sprintf("%c", 1)   # SOH -- placeholder for a quoted/arith-context ">"/"<" (never a real char)
    out = ""
    n = length(s)
    i = 1
    mode = 0     # 0 = unquoted, 1 = single-quoted, 2 = double-quoted
    adepth = 0   # `((...))` arithmetic-context nesting depth (unquoted only)
    tdepth = 0   # `[[...]]` test-context nesting depth (unquoted only)
    while (i <= n) {
        c = substr(s, i, 1)
        if (mode == 0) {
            if (c == SQ) { mode = 1; out = out c; i++; continue }
            if (c == DQ) { mode = 2; out = out c; i++; continue }
            if (c == "(" && i < n && substr(s, i + 1, 1) == "(") {
                adepth++
                out = out "(("
                i += 2
                continue
            }
            if (c == ")" && i < n && substr(s, i + 1, 1) == ")" && adepth > 0) {
                adepth--
                out = out "))"
                i += 2
                continue
            }
            if (c == "[" && i < n && substr(s, i + 1, 1) == "[") {
                tdepth++
                out = out "[["
                i += 2
                continue
            }
            if (c == "]" && i < n && substr(s, i + 1, 1) == "]" && tdepth > 0) {
                tdepth--
                out = out "]]"
                i += 2
                continue
            }
            if ((adepth > 0 || tdepth > 0) && (c == ">" || c == "<")) {
                out = out MASK
                i++
                continue
            }
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
#      NARROWED (#6353): the write-confinement scan (extract_write_targets(),
#      via mask_heredoc_bodies_selective(buf, 1)) no longer treats
#      python[0-9.]*/perl/ruby/node/nodejs as "interpreter-fed" for THIS
#      purpose -- only bash/sh/zsh/dash/ksh/eval/source/. keep the #5351
#      "leave body visible" treatment there. Those four languages never
#      resolved a heredoc-body `>`/`>=`/`<`/`<=` to a write/read redirect in
#      their own syntax in the first place (it is an ordinary comparison
#      operator), so leaving their bodies visible bought no real protection
#      while manufacturing false DENYs on ordinary code. The catastrophic
#      tier's gh-api-rawfield-body-literal-at check (#5198) and the general
#      COMMAND_ASK_SCAN heredoc pass are UNCHANGED -- they still call
#      mask_heredoc_bodies_selective() with shell_only unset, so all five
#      language families stay "interpreter-fed" for those two purposes.
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
# or "" when that `<<` is not a recognized heredoc opener. As a side effect,
# sets the global HEREDOC_DELIM_QUOTED to 1 when the opening delimiter was
# single- or double-quoted and 0 when it was bare/unquoted -- callers that
# need to distinguish an inert quoted heredoc body (no expansion) from a live
# unquoted one (command substitution expanded by the OUTER shell while
# building the body, see mask_heredoc_bodies_selective()) read this right
# after calling heredoc_delim_at(); it is only meaningful when the return
# value is non-empty.
function heredoc_delim_at(line, p,   start, qc, c, wordend, d, SQ, DQ) {
    SQ = sprintf("%c", 39)    # single quote
    DQ = sprintf("%c", 34)    # double quote
    HEREDOC_DELIM_QUOTED = 0
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
    HEREDOC_DELIM_QUOTED = (qc != "")
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
function is_interpreter_opener(line, shell_only,   n, segs, i, seg, m, toks, j, base) {
    # Split into command segments on ; & | (covers && and || too) so a piped
    # or chained interpreter is caught in ANY position, e.g. `cat <<EOF | bash`
    # and `cat <<EOF | sudo bash`.
    #
    # shell_only (#6353): an OPTIONAL second argument. When truthy, only the
    # genuine SHELL interpreters (bash/sh/zsh/dash/ksh/eval/source/.) count as
    # "interpreter-fed" -- python[0-9.]*/perl/ruby/node/nodejs do NOT, even
    # though they are still real interpreters. Left unset ("", falsy) by
    # every pre-existing caller, so the default behavior (all of the above
    # count) is UNCHANGED for them. Only the write-confinement scan inside
    # extract_write_targets() (below) passes shell_only=1 -- see the comment
    # at that call site for why: `>`/`>=`/`<`/`<=` is a live redirect ONLY in
    # real shell syntax; in Python/Perl/Ruby/JS source it is an ordinary
    # comparison/generic operator, so leaving heredoc bodies fed to those
    # languages visible to a shell-write-idiom scan buys no real protection
    # (their actual writes go through language-level APIs this scanner does
    # not parse regardless) while manufacturing false DENYs on ordinary code
    # like `if len(affected) > 20:` (#6353).
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
        if (shell_only) {
            if (base ~ /^(bash|sh|zsh|dash|ksh|eval|source|\.)$/)
                return 1
        } else {
            if (base ~ /^(bash|sh|zsh|dash|ksh|python[0-9.]*|perl|ruby|node|nodejs|eval|source|\.)$/)
                return 1
        }
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
# per is_interpreter_opener() -- see KNOWN LIMITATIONS #1 above -- OR whose
# delimiter was bare/unquoted (`<<EOF`, `<<-EOF`), per HEREDOC_DELIM_QUOTED
# from heredoc_delim_at(). An unquoted heredoc body is NOT inert text: the
# OUTER shell still expands `$(...)`/backticks/`${...}` inside it while
# building the body, even when the sink is an inert command like `cat` --
# masking it would blank a genuinely live command out of the scan
# (regression found and fixed in review of #5779/#5781; a single-quoted
# `<<'"'"'EOF'"'"'` body has no such expansion and stays maskable). Used by
# BOTH tiers: the gh-api-rawfield-body-literal-at catastrophic check (#5198)
# and, as of #5351, the extract_write_targets() ask-tier write-confinement
# scan (the END-block call below) -- so a write into the main checkout
# inside an interpreter-fed heredoc body is no longer masked out of the
# confinement check. Plain mask_heredoc_bodies() above is retained as the
# reference primitive (identical minus the interpreter carve-out) but now
# has no runtime caller.
#
# shell_only (#6353): an OPTIONAL second argument, forwarded verbatim to
# is_interpreter_opener() -- see the header comment on that function for the
# full rationale. Left unset by the #5198 gh-api-rawfield-body-literal-at
# caller and the general COMMAND_ASK_SCAN heredoc pass (both keep treating
# python/perl/ruby/node[js] heredoc bodies as interpreter-fed, unchanged);
# passed as 1 ONLY by the internal call inside extract_write_targets(), so
# just the worktree-write-confinement scan narrows to shell interpreters.
function mask_heredoc_bodies_selective(s, shell_only,   out, lines, nl, i, j, line, trimmed, body, delim, delim_quoted, closeat, p, off, MASKC) {
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
            delim_quoted = HEREDOC_DELIM_QUOTED
            if (delim == "") continue
            closeat = 0
            for (j = i + 1; j <= nl; j++) {
                trimmed = lines[j]
                sub(/^\t+/, "", trimmed)
                if (trimmed == delim) { closeat = j; break }
            }
            if (closeat == 0) continue
            if (delim_quoted && !is_interpreter_opener(line, shell_only)) {
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
# True when every line of the heredoc body span [from, to) is PROVABLY free of
# the two constructs that make an UNQUOTED-delimiter heredoc body live code to
# the outer shell:
#
#   * a `$(` command substitution -- the shell runs whatever is inside it while
#     building the body, so text there is executable, not data. (`$((...))`
#     arithmetic also starts with `$(` and is likewise rejected: conservative,
#     and arithmetic never appears in prose bodies anyway.)
#   * an UNESCAPED backtick -- the older command-substitution spelling. A
#     backslash-escaped backtick (`\`` -- overwhelmingly the common case, since
#     a markdown fenced code block inside a double-quoted `"$(cat <<EOF ...)"`
#     capture must escape every backtick to survive the outer quoting) is
#     literal text and does NOT disqualify the body.
#
# NOT rejected: a bare `$VAR` / `${VAR}` parameter expansion. The shell expands
# it to TEXT and never re-scans that text for command substitution, so a
# variable reference in the body cannot execute anything -- and the guard scans
# the raw command string, never the expanded result. Rejecting `$` outright
# would leave the real-world false positive in issue #6056 unfixed: both logged
# occurrences carried a `<!-- loom:verdict-sha sha=$VERDICT_SHA ... -->` trailer
# and escaped markdown fences.
#
# Backslash handling walks the line so an escaped backslash (`\\`) does not
# swallow the character after it -- `\\` followed by a backtick is a LIVE
# backtick and correctly disqualifies the body.
function _heredoc_body_expansion_free(lines, from, to,   j, line, k, n, c, BTC) {
    BTC = sprintf("%c", 96)   # backtick
    for (j = from; j < to; j++) {
        line = lines[j]
        if (index(line, "$(")) return 0
        if (index(line, BTC) == 0) continue
        n = length(line)
        for (k = 1; k <= n; k++) {
            c = substr(line, k, 1)
            if (c == "\\") { k++; continue }
            if (c == BTC) return 0
        }
    }
    return 1
}
# Mask the body of an UNQUOTED-delimiter cat-heredoc (`cat <<EOF` / `cat <<-EOF`)
# whose stdout is captured by a command substitution that is itself the VALUE of
# a known non-executing text-data flag -- the `gh pr comment N --body "$(cat
# <<EOF ... EOF)"` idiom (issue #6056).
#
# mask_heredoc_bodies_selective() above deliberately leaves EVERY unquoted
# heredoc body visible, because the outer shell expands `$(...)`/backticks
# inside it while building the body (#5781). That is the right default, but it
# is stricter than necessary: when the body provably contains no such expansion
# (see _heredoc_body_expansion_free() above), the text is exactly as inert as a
# single-quoted heredoc body and should not be scanned by the ASK_PATTERNS /
# parse_force_ops() passes any more than a quoted one is. Both real occurrences
# in #6056 were Judge "changes requested - merge conflict" comments whose prose
# quotes `git push --force-with-lease` inside a fenced code block as advice to a
# human -- flagged force-op:protected as though the force-push were live, with
# no human present in headless mode to answer the ask.
#
# The confinement proof mirrors guard-loom-workflow.sh mask_cat_heredoc_bodies()
# (#5109/#5122/#5672), and all four conditions are required:
#   1. the word immediately before `<<` is a bare `cat` (never an interpreter),
#   2. the text before that `cat` ends with a text-data flag whose value is an
#      opening `$(`/backtick capture (`capre`) -- so cat stdout is provably
#      confined to inert message text and can never reach a shell,
#   3. the opener line ENDS after the delimiter -- anything trailing it (`| bash`,
#      `> file`) routes cat stdout elsewhere and is left visible,
#   4. the body is expansion-free per _heredoc_body_expansion_free().
# Anything that fails any condition masks NOTHING and is scanned exactly as
# before, so this can only narrow an ask, never miss one. QUOTED-delimiter
# heredocs are skipped here entirely -- mask_heredoc_bodies_selective() already
# handles those (with its interpreter carve-out), and re-handling them here
# would bypass that carve-out.
function mask_unquoted_cat_heredoc_bodies(s,   out, lines, nl, i, j, line, trimmed, body, delim, closeat, p, off, start, wordend, qc, rest, pre, before_cat, capre, MASKC, SQ, DQ, BT) {
    MASKC = sprintf("%c", 23) # ETB -- placeholder for inert heredoc-body text
    SQ = sprintf("%c", 39)
    DQ = sprintf("%c", 34)
    BT = sprintf("%c", 96)
    # Same allowlist of non-executing text-data flags / `gh api -f <field>=`
    # fields used by guard-loom-workflow.sh mask_cat_heredoc_bodies().
    capre = "(^|[ \t])((-m|--message|--body|--notes|--title|--comment|--search)[ \t]*=?|-f[ \t]+(body|message|comment|title|notes|search)=)[ \t]*(" DQ "|" SQ ")?[ \t]*([$][(]|" BT ")[ \t]*$"
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
            # (1) the consuming command word must be a bare `cat`.
            pre = substr(line, 1, p - 1)
            if (pre !~ /(^|[^A-Za-z0-9_])cat[ \t]*$/) continue
            # (2) that `cat` must be captured into a text-data flag value.
            before_cat = pre
            sub(/cat[ \t]*$/, "", before_cat)
            if (before_cat !~ capre) continue
            start = p + 2
            if (substr(line, start, 1) == "<") continue    # `<<<` herestring
            if (substr(line, start, 1) == "-") start++
            while (substr(line, start, 1) == " " || substr(line, start, 1) == "\t") start++
            qc = substr(line, start, 1)
            # QUOTED delimiters belong to mask_heredoc_bodies_selective().
            if (qc == SQ || qc == DQ) continue
            wordend = start
            while (substr(line, wordend, 1) ~ /^[A-Za-z0-9_]$/) wordend++
            if (wordend <= start) continue
            delim = substr(line, start, wordend - start)
            # A bare delimiter starting with a digit is an arithmetic shift
            # operand (`$((1 << 3))`), not a heredoc -- same rule as
            # heredoc_delim_at() applies to bare delimiters.
            if (delim ~ /^[0-9]/) continue
            # (3) the opener line must end right after the delimiter.
            rest = substr(line, wordend)
            if (rest ~ /[^ \t]/) continue
            closeat = 0
            for (j = i + 1; j <= nl; j++) {
                trimmed = lines[j]
                sub(/^\t+/, "", trimmed)
                if (trimmed == delim) { closeat = j; break }
            }
            if (closeat == 0) continue
            # (4) the body must be provably free of live expansion.
            if (!_heredoc_body_expansion_free(lines, i + 1, closeat)) continue
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
'

# =============================================================================
# QUOTE-AWARE COMMENT STRIPPING (#6252) -- mask_comment()
#
# COMMAND_NO_COMMENT (built further below) used to strip a `#...end-of-line`
# span whenever the `#` was preceded by whitespace or started a line, WITHOUT
# tracking quote state -- so a `#` inside a single- or double-quoted argument
# (a sed script, a `--body`/`-m`/`--title` prose string, a markdown heading,
# a PR/issue reference like `#958`) was ALSO treated as a comment start,
# truncating everything textually AFTER it. That matters well beyond a
# cosmetic mis-strip: COMMAND_ASK_SCAN (built from COMMAND_NO_COMMENT) is not
# only the ASK/DDL tier's input, it is ALSO the exact input
# extract_write_targets() scans to compute the worktree-write-confinement
# DENY (WRITE_TARGETS, below) -- so the truncation could silently drop a real
# write target from the scan, producing a silent ALLOW where #4178/#4921
# require a DENY. Root-caused in ADR-0016
# (docs/adr/0016-write-target-confinement-approach.md, "Sed / argument-
# position false positive"); live repro: `sed -i '' 's/x/y #958/'
# $SP/file.md`.
#
# mask_comment() walks the string tracking quote state exactly like
# mask_gt()/strip_target_quoting()/mark_expandable_dollars() elsewhere in
# this file (single-quoted, double-quoted, unquoted) and strips a `#...`
# span ONLY when the `#` is UNQUOTED and either starts the buffer, starts a
# new line, or is immediately preceded by a space or tab -- mirroring the
# original sed's `(^|[[:space:]])#.*$` shape exactly, just quote-aware. A
# `#` found while inside a quoted span is never treated as a comment start,
# regardless of what precedes it. Runs over the WHOLE (possibly multi-line)
# buffer in a single pass -- quote state threads across embedded newlines,
# so a quoted argument that itself spans multiple lines is tracked
# correctly too, unlike sed'"'"'s original per-physical-line pattern space.
#
# Deliberately does NOT model backslash-escaped quotes -- same accepted
# simplification qsplit()/mask_gt()/strip_target_quoting() already make for
# this file'"'"'s other quote-tracking scans (see mask_gt()'"'"'s header for the
# detailed rationale). An unterminated quote just runs to the end of the
# buffer in that quote state -- never crashes, never mis-indexes, and only
# ever risks UNDER-stripping (leaving more text visible to the ASK/DDL tier
# and the write-confinement scan), never over-stripping into a missed DENY.
# =============================================================================
_MASKCOMMENT_AWK='
function mask_comment(s,   out, n, i, c, prev, mode, SQ, DQ) {
    SQ = sprintf("%c", 39)    # single quote
    DQ = sprintf("%c", 34)    # double quote
    out = ""
    n = length(s)
    i = 1
    mode = 0     # 0 = unquoted, 1 = single-quoted, 2 = double-quoted
    prev = ""    # previous character, for the start-of-line/whitespace test
    while (i <= n) {
        c = substr(s, i, 1)
        if (mode == 0) {
            if (c == SQ) { mode = 1; out = out c; prev = c; i++; continue }
            if (c == DQ) { mode = 2; out = out c; prev = c; i++; continue }
            if (c == "#" && (prev == "" || prev == "\n" || prev == " " || prev == "\t")) {
                while (i <= n && substr(s, i, 1) != "\n") i++
                continue
            }
            out = out c
            prev = c
            i++
            continue
        }
        if (mode == 1) {
            # Single-quoted: only the matching quote ends the span; a `#`
            # here is always literal data, never a comment start.
            if (c == SQ) mode = 0
            out = out c
            prev = c
            i++
            continue
        }
        # mode == 2 (double-quoted): only the matching quote ends the span.
        if (c == DQ) mode = 0
        out = out c
        prev = c
        i++
    }
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
#   - reset --hard: always emitted with <target> = "@HEAD@", PLUS a third
#     SEP-joined field carrying the reset command's own positional TARGET
#     literal verbatim (e.g. "origin/main", "HEAD~1"; defaults to the literal
#     "HEAD" for a bare `git reset --hard` with no positional target — which
#     is also what makes this field ALWAYS non-empty for a reset line, so the
#     caller can tell a reset line apart from a push "@HEAD@" line, whose
#     third field is simply absent, without a separate op-kind marker).
#     reset --hard never switches branches, so branch IDENTITY is still
#     resolved off the checked-out branch via "@HEAD@" as before — this third
#     field only lets the caller recognize a known-safe RECOVERY target
#     (origin/<default>/origin main|master/HEAD) when that identity
#     resolution lands on a detached HEAD (#5772).
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
#
# SAME-COMMAND VARIABLE RESOLUTION AT THE CWD-CAPTURE POINTS (#6152): both the
# `-C <path>` and `cd <dir>` capture points below now also try resolve_var()
# (the shared _VARRESOLVE_AWK helper, #4881) when the raw argument is a quoted
# or bare `$NAME`/`${NAME}` reference — e.g. the Guide role's own
# `DOCS_WT="..."; git -C "$DOCS_WT" reset --hard HEAD` shape. A preceding
# same-command `NAME=value` assignment is recorded into `varmap` exactly like
# extract_write_targets() does (mirrored below, per-segment, before the
# `cd`/`git` dispatch). resolve_var() itself only matches a BARE `$NAME`
# token, so each capture point first unquotes the raw argument via
# strip_cd_quoting() (#5363/#5372, already used here for classification) —
# this is the ONLY reason strip_cd_quoting() is applied before resolve_var();
# it does not change strip_cd_quoting()'s existing classification-only role
# elsewhere. WHEN RESOLUTION SUCCEEDS the resolved (absolute, unquoted) value
# is used directly, so the #5775 managed-worktree detached-HEAD reset-
# recovery allowlist can actually evaluate it. WHEN IT FAILS (no matching
# assignment, a chained/ambiguous/command-substitution value, or the argument
# was never a variable reference at all) each capture point falls back to
# EXACTLY the pre-#6152 code path — same fail-toward-asking behavior,
# unchanged.
parse_force_ops() {
    printf '%s' "$1" | awk -v startcwd="$2" -v home="$HOME" "$_QSPLIT_AWK""$_CDEXPAND_AWK""$_CDQUOTE_AWK""$_VARRESOLVE_AWK"'
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
            # Record any `NAME=value` assignment(s) leading this segment into
            # varmap for LATER segments'"'"' -C/cd resolve_var() lookups (#6152) —
            # mirrors extract_write_targets()'"'"'s identical assignment scan
            # (see its header comment for the full recognized-shape list and
            # the conflicting-assignment poison rule). Consuming the
            # assignment prefix never hides a real `cd`/`git` command in the
            # same segment (the `A=1 cmd …` env-prefix shape) — whatever
            # remains after the assignment words keeps flowing into the
            # existing dispatch below.
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
            if (seg == "") continue
            m = split(seg, toks, /[ \t]+/)
            if (m == 0) continue
            # Thread a `cd <dir>` prefix through LATER segments of this same
            # compound command (mirrors extract_write_targets, #4933/#4881).
            # `cd -` and a bare `cd` are left unresolved (matches the same
            # known limitation there) rather than guessed. Classification
            # uses strip_cd_quoting() (#5363/#5372) so a fully or partially
            # quoted absolute argument (e.g. '"'"'<dir>'"'"'/sub) is not
            # misclassified as relative; curcwd is still built from the RAW
            # cdarg, exactly mirroring extract_write_targets (#5372) — UNLESS
            # resolve_var() (#6152) resolves toks[2] to a proven value first,
            # in which case that resolved value is cdarg instead.
            if (toks[1] == "cd") {
                if (m >= 2 && toks[2] != "" && toks[2] != "-") {
                    cdunq = strip_cd_quoting(toks[2])
                    cdresolved = resolve_var(cdunq)
                    if (cdresolved != cdunq) {
                        cdarg = cdresolved   # #6152: proven $VAR resolution
                    } else {
                        cdarg = expand_cd_arg(toks[2], home)   # #5315
                    }
                    cdclass = strip_cd_quoting(cdarg)   # #5372
                    if (cdclass ~ /^\//) {
                        curcwd = cdarg
                    } else if (curcwd != "") {
                        curcwd = curcwd "/" cdarg
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
                if (t == "-C") {
                    # #6152: try resolve_var() on the unquoted -C argument
                    # first; fall back to the raw (possibly quoted) token
                    # UNCHANGED, exactly as before, when it does not prove a
                    # substitution (the downstream caller still strips
                    # quoting from a literal quoted path itself, #5372).
                    craw = toks[k+1]
                    cunq = strip_cd_quoting(craw)
                    cresolved = resolve_var(cunq)
                    cpath = (cresolved != cunq) ? cresolved : craw
                    k += 2
                    continue
                }
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
                rt = ""
                # Capture the first positional (non-flag) token as the reset
                # TARGET literal (e.g. "origin/main", "HEAD~1") — a bare
                # `git reset --hard` with no target leaves rt empty (#5772).
                # This is ADDITIONAL to the existing "@HEAD@" identity slot:
                # the caller still resolves branch identity off the CHECKED-
                # OUT branch (reset --hard never switches branches), this
                # third field only lets the caller recognize a known-safe
                # RECOVERY target (origin/main / HEAD) when that identity
                # resolution hits a detached HEAD.
                for (j = k+1; j <= m; j++) {
                    t = toks[j]
                    if (t == "--hard") { hard = 1; continue }
                    if (t ~ /^-/) continue
                    if (rt == "") rt = t
                }
                # A bare `git reset --hard` (no positional target) resets to
                # HEAD itself — default rt to the literal "HEAD" so this
                # third field is ALWAYS non-empty for a reset line, which is
                # exactly what lets the caller tell a reset line apart from a
                # push line (whose "@HEAD@" line never carries a third field
                # at all, since bash `read` leaves a missing trailing field
                # empty) without a separate op-kind marker (#5772).
                if (hard) {
                    if (rt == "") rt = "HEAD"
                    print headcpath SEP "@HEAD@" SEP rt
                }
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
    printf '%s' "$1" | awk -v startcwd="$2" -v home="$HOME" "$_QSPLIT_AWK""$_CDEXPAND_AWK""$_CDQUOTE_AWK""$_MASKWS_AWK"'
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
            # Quote-aware whitespace masking (#6552, same technique as
            # extract_write_targets()'"'"'s #4934/mask_ws() fix): mask a
            # space/tab INSIDE a quoted span with a non-whitespace
            # placeholder before the plain `/[ \t]+/` split runs, so a
            # quoted `cd` argument containing a literal space (e.g. `cd
            # "/Users/me/Real Estate CRM/wt"`) yields exactly ONE token
            # instead of truncating at the first embedded space and being
            # misclassified as relative (which fed a bogus cwd join and a
            # spurious cd-unresolved ask). unmask_ws() restores the real
            # whitespace bytes afterward, so toks[] still carries the RAW,
            # quote-intact text -- preserving the #5372 contract that
            # curcwd is built from the raw cdarg, not an early-unquoted copy.
            wseg = mask_ws(seg)
            m = split(wseg, toks, /[ \t]+/)
            if (m == 0) continue
            for (j = 1; j <= m; j++) toks[j] = unmask_ws(toks[j])
            # Thread a `cd <dir>` prefix through LATER segments of this same
            # compound command (mirrors parse_force_ops above). Classification
            # uses strip_cd_quoting() (#5363/#5372) so a fully or partially
            # quoted absolute argument is not misclassified as relative;
            # curcwd is still built from the RAW cdarg (#5372).
            if (toks[1] == "cd") {
                if (m >= 2 && toks[2] != "" && toks[2] != "-") {
                    cdarg = expand_cd_arg(toks[2], home)   # #5315
                    cdclass = strip_cd_quoting(cdarg)   # #5372
                    if (cdclass ~ /^\//) {
                        curcwd = cdarg
                    } else if (curcwd != "") {
                        curcwd = curcwd "/" cdarg
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
# --title, --notes, --comment, --search) so a dangerous-looking phrase quoted
# INSIDE such a value no longer trips the raw ALWAYS_BLOCK_PATTERNS substring
# scan (catastrophic tier) or the ASK_PATTERNS scan (ask tier, #3756). Used
# ONLY to build the literal-redacted working copies for those two loops
# (mirrors the COMMAND_NO_COMMENT precedent); every other scan keeps reading
# the raw command. This kills the #3679 false positive where `gh pr comment
# --body "…git push --force origin main…"` / `git commit -m "…"` hard-denied
# even though nothing executes, and (#3756) the analogous ask-tier false ask
# where an ask-phrase like `gh issue close` quoted inside a
# `--comment`/`--body` value prompted for confirmation despite no such
# command actually being run.
#
# #5797: `--search` (e.g. `gh issue list --search "docker system prune"`) is a
# read-only query-string value, not an invocation, and gets the same
# same-shape `<flag> "<quoted value>"` redaction as the flags above. A second,
# separate alternative in the regex below handles `jq --arg NAME "<value>"` /
# `jq --argjson NAME "<value>"` — jq's `--arg`/`--argjson` values are filter
# comparands, never executed — which doesn't fit the `<flag> "<value>"` shape
# because jq requires a bare identifier token (NAME) between the flag and the
# quoted value; the second alternative below matches that shape specifically
# so a phrase like `jq --arg p "aws s3 rb" '.'` no longer hard-denies on the
# catastrophic tier (this false positive reproduced there, not only on the
# `cloud-cli` ask tier). Both additions are narrowly scoped to `gh`/`jq`
# read-only value arguments, per the #5214/#5157/#5158 precedent of NOT
# generalizing this into a full qsplit()-segment-parsed rewrite.
#
# Safety floor preserved two ways:
#   - `-c` is deliberately NOT a text-carrying flag, so `bash -c '<payload>'`
#     is never redacted and its payload stays caught by the raw scan.
#   - a DOUBLE-quoted span is redacted ONLY when it carries no command-substitution
#     or backtick opener (`$(` — which also subsumes the arithmetic `$((` — or a
#     backtick). So a smuggling attempt like `git commit -m "$(git push --force
#     origin main)"` is left intact and still hard-denies. A SINGLE-quoted span is
#     always redacted regardless of `$(`/backtick content — real single quotes give
#     bash NO expansion of any kind, so such a span is provably inert either way
#     (#5783; see the qchar == SQ branch at strip_literal_text()'s call site below).
# Each redacted span is replaced by a SAME-LENGTH placeholder so byte offsets of
# the surrounding command are unchanged. Best-effort like COMMAND_NO_COMMENT:
# a backslash-escaped inner double quote (`\"`, DQSPAN below, #7095) IS modeled
# for the double-quoted-span case, but other escape shapes (e.g. an escaped
# quote inside a SINGLE-quoted span, which real bash quoting does not even
# support) are not attempted. Since the result feeds only the narrowing
# (never widening) catastrophic scan, the worst case is a raw substring
# surviving — never a catastrophic block being skipped incorrectly.
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
        #
        # Second alternative (#5797): `jq --arg NAME "<value>"` / `jq --argjson
        # NAME "<value>"` — a bare identifier token (NAME) sits between the flag
        # and the quoted value, which the first alternative'"'"'s shape does not
        # anticipate, so it gets its own alternative rather than being folded
        # into the flag list above.
        #
        # DQSPAN (#7095): an escape-aware double-quoted-span pattern. A bare
        # `[^"]*` stops at the FIRST raw `"` it meets, including one that is
        # itself backslash-escaped -- exactly the shape an exact-phrase
        # `gh ... --search "\"phrase\""` value takes. That collapsed the
        # redacted span down to just the leading backslash, leaving the rest
        # of the value (and any dangerous-looking phrase inside it) fully
        # visible to the raw ALWAYS_BLOCK_PATTERNS scan below. `(\\.|[^"\\])*`
        # instead walks the span two ways: the `\\.` alternative consumes an
        # escaping backslash TOGETHER WITH whatever it escapes (so `\"` and
        # `\\` are swallowed as one unit and never read as a bare closing
        # quote), while `[^"\\]` consumes any ordinary character that is
        # neither the quote delimiter nor a backslash. The span still
        # terminates on the first UNESCAPED `"`, so span-boundary detection
        # across multiple flags on one line is unaffected, and the `$(`/
        # backtick safety-floor check further below still sees the full inner
        # text -- escapes and all -- since that check is a plain substring
        # search over `inner`, not a re-parse of it.
        DQSPAN = DQ "(\\\\.|[^" DQ "\\\\])*" DQ
        re = "(^|[ \t\n])(--message|--body|--notes|--title|--comment|--search|-m)[ \t]*=?[ \t]*(" \
             DQSPAN "|" SQ "[^" SQ "]*" SQ ")" \
             "|(^|[ \t\n])(--arg|--argjson)[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]+(" \
             DQSPAN "|" SQ "[^" SQ "]*" SQ ")"
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
            # Redact ONLY provably inert text (no command substitution / backtick)
            # -- UNLESS the span is single-quoted (#5783). Inside real single
            # quotes bash performs NO expansion of any kind: dollar-paren and
            # a backtick are 100% inert there, always, with no exception --
            # single quotes are the only fully-literal shell quoting form. So
            # a single-quoted --body/-m/etc value that merely quotes a
            # dangerous phrase as documentation (e.g. gh issue comment --body
            # quoting a backticked example command) is safe to redact even
            # though it contains a backtick -- closing the boundary-anchor gap
            # elsewhere in this file must not turn that inert prose into a new
            # false ask/deny. A DOUBLE-quoted span keeps the original
            # conservative floor: dollar-paren / backtick there IS live shell
            # syntax, so it stays un-redacted and visible to the scans below.
            # gsub(/./) leaves embedded newlines untouched (awk `.` never matches a
            # newline), so a multi-line span stays SAME-LENGTH and byte offsets of
            # the surrounding command are preserved.
            if (qchar == SQ || (index(inner, "$(") == 0 && index(inner, "`") == 0)) {
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
# stash-scope:main-checkout / :worktree-collision / :create-redirect) to a real
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

# Mask quoted POSITIONAL arguments (no preceding flag name) to a small
# allowlist of known non-executing SEARCH commands (issue #5158). Extends the
# #5235 mask_ask_positional_args() fix shape to the CATASTROPHIC-tier working
# copy (COMMAND_NO_LITERAL_TEXT below), which mask_ask_positional_args()
# deliberately excludes grep/egrep/fgrep/rg from — see that function's header
# comment for why: COMMAND_ASK_SCAN also feeds SQL_DDL_PATTERN, which
# intentionally scans a `grep '<pattern>' file` invocation's own quoted
# positional pattern for a literal destructive-DDL phrase and denies, by
# design. COMMAND_NO_LITERAL_TEXT is built and scanned entirely independently
# of COMMAND_ASK_SCAN — SQL_DDL_PATTERN never reads COMMAND_NO_LITERAL_TEXT —
# so masking grep/rg's own quoted pattern argument on THIS working copy does
# not reintroduce that regression. Without this, `grep -n "curl .*|" <file>` —
# read-only introspection of a guard's own source text — gets misread as a
# live curl-pipe-to-shell invocation by ALWAYS_BLOCK_PATTERNS, because grep
# never executes what it searches for.
#
# #5838: also includes ./.loom/scripts/check-duplicate.sh, mirroring
# mask_ask_positional_args()'s own allowlist entry for it just above. That
# function's header comment already establishes check-duplicate.sh as safe to
# mask (it never executes either of its TITLE/DESCRIPTION positional
# arguments, only reads them as dedup text) — this was simply missing from
# the catastrophic-tier copy, so a dedup title/description that merely quotes
# a catastrophic-tier phrase (e.g. while filing a bug report ABOUT that
# phrase) hard-denied the read-only dedup check itself.
#
# This is an intentional NEAR-DUPLICATE of mask_ask_positional_args() just
# above (itself a near-duplicate of guard-loom-workflow.sh's
# mask_command_positional_args(), #5155/#5160) — kept as a SEPARATE function
# so a future tuning of one masking pass can never silently change another's
# behavior, per the "never couple the two guards'/tiers' masking" convention
# documented in mask_ask_positional_args()'s header comment above.
#
# #6002: also includes jq. jq is unconditionally admitted with any arguments
# by the #3687/#3772 read-only fast path (fastpath_builtin_admits() above) —
# it never executes its filter-script argument as shell syntax, only reads
# it as a filter program — so masking that same operand here (once the
# command is chained/piped and no longer fast-path-eligible) carries the
# identical safety rationale as fast-path admission, just applied on the
# full-scan path. Without this, a filter script like `jq -c "select(.pattern
# == <phrase>)" file.log`, chained onto another command, previously fell
# through to the raw substring scan below and hard-denied on read-only
# forensic log inspection even though the phrase was only ever quoted DATA
# inside the filter, never a live invocation.
mask_catastrophic_positional_args() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)
        DQ = sprintf("%c", 34)
        # Command-name allowlist: known non-executing search commands whose
        # positional pattern arguments are inert search text, never live
        # shell syntax. Unlike mask_ask_positional_args() above, grep/egrep/
        # fgrep/rg ARE included here — see the function header comment for
        # why that is safe on this (catastrophic-tier) working copy.
        # ./.loom/scripts/check-duplicate.sh (#5838) is added for the same
        # reason mask_ask_positional_args() already carries it below.
        # jq (#6002) is added for the same reason -- see the function header
        # comment above for the full rationale.
        cmdre = "(grep|egrep|fgrep|rg|jq|\\./\\.loom/scripts/check-duplicate\\.sh)"
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
            # (a pipe, &&, an unrelated command chained on the same line) is
            # left fully visible.
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

# Mask a bare shell variable assignment (`NAME='...'` / `NAME="..."`, at
# command position, optionally after a leading `export`) whose quoted value
# is never subsequently read via `$NAME`/`${NAME}` ANYWHERE else in the same
# command buffer (issue #6269, shape 2). Without this, a purely declarative
# assignment like:
#
#   PATTERN='catastrophic:aws s3 rb'
#
# — a standalone line, not followed by anything that actually reads
# $PATTERN — hard-denies exactly like a live invocation, even though the
# assigned value is never executed or even referenced again. This is the
# real-world shape seen repeatedly in `.loom/logs/guard-decisions.log` while
# investigating (and filing an issue about) this very false-positive class:
# a forensic/documentation assignment quoting a flagged phrase as inert data.
#
# SAFETY (mirrors mask_catastrophic_forloop_wordlist()'s fail-closed
# contract just below, applied here via the simplest sufficient check for
# this narrower shape): masking only ever happens when `$NAME` and `${NAME}`
# do not appear ANYWHERE in the full original command buffer — checked
# against the buffer BEFORE any masking, so a later pass's redaction can
# never hide a live reference from this check. Since the assignment
# `NAME=<quote>...<quote>` itself never contains the substring `$NAME` (it
# defines the variable, it does not read it), this single whole-buffer check
# already correctly excludes the assignment's own text — no separate
# self-exclusion bookkeeping is needed. If `$NAME`/`${NAME}` appears
# anywhere else — including a genuinely dangerous consumer like
# `eval "$NAME"`, an unrelated later reassignment that itself reads the old
# value (`NAME="$NAME-more"`), or even an already-trusted inert consumer
# such as `--search "$NAME"` — masking is skipped and the assignment's raw
# text stays fully exposed to the raw substring scan below, exactly as
# before this fix. This is deliberately narrower than "only mask if every
# use is a trusted consumer": it trades a few false-positive assignments
# that DO have a later inert consumer (still denied, no regression — just
# not newly fixed) for a much simpler, more obviously-correct safety
# argument than re-deriving mask_catastrophic_forloop_wordlist()'s full
# consumer-allowlist logic for a different syntactic shape. KNOWN ACCEPTED
# GAP: indirect reads that never spell `$NAME`/`${NAME}` literally (bash
# indirect expansion `${!ref}`, `env`/`printenv` dumps, a second variable
# copied from the first and read under ITS OWN name) are not detected by
# this textual check and so are simply never masked by this pass (fail
# closed, same posture as every other approximation in this file).
#
# Only fires at command position (start of buffer or immediately after one
# of `; & | \` ( <newline>`, mirroring mask_catastrophic_positional_args()'s
# own anchor above) so an incidental `NAME=` substring inside an unrelated
# quoted string or URL query component is not mistaken for an assignment.
mask_catastrophic_var_assignment() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)
        DQ = sprintf("%c", 34)
        anchor = "(^|[ \t\n;&|`(])(export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*="
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
            name = matched
            sub(/^[ \t\n;&|`(]/, "", name)
            sub(/^export[ \t]+/, "", name)
            sub(/=$/, "", name)
            qc = substr(rest, 1, 1)
            if (qc != DQ && qc != SQ) {
                # Not a quoted-literal assignment (e.g. NAME=bareword, or
                # NAME=$(...)) -- nothing this function is scoped to touch.
                out = out pre matched
                s = rest
                continue
            }
            endpos = 0
            for (i = 2; i <= length(rest); i++) {
                if (substr(rest, i, 1) == qc) { endpos = i; break }
            }
            if (endpos == 0) {
                # Unterminated quote -- fail closed, leave unmasked.
                out = out pre matched
                s = rest
                continue
            }
            inner = substr(rest, 2, endpos - 2)
            after = substr(rest, endpos + 1)
            if (index(inner, "$(") != 0 || index(inner, "`") != 0) {
                # Value itself carries a command substitution -- never mask.
                out = out pre matched qc inner qc
                s = after
                continue
            }
            ref1 = "\\$" name "([^A-Za-z0-9_]|$)"
            ref2 = "\\$\\{" name "\\}"
            if (match(buf, ref1) || match(buf, ref2)) {
                # $NAME/${NAME} is read somewhere in the command -- fail
                # closed, leave this assignment'"'"'s value unmasked.
                out = out pre matched qc inner qc
                s = after
                continue
            }
            gsub(/./, "X", inner)
            out = out pre matched qc inner qc
            s = after
        }
        out = out s
        printf "%s", out
    }'
}

# Mask quoted word-list literals inside a `for <var> in "<lit>" "<lit>" ...;
# do ... done` loop, but ONLY when every reference to <var> inside the loop
# body is a provably-inert consumer already trusted elsewhere in this file
# (issue #6002).
#
# Neither strip_literal_text() nor mask_catastrophic_positional_args() above
# touches this shape: the dangerous phrase is a literal token in the for-
# loop's OWN word list, not a value directly following a recognized flag or
# command name — `--search` (etc.) is instead followed by the loop
# VARIABLE (`"$q"`), so the phrase sits structurally distant from any
# command invocation on the same or a later line, e.g.:
#
#   for q in "sql-ddl" "catastrophic:aws s3 rb"; do
#       gh issue list --search "$q" --limit 5
#   done
#
# Blindly masking every for-loop word list would be UNSAFE: the literal
# itself is never executed directly — only its interpolation into <var>
# later matters — so `for cmd in "aws s3 rb s3://victim --force"; do eval
# "$cmd"; done` would silently blind the catastrophic scan to a REAL
# destructive invocation smuggled through the loop variable. This function
# therefore fails CLOSED (leaves the word list fully unmasked, still
# visible to the raw scan below) unless ALL of the following hold:
#
#   1. The loop is fully closed inside this buffer (a `; do`/newline-`do`
#      header and a matching `done`) — mirrors the "must be CLOSED inside
#      this buffer" convention in mask_flag_cat_heredocs() above.
#   2. The body contains no nested for/while/until loop, `eval`, `source`/
#      `. `, or `sh|bash|zsh|dash -c` wrapper — anything that could re-
#      interpret the variable as code rather than read as data.
#   3. The loop variable (`$var` or `${var}`) appears at least once in the
#      body, and EVERY occurrence is immediately preceded by one of the
#      exact same trusted consumer shapes the sibling masking passes above
#      already trust: `--search`/`--arg NAME`/`--argjson NAME` (the
#      strip_literal_text() flag set), directly after
#      grep/egrep/fgrep/rg/jq/check-duplicate.sh (the
#      mask_catastrophic_positional_args() command set), OR interpolated
#      anywhere inside a still-open `echo`/`printf` quoted argument (#6069)
#      — e.g. `echo "=== $q ==="`, the narrated-progress-heading shape
#      CLAUDE.md's own Guard-Decision Telemetry Review section pairs with a
#      `--search "$q"` lookup in the very same loop body, and the shape
#      actually observed recurring in `.loom/logs/guard-decisions.log`.
#      Unlike the grep/jq case (which requires `$var` to BE the whole
#      positional argument), the echo/printf check only requires the
#      variable to sit inside an argument whose quote is still open when
#      `$var` is reached — `echo`/`printf` never execute their arguments as
#      shell syntax, so any position inside an already-open quoted span
#      carries the identical safety rationale. A single occurrence in ANY
#      other context (bare command-position use, `eval "$var"`, an
#      UNQUOTED `echo $var`, etc.) aborts masking for that loop entirely —
#      fail closed, not partial. Known accepted gap: `printf '%s' "$var"`
#      (var in a SECOND, separate argument after a complete format-string
#      argument) is NOT covered — the still-open-quote check below only
#      sees the argument immediately following the command name, so that
#      shape stays fail-closed like any other unrecognized consumer;
#      `printf "text $var text"` (var interpolated directly in the one
#      format-string argument) IS covered.
#
# Only when every check passes are the word-list literals masked, using the
# same inertness floor as every other pass in this file: a span containing
# `$(` or a backtick is left unmasked so command-substitution smuggling
# still reaches the raw scan.
mask_catastrophic_forloop_wordlist() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)
        DQ = sprintf("%c", 34)
        buf = ""
    }
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    function extract_varname(m,    tmp) {
        tmp = m
        sub(/^.*for[ \t]+/, "", tmp)
        sub(/[ \t]+in[ \t]+$/, "", tmp)
        return tmp
    }
    END {
        s = buf
        openre = "(^|[ \t\n;&|`(])for[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]+in[ \t]+"
        out = ""
        while (match(s, openre)) {
            pre     = substr(s, 1, RSTART - 1)
            matched = substr(s, RSTART, RLENGTH)
            varname = extract_varname(matched)
            cursor  = substr(s, RSTART + RLENGTH)

            # Walk consecutive quoted words (the for-loop word list),
            # recording each word'"'"'s quote char, inner text, and trailing
            # whitespace so the span can be reconstructed either masked or
            # verbatim.
            words_n = 0
            delete word_q
            delete word_inner
            delete word_trail
            while (1) {
                qc = substr(cursor, 1, 1)
                if (qc != DQ && qc != SQ) break
                endpos = 0
                for (i = 2; i <= length(cursor); i++) {
                    if (substr(cursor, i, 1) == qc) { endpos = i; break }
                }
                if (endpos == 0) break
                words_n++
                word_q[words_n] = qc
                word_inner[words_n] = substr(cursor, 2, endpos - 2)
                cursor = substr(cursor, endpos + 1)
                trail = ""
                while (substr(cursor, 1, 1) == " " || substr(cursor, 1, 1) == "\t") {
                    trail = trail substr(cursor, 1, 1)
                    cursor = substr(cursor, 2)
                }
                word_trail[words_n] = trail
            }

            # Fallback reconstruction (word list left fully unmasked) used
            # whenever any safety check below fails.
            verbatim = ""
            for (wi = 1; wi <= words_n; wi++) {
                verbatim = verbatim word_q[wi] word_inner[wi] word_q[wi] word_trail[wi]
            }

            bail = 0
            if (words_n == 0) bail = 1

            if (!bail && match(cursor, /^;?[ \t\n]*do([ \t\n]|$)/) == 0) bail = 1

            body = ""
            after_done = ""
            if (!bail) {
                do_head  = substr(cursor, RSTART, RLENGTH)
                after_do = substr(cursor, RSTART + RLENGTH)
                if (match(after_do, /(^|[ \t\n;&|`(])done([ \t\n;&|`)]|$)/) == 0) {
                    bail = 1
                } else {
                    body = substr(after_do, 1, RSTART - 1)
                    done_matched = substr(after_do, RSTART, RLENGTH)
                    after_done = substr(after_do, RSTART + RLENGTH)
                }
            }

            # Refuse to reason about anything beyond a flat, single-
            # statement body: a nested loop, eval, dot-source, or a shell -c
            # wrapper aborts masking for this loop entirely (fail closed).
            if (!bail && (body ~ /(^|[ \t\n;&|`(])(for|while|until|eval|source)([ \t]|$)/ \
                          || body ~ /(^|[ \t\n;&|`(])\.[ \t]+\$/ \
                          || body ~ /(^|[ \t])(sh|bash|zsh|dash)[ \t]+-c([ \t]|$)/)) {
                bail = 1
            }

            # Every occurrence of the loop variable inside body must be a
            # provably-inert reference (see function header comment above
            # for the exact trusted-consumer list). Any other appearance
            # aborts masking for this loop (fail closed).
            if (!bail) {
                varref = "\\$\\{?" varname "\\}?"
                btmp = body
                found_any = 0
                safe = 1
                while (match(btmp, varref)) {
                    matchtext = substr(btmp, RSTART, RLENGTH)
                    nextchar  = substr(btmp, RSTART + RLENGTH, 1)
                    # A bare `$name` match must not be a prefix of a LONGER
                    # identifier (e.g. `$q` inside `$qq`) — the brace form
                    # (`${name}`) already has a hard boundary via the
                    # closing brace, so only the braceless form needs this
                    # check.
                    if (matchtext !~ /\}$/ && nextchar ~ /[A-Za-z0-9_]/) {
                        btmp = substr(btmp, RSTART + RLENGTH)
                        continue
                    }
                    found_any = 1
                    vpre = substr(btmp, 1, RSTART - 1)
                    if (vpre !~ /(--search|--arg[ \t]+[A-Za-z_][A-Za-z0-9_]*|--argjson[ \t]+[A-Za-z_][A-Za-z0-9_]*)[ \t]*=?[ \t]*"?$/ \
                        && vpre !~ /(grep|egrep|fgrep|rg|jq|\.\/\.loom\/scripts\/check-duplicate\.sh)([ \t]+-[A-Za-z0-9_-]+)*[ \t]+"?$/ \
                        && vpre !~ /(^|[ \t\n;&|`(])(echo|printf)([ \t]+-[A-Za-z0-9_-]+)*[ \t]+["'"'"'][^"'"'"']*$/) {
                        safe = 0
                    }
                    btmp = substr(btmp, RSTART + RLENGTH)
                }
                if (!found_any || !safe) bail = 1
            }

            if (bail) {
                out = out pre matched verbatim
                s = cursor
                continue
            }

            # All checks passed — mask each word-list literal (same
            # inertness floor as every other masking pass in this file: only
            # a span with no `$(` / backtick is redacted).
            masked = ""
            for (wi = 1; wi <= words_n; wi++) {
                inner = word_inner[wi]
                if (index(inner, "$(") == 0 && index(inner, "`") == 0) {
                    gsub(/./, "X", inner)
                }
                masked = masked word_q[wi] inner word_q[wi] word_trail[wi]
            }
            out = out pre matched masked do_head body done_matched
            s = after_done
        }
        out = out s
        printf "%s", out
    }'
}

# Mask a WHOLE-LINE `#`-prefixed shell comment (issue #6394) from the
# catastrophic-tier working copy, so a comment that merely QUOTES a
# catastrophic-tier phrase for documentation/forensic purposes (e.g.
# `# aws s3 rb mentioned here only, single line comment`, the exact shape
# this repo's own Auditor "Guard-Decision Telemetry Review" standing policy,
# #3898, produces while reporting on `.loom/logs/guard-decisions.log`) no
# longer hard-denies. A physical line whose first non-whitespace character is
# `#` is NEVER live shell syntax to any interpreter — bash, the outer shell,
# or an inner `sh -c`/heredoc-fed interpreter all treat it as a no-op comment
# — so dropping such a line can never hide a real invocation. Reproduced
# twice, single-line and multi-line, in #6394's own filing.
#
# DELIBERATELY NOT a reuse of COMMAND_NO_COMMENT / mask_comment(): that
# working copy is explicitly reserved for the ASK/DDL tier only (see the
# "COMMENT-STRIPPED WORKING COPY" note further below in this file) — a
# missed ASK there is an accepted risk, but the catastrophic tier is kept
# strictly stricter so a missed BLOCK can never happen from a shared masking
# pass. This function is a SEPARATE, narrower primitive built solely for
# ALWAYS_BLOCK_PATTERNS, with its own, more conservative safety contract:
#
#   1. WHOLE-LINE ONLY: a line is masked only when the ENTIRE line (after
#      stripping leading whitespace) is a comment — i.e. the `#` is the
#      first non-whitespace character on that physical line. A TRAILING
#      comment on a line that also carries real command text (e.g.
#      `aws s3 rb bucket  # decommission`) is deliberately left untouched —
#      unlike mask_comment(), which strips those too — because there is no
#      way to redact the trailing span without touching the command text
#      immediately before it on the same line; scoping to whole-line-only
#      keeps the safety argument for this tier simple and obviously correct.
#      KNOWN ACCEPTED GAP, same posture as every other approximation in this
#      file: a whole-line comment that additionally quotes a catastrophic
#      phrase as a TRAILING comment on a real command's own line is not
#      unmasked by this pass and stays hard-denied (mirrors the accepted-gap
#      convention documented on mask_catastrophic_var_assignment() above).
#   2. QUOTE-AWARE: tracks single-/double-quote state across the WHOLE
#      (possibly multi-line) buffer exactly like mask_comment() does, so a
#      `#` that merely LOOKS like a line-start because it sits on its own
#      physical line, but is actually still inside an unterminated quoted
#      span from a PRIOR line, is never treated as a comment — it stays
#      fully visible to the raw scan and still denies. Same accepted
#      simplification as mask_comment()/mask_gt(): no backslash-escaped-quote
#      modeling.
#   3. HEREDOC-CONSERVATIVE: fails closed (does nothing) for the WHOLE buffer
#      whenever a `<<` heredoc redirect appears anywhere in it, rather than
#      attempting to reason about heredoc body boundaries or interpreter-fed
#      vs. plain-data heredocs. This deliberately mirrors (by NOT touching
#      anything) mask_heredoc_bodies_selective()'s interpreter-fed exclusion
#      further below: a `#`-looking line inside an interpreter-fed heredoc
#      body (`bash <<EOF ... EOF`) stays fully visible and still denies,
#      exactly like a plain (non-interpreter) heredoc body line does today.
mask_catastrophic_comment_lines() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)
        DQ = sprintf("%c", 34)
        buf = ""
    }
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END {
        s = buf
        if (index(s, "<<") != 0) {
            # Fail closed: a heredoc redirect is present somewhere in this
            # buffer -- leave the whole buffer untouched (see contract #3
            # above) rather than try to reason about heredoc boundaries here.
            printf "%s", s
        } else {
            out = ""
            n = length(s)
            i = 1
            mode = 0      # 0 = unquoted, 1 = single-quoted, 2 = double-quoted
            atline = 1    # true at buffer start and right after an unquoted \n
            while (i <= n) {
                c = substr(s, i, 1)
                if (mode == 0) {
                    if (c == SQ) { mode = 1; out = out c; atline = 0; i++; continue }
                    if (c == DQ) { mode = 2; out = out c; atline = 0; i++; continue }
                    if (c == "\n") { out = out c; atline = 1; i++; continue }
                    if ((c == " " || c == "\t") && atline) { out = out c; i++; continue }
                    if (c == "#" && atline) {
                        # Whole-line comment: drop through to (but not
                        # including) the terminating newline, mirroring the
                        # deletion style mask_comment() uses above.
                        while (i <= n && substr(s, i, 1) != "\n") i++
                        continue
                    }
                    out = out c
                    atline = 0
                    i++
                    continue
                }
                if (mode == 1) {
                    if (c == SQ) mode = 0
                    out = out c
                    i++
                    continue
                }
                # mode == 2 (double-quoted)
                if (c == DQ) mode = 0
                out = out c
                i++
            }
            printf "%s", out
        }
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
    #
    # #5797: staying a raw substring scan means these (and `docker system
    # prune` below) still match inside a QUOTED DATA argument to an unrelated,
    # non-executing read-only command — e.g. `gh issue list --search "docker
    # system prune"` or `jq --arg p "cloud-cli:aws s3 rb" …` — neither of
    # which ever runs docker/aws. Rather than command-word-anchoring these
    # ungated denial-floor patterns (risking the FLOOR tests below, which
    # require them to keep denying even under guards.cloudCli:false /
    # LOOM_GUARD_CLOUD=0, unlike the az/gcloud ask-tier branch), the fix
    # narrows COMMAND_NO_LITERAL_TEXT itself: strip_literal_text() now also
    # redacts `--search "…"` and jq's `--arg`/`--argjson NAME "…"` quoted
    # values (see that function's header), so this scan never sees them in
    # the first place. A real docker/aws invocation — quoted or not — is
    # untouched by that redaction and still denies.
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
# #6002: mask a fully-closed `for <var> in "<lit>" ...; do ... done` word
# list's OWN quoted literals BEFORE every other pass below, so a phrase like
# `for q in "sql-ddl" "catastrophic:aws s3 rb"; do gh issue list --search
# "$q"; done` no longer hard-denies on a read-only search built from a
# for-loop word list. This must run first (before the grep/rg/jq/
# check-duplicate positional-arg pass and the flag-value strip below) so the
# loop body still contains the literal `$var`/`${var}` text those two passes
# would otherwise redact away — mask_catastrophic_forloop_wordlist()'s own
# safety check depends on seeing the unredacted variable reference to prove
# every use of it is a trusted consumer. See that function's header comment
# for the full fail-closed safety contract (masking never happens unless the
# loop is fully closed AND every use of the variable in the body is a
# provably-inert consumer already trusted elsewhere in this file). Cheap
# substring gate keeps the awk call off the hot path for the vast majority
# of commands that never contain a for-loop.
if [[ "$COMMAND" == *"for "* && "$COMMAND" == *" in "* ]]; then
    COMMAND_NO_LITERAL_TEXT=$(mask_catastrophic_forloop_wordlist "$COMMAND_NO_LITERAL_TEXT")
fi
# #5158: mask a leading grep/egrep/fgrep/rg invocation's own quoted pattern
# argument BEFORE the flag-keyed strip below, so introspecting the guard's
# own source (e.g. `grep -n "curl .*|" defaults/hooks/guard-destructive.sh`)
# isn't misread as a live curl-pipe-to-shell invocation. Cheap substring gate
# keeps the awk call off the hot path for the vast majority of commands that
# never invoke grep/rg, mirroring the check-duplicate.sh substring gate used
# for mask_ask_positional_args() below. #5838 widens the gate to also cover a
# chained/piped check-duplicate.sh invocation (the bare single-command shape
# is already covered by the #3687 read-only fast path, which doesn't apply
# once the command is chained onto something else, e.g. inside a loop body).
# #6002 adds `jq` to this gate: its filter-script positional operand (e.g.
# `jq -c 'select(.pattern == "aws s3 rb")' file`, once chained onto another
# command) is masked by the same allowlisted-command pass, mirroring jq's
# unconditional #3687/#3772 fast-path admission for the bare single-command
# shape.
if [[ "$COMMAND" == *"grep"* || "$COMMAND" == *"rg "* || \
      "$COMMAND" == *"check-duplicate"* || "$COMMAND" == *"jq"* ]]; then
    COMMAND_NO_LITERAL_TEXT=$(mask_catastrophic_positional_args "$COMMAND_NO_LITERAL_TEXT")
fi
# #6269: mask a bare `NAME='...'`/`NAME="..."` shell variable assignment
# whose value is never read (via `$NAME`/`${NAME}`) anywhere else in the
# command -- see mask_catastrophic_var_assignment()'s header comment for the
# full fail-closed safety contract. Cheap substring gate (an `=` directly
# followed by a quote character) keeps the awk call off the hot path for the
# vast majority of commands that never assign a quoted literal to a
# variable.
if [[ "$COMMAND" == *"='"* || "$COMMAND" == *'="'* ]]; then
    COMMAND_NO_LITERAL_TEXT=$(mask_catastrophic_var_assignment "$COMMAND_NO_LITERAL_TEXT")
fi
# #5797: "--arg" as a substring gate also covers "--argjson" (a superset
# spelling of "--arg"), so no separate "--argjson" check is needed here.
if [[ "$COMMAND" == *"--body"* || "$COMMAND" == *"--message"* || \
      "$COMMAND" == *"--title"* || "$COMMAND" == *"--notes"* || \
      "$COMMAND" == *"--comment"* || "$COMMAND" == *"-m"* || \
      "$COMMAND" == *"--search"* || "$COMMAND" == *"--arg"* ]]; then
    COMMAND_NO_LITERAL_TEXT=$(strip_literal_text "$COMMAND_NO_LITERAL_TEXT")
fi
# #6394: mask any WHOLE-LINE `#`-prefixed shell comment last, so a comment
# that merely quotes a catastrophic-tier phrase for documentation/forensic
# purposes (e.g. `# aws s3 rb mentioned here only`) no longer hard-denies,
# whether it is the entire command or one line among several. See
# mask_catastrophic_comment_lines()'s own header comment (above, alongside
# the other mask_catastrophic_* functions) for the full quote-/heredoc-aware
# safety contract, and why this is deliberately NOT a reuse of
# COMMAND_NO_COMMENT (see the "COMMENT-STRIPPED WORKING COPY" note further
# below in this file for why that copy is reserved for the ASK/DDL tier
# only). Cheap substring gate keeps the awk call off the hot path for the
# vast majority of commands that never contain a `#`.
if [[ "$COMMAND" == *"#"* ]]; then
    COMMAND_NO_LITERAL_TEXT=$(mask_catastrophic_comment_lines "$COMMAND_NO_LITERAL_TEXT")
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
    # primitive from #5000; as of #5351 extract_write_targets() also uses this
    # _selective() variant) to build a
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
    # an ALLOW (#5198's regression). As of #5351 extract_write_targets() calls
    # this SAME _selective() variant (not plain mask_heredoc_bodies()), so both
    # tiers now leave interpreter-fed bodies visible and share one masking
    # contract -- see KNOWN LIMITATIONS #1 above.
    # -------------------------------------------------------------------------
    if [[ "$COMMAND" == *"<<"* ]]; then
        COMMAND_HEREDOC_MASKED=$(printf '%s' "$COMMAND" | awk "$_MASKHEREDOC_AWK"'
        { buf = buf (NR > 1 ? "\n" : "") $0 }
        END { printf "%s", mask_heredoc_bodies_selective(buf) }')
    else
        COMMAND_HEREDOC_MASKED="$COMMAND"
    fi
    # QUOTED-STRING-LITERAL MASKED SCAN (#5835): the heredoc masking above closes
    # the false positive where the denied phrase is quoted inside a heredoc BODY,
    # but a command never needs a heredoc to quote the phrase as inert prose — a
    # plain quoted argument does it too, e.g. a check-duplicate.sh dedup call
    # whose TITLE/DESCRIPTION string literally spells out
    # "gh api ... -f body=@path" while describing this exact bug, never invoking
    # `gh api` at all (production repro: a prior agent's OWN attempt to file that
    # bug report was denied by this check, #5835). Reuses the two masking
    # functions this file already relies on elsewhere to make the narrowing
    # ASK-tier scan (COMMAND_ASK_SCAN, below) quote-aware, applied here to this
    # catastrophic check's OWN dedicated working copy — never to COMMAND_NO_COMMENT
    # / COMMAND_ASK_SCAN itself, so the catastrophic tier still never benefits
    # from `#`-comment stripping (see the "COMMENT-STRIPPED WORKING COPY" note
    # below: comment stripping is reserved for the ASK/DDL tier only):
    #   - mask_ask_positional_args() (#5235) masks quoted POSITIONAL arguments to
    #     the narrow non-executing-script allowlist (currently just
    #     check-duplicate.sh) -- exactly the repro shape above.
    #   - strip_literal_text() (#3679/#5216/#5783) masks quoted values following a
    #     text-carrying FLAG (--body/--message/--title/--notes/--comment/--search/
    #     -m/--arg/--argjson), single-quoted spans unconditionally (real single
    #     quotes give bash zero expansion) and double-quoted spans only when they
    #     carry no `$(`/backtick (so a live command-substitution smuggled through a
    #     double-quoted value stays visible and still denies).
    # Neither function can widen this check: a REAL `gh api ... -f body=@path`
    # invocation is never itself preceded by check-duplicate.sh, nor by any of
    # strip_literal_text()'s flags (`gh api` takes `-f`/`--raw-field`/`-F`/
    # `--field`, none of which are in that flag list) -- see the "narrows, never
    # widens" regression tests in tests/hooks/test-guard-destructive.sh.
    COMMAND_GH_API_RAWFIELD_SCAN="$COMMAND_HEREDOC_MASKED"
    if [[ "$COMMAND_GH_API_RAWFIELD_SCAN" == *"check-duplicate.sh"* ]]; then
        COMMAND_GH_API_RAWFIELD_SCAN=$(mask_ask_positional_args "$COMMAND_GH_API_RAWFIELD_SCAN")
    fi
    if [[ "$COMMAND_GH_API_RAWFIELD_SCAN" == *"--body"* || "$COMMAND_GH_API_RAWFIELD_SCAN" == *"--message"* || \
          "$COMMAND_GH_API_RAWFIELD_SCAN" == *"--title"* || "$COMMAND_GH_API_RAWFIELD_SCAN" == *"--notes"* || \
          "$COMMAND_GH_API_RAWFIELD_SCAN" == *"--comment"* || "$COMMAND_GH_API_RAWFIELD_SCAN" == *"-m"* || \
          "$COMMAND_GH_API_RAWFIELD_SCAN" == *"--search"* || "$COMMAND_GH_API_RAWFIELD_SCAN" == *"--arg"* ]]; then
        COMMAND_GH_API_RAWFIELD_SCAN=$(strip_literal_text "$COMMAND_GH_API_RAWFIELD_SCAN")
    fi
    GH_API_RAWFIELD_BODY_AT_PATTERN="(^|[;&|[:space:]])gh[[:space:]]+api[^;&]*[[:space:]](-f|--raw-field)[[:space:]]*=?[[:space:]]*[\"']?body=[\"']?$GH_AT_PATHISH"
    if echo "$COMMAND_GH_API_RAWFIELD_SCAN" | grep -qE "$GH_API_RAWFIELD_BODY_AT_PATTERN"; then
        deny "BLOCKED: 'gh api ... -f/--raw-field body=@<path>' does NOT read the file — only -F/--field gives '@<path>' its read-from-file meaning. As written this posts the literal string '@<path>' as the body (same silent data loss as PR #4457/issue #4608). Use '-F body=@<path>' instead." "gh-api-rawfield-body-literal-at"
    fi
fi

# =============================================================================
# COMMENT-STRIPPED WORKING COPY - used for the ASK-word and SQL DDL/DML
# matches below, never for the catastrophic ALWAYS_BLOCK scan -- BUT also, as
# of #6252, the input extract_write_targets() scans for the
# worktree-write-confinement DENY (WRITE_TARGETS, below), via COMMAND_ASK_SCAN.
#
# Strips a `#…EOL` shell comment when the `#` is at start-of-line or preceded
# by whitespace (the common comment shape), so a pattern word that appears only
# in a trailing comment ("# drop database first", "# git push --force") no
# longer trips the ASK/DDL gates.
#
# QUOTE-AWARE as of #6252 (mask_comment(), defined above with the other
# quote-state walkers): a `#` found while inside a single- or double-quoted
# span is NEVER treated as a comment start, regardless of what precedes it.
# Before #6252 this was a plain non-quote-aware sed
# (`s/(^|[[:space:]])#.*$//`), which silently truncated the scan at a `#`
# inside ANY whitespace-preceded quoted argument (a sed script, a
# `--body`/`-m` prose string, a PR/issue reference like `#958`) -- harmless
# for the ASK/DDL tier alone (a missed ask on quoted data), but an ACTIVE,
# previously unreported unsound false-negative for the write-confinement DENY
# that also reads this copy: the real write target after the truncation point
# could silently vanish from the scan, producing a silent ALLOW where
# #4178/#4921 require a DENY. Root-caused and fixed per ADR-0016
# (docs/adr/0016-write-target-confinement-approach.md, "Sed / argument-
# position false positive"); regression coverage in
# tests/hooks/test-guard-destructive.sh. The awk only runs when a `#` is
# actually present, keeping it off the hot path (#3553).
# =============================================================================
if [[ "$COMMAND" == *"#"* ]]; then
    COMMAND_NO_COMMENT=$(printf '%s' "$COMMAND" | awk "$_MASKCOMMENT_AWK"'
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END { printf "%s", mask_comment(buf) }')
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
# HEREDOC-BODY MASKING (#5779): none of the narrowings above touch a
# single-quoted heredoc BODY -- e.g. `cat > /tmp/x.md <<'EOF' ... git reset
# --hard ... EOF` -- since that shape carries no --body/-m/etc. flag and is
# not a check-duplicate.sh positional argument either. That heredoc body is
# exactly as inert as a single-quoted string literal (no interpolation,
# nothing executes), so it should not be scanned by the force-op/stash-scope
# ASK_PATTERNS below any more than a quoted string is. Mirrors the
# catastrophic tier's "HEREDOC-MASKED SCAN" (#5181/#5198) above: reuse
# mask_heredoc_bodies_selective() to blank inert heredoc bodies while
# leaving an INTERPRETER-fed heredoc (`bash <<EOF`, `sh -s <<EOF`, ...)
# visible, since that body is genuinely live code (KNOWN LIMITATIONS #1).
# Gated on literal '<<' presence, keeping it off the hot path for the vast
# majority of commands with no heredoc at all. Narrows only: a real,
# non-heredoc force-op/stash invocation is untouched and still asks, even
# sitting in the same multi-line command as an unrelated heredoc.
#
# UNQUOTED-DELIMITER SECOND PASS (#6056): mask_heredoc_bodies_selective()
# deliberately leaves every UNQUOTED-delimiter body (`cat <<EOF`, no quotes
# around EOF) visible, because the outer shell expands `$(...)`/backticks
# inside it (#5781). Correct as a default, but it made the routine Judge
# idiom `gh pr comment N --body "$(cat <<EOF ... EOF)"` false-ask
# force-op:protected whenever the comment prose quotes `git push
# --force-with-lease` as advice to a human -- an unanswerable stall in a
# headless run. mask_unquoted_cat_heredoc_bodies() (defined above, mirroring
# guard-loom-workflow.sh mask_cat_heredoc_bodies()) closes exactly that shape:
# it masks an unquoted cat-heredoc ONLY when the capture is confined to a
# text-data flag value AND the body is proven free of `$(`/unescaped-backtick
# expansion, so a body that could actually execute something stays visible and
# still asks. Runs SECOND so the quoted-delimiter pass (and its interpreter
# carve-out) keeps full authority over the shapes it already handles.
if [[ "$COMMAND_ASK_SCAN" == *"<<"* ]]; then
    COMMAND_ASK_SCAN=$(printf '%s' "$COMMAND_ASK_SCAN" | awk "$_MASKHEREDOC_AWK"'
    { buf = buf (NR > 1 ? "\n" : "") $0 }
    END { printf "%s", mask_unquoted_cat_heredoc_bodies(mask_heredoc_bodies_selective(buf)) }')
fi

# COMMAND_CLOUD_ASK_SCAN (#6002): a SEPARATE, further-redacted copy branched
# off HERE -- right after heredoc-body masking, BEFORE the check-duplicate.sh/
# strip_literal_text passes just below -- used ONLY by CLOUD_ASK_PATTERNS
# further down. It is deliberately NOT fed back into COMMAND_ASK_SCAN itself,
# so SQL_DDL_PATTERN (and every other COMMAND_ASK_SCAN consumer) keeps seeing
# grep/rg/jq/for-loop text completely unredacted, exactly as before.
# mask_ask_positional_args() stays narrow for the same reason (see its header
# comment: grep/rg are deliberately excluded because COMMAND_ASK_SCAN also
# feeds SQL_DDL_PATTERN, which intentionally still scans a `grep '<pattern>'
# file` invocation's own quoted argument for a DDL phrase). CLOUD_ASK_PATTERNS
# is a DIFFERENT, narrower-purpose scan -- and it is also a TOGGLEABLE tier
# (guards.cloudCli), not the catastrophic tier's ungated denial floor -- so it
# is safe to give it its own, more-aggressively-masked copy without touching
# that SQL-DDL invariant: reuses the exact same
# mask_catastrophic_forloop_wordlist() / mask_catastrophic_positional_args()
# passes the catastrophic-tier COMMAND_NO_LITERAL_TEXT copy uses above, so a
# phrase like `aws s3 rb` that is merely quoted DATA in a for-loop word list
# or a jq filter script (chained, not fast-path-eligible) no longer
# false-asks on CLOUD_ASK_PATTERNS either, once the catastrophic scan has
# already stopped false-denying it.
#
# MUST branch off before strip_literal_text() runs below: that pass masks
# ANY quoted value following --search/--arg/etc, including a loop variable
# reference like `--search "$q"` (it has no notion of bash semantics, so it
# cannot tell "$q" apart from a real literal) -- masking that away first
# would erase the very `$q` text mask_catastrophic_forloop_wordlist()'s own
# safety check depends on seeing, causing it to fail closed for no reason.
COMMAND_CLOUD_ASK_SCAN="$COMMAND_ASK_SCAN"
if [[ "$COMMAND" == *"for "* && "$COMMAND" == *" in "* ]]; then
    COMMAND_CLOUD_ASK_SCAN=$(mask_catastrophic_forloop_wordlist "$COMMAND_CLOUD_ASK_SCAN")
fi
if [[ "$COMMAND" == *"grep"* || "$COMMAND" == *"rg "* || \
      "$COMMAND" == *"check-duplicate"* || "$COMMAND" == *"jq"* ]]; then
    COMMAND_CLOUD_ASK_SCAN=$(mask_catastrophic_positional_args "$COMMAND_CLOUD_ASK_SCAN")
fi
# #6269: same NAME='...'/NAME="..." dead-assignment masking as the
# catastrophic-tier COMMAND_NO_LITERAL_TEXT copy above, applied here so a
# CLOUD_ASK_PATTERNS phrase quoted the same way no longer false-asks either.
if [[ "$COMMAND" == *"='"* || "$COMMAND" == *'="'* ]]; then
    COMMAND_CLOUD_ASK_SCAN=$(mask_catastrophic_var_assignment "$COMMAND_CLOUD_ASK_SCAN")
fi

if [[ "$COMMAND_NO_COMMENT" == *"check-duplicate.sh"* ]]; then
    COMMAND_ASK_SCAN=$(mask_ask_positional_args "$COMMAND_ASK_SCAN")
    COMMAND_CLOUD_ASK_SCAN=$(mask_ask_positional_args "$COMMAND_CLOUD_ASK_SCAN")
fi
# #5797: "--arg" as a substring gate also covers "--argjson" (a superset
# spelling of "--arg"), so no separate "--argjson" check is needed here.
if [[ "$COMMAND_NO_COMMENT" == *"--body"* || "$COMMAND_NO_COMMENT" == *"--message"* || \
      "$COMMAND_NO_COMMENT" == *"--title"* || "$COMMAND_NO_COMMENT" == *"--notes"* || \
      "$COMMAND_NO_COMMENT" == *"--comment"* || "$COMMAND_NO_COMMENT" == *"-m"* || \
      "$COMMAND_NO_COMMENT" == *"--search"* || "$COMMAND_NO_COMMENT" == *"--arg"* ]]; then
    COMMAND_ASK_SCAN=$(strip_literal_text "$COMMAND_ASK_SCAN")
    COMMAND_CLOUD_ASK_SCAN=$(strip_literal_text "$COMMAND_CLOUD_ASK_SCAN")
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
# rm-scope SAME-COMMAND mktemp RESOLUTION (#6520)
#
# The `rm-scope-unresolved-var` deny (below) fails closed on ANY expandable
# `$` in an rm target, including the extremely common scratch-dir idiom
#   tmpdir=$(mktemp -d) && ... && rm -rf "$tmpdir"
# whose value is, in fact, provably rooted under /tmp (or $TMPDIR) — mktemp's
# own contract with no output-redirecting flags. This is a NEW resolution
# step, deliberately NOT built on resolve_var()/record_assign() (#4881,
# #6152): that pair only ever substitutes the LITERAL text following `=`
# (after quote-stripping), so a command-substitution RHS like `$(mktemp -d)`
# would still start with `$` and stay unresolved even if this path called it.
#
# rm_scope_mktemp_same_command_safe() returns success (0) ONLY when:
#   1. The rm target, after stripping at most one layer of surrounding quotes,
#      is a BARE `$NAME` or `${NAME}` reference — nothing else in the token
#      (a suffix like `$NAME/sub` is deliberately excluded; fail closed).
#   2. Scanning every ;/&/&&/||-separated segment of the SAME command text
#      for a `NAME=...` assignment whose entire segment is EXACTLY
#      `NAME=$(mktemp -d)` or `NAME=$(mktemp)` (optionally the same forms
#      double-quoted) — the plain, default-temp-root-rooted invocations only.
#      A custom template/prefix (`mktemp -d /other/dir/XXXXXX`,
#      `mktemp --tmpdir=/other/dir`, …) never matches this exact-string test,
#      so it is excluded from the fast path and falls through to today's
#      fail-closed deny (routine complexity: prefer excluding an unusual
#      mktemp shape over guessing its output root, #6520).
#   3. Exactly ONE assignment to NAME exists anywhere in the command, and it
#      is the safe form above. TWO OR MORE assignments to NAME — even a
#      second, differently-shaped one appearing AFTER the safe mktemp
#      assignment — poison the resolution and fail closed: the guard cannot
#      tell which assignment's value the shell will see at the `rm` word, so
#      ambiguity must never resolve to an allow.
#
# On success, the caller treats the target as a proven /tmp-or-$TMPDIR-rooted
# path and skips BOTH the unresolved-var deny AND the string-prefix scope
# check below it — the raw `$CWD/$target` concatenation used by that check
# still contains the literal, un-substituted `$NAME` text (this is a
# tokenizer, not a shell evaluator) and cannot be trusted for anything beyond
# the narrow proof made here.
#
# DECOY-HEREDOC BYPASS (#6549): the awk body below processes its `cmdtext`
# argument one PHYSICAL LINE at a time with no heredoc-body awareness of its
# own, so passing it the raw (heredoc-unmasked) command text let an attacker
# set the REAL value of `NAME` via a shape this scan's exact `NAME=` prefix
# match never sees (e.g. `export NAME=$(malicious_command)`), issue a live
# `rm -rf "$NAME"`, then plant an inert, never-executed decoy
# `NAME=$(mktemp -d)` line inside a heredoc body later in the same command
# purely to make `total == 1 && safe == 1` come out true. The caller
# (rm-scope SCOPE CHECK, below) now passes a heredoc-body-masked working
# copy — see its own `COMMAND_RM_MKTEMP_SCAN` comment for why it masks
# unconditionally (every heredoc shape, including an interpreter-fed one)
# rather than reusing `COMMAND_ASK_SCAN`'s narrower, interpreter-aware
# masking.
# =============================================================================
_rm_scope_bare_var_name() {
    local tok="$1" t c1 c2
    t="$tok"
    if [[ ${#t} -ge 2 ]]; then
        c1="${t:0:1}"
        c2="${t: -1}"
        if [[ ("$c1" == '"' && "$c2" == '"') || ("$c1" == "'" && "$c2" == "'") ]]; then
            t="${t:1:${#t}-2}"
        fi
    fi
    if [[ "$t" =~ ^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$ ]]; then
        printf '%s' "${BASH_REMATCH[1]}"
        return 0
    fi
    if [[ "$t" =~ ^\$([A-Za-z_][A-Za-z0-9_]*)$ ]]; then
        printf '%s' "${BASH_REMATCH[1]}"
        return 0
    fi
    return 1
}

rm_scope_mktemp_same_command_safe() {
    local target="$1" cmdtext="$2" varname verdict
    varname=$(_rm_scope_bare_var_name "$target") || return 1
    [[ -n "$varname" ]] || return 1
    verdict=$(printf '%s' "$cmdtext" | awk -v varname="$varname" "$_QSPLIT_AWK"'
    {
        $0 = qsplit($0)
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/[ \t]+$/, "", seg)
            prefix = varname "="
            plen = length(prefix)
            if (length(seg) > plen && substr(seg, 1, plen) == prefix) {
                rhs = substr(seg, plen + 1)
                total++
                if (rhs == "$(mktemp -d)" || rhs == "$(mktemp)" || \
                    rhs == "\"$(mktemp -d)\"" || rhs == "\"$(mktemp)\"") {
                    safe++
                }
            }
        }
    }
    END {
        if (total == 1 && safe == 1) print "SAFE"
        else print "UNSAFE"
    }')
    [[ "$verdict" == "SAFE" ]]
}

# =============================================================================
# write-confinement SAME-COMMAND mktemp RESOLUTION (#6949)
#
# Sibling of rm_scope_mktemp_same_command_safe() (above) for the
# `worktree-write-confinement-unresolved-var` deny (below, in the Bash-tool
# write-confinement block): the same scratch-dir idiom that fixed #6520 for
# `rm` targets —
#   tmp=$(mktemp -d) && mkdir -p "$tmp/sub" && cat > "$tmp/sub/out.txt"
#   TMPGUARD=$(mktemp) && ... > "$TMPGUARD"
# — was equally unresolvable here: resolve_var()/resolve_var_q() only ever
# substitute the LITERAL text following `=`, so a command-substitution RHS
# like `$(mktemp -d)` still starts with `$` and stays unresolved (same root
# cause documented on rm_scope_mktemp_same_command_safe() above). Because a
# write target routinely carries a SUFFIX after the captured scratch dir
# (`"$tmp/sub/out.txt"`, not just a bare `"$tmp"`), this sibling — unlike the
# rm-scope original, which only ever proves a BARE `$NAME` reference safe —
# additionally accepts a `/`-prefixed suffix after the variable reference.
#
# _wt_write_mktemp_leading_var() returns success (0), with the variable name
# in `_WT_WRITE_VARNAME` and the (possibly empty) `/`-prefixed suffix in
# `_WT_WRITE_SUFFIX`, ONLY when the write target, after stripping at most one
# layer of surrounding quotes, is a bare `$NAME`/`${NAME}` reference OR that
# same reference immediately followed by `/<rest>` — nothing else in the
# token. wt_write_mktemp_same_command_safe() then applies:
#
#   1. The SAME exact-string same-command mktemp scan rm-scope uses (only a
#      SAME command NAME=$(mktemp -d) / NAME=$(mktemp) — optionally
#      double-quoted — assignment counts; a custom template/prefix
#      (`mktemp -d /other/dir/XXXXXX`, `mktemp --tmpdir=/other/dir`, …) never
#      matches this exact-string test and falls through to today's
#      fail-closed deny) and the SAME ambiguity rule (two or more assignments
#      to NAME anywhere in the command poison the resolution and fail closed).
#   2. A NEW safety condition the rm-scope original does not need, precisely
#      because it never allows a suffix at all: any `..` path-traversal
#      component in the suffix (`/../`, or a suffix that IS or ends in `/..`)
#      fails closed. mktemp's own OUTPUT PATH is never known to this static
#      scanner — only that it is freshly created and /tmp-or-$TMPDIR-rooted —
#      so a `..` in the suffix could walk back out of that unknown directory
#      to an unknown number of levels, potentially back into a protected
#      worktree/checkout. Excluding any `..` component keeps the same
#      "provably safe or fail closed" guarantee the exact-string mktemp match
#      itself relies on.
#
# On success the caller treats the write as landing inside a freshly created
# scratch directory outside every worktree and skips the
# `worktree-write-confinement-unresolved-var` deny for this target — mirrors
# the rm-scope caller's own "no need to re-run any further scope check"
# treatment, since a fresh mktemp path can never coincide with the main
# checkout or any managed worktree.
# =============================================================================
_WT_WRITE_VARNAME=""
_WT_WRITE_SUFFIX=""
_wt_write_mktemp_leading_var() {
    local tok="$1" t c1 c2
    t="$tok"
    if [[ ${#t} -ge 2 ]]; then
        c1="${t:0:1}"
        c2="${t: -1}"
        if [[ ("$c1" == '"' && "$c2" == '"') || ("$c1" == "'" && "$c2" == "'") ]]; then
            t="${t:1:${#t}-2}"
        fi
    fi
    if [[ "$t" =~ ^\$\{([A-Za-z_][A-Za-z0-9_]*)\}(/.*)?$ ]]; then
        _WT_WRITE_VARNAME="${BASH_REMATCH[1]}"
        _WT_WRITE_SUFFIX="${BASH_REMATCH[2]}"
        return 0
    fi
    if [[ "$t" =~ ^\$([A-Za-z_][A-Za-z0-9_]*)(/.*)?$ ]]; then
        _WT_WRITE_VARNAME="${BASH_REMATCH[1]}"
        _WT_WRITE_SUFFIX="${BASH_REMATCH[2]}"
        return 0
    fi
    return 1
}

wt_write_mktemp_same_command_safe() {
    local target="$1" cmdtext="$2" varname suffix verdict
    _wt_write_mktemp_leading_var "$target" || return 1
    varname="$_WT_WRITE_VARNAME"
    suffix="$_WT_WRITE_SUFFIX"
    [[ -n "$varname" ]] || return 1
    if [[ -n "$suffix" ]]; then
        case "$suffix" in
            */../*|*/..) return 1 ;;
        esac
    fi
    verdict=$(printf '%s' "$cmdtext" | awk -v varname="$varname" "$_QSPLIT_AWK"'
    {
        $0 = qsplit($0)
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/[ \t]+$/, "", seg)
            prefix = varname "="
            plen = length(prefix)
            if (length(seg) > plen && substr(seg, 1, plen) == prefix) {
                rhs = substr(seg, plen + 1)
                total++
                if (rhs == "$(mktemp -d)" || rhs == "$(mktemp)" || \
                    rhs == "\"$(mktemp -d)\"" || rhs == "\"$(mktemp)\"") {
                    safe++
                }
            }
        }
    }
    END {
        if (total == 1 && safe == 1) print "SAFE"
        else print "UNSAFE"
    }')
    [[ "$verdict" == "SAFE" ]]
}

# =============================================================================
# rm-scope SAME-COMMAND LITERAL-PATH RESOLUTION (#6676)
#
# rm_scope_mktemp_same_command_safe() (above) proves ONLY the exact-string
# `$(mktemp -d)`/`$(mktemp)` same-command RHS shape safe — a same-command
# assignment of a LITERAL absolute path (`FARM=/tmp/nofile-bin-6662; rm -rf
# "$FARM"`) never matches that exact-string test and previously fell straight
# through to the fail-closed `rm-scope-unresolved-var` deny even though the
# value is a provably static, non-command-substitution literal.
#
# rm_scope_literal_same_command_resolve() is a SIBLING fast path, not a
# replacement for the mktemp one: it reuses record_assign()'s DQ/SQ-aware
# quote-stripping logic (one layer of surrounding quotes stripped, #4881/
# #6152) to resolve a same-command `NAME=<value>` assignment, then judges the
# result:
#
#   1. Exactly ONE assignment to NAME exists anywhere in the SAME command
#      text — mirrors the mktemp fast path's own ambiguity rule (its own doc
#      comment, point 3, above): a second assignment to the same name — even
#      an identical or otherwise-safe-looking one — poisons the resolution
#      and fails closed, since the guard cannot tell which assignment's value
#      the shell sees at the `rm` word.
#   2. The (quote-stripped) RHS is a PURE LITERAL absolute path: starts with
#      `/`, and contains neither `$` nor a backtick anywhere — so a RHS that
#      itself still carries an unresolved expansion (`NAME="$OTHER/sub"`,
#      `NAME=$(cmd)`, `` NAME=`cmd` ``) is rejected rather than trusted as a
#      literal (fail closed).
#
# On success this prints the resolved literal absolute path on stdout and
# returns 0. Unlike the mktemp fast path (which is unconditionally
# /tmp-or-$TMPDIR-rooted and lets the CALLER skip the scope check entirely),
# a literal path can resolve anywhere — the CALLER is responsible for
# re-running the normal rm-scope checks (the top-level catastrophic-path deny
# AND the repo/worktree/tmp IN_SCOPE check) against the resolved path, so it
# is judged exactly like an equivalent literal `rm -rf /that/path` would be.
# =============================================================================
rm_scope_literal_same_command_resolve() {
    local target="$1" cmdtext="$2" varname resolved
    varname=$(_rm_scope_bare_var_name "$target") || return 1
    [[ -n "$varname" ]] || return 1
    resolved=$(printf '%s' "$cmdtext" | awk -v varname="$varname" "$_QSPLIT_AWK"'
    BEGIN {
        DQ = sprintf("%c", 34)
        SQ = sprintf("%c", 39)
    }
    {
        $0 = qsplit($0)
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/[ \t]+$/, "", seg)
            prefix = varname "="
            plen = length(prefix)
            if (length(seg) > plen && substr(seg, 1, plen) == prefix) {
                total++
                val = substr(seg, plen + 1)
            }
        }
    }
    END {
        if (total == 1) {
            vlen = length(val)
            if (vlen >= 2) {
                c1 = substr(val, 1, 1)
                c2 = substr(val, vlen, 1)
                if ((c1 == DQ && c2 == DQ) || (c1 == SQ && c2 == SQ)) {
                    val = substr(val, 2, vlen - 2)
                }
            }
            if (val ~ /^\// && val !~ /[$`]/) print val
        }
    }')
    [[ -n "$resolved" ]] || return 1
    printf '%s' "$resolved"
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
# In the three idiom scans above (NOT the `>`/`>>` scan, which has its own
# operator detection), a `<` stdin redirection is recognized and EXCLUDED
# (#5369): neither the operator token (`<`, `0<`, `</path`) nor the file a
# bare `<` reads FROM is a write target. Skipping it fixes both a false DENY
# (phantom `<repo>/<` targets on `tee f < in`) and, for `cp`/`mv` — whose
# destination is the LAST non-flag token — a false ALLOW where a trailing
# `< in` displaced the real destination. See the inline comment at the scan.
#
# Likewise, a same-line NUMBERED-FD output redirect (`2>/dev/null`, `2>&1`,
# `1>/tmp/x`, ...) is recognized and EXCLUDED from those same three idiom
# scans (#6326): neither the operator token nor (for the bare/spaced form)
# the file it writes TO is treated as an extra tee/sed-i/cp/mv file operand.
# Without this, a trailing `2>/dev/null` on an otherwise-harmless
# `cp src /tmp/dst 2>/dev/null` was misread as the cp/mv destination itself
# (the LAST non-flag token), producing a bogus relative "target" that joined
# against cwd and false-denied as a worktree-confinement bypass even though
# the command never wrote inside the main checkout. A bare `>`/`>>` with NO
# leading digit is deliberately left OUTSIDE this exclusion (unchanged
# behavior) — see the inline comment at the scan.
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
    printf '%s' "$1" | awk -v startcwd="$2" -v home="$HOME" "$_QSPLIT_AWK""$_CDEXPAND_AWK""$_CDQUOTE_AWK""$_VARRESOLVE_AWK""$_MASKGT_AWK""$_MASKWS_AWK""$_MASKHEREDOC_AWK"'
    # resolve_var()/record_assign() (same-command $VAR resolution, #4881) and
    # the DQ/SQ/AMBIG constants they use now come from the shared
    # _VARRESOLVE_AWK snippet above (#6152) — see its header comment for the
    # full contract. Unresolvable cases all return tok UNCHANGED, which is
    # exactly the pre-#4881 treatment (literal, cwd-prefixed => still denied
    # when it lands in the main checkout). Fail-closed by construction: this
    # function can only ever REPLACE a token with a value it actually proved,
    # never make one disappear.
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
        #
        # SHELL-ONLY NARROWING (#6353): pass shell_only=1 here (and ONLY
        # here -- the gh-api-rawfield check and the general COMMAND_ASK_SCAN
        # heredoc pass elsewhere keep the default, unnarrowed behavior). A
        # `>`/`>=`/`<`/`<=` is a live write/read redirect ONLY in real shell
        # syntax itself; in Python/Perl/Ruby/JS source the same bytes are
        # ordinary comparison operators, so leaving heredoc bodies fed to
        # those languages visible to this write-idiom scan bought no real
        # protection (their actual writes go through language-level APIs
        # this command-word scanner never parses anyway) while
        # manufacturing a false worktree-write-confinement DENY on ordinary
        # code like `if len(affected) > 20:` inside a `python - <<'EOF'`
        # heredoc. A genuine `bash <<'EOF' ... > file ... EOF`-style body
        # still scans and still denies -- shell_only=1 only removes
        # python[0-9.]*/perl/ruby/node/nodejs from the "leave visible"
        # bucket, per the header comment on is_interpreter_opener().
        buf = mask_heredoc_bodies_selective(buf, 1)
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
            # QUOTE AWARENESS COMES FREE for a quoted/escaped literal
            # filename that merely BEGINS with `<`: qsplit() preserves quote
            # characters VERBATIM in toks[] and mask_ws() guarantees a quoted
            # span never spans two tokens, so such a token starts with the
            # quote/backslash byte and can never match the anchored patterns
            # here -- it stays a scanned write target, opening no new escape
            # vector, which is the fail-closed direction this file requires.
            #
            # ARITHMETIC/TEST-CONTEXT AWARENESS (#5515) is why this now reads
            # mtoks[] (mask_gt() output) rather than the raw toks[]: a bare
            # `<`/`<=` used as a comparison inside `(( ... ))`/`[[ ... ]]`
            # (e.g. `(( x <= y ))`) is not a redirection operator either, and
            # mask_gt() (see its own header above) now masks such a byte the
            # same way it masks one found inside quotes. Without this, the
            # comparisons own `<` token wrongly marked itself (and the
            # following token) as "read from stdin", which cannot itself
            # manufacture a phantom write target here but could silently
            # suppress a real one later in the SAME segment. mtoks[] is
            # byte-for-byte token-aligned with toks[] (mask_gt()/mask_ws()
            # only ever substitute one byte for one byte), so switching the
            # PATTERN test from toks[] to mtoks[] changes nothing about which
            # token INDEX gets marked -- only whether a masked (quoted or
            # arith/test-context) `<` can still match.
            # (mask_gt() exists because a `>`/`<` can appear MID-token inside
            # a quoted or arith/test-context span; these patterns only ever
            # look at the first bytes of a token, so that case cannot arise
            # here either way.)
            #
            # Deliberately NOT matched: `<<`, `<<-`, `<<<`. Those are heredoc
            # /herestring operators handled separately by the pre-tokenization
            # heredoc machinery above (mask_heredoc_bodies_selective) and by
            # #5232/#5233; the `[^<]` guard below keeps this fix strictly
            # disjoint from that one.
            delete stdin_redir
            for (j = 1; j <= m; j++) {
                if (toks[j] == "") continue
                if (mtoks[j] ~ /^[0-9]*<$/) {
                    stdin_redir[j] = 1
                    for (k = j + 1; k <= m; k++) {
                        if (toks[k] == "") continue
                        stdin_redir[k] = 1
                        break
                    }
                } else if (mtoks[j] ~ /^[0-9]*<[^<]/) {
                    stdin_redir[j] = 1
                }
            }

            # NUMBERED-FD OUTPUT-REDIRECT EXCLUSION (#6326) -- a same-line
            # numbered file-descriptor redirect (`2>/dev/null`, `2>&1`,
            # `1>/tmp/x`, ...) is a REDIRECTION OPERATOR (plus, for the
            # attached fd-to-file form, its own operand), never an extra
            # tee/sed-i/cp/mv file argument. Mirrors the stdin_redir exclusion
            # immediately above, but for `[0-9]+>`/`[0-9]+>>` rather than
            # `[0-9]*<`.
            #
            # Deliberately requires AT LEAST ONE leading digit (`[0-9]+`, not
            # `[0-9]*`): a bare `>`/`>>` with NO leading digit is intentionally
            # left OUTSIDE this exclusion and keeps flowing into the tee/sed/
            # cp-mv loops exactly as before this fix -- narrowing an
            # over-broad match must never also widen an unrelated one.
            #
            # The genuine write-target text such a token carries is still
            # captured separately by the dedicated `>`/`>>` scan below (which
            # already supports an optional leading digit, `[0-9]*>>?`) --
            # excluding the token HERE only stops it from being
            # misappropriated as a tee/sed-i/cp/mv FILE OPERAND; it is not
            # dropped from write-target scanning altogether.
            #
            # Bare-operator form (`2>` followed by a separate token, e.g.
            # `sed -i s/a/b/ file 2> /tmp/err`): the operator token AND the
            # single token it consumes as its target are both excluded,
            # UNLESS that next token starts with `&` (a spaced dup-to-fd form,
            # `2> &1` -- which duplicates a file descriptor, not a file, so
            # nothing after it is a real operand to exclude).
            #
            # Attached form (`2>/dev/null`, `2>>/tmp/log`): the single token
            # already carries both the operator and its target, so only that
            # one token needs excluding.
            delete numfd_redir
            for (j = 1; j <= m; j++) {
                if (toks[j] == "") continue
                if (mtoks[j] ~ /^[0-9]+>>?$/) {
                    numfd_redir[j] = 1
                    if (j + 1 <= m && toks[j+1] != "" && mtoks[j+1] !~ /^&/) {
                        numfd_redir[j+1] = 1
                    }
                } else if (mtoks[j] ~ /^[0-9]+>>?[^ \t&]/) {
                    numfd_redir[j] = 1
                }
            }

            if (toks[1] == "tee") {
                for (j = 2; j <= m; j++) {
                    if (j in stdin_redir) continue
                    if (j in numfd_redir) continue
                    if (toks[j] == "" || toks[j] ~ /^-/) continue
                    # Heredoc/herestring redirection (attached or quoted
                    # delimiter, or the bare double-angle-bracket / dashed
                    # form, plus the triple-angle-bracket herestring) is a
                    # REDIRECTION OPERATOR feeding the tee commands stdin,
                    # never a write-target argument -- but it is still just
                    # another whitespace-bounded non-flag token to this
                    # scanner, so without this exclusion the delimiter (or the
                    # operator itself, in the space-separated bare spelling)
                    # was misread as an extra file target and resolved against
                    # curcwd, producing a bogus "<repo>/<<EOF" write that the
                    # worktree-isolation check below then falsely denied
                    # (#5232). A BARE operator token (`<<`, `<<-`, `<<<` with
                    # no attached delimiter/content) also consumes the ONE
                    # following word -- the heredoc delimiter, or the
                    # herestring content. Consuming exactly one word is
                    # shell-accurate for both: a herestring takes a single
                    # word, so in `tee f <<< a b` the `b` really IS a tee
                    # operand and must still be scanned.
                    if (toks[j] ~ /^<<-?/) {
                        if (toks[j] == "<<" || toks[j] == "<<-" || toks[j] == "<<<") j++
                        continue
                    }
                    print curcwd SEP resolve_var_q(toks[j])
                }
            } else if (toks[1] == "sed") {
                has_i = 0
                # BSD `-i` SEPARATE-ARGUMENT FORM (#5674): unlike GNU sed
                # (where the -i option optional backup suffix is always
                # ATTACHED to the same token -- bare `-i` or `-i.bak` -- so
                # the very next
                # non-flag token is always the mandatory SCRIPT argument, not
                # a file), BSD/macOS sed requires `-i` to take its backup
                # suffix as a SEPARATE following token, almost always the
                # empty string (`sed -i` followed by an empty-quote argument
                # then the script, e.g. `sed -i EMPTYQUOTES s/a/b/ file` --
                # the idiom every reported false positive used). That
                # inserts ONE EXTRA
                # non-file token (the suffix) before the script, so the
                # "skip exactly nfargs[1]" logic below -- correct for GNU,
                # where nfargs[1] IS the script -- instead skips the suffix
                # and lets the SCRIPT (nfargs[2], e.g. `s/a/b/`) fall through
                # as a phantom file target, resolved against curcwd and
                # denied as a worktree-confinement bypass for a file that was
                # never actually written.
                #
                # Detected narrowly and safely: only a BARE `-i` token (not
                # `-i.bak`, which is unambiguous GNU-attached-form and
                # already handled) immediately followed by a token that,
                # quote-stripped, is the EMPTY STRING -- never a plausible
                # relative path and never a meaningful backup suffix on its
                # own, so treating it purely as "the BSD marker" cannot hide
                # a real write target. sed_skip then covers both the suffix
                # AND the script (2 tokens) instead of just the script (1);
                # every FILE argument after that is still fully scanned, so a
                # genuine main-checkout target among them still denies.
                bare_i_pending = 0
                sed_skip = 1
                nf = 0
                delete nfargs
                for (j = 2; j <= m; j++) {
                    if (j in stdin_redir) continue
                    if (j in numfd_redir) continue
                    if (toks[j] == "-i") { has_i = 1; bare_i_pending = 1; continue }
                    if (toks[j] ~ /^-i/) has_i = 1
                    if (toks[j] ~ /^-/) continue
                    if (toks[j] == "") continue
                    # Same heredoc/herestring exclusion as the `tee` branch
                    # above (#5232) -- a trailing `sed -i ... file <<EOF` (or
                    # `... <<< word`) must not misread the redirection
                    # operator, its delimiter, or the herestring content as an
                    # extra file operand.
                    if (toks[j] ~ /^<<-?/) {
                        if (toks[j] == "<<" || toks[j] == "<<-" || toks[j] == "<<<") j++
                        continue
                    }
                    nf++
                    nfargs[nf] = toks[j]
                    if (bare_i_pending && nf == 1 && strip_cd_quoting(toks[j]) == "") {
                        sed_skip = 2
                    }
                    bare_i_pending = 0
                }
                if (has_i && nf > sed_skip) {
                    for (j = sed_skip + 1; j <= nf; j++) print curcwd SEP resolve_var_q(nfargs[j])
                }
            } else if (toks[1] == "cp" || toks[1] == "mv") {
                nf = 0
                delete nfargs
                for (j = 2; j <= m; j++) {
                    if (j in stdin_redir) continue
                    if (j in numfd_redir) continue
                    if (toks[j] ~ /^-/) continue
                    if (toks[j] == "") continue
                    # Same heredoc/herestring exclusion as the `tee` branch
                    # above (#5232) -- without it a trailing `<<EOF` (or
                    # `<<< word`) after the real cp/mv operands was misread as
                    # the LAST non-flag token (the field this branch treats as
                    # the destination), so it would win over the real
                    # destination entirely.
                    if (toks[j] ~ /^<<-?/) {
                        if (toks[j] == "<<" || toks[j] == "<<-" || toks[j] == "<<<") j++
                        continue
                    }
                    nf++
                    nfargs[nf] = toks[j]
                }
                if (nf >= 2) print curcwd SEP resolve_var_q(nfargs[nf])
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
                        print curcwd SEP resolve_var_q(toks[j+1])
                    }
                    continue
                }
                if (mt ~ /^[0-9]*>>?[^ \t&]/) {
                    # Attached form (`>file`, `2>file`, `>>file`).
                    op = toks[j]
                    sub(/^[0-9]*>>?/, "", op)
                    if (op != "") print curcwd SEP resolve_var_q(op)
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

physical_abs_path() {
    # Resolve an ABSOLUTE path's symlinks as far as the filesystem allows,
    # keeping any not-yet-existing trailing segments lexically appended.
    #
    # This is the symlink-resolving counterpart to normalize_abs_path(), which
    # is deliberately pure-lexical (see its header) and therefore leaves a
    # symlinked ancestor intact. Whenever a lexically-built path is compared
    # against a path that came from git (`rev-parse --show-toplevel` /
    # `--git-common-dir`, both of which return the symlink-RESOLVED spelling),
    # the two can describe the same directory in different words and never
    # string-match. On macOS this is the default state of any $TMPDIR path:
    # /var is a symlink to /private/var, so `mktemp -d` yields
    # /var/folders/... while git reports /private/var/folders/... (#6684; the
    # same divergence the /tmp -> /private/tmp `pwd -P` note at the worktree
    # containment block below already handles for its own comparisons).
    #
    # Walking up to the deepest EXISTING ancestor before `cd`-ing matters:
    # the paths compared here (e.g. a target-dir cargo has not created yet)
    # routinely do not exist on disk, and `realpath -m`, which would handle
    # that, is GNU-only and silently no-ops on macOS.
    local path="$1" tail="" phys
    [[ "$path" == /* ]] || { printf '%s' "$path"; return 0; }
    path=$(normalize_abs_path "$path")
    while [[ ! -d "$path" && "$path" != "/" ]]; do
        tail="${path##*/}${tail:+/}$tail"
        path="${path%/*}"
        [[ -n "$path" ]] || path="/"
    done
    phys=$(cd "$path" 2>/dev/null && pwd -P) || phys=""
    [[ -n "$phys" ]] || phys="$path"
    if [[ -n "$tail" ]]; then
        if [[ "$phys" == "/" ]]; then
            printf '/%s' "$tail"
        else
            printf '%s/%s' "$phys" "$tail"
        fi
    else
        printf '%s' "$phys"
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

# SCANS COMMAND_ASK_SCAN, NOT COMMAND_NO_LITERAL_TEXT (#5216, widened #6519).
# extract_rm_targets() segments with qsplit(), which — like every
# quote-tracking scan in this file — is driven one PHYSICAL LINE at a time and
# has no memory of a `"` opened on an earlier line. So a heredoc BODY line
# inside `--body "$(cat <<'EOF' … EOF)"` was segmented as if it were live
# shell: the prose `Example payload: \`owner/name; rm -rf /\`` split on its
# `;` into a segment whose command word is `rm`, manufacturing the target
# ``/` `` and hard-denying a Judge comment that deletes nothing (observed on
# PR #4357). Same failure family as #5000's phantom write targets.
#
# #5216 closed that ONE shape (a `<flag> "$(cat <<'EOF' … EOF)"` value
# directly following a text-carrying flag) by scanning COMMAND_NO_LITERAL_TEXT
# — narrow on purpose at the time, mirroring the catastrophic
# ALWAYS_BLOCK_PATTERNS scan's own copy. But it left a SIBLING shape open
# (#6519): `cat <<'EOF' > /tmp/x.md … EOF` writing an inert example to a file,
# referenced LATER via `--body-file /tmp/x.md` (or any other non-substitution
# consumer) — no flag ever sits directly before the heredoc opener, so neither
# strip_literal_text() nor its mask_flag_cat_heredocs() helper ever sees it,
# and a heredoc body line whose own first word happens to be `rm` (e.g. a
# standalone "rm -rf /opt/vendor/important" example line in acceptance-
# criteria prose) still manufactured a phantom local `rm` segment and
# hard-denied a write that deletes nothing (reproduced against rjwalters/
# anvil#1073's shape). This is the exact same failure family
# parse_force_ops()/lifecycle_or_cloud_reason() already fixed by reading
# COMMAND_ASK_SCAN (comment-stripped AND heredoc-body-masked via
# mask_heredoc_bodies_selective()/mask_unquoted_cat_heredoc_bodies(), gated
# only on heredoc/flag PRESENCE, not on a specific flag+substitution shape) —
# extract_rm_targets() now matches that established pattern instead of
# growing its own narrower one. mask_heredoc_bodies_selective() masks ONLY a
# CLOSED, quoted-delimiter heredoc BODY and deliberately leaves an
# INTERPRETER-fed heredoc (`bash <<EOF`, `sh -s <<EOF`, `cat <<EOF | sh`, …)
# and everything OUTSIDE the heredoc untouched, so a REAL `rm -rf /` — bare,
# sudo-prefixed, after a `&&`, chained after a heredoc closes, inside an
# interpreter-fed heredoc, or smuggled through `bash -c '…'` / `-m "$(rm -rf
# /)"` — still reaches this check unchanged; only inert, provably-non-executing
# heredoc-body prose is newly excluded. Both `rm-protected-path` (unconditional)
# and `rm-scope-outside-repo` (guards.rmScope-gated) consume the same
# RM_TARGETS list built below, so this widening applies to both — matching
# lifecycle_or_cloud_reason()'s precedent of an unconditional deny already
# reading COMMAND_ASK_SCAN for the identical reason.
#
# Cheap pre-check keeps awk off the hot path for the ~99% of commands that have
# no recursive/force rm at all.
if echo "$COMMAND_ASK_SCAN" | grep -qE 'rm[[:space:]]+-[a-zA-Z]*[rf]'; then
    RM_TARGETS=$(extract_rm_targets "$COMMAND_ASK_SCAN" | head -20)

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

        # Shell-accurate quote removal, for the classification only (#4926,
        # mirrored here for the rm-scope path by #6814): extract_rm_targets()
        # emits tokens with their quote characters preserved verbatim
        # (qsplit's contract), so a quoted absolute target (`'/opt/evil'`,
        # `"/opt/evil"`) reaches here starting with a quote character rather
        # than `/` — the `= /*` test below would then misclassify it as
        # RELATIVE and cwd-prefix it into `<repo-root>/'/opt/evil'`, which
        # lexically starts with $REPO_ROOT and so passes the IN_SCOPE
        # prefix check further down, silently ADMITTING an out-of-repo
        # target by simply quoting it. Unquote a COPY for the classification
        # only: the allowlist `case "$target"` above and the deny messages
        # below still use the raw, quote-preserved `$target`. An unterminated
        # quote falls back to the raw token (today's verdict), never
        # widening a deny into an allow.
        _rmclassify="$target"
        strip_target_quoting "$target" && _rmclassify="$_UNQUOTED_TARGET"

        # Resolve path to absolute (raw — normalization happens next).
        ABS_PATH=""
        if [[ "$_rmclassify" = /* ]]; then
            ABS_PATH="$_rmclassify"
        elif [[ -n "$CWD" ]]; then
            ABS_PATH="$CWD/$_rmclassify"
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
                # Unresolved-variable fail-closed check (rjwalters/repo#244,
                # fixing rjwalters/repo#239). extract_rm_targets() is a
                # TOKENIZER, not a shell evaluator: a target like `"$p"` or
                # `$TMP` reaches this loop completely unexpanded. The
                # `$CWD/$target` concatenation above builds the literal
                # string `<repo-root>/$p` — which lexically starts with
                # $REPO_ROOT — so without this check the string-prefix scope
                # test below would treat it as IN_SCOPE and silently ALLOW
                # it, no matter what `$p` actually expands to at runtime
                # (the #239 regression: a same-named or inherited variable
                # can point anywhere, including outside the repo). Reuses
                # mark_expandable_dollars() — the same shape classifier the
                # Bash-tool write-confinement check above uses (#4921) — so
                # an EXPANDABLE `$` (bare or double-quoted) is distinguished
                # from a LITERAL one (single-quoted or backslash-escaped, a
                # file genuinely named `$p`); both quote styles normalize to
                # the same shape first. Deliberately checked BEFORE the
                # $CWD/$target concatenation is trusted for anything else —
                # unresolvable targets must fail closed, not fall through to
                # the prefix check.
                mark_expandable_dollars "$target"
                _rm_marked="$_MARKED_TOKEN"
                if [[ "$_rm_marked" == $'\001'* || "$_rm_marked" == /$'\001'* ]]; then
                    # Narrow escape hatch (#6520): a same-command
                    # `NAME=$(mktemp -d)`/`NAME=$(mktemp)` assignment proves
                    # this variable is /tmp-or-$TMPDIR-rooted, even though the
                    # raw token is unexpanded. See
                    # rm_scope_mktemp_same_command_safe()'s own doc comment
                    # (above extract_rm_targets()) for the exact, deliberately
                    # narrow conditions. On success this target is fully
                    # vetted — skip the string-prefix scope check below too,
                    # since $ABS_PATH still holds the un-substituted literal
                    # `$NAME` text and cannot be trusted for anything else.
                    #
                    # HEREDOC-BODY-MASKED SCAN (#6549): scans
                    # $COMMAND_RM_MKTEMP_SCAN (lazily built just below, cached
                    # across loop iterations), NOT the raw $COMMAND_NO_LITERAL_TEXT
                    # -- see that variable's own definition for why. Unlike this
                    # decision's earlier precedent (#6519's extract_rm_targets()
                    # widening, and COMMAND_ASK_SCAN generally), this dedicated
                    # copy masks EVERY heredoc body unconditionally, including an
                    # interpreter-fed one (`bash <<EOF`) and an unquoted-delimiter
                    # one whose `$(...)` the outer shell does expand while
                    # building the body: a heredoc body is never itself a
                    # top-level statement in the CURRENT shell -- it is either
                    # data handed to the consuming command's stdin, or (for an
                    # interpreter-fed opener) a script handed to a CHILD process
                    # -- so no heredoc body line, of any shape, can ever be the
                    # live assignment this function is trying to prove exists.
                    # Masking it here can therefore only narrow (turn a
                    # decoy-inflated ambiguous/false SAFE into the correct
                    # fail-closed UNSAFE), never widen: a real, live top-level
                    # `NAME=$(mktemp -d)` sitting outside every heredoc in the
                    # same command is completely unaffected.
                    if [[ -z "${COMMAND_RM_MKTEMP_SCAN+x}" ]]; then
                        COMMAND_RM_MKTEMP_SCAN="$COMMAND_NO_LITERAL_TEXT"
                        if [[ "$COMMAND_RM_MKTEMP_SCAN" == *"<<"* ]]; then
                            COMMAND_RM_MKTEMP_SCAN=$(printf '%s' "$COMMAND_RM_MKTEMP_SCAN" | awk "$_MASKHEREDOC_AWK"'
                            { buf = buf (NR > 1 ? "\n" : "") $0 }
                            END { printf "%s", mask_heredoc_bodies(buf) }')
                        fi
                    fi
                    if rm_scope_mktemp_same_command_safe "$target" "$COMMAND_RM_MKTEMP_SCAN"; then
                        continue
                    fi

                    # Same-command LITERAL-path fast path (#6676): unlike the
                    # mktemp form above, a resolved literal is NOT skipped
                    # past the scope check — ABS_PATH is replaced with the
                    # proven literal and falls through to the same
                    # catastrophic-path deny and repo/worktree/tmp IN_SCOPE
                    # check just below, exactly as an equivalent literal
                    # `rm -rf /that/path` would be judged. See
                    # rm_scope_literal_same_command_resolve()'s own doc
                    # comment (above rm_scope_mktemp_same_command_safe()) for
                    # the exact resolution conditions.
                    _rm_literal_resolved=$(rm_scope_literal_same_command_resolve "$target" "$COMMAND_RM_MKTEMP_SCAN") || true
                    if [[ -n "$_rm_literal_resolved" ]]; then
                        ABS_PATH="$_rm_literal_resolved"
                        if [[ "$ABS_PATH" = /* ]]; then
                            ABS_PATH=$(normalize_abs_path "$ABS_PATH")
                        fi
                        if [[ "$ABS_PATH" == "/" ]] || \
                           [[ -n "$HOME" && "$ABS_PATH" == "$HOME" ]] || \
                           [[ "$ABS_PATH" =~ ^/[^/]+$ ]]; then
                            deny "BLOCKED: rm on protected system path: $ABS_PATH" "rm-protected-path"
                        fi
                    else
                        deny "BLOCKED: rm target '${target}' is an unexpanded shell variable from the path root down, so this guard cannot tell where it resolves at runtime (guards.rmScope=repo). Unresolvable rm targets fail closed (mirrors rjwalters/repo#244, fixing #239). Use an explicit literal path." "rm-scope-unresolved-var"
                    fi
                fi

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
#
# CARVE-OUT: read-only-by-role scratch staging in `dist/` (#6021). This
# block's threat model (above) is a session that HAS Write/Edit — a
# Builder/Doctor denied on the Edit/Write tool falling back to a Bash write
# to land the same edit in the main checkout. A role with NO Write/Edit tool
# at all was never the threat this guard defends against, and such a role
# also has no issue worktree to redirect to — the deny's own remediation
# ("cd into your issue worktree") is not actionable for it. Concretely: the
# Auditor validating the `worker-image-smoke` CI leg locally needs to stage
# the release binary at `dist/loom-daemon-<target>` (the Docker build
# context `docker/worker/Dockerfile`'s `LOOM_DAEMON_BIN` ARG documents, the
# same convention `.github/workflows/release.yml` uses for release assets)
# before running `docker build`, and had no way to do that without tripping
# this guard.
#
# Scoped narrowly on BOTH axes so this cannot widen into a general opt-out:
#   1. Role: LOOM_ROLE (set by role_runner/daemon dispatch, #4768) must
#      match _WT_READONLY_ROLES below — the allowlist of roles whose
#      `tools:` frontmatter in defaults/.claude/agents/loom-<role>.md grants
#      no Write/Edit tool (verified against that frontmatter at the time of
#      #6021: architect, auditor, champion, curator, guide, hermit, judge).
#      Builder and Doctor — the only two roles WITH Write/Edit — are
#      deliberately never in this list, and an unset/unrecognized LOOM_ROLE
#      (every interactive Builder/Doctor session, and any automation that
#      does not explicitly identify itself) fails CLOSED to the pre-existing
#      deny below. If a future role gains Write/Edit, its name must be
#      removed from this list.
#   2. Path: the write target must resolve inside `<main-checkout>/dist/`
#      specifically — a small, already-`.gitignore`d, well-known scratch
#      directory this repo's own release pipeline already treats as a
#      build-artifact staging area, NOT "anywhere outside the worktree."
# =============================================================================
_WT_READONLY_ROLES=" architect auditor champion curator guide hermit judge "

# True if the CURRENT LOOM_ROLE identifies a role with no Write/Edit tool
# (see the allowlist doc comment above). Case-insensitive; empty/unset
# LOOM_ROLE never matches (fails closed).
_wt_readonly_role_active() {
    [[ -n "${LOOM_ROLE:-}" ]] || return 1
    local _role_lc
    _role_lc=$(printf '%s' "$LOOM_ROLE" | tr '[:upper:]' '[:lower:]')
    [[ "$_WT_READONLY_ROLES" == *" ${_role_lc} "* ]]
}

# True if $1 (an absolute, normalized path) sits inside the well-known
# `dist/` scratch directory at the main-checkout root (either root spelling).
_wt_dist_scratch_path() {
    local _p="$1"
    [[ -n "$_p" ]] || return 1
    if [[ -n "$_WT_MAIN_ROOT" ]]; then
        case "$_p" in
            "$_WT_MAIN_ROOT/dist"|"$_WT_MAIN_ROOT/dist"/*) return 0 ;;
        esac
    fi
    if [[ -n "$_WT_MAIN_ROOT_LOGICAL" ]]; then
        case "$_p" in
            "$_WT_MAIN_ROOT_LOGICAL/dist"|"$_WT_MAIN_ROOT_LOGICAL/dist"/*) return 0 ;;
        esac
    fi
    return 1
}

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
                        # Same-command mktemp scratch-write resolution (#6949):
                        # a target whose leading `$NAME`/`${NAME}` (optionally
                        # followed by a `/`-suffix, and with no `..` traversal
                        # in that suffix) matches a SAME-command exact-string
                        # `NAME=$(mktemp -d)`/`NAME=$(mktemp)` assignment is a
                        # provably fresh /tmp-or-$TMPDIR-rooted scratch path —
                        # see wt_write_mktemp_same_command_safe()'s own doc
                        # comment (above rm_scope_literal_same_command_resolve())
                        # for the exact, deliberately narrow conditions
                        # (mirrors rm_scope_mktemp_same_command_safe(), #6520).
                        # Skip the unresolved-var deny entirely on success —
                        # a fresh mktemp path can never coincide with the main
                        # checkout or any managed worktree.
                        #
                        # HEREDOC-BODY-MASKED SCAN (#6549, same rationale as
                        # COMMAND_RM_MKTEMP_SCAN): scans a dedicated
                        # COMMAND_WT_MKTEMP_SCAN copy (lazily built once, cached
                        # across loop iterations) that masks EVERY heredoc body
                        # unconditionally, so a decoy `NAME=$(mktemp -d)` line
                        # planted inside an inert heredoc body cannot manufacture
                        # a false SAFE verdict for a live assignment elsewhere in
                        # the same command.
                        if [[ -z "${COMMAND_WT_MKTEMP_SCAN+x}" ]]; then
                            COMMAND_WT_MKTEMP_SCAN="$COMMAND_NO_LITERAL_TEXT"
                            if [[ "$COMMAND_WT_MKTEMP_SCAN" == *"<<"* ]]; then
                                COMMAND_WT_MKTEMP_SCAN=$(printf '%s' "$COMMAND_WT_MKTEMP_SCAN" | awk "$_MASKHEREDOC_AWK"'
                                { buf = buf (NR > 1 ? "\n" : "") $0 }
                                END { printf "%s", mask_heredoc_bodies(buf) }')
                            fi
                        fi
                        if wt_write_mktemp_same_command_safe "$_wtarget" "$COMMAND_WT_MKTEMP_SCAN"; then
                            continue
                        fi
                        deny "BLOCKED: Bash-tool write target '${_wtarget}' is an unexpanded shell variable from the path root down, so this guard cannot tell where the write lands — it may resolve to an absolute path inside the main repository checkout ('${_WT_MAIN_ROOT}'), and a Loom-managed worktree exists in this repository. Unresolvable write targets fail closed (#4921). Need this variable resolved instead? Declare it literally in the SAME command, before the write: VAR=/literal/path; <write> -- the guard's same-command resolver (record_assign()/resolve_var(), #4881) substitutes it before this check runs, so the write is judged on the real resolved path. A false or self-serving declaration gains nothing: the resolved path is still checked against this same containment rule, so it can never grant an allow beyond what writing that literal path outright would already grant (#6172). Otherwise, write to an explicit literal path — inside your issue worktree (.loom/worktrees/issue-<N>) for repo files, or a spelled-out /tmp path for scratch. Not a Builder and need to write here directly? Set guards.worktreeIsolation:false in .loom/config.json for the session -- an inline 'LOOM_GUARD_WORKTREE_ISOLATION=0 <command>' prefix does NOT work (this hook runs as a separate process). (#4178)" "worktree-write-confinement-unresolved-var"
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
                            deny "BLOCKED: Bash-tool write target '${_wtarget}' has an unexpanded shell variable as its first real path component, so this guard cannot tell where the write lands — it may resolve inside the main repository checkout ('${_WT_MAIN_ROOT}'), and a Loom-managed worktree exists in this repository. Unresolvable write targets fail closed (#4921). Need this variable resolved instead? Declare it literally in the SAME command, before the write: VAR=/literal/path; <write> -- the guard's same-command resolver (record_assign()/resolve_var(), #4881) substitutes it before this check runs, so the write is judged on the real resolved path. A false or self-serving declaration gains nothing: the resolved path is still checked against this same containment rule, so it can never grant an allow beyond what writing that literal path outright would already grant (#6172). Otherwise, write to an explicit literal path — inside your issue worktree (.loom/worktrees/issue-<N>) for repo files, or a spelled-out /tmp path for scratch. Not a Builder and need to write here directly? Set guards.worktreeIsolation:false in .loom/config.json for the session -- an inline 'LOOM_GUARD_WORKTREE_ISOLATION=0 <command>' prefix does NOT work (this hook runs as a separate process). (#4178)" "worktree-write-confinement-unresolved-var"
                        fi
                    elif _wt_in_protected_area "$_wknown"; then
                        if _wt_isolation_in_play; then
                            deny "BLOCKED: Bash-tool write target '${_wtarget}' contains an unexpanded shell variable in a directory component, and its known prefix ('${_wknown}') is inside this repository's worktree/checkout area — this guard cannot tell whether the expanded path stays in your worktree or lands in the main repository checkout ('${_WT_MAIN_ROOT}'). Unresolvable write targets fail closed (#4921). Need this variable resolved instead? Declare it literally in the SAME command, before the write: VAR=/literal/path; <write> -- the guard's same-command resolver (record_assign()/resolve_var(), #4881) substitutes it before this check runs, so the write is judged on the real resolved path. A false or self-serving declaration gains nothing: the resolved path is still checked against this same containment rule, so it can never grant an allow beyond what writing that literal path outright would already grant (#6172). Otherwise, write to an explicit literal path — inside your issue worktree (.loom/worktrees/issue-<N>) for repo files, or a spelled-out /tmp path for scratch. Not a Builder and need to write here directly? Set guards.worktreeIsolation:false in .loom/config.json for the session -- an inline 'LOOM_GUARD_WORKTREE_ISOLATION=0 <command>' prefix does NOT work (this hook runs as a separate process). (#4178)" "worktree-write-confinement-unresolved-var"
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

        # CARVE-OUT (#6021): a read-only-by-role session (no Write/Edit tool
        # at all, see _WT_READONLY_ROLES doc comment above) staging a
        # scratch build artifact under the well-known `dist/` directory —
        # e.g. the Auditor's `cp target/release/loom-daemon
        # dist/loom-daemon-<target>` ahead of a local `docker build` of
        # `docker/worker/Dockerfile`. Checked BEFORE the deny below so it
        # never reaches the worktree-isolation-bypass message; does not
        # apply to any other path in the main checkout, and does not apply
        # at all unless LOOM_ROLE affirmatively names a Write/Edit-free role.
        if _wt_dist_scratch_path "$_wabs" && _wt_readonly_role_active; then
            continue
        fi

        # Target resolves inside the main checkout and outside every
        # worktree. Deny only if worktree isolation is actually in play for
        # this repo/session (a managed worktree exists somewhere); otherwise
        # fail open — a repo/session that has never created a worktree is
        # unaffected, mirroring guard-worktree-paths.sh exactly. The worktree
        # base is resolved off the same main-checkout root so the "a managed
        # worktree exists" gate stays consistent with the containment test.
        if _wt_isolation_in_play; then
            deny "BLOCKED: Bash-tool write to '${_wabs}' resolves to the main repository checkout ('${_WT_MAIN_ROOT}'), but a Loom-managed worktree exists elsewhere in this repository (this check cannot verify it belongs to the acting session — see #4245). This is a worktree-isolation bypass via Bash redirection/tee/sed -i/cp/mv — do NOT retry the write through Bash. cd into your issue worktree (.loom/worktrees/issue-<N>) and write there instead. Not a Builder and need to write here directly? Set guards.worktreeIsolation:false in .loom/config.json for the session -- an inline 'LOOM_GUARD_WORKTREE_ISOLATION=0 <command>' prefix does NOT work (this hook runs as a separate process). (#4178)" "worktree-write-confinement"
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
            while IFS=$'\037' read -r _fcpath _ftarget _fresettarget; do
                [[ -z "$_ftarget" ]] && _ftarget="@HEAD@"
                _fcwd="$_fcpath"
                [[ -z "$_fcwd" ]] && _fcwd="$CWD"
                # Shell-accurate quote removal for cwd RESOLUTION (#5372,
                # mirrors write-confinement's _wcwdclassify split at
                # #4933/#4926). parse_force_ops() deliberately threads
                # curcwd from the RAW cd-argument token (quote characters
                # intact, see its own header comment); unquote a COPY here
                # before actually resolving it against the filesystem, so a
                # quoted or partially-quoted absolute `cd` argument resolves
                # to the real directory instead of a literal path containing
                # stray quote characters that can never exist on disk. Only
                # touched when a quote character is actually present, so the
                # ordinary quote-free case stays byte-identical; an
                # unterminated quote falls back to the raw value (today's
                # verdict — ambiguous/ask), never widening toward an allow.
                if [[ "$_fcwd" == *"'"* || "$_fcwd" == *'"'* ]]; then
                    strip_target_quoting "$_fcwd" && _fcwd="$_UNQUOTED_TARGET"
                fi
                if [[ "$_ftarget" == "@HEAD@" ]]; then
                    _fbranch=""
                    if [[ -n "$_fcwd" ]]; then
                        _fbranch=$(git -C "$_fcwd" symbolic-ref --short HEAD 2>/dev/null || true)
                    fi
                    if [[ -z "$_fbranch" ]]; then
                        # Detached HEAD / unresolved identity is ambiguous by
                        # default — ask, never silently allow (fail toward
                        # asking) UNLESS this is recognizably the "reset a
                        # Loom-managed worktree back to a known-good ref"
                        # recovery shape (#5772): a `git reset --hard` line's
                        # own RESET-TARGET literal (never empty for a reset
                        # line — see parse_force_ops()'s header comment;
                        # empty here means this was actually a PUSH line, not
                        # a reset, and stays fully ambiguous) resolves to
                        # origin/main, origin/master, origin/<repo-default>,
                        # or plain HEAD (a bare `git reset --hard` — a no-op
                        # ref move, only discards uncommitted changes) — none
                        # of those name a protected branch or another agent's
                        # WIP — AND the cwd resolves inside a Loom-managed
                        # worktree (the disposable, session-owned checkout
                        # this recovery pattern is scoped to, never the main
                        # checkout, never an unmanaged directory). Any other
                        # shape — an unrecognized reset target, a non-reset
                        # (push) line, or a cwd outside a managed worktree —
                        # still asks exactly as before.
                        _fdetached_safe=false
                        if [[ -n "$_fresettarget" ]]; then
                            if [[ "$_fresettarget" == "HEAD" || \
                                  "$_fresettarget" == "origin/main" || \
                                  "$_fresettarget" == "origin/master" ]]; then
                                _fdetached_safe=true
                            else
                                _fdetdefault=$(resolve_default_branch "$_fcwd")
                                if [[ -n "$_fdetdefault" && "$_fresettarget" == "origin/$_fdetdefault" ]]; then
                                    _fdetached_safe=true
                                fi
                            fi
                        fi
                        if [[ "$_fdetached_safe" == true ]]; then
                            _fcwdabs=""
                            [[ "$_fcwd" == /* ]] && _fcwdabs=$(normalize_abs_path "$_fcwd")
                            _in_any_managed_worktree "$_fcwdabs" || _fdetached_safe=false
                        fi
                        if [[ "$_fdetached_safe" != true ]]; then
                            ask "Command requires confirmation: $COMMAND (force operation on a detached or unresolved branch)" "force-op:detached"
                        fi
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
    #
    # BACKTICK / `(`-OPENER BOUNDARY (#5783): the three entries immediately
    # below used to anchor ONLY on `^|[;&|[:space:]]`, which omits both a
    # backtick and a bare `(` (no following space) as valid command-position
    # boundaries. So `` echo `git clean -fd` `` and (in principle) a no-space
    # `$(git clean -fd)` were invisible to this array even though the
    # equivalent unwrapped command asks — a real narrowing gap (missed ask),
    # not a false positive. The class now also admits a backtick and `(`,
    # matching the boundary class the stash-scope/read-tree checks already
    # use below (which independently had the `(` but not the backtick).
    '(^|[;&|(`[:space:]])git clean -fd'
    '(^|[;&|(`[:space:]])git checkout \.'
    '(^|[;&|(`[:space:]])git restore \.'

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
    # NOTE: `cat .../.ssh/<file>` is NOT a plain substring entry here. It used
    # to be '(^|[;&|[:space:]])cat.*/\.ssh/', which matched the whole `.ssh/`
    # directory rather than the specific secret-bearing files inside it — so
    # routine, non-secret reads (`cat ~/.ssh/config`, `known_hosts`,
    # `known_hosts.old`, `authorized_keys`, none of which contain key
    # material) false-asked identically to reading an actual private key
    # (#5824). It is handled by the segment-parsed, basename-allowlisted
    # ssh_cat_ask_reason() check below instead — see its own comment block.
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
# SSH-DIRECTORY READ ASK — cat under .ssh/, basename-allowlisted (#5824)
#
# The plain-substring ASK_PATTERNS entry this replaced —
# '(^|[;&|[:space:]])cat.*/\.ssh/' — matched the whole `.ssh/` directory, so
# reading a routine, non-secret file (`config`, `known_hosts`,
# `known_hosts.old`, `authorized_keys` — at most host aliases / key
# fingerprints, never key material) asked identically to reading an actual
# private key. `grep -E` substring matching cannot capture the matched
# operand to inspect its basename, so — mirroring systemctl_ask_reason()
# above — this segment-parses the command with qsplit() (quote-aware,
# #3755), strips a leading sudo/env wrapper per segment, and only inspects
# segments whose command word is literally `cat`.
#
# ALLOWLIST, NOT DENYLIST (deliberate, per the issue's acceptance criteria):
# a `cat` operand under `.ssh/` still asks unless its basename is one of the
# four known-safe filenames below. Any unrecognized/unlisted filename —
# including a bare `.ssh/` with no filename at all — falls through to the
# safer default (ask), so a new key-naming convention or an unforeseen file
# is never silently allowed. Private key material (`id_rsa`, `id_ed25519`,
# anything else) always misses the allowlist and keeps asking.
# =============================================================================
ssh_cat_ask_reason() {
    printf '%s' "$1" | awk "$_QSPLIT_AWK"'
    {
        $0 = qsplit($0)   # quote-aware segmentation (#3755)
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            sub(/^sudo[ \t]+/, "", seg)
            # Strip a leading `env` wrapper + its flags/assignments, mirroring
            # systemctl_ask_reason() above (#3586), so `env FOO=bar cat
            # ~/.ssh/id_rsa` still resolves its command word to `cat`.
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
            if (toks[1] != "cat") continue
            for (j = 2; j <= m; j++) {
                tok = toks[j]
                if (tok !~ /\/\.ssh\//) continue
                # Operand after the LAST /.ssh/ in this token (greedy .*
                # backtracks to the rightmost occurrence).
                if (!match(tok, /.*\/\.ssh\//)) continue
                rest = substr(tok, RLENGTH + 1)
                # basename: strip any further path components after /.ssh/
                if (match(rest, /.*\//)) {
                    base = substr(rest, RLENGTH + 1)
                } else {
                    base = rest
                }
                # Strip stray quote characters a quoted operand (copied
                # verbatim by qsplit) may leave attached to the basename.
                gsub(/[\047\042]/, "", base)
                if (base != "config" && base != "known_hosts" && base != "known_hosts.old" && base != "authorized_keys") {
                    print "cat .ssh/" base
                    exit
                }
            }
        }
    }'
}
_SSH_CAT_ASK=$(ssh_cat_ask_reason "$COMMAND_ASK_SCAN" | head -1)
if [[ -n "$_SSH_CAT_ASK" ]]; then
    ask "Command requires confirmation: $COMMAND" "ask:$_SSH_CAT_ASK"
fi

# =============================================================================
# CARGO CLEAN SCOPE ASK — bare `cargo clean` clearing a build.target-dir
# SHARED outside the current repo (#6684), gated by cargo_clean_guard_enabled()
#
# On a host whose `~/.cargo/config.toml` (or an ancestor `.cargo/config.toml`)
# sets a single `build.target-dir` shared across every project — e.g. an
# external volume used after the internal disk hit ENOSPC — a bare `cargo
# clean` is not a local operation: it deletes the build output of EVERY
# project on that host, including whatever sweep is compiling concurrently
# elsewhere. The failure this produces names nothing about the cause (a mid-
# build "No such file or directory" on the victim's OWN target dir), so the
# agent that gets hit has no way to tell a shared-clean collision apart from a
# faulted volume.
#
# `cargo clean -p <pkg>` (package-scoped) and a repo-local target dir are
# completely unaffected — no resolution is attempted, no prompt — so this adds
# zero friction to the common case.
#
# Detection is segment-parsed (qsplit(), #3755) and command-word anchored,
# mirroring systemctl_ask_reason()/ssh_cat_ask_reason() immediately above:
# only a segment whose actual command word is `cargo` (after stripping a
# leading sudo) with `clean` as the very next token, and no `-p`/`--package`
# flag anywhere in its remaining tokens, is a candidate. A same-command
# `CARGO_TARGET_DIR=<value> cargo clean` assignment immediately preceding the
# command word is captured too (cargo's own env-override precedence).
#
# CARGO_TARGET_DIR — same-command OR the guard's own process env — is always
# treated as an explicit, deliberate scoping decision and NEVER asks, however
# it resolves: it is the exact fix this ask's own message recommends, so
# treating it as unsafe would be self-defeating. Only the CONFIG-derived
# resolution (`cargo config get build.target-dir`, falling back to a manual
# `.cargo/config.toml` walk-up from cwd to the filesystem root, then
# `$CARGO_HOME/config.toml`) is compared against the repo root — matching
# cargo's own real precedence, and the exact silent, invisible-to-the-agent
# sharing mechanism this issue is about.
# =============================================================================
cargo_clean_scope_match() {
    printf '%s' "$1" | awk "$_QSPLIT_AWK"'
    {
        $0 = qsplit($0)
        n = split($0, segs, "\n")
        for (i = 1; i <= n; i++) {
            seg = segs[i]
            sub(/^[ \t]+/, "", seg)
            m = split(seg, toks, /[ \t]+/)
            if (m == 0) continue
            j = 1
            envval = ""
            # Strip leading bare `VAR=value` assignment(s), capturing
            # CARGO_TARGET_DIR if one is present among them (ordinary shell
            # same-command env override, no `env` wrapper required).
            while (j <= m && toks[j] ~ /^[A-Za-z_][A-Za-z0-9_]*=/) {
                if (toks[j] ~ /^CARGO_TARGET_DIR=/) {
                    envval = substr(toks[j], index(toks[j], "=") + 1)
                    gsub(/[\047\042]/, "", envval)
                }
                j++
            }
            if (j <= m && toks[j] == "sudo") j++
            if (j > m || toks[j] != "cargo") continue
            j++
            if (j > m || toks[j] != "clean") continue
            j++
            scoped = 0
            for (k = j; k <= m; k++) {
                if (toks[k] == "-p" || toks[k] == "--package" || toks[k] ~ /^--package=/) { scoped = 1; break }
            }
            if (scoped) continue
            print "1"
            print envval
            exit
        }
        print "0"
    }'
}

# Minimal TOML reader for a single `[build]` -> `target-dir` key (#6684). Not a
# general TOML parser: only tracks top-level `[table]` headers to know when a
# `target-dir = "..."` line is inside the literal `[build]` table (not
# `[build.foo]` or an unrelated table), matching the one key cargo's own
# resolution needs here.
_cargo_toml_target_dir_value() {
    local f="$1"
    [[ -f "$f" ]] || return 1
    awk '
        function strip(v) {
            gsub(/^[ \t]+/, "", v); gsub(/[ \t]+$/, "", v)
            gsub(/^"/, "", v); gsub(/"$/, "", v)
            gsub(/^\047/, "", v); gsub(/\047$/, "", v)
            return v
        }
        BEGIN { in_build = 0 }
        /^[ \t]*\[/ {
            line = $0
            gsub(/^[ \t]+/, "", line)
            in_build = (line ~ /^\[build\][ \t]*(#.*)?$/) ? 1 : 0
            next
        }
        in_build && /^[ \t]*target-dir[ \t]*=/ {
            val = $0
            sub(/^[^=]*=/, "", val)
            sub(/#.*$/, "", val)
            print strip(val)
            exit
        }
    ' "$f" 2>/dev/null
}

# Walks up from $1 to the filesystem root looking for `.cargo/config.toml` /
# `.cargo/config`, mirroring cargo's own directory-ancestor search order — the
# CLOSEST ancestor that sets `build.target-dir` wins. A relative value is
# resolved against the directory the config file itself was found in (cargo's
# documented behavior: relative target-dir paths are relative to the config
# file's own location, not the invocation cwd).
_cargo_config_walk_up_target_dir() {
    local dir="$1" f val
    while [[ -n "$dir" ]]; do
        for f in "$dir/.cargo/config.toml" "$dir/.cargo/config"; do
            if [[ -f "$f" ]]; then
                val=$(_cargo_toml_target_dir_value "$f")
                if [[ -n "$val" ]]; then
                    [[ "$val" != /* ]] && val="$dir/$val"
                    printf '%s' "$val"
                    return 0
                fi
            fi
        done
        [[ "$dir" == "/" ]] && break
        dir=$(dirname "$dir")
    done
    return 1
}

# Lowest-precedence fallback: the user-global $CARGO_HOME/config.toml (default
# ~/.cargo/config.toml) — this is the actual file the #6684 incident hit.
_cargo_home_config_target_dir() {
    local home="${CARGO_HOME:-$HOME/.cargo}" f val
    [[ -n "$home" ]] || return 1
    for f in "$home/config.toml" "$home/config"; do
        if [[ -f "$f" ]]; then
            val=$(_cargo_toml_target_dir_value "$f")
            if [[ -n "$val" ]]; then
                [[ "$val" != /* ]] && val="$home/$val"
                printf '%s' "$val"
                return 0
            fi
        fi
    done
    return 1
}

# Resolves the effective cargo target-dir for a candidate bare `cargo clean`
# and reports WHERE it came from, printed as two lines: SOURCE (env|config|
# default) then the resolved absolute path. Only a "config" source is ever
# compared against the repo root by the caller — see the block header above
# for why an explicit CARGO_TARGET_DIR (env) is never treated as the hazard.
cargo_clean_effective_target_dir() {
    local repo_root="$1" cwd="$2" same_cmd_env="$3"
    local base_cwd="${cwd:-$repo_root}"
    local resolved="" source=""
    if [[ -n "$same_cmd_env" ]]; then
        resolved="$same_cmd_env"
        source="env"
    elif [[ -n "${CARGO_TARGET_DIR:-}" ]]; then
        resolved="$CARGO_TARGET_DIR"
        source="env"
    else
        source="config"
        if command -v cargo >/dev/null 2>&1; then
            local cg
            cg=$(cd "$base_cwd" 2>/dev/null && cargo config get build.target-dir 2>/dev/null) || cg=""
            if [[ -n "$cg" ]]; then
                resolved=$(printf '%s' "$cg" | sed -e 's/^build\.target-dir[[:space:]]*=[[:space:]]*//' -e 's/^"//' -e 's/"$//')
            fi
        fi
        if [[ -z "$resolved" ]]; then
            resolved=$(_cargo_config_walk_up_target_dir "$base_cwd") || resolved=""
        fi
        if [[ -z "$resolved" ]]; then
            resolved=$(_cargo_home_config_target_dir) || resolved=""
        fi
        if [[ -z "$resolved" ]]; then
            # No config anywhere sets target-dir — cargo's own default.
            resolved="${repo_root}/target"
            source="default"
        fi
    fi
    if [[ -n "$resolved" && "$resolved" != /* ]]; then
        resolved="$base_cwd/$resolved"
    fi
    [[ "$resolved" = /* ]] && resolved=$(normalize_abs_path "$resolved")
    printf '%s\n%s\n' "$source" "$resolved"
}

if echo "$COMMAND_ASK_SCAN" | grep -qE '(^|[;&|(`[:space:]])cargo[[:space:]]+clean'; then
    _CARGO_CLEAN_MATCH=$(cargo_clean_scope_match "$COMMAND_ASK_SCAN")
    _CARGO_CLEAN_FLAG=$(printf '%s\n' "$_CARGO_CLEAN_MATCH" | sed -n '1p')
    if [[ "$_CARGO_CLEAN_FLAG" == "1" ]] && [[ -n "$REPO_ROOT" ]] && cargo_clean_guard_enabled; then
        _CARGO_CLEAN_ENV=$(printf '%s\n' "$_CARGO_CLEAN_MATCH" | sed -n '2p')
        _CARGO_TD_INFO=$(cargo_clean_effective_target_dir "$REPO_ROOT" "$CWD" "$_CARGO_CLEAN_ENV")
        _CARGO_TD_SOURCE=$(printf '%s\n' "$_CARGO_TD_INFO" | sed -n '1p')
        _CARGO_TD_PATH=$(printf '%s\n' "$_CARGO_TD_INFO" | sed -n '2p')
        if [[ "$_CARGO_TD_SOURCE" == "config" ]] && [[ -n "$_CARGO_TD_PATH" ]] && \
           [[ "$_CARGO_TD_PATH" != "$REPO_ROOT" && "$_CARGO_TD_PATH" != "$REPO_ROOT"/* ]]; then
            # The comparison above is LEXICAL on both sides, and the two sides
            # are not built the same way: $_CARGO_TD_PATH comes from the
            # config walk-up (rooted at the guard's raw $CWD, symlinks intact,
            # only normalize_abs_path'd), while $REPO_ROOT comes from `git
            # rev-parse --show-toplevel`, which returns the symlink-RESOLVED
            # spelling. For a repo reached through a symlinked ancestor the
            # two never string-match even when the target-dir is genuinely
            # repo-local — on macOS that is every $TMPDIR/mktemp -d repo,
            # because /var is a symlink to /private/var (#6684). So before
            # asking, re-run the containment test with BOTH sides resolved the
            # same way; only a target-dir that is outside the repo under the
            # physical spelling too is really shared with other projects.
            _CARGO_TD_PATH_PHYS=$(physical_abs_path "$_CARGO_TD_PATH")
            _CARGO_REPO_ROOT_PHYS=$(physical_abs_path "$REPO_ROOT")
            if [[ "$_CARGO_TD_PATH_PHYS" != "$_CARGO_REPO_ROOT_PHYS" && \
                  "$_CARGO_TD_PATH_PHYS" != "$_CARGO_REPO_ROOT_PHYS"/* ]]; then
                # Report the path as CONFIGURED (logical spelling), not the
                # physically-resolved one — that is the string the operator
                # will recognize from their own .cargo/config.toml.
                ask "Command requires confirmation: $COMMAND (target-dir is shared at '$_CARGO_TD_PATH'; this clears every project on this host, including in-flight sweeps — use 'cargo clean -p <pkg>' or set CARGO_TARGET_DIR)" "cargo-clean-scope-outside-repo"
            fi
        fi
    fi
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
#
# BACKTICK BOUNDARY (#5783): the leading class below now also admits a
# backtick, matching the `(` it already had — `` `git read-tree` `` used to
# be invisible to this check for the same reason `` `git clean -fd` `` was
# invisible to ASK_PATTERNS above.
# =============================================================================
if echo "$COMMAND_NO_COMMENT" | grep -qE '(^|[;&|(`]|[[:space:]])git[[:space:]]+read-tree'; then
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
# CREATE-SIDE REDIRECT (#5754): the two asks below fired 32 times in the five
# days 2026-08-04..08 (`.loom/logs/guard-decisions.log`, ~7.2/day) — all of them
# AFTER both the role-prompt guidance and the inline suggestion text in the ask
# itself had landed, so restating the same advice a third time was not going to
# help. Classifying those 32 by chain shape showed the guard was gated on the
# wrong half of the stash cycle:
#
#   - 15/32 chained a CREATE and a RECOVERY in one command (`cd <wt> && git
#     stash && <check>; git stash pop`). The create is silently allowed today,
#     so the guard only speaks up at the pop — at the END of the chain, about a
#     decision made at the START of it.
#   - 11/32 were RECOVERY-ONLY: WIP already sitting on `refs/stash` from an
#     earlier, silently-allowed create. Three consecutive entries from one
#     worktree (issue-5654, 01:46:00/13/18Z) show an agent trying to force the
#     pop through with an inline `LOOM_GUARD_STASH_SCOPE=0` prefix, which
#     cannot work — the hook is a separate process and reads its OWN
#     environment.
#   - 6/32 were guard self-tests, where `git stash pop` appears as inert text.
#
# That distribution is why the RECOVERY asks below are NOT escalated to deny.
# The hazard needs two parties: A pushes onto the shared stack, B pops. Denying
# only B protects A but strands B — `refs/stash` has no sanctioned reader other
# than `git stash pop` (worktree.sh's stash-pop reads a per-issue ref), so a
# deny there converts "ask a human" into "lose the work".
#
# Blocking the CREATE instead is lossless: the working tree is untouched, so
# the agent simply reruns with the replacement command, and no entry ever
# reaches the shared stack for anyone to collide on. So a raw stash CREATE is
# DENIED — not asked — but only where a scriptable safe equivalent provably
# exists and can be named exactly:
#
#   1. cwd resolves inside a LINKED worktree (never the main checkout: there is
#      no `worktree.sh stash-push` for the main checkout, so nothing to
#      redirect to — main-checkout creates stay allowed, exactly as today);
#   2. that worktree carries the `.loom-managed` sentinel and its directory
#      name yields an issue number, so the message can print the literal
#      `stash-push <N>` / `stash-pop <N>` pair instead of a `<issue-number>`
#      placeholder the agent has to fill in;
#   3. `<main>/.loom/scripts/worktree.sh` actually exists;
#   4. two or more `.loom-managed` worktrees are active — the SAME predicate as
#      the worktree-collision ask below, so the deny fires exactly where the
#      paired pop would have stalled, and a solo worktree stays fully ungated.
#
# Verification (falsifiable, for a later Curator/Auditor pass): re-run
# `jq -r 'select(.pattern|startswith("stash-scope"))|.ts[0:10]'
# .loom/logs/guard-decisions.log | sort | uniq -c` at least 7 days after this
# lands. `stash-scope:worktree-collision` should fall well below its 2026-08-04
# ..08 baseline of ~7.2 combined hits/day, with any residue concentrated in
# `stash-scope:create-redirect` (a deny, which does not stall) and in
# main-checkout hits (deliberately unchanged).
#
# Gated by stash_scope_guard_enabled() (guards.stashScope /
# LOOM_GUARD_STASH_SCOPE, default on), invoked LAZILY only after the pattern
# already matched, mirroring every other cold-path toggle in this file.
#
# BACKTICK / NO-SPACE-PAREN BOUNDARY (#5783): both checks below now admit a
# backtick as a leading boundary (alongside the `(` they already had), so
# `` `git stash pop` `` and `` X=`git stash pop` `` are no longer invisible
# to this scan the way `` `git clean -fd` `` was to ASK_PATTERNS above. The
# recovery-subcommand check's TRAILING boundary is also widened — it used to
# require whitespace or end-of-string right after `pop`/`drop`/`clear`, which
# missed both a no-space `$(git stash pop)` (trailing `)`) and a no-space
# `` `git stash pop` `` (trailing backtick); it now accepts either, plus the
# shell-separator set the pre-check's own trailing class already accepted.
# =============================================================================
_stash_is_recover=false
_stash_is_pop=false
_stash_is_create=false
if echo "$COMMAND_ASK_SCAN" | grep -qE '(^|[;&|(`]|[[:space:]])git[[:space:]]+stash([[:space:]]|[;&|)`]|$)'; then
    if echo "$COMMAND_ASK_SCAN" | grep -qE '(^|[;&|(`]|[[:space:]])git[[:space:]]+stash[[:space:]]+(pop|drop|clear)([[:space:]]|[;&|)`]|$)'; then
        _stash_is_recover=true
    fi
    # `pop` alone has a scriptable safe equivalent (safe-stash-pop.sh, #6501);
    # `drop`/`clear` do not — they destroy an entry outright with nothing to
    # verify afterwards. Track it separately so the main-checkout ask only
    # names the wrapper when the wrapper actually applies.
    if echo "$COMMAND_ASK_SCAN" | grep -qE '(^|[;&|(`]|[[:space:]])git[[:space:]]+stash[[:space:]]+pop([[:space:]]|[;&|)`]|$)'; then
        _stash_is_pop=true
    fi
    if stash_create_invoked "$COMMAND_ASK_SCAN"; then
        _stash_is_create=true
    fi
fi

if [[ "$_stash_is_recover" == true || "$_stash_is_create" == true ]] \
   && stash_scope_guard_enabled; then
    _stash_effective_cwd="$CWD"
    if [[ -n "$CWD" ]]; then
        _stash_effective_cwd=$(resolve_stash_cwd "$COMMAND_NO_COMMENT" "$CWD")
        [[ -z "$_stash_effective_cwd" ]] && _stash_effective_cwd="$CWD"
    fi
    # Shell-accurate quote removal for cwd RESOLUTION (#5372, mirrors
    # write-confinement's _wcwdclassify split at #4933/#4926 and the
    # parse_force_ops() _fcwd unquote above). resolve_stash_cwd()
    # deliberately threads curcwd from the RAW cd-argument token (quote
    # characters intact, see its own header comment); unquote a COPY here
    # before actually resolving it against the filesystem, so a quoted or
    # partially-quoted absolute `cd` argument resolves to the real directory
    # instead of a literal path containing stray quote characters that can
    # never exist on disk. Only touched when a quote character is actually
    # present, so the ordinary quote-free case stays byte-identical; an
    # unterminated quote falls back to the raw value (today's verdict —
    # ambiguous/ask), never widening toward an allow.
    if [[ "$_stash_effective_cwd" == *"'"* || "$_stash_effective_cwd" == *'"'* ]]; then
        strip_target_quoting "$_stash_effective_cwd" && _stash_effective_cwd="$_UNQUOTED_TARGET"
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
        # MAIN CHECKOUT. Only the RECOVERY half is gated here. There is no
        # `worktree.sh stash-push` equivalent for the main checkout (it takes
        # an issue number and operates on that issue's worktree), so a raw
        # create has nothing to be redirected to and stays allowed exactly as
        # before — the create-side deny (#5754) is worktree-only by design.
        if [[ "$_stash_is_recover" == true ]]; then
            # RECOMMENDED-PATH HINT (#6501). A raw main-checkout `git stash pop`
            # is not just a stack-ownership hazard — it is also the mechanism
            # behind #6499/#6502, where a conflicting pop left live
            # `<<<<<<<`/`=======`/`>>>>>>>` markers in a tracked
            # `.loom/config.json` that were then committed, silently breaking
            # the daemon's config parse fleet-wide. `safe-stash-pop.sh` is the
            # verified replacement. Named only when it PROVABLY exists and
            # actually applies (pop, not drop/clear) — the same discipline the
            # create-side redirect below uses before printing a literal
            # replacement command. This stays an ASK, not a deny: `refs/stash`
            # has no sanctioned reader other than a pop, so denying would
            # strand work rather than protect it.
            _stash_pop_hint=""
            if [[ "$_stash_is_pop" == true && -f "$_stash_common_parent/.loom/scripts/safe-stash-pop.sh" ]]; then
                _stash_pop_hint=" If you do need this entry back, use the verified wrapper instead of a raw pop: './.loom/scripts/safe-stash-pop.sh' — it snapshots the pre-pop tree, pops, verifies no conflict markers or unmerged index entries were left behind, and rolls the tree back (keeping the stash entry) when the pop conflicts, so it can never leave a tracked file carrying unresolved conflict markers for someone to commit (#6501; add --no-restore to keep a conflicted tree for manual resolution)."
            fi
            ask "Command requires confirmation: $COMMAND (git stash pop/drop/clear in the MAIN checkout can destroy operator-preserved state — the main checkout's stash stack is operator-owned, not scratch space for an integration check. Run test-merges in an isolated worktree instead; set guards.stashScope:false in .loom/config.json, or export LOOM_GUARD_STASH_SCOPE=0 in the agent's OWN environment before the session — an inline 'LOOM_GUARD_STASH_SCOPE=0 git stash pop' prefix does not reach this hook, which runs as a separate process)${_stash_pop_hint}" "stash-scope:main-checkout"
        fi
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
            # CREATE-SIDE REDIRECT (#5754), evaluated BEFORE the recovery ask
            # so a `git stash && <check>; git stash pop` chain gets the
            # actionable, lossless deny at the front rather than an
            # unanswerable ask about its tail. Requires a named replacement:
            # the `.loom-managed` sentinel, an `issue-<N>` directory name to
            # interpolate, and a real worktree.sh to call. If any of those is
            # missing there is no safe equivalent to point at, so behaviour is
            # unchanged (allow) — this never blocks a caller who has no
            # alternative.
            if [[ "$_stash_is_create" == true && -f "$_stash_toplevel/.loom-managed" \
                  && -f "$_stash_common_parent/.loom/scripts/worktree.sh" ]]; then
                _stash_wt_base="${_stash_toplevel##*/}"
                if [[ "$_stash_wt_base" =~ ^issue-([0-9]+)$ ]]; then
                    _stash_issue_num="${BASH_REMATCH[1]}"
                    deny "Blocked: $COMMAND (raw 'git stash' puts WIP on refs/stash — a SINGLE stack SHARED across every linked worktree of this repo, not per-worktree — where any of the $_stash_worktree_count currently-active managed worktrees can pop or drop it, and where the recovery step ('git stash pop') is itself gated. Nothing has been run: your working tree is untouched, so just rerun with the per-issue equivalent, which never touches refs/stash. Shelve WIP as a patch: './.loom/scripts/worktree.sh snapshot $_stash_issue_num'. Clean baseline vs. diff: './.loom/scripts/worktree.sh stash-push $_stash_issue_num' ... './.loom/scripts/worktree.sh stash-pop $_stash_issue_num'. To opt out repo-wide set guards.stashScope:false in .loom/config.json, or export LOOM_GUARD_STASH_SCOPE=0 in the agent's OWN environment before the session — an inline 'LOOM_GUARD_STASH_SCOPE=0 git stash' prefix does not reach this hook, which runs as a separate process)" "stash-scope:create-redirect"
                fi
            fi

            if [[ "$_stash_is_recover" == true ]]; then
                ask "Command requires confirmation: $COMMAND (git stash pop/drop/clear from a linked worktree can destroy ANOTHER builder's WIP — refs/stash is a single stack SHARED across every linked worktree of this repo, not per-worktree, and $_stash_worktree_count managed worktrees are currently active. Use './.loom/scripts/worktree.sh snapshot <issue-number>' instead of git stash for ad-hoc WIP, or './.loom/scripts/worktree.sh stash-push <issue-number>' + 'stash-pop <issue-number>' for a clean-baseline-vs-diff comparison — neither touches the shared refs/stash stack, so neither needs this ask; set guards.stashScope:false in .loom/config.json, or export LOOM_GUARD_STASH_SCOPE=0 in the agent's OWN environment before the session — an inline 'LOOM_GUARD_STASH_SCOPE=0 git stash pop' prefix does not reach this hook, which runs as a separate process)" "stash-scope:worktree-collision"
            fi
        fi
    elif [[ "$_stash_is_recover" == true && "$_stash_effective_cwd" != "$CWD" ]]; then
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
# The docker entries name only mutating verbs (rm/rmi/stop/kill/restart) and
# never match read-only `docker ps`/`docker logs`. `docker rmi`/`stop`/`kill`/
# `restart` are unchanged — they only move under this toggle. `docker rm`
# (#5823) is narrowed to its genuinely destructive shape: a bare/ID/name-only
# `docker rm [-f] <container>` only removes container *instances* — it cannot
# touch images, volumes, or networks — so ordinary self-scoped cleanup (e.g.
# `docker ps -a --filter ancestor=... -q | xargs -r docker rm -f`, or
# `docker rm -f <id> <id>`) no longer asks. Only the volume-destroying variant
# (`-v`/`--volumes`, which DOES delete named/anonymous volumes and can take
# out state a *different* container still depends on) keeps asking; the
# genuinely catastrophic host-wide `docker system prune` stays covered as an
# ungated catastrophic deny above, unaffected by this change.
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
    #
    # `docker rm` (#5823) is narrowed to only the volume-destroying variant: a
    # `-v`/`--volumes` short/long flag token, boundary-anchored on whitespace
    # so it cannot false-match a container name that merely *contains* "-v"
    # (e.g. `docker rm my-container-v1` does not ask; `docker rm -v
    # my-container` does). A bare/ID/name-only `docker rm [-f] <container>`
    # (no `-v`) is intentionally NOT covered — it cannot destroy images,
    # volumes, or networks, and the host-wide catastrophic case is already
    # covered by the ungated `docker system prune` deny above.
    'docker rm[^;&|]*[[:space:]](-[a-zA-Z]*v[a-zA-Z]*|--volumes)([[:space:]]|$)'
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
#
# SCANS COMMAND_CLOUD_ASK_SCAN (#6002), NOT COMMAND_ASK_SCAN directly -- see
# that variable's own definition above for why it carries additional
# for-loop-word-list / grep-rg-jq-positional masking that COMMAND_ASK_SCAN
# itself deliberately does not (SQL_DDL_PATTERN's competing need to still see
# that same text). COMMAND_CLOUD_ASK_SCAN is a strict superset-redaction of
# COMMAND_ASK_SCAN (every byte COMMAND_ASK_SCAN already masks stays masked
# here too), so this substitution only narrows what CLOUD_ASK_PATTERNS can
# match -- it can never widen it.
for pattern in "${CLOUD_ASK_PATTERNS[@]}"; do
    if echo "$COMMAND_CLOUD_ASK_SCAN" | grep -qE "$pattern" && cloud_guard_enabled; then
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
