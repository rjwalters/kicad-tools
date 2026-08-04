#!/usr/bin/env bash
# guard-loom-workflow.sh - PreToolUse hook for Loom-workflow-specific Bash guards
#
# Claude Code PreToolUse hook that intercepts Bash commands before execution.
# Receives JSON on stdin with tool_input.command and cwd fields.
#
# This hook carries the Loom-workflow-specific guards that were extracted from
# guard-destructive.sh (issue #3604), plus later Loom-specific additions:
#
#   1. LOOM: Prefer merge-pr.sh over 'gh pr merge'
#   2. LOOM: Block 'pip install -e' inside worktrees (issue #2495, #4079)
#   3. LOOM: Ask before real-registry-mutating `loom-daemon workspace
#      add|remove|set-priority` (issue #4326)
#
# The generic repository-hygiene guards (catastrophic denies, SQL/cloud toggles,
# ASK patterns) live in guard-destructive.sh and are being migrated toward Repo
# Skills (rjwalters/repo#13). This file stays Loom-owned because these guards
# are specific to the Loom worktree/merge/daemon workflow.
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

# Shared config-tier resolver (#4063). Source defaults/scripts/lib/config-resolver.sh
# so decision_log_enabled() below reads the full config tier chain through the
# same code path as guard-destructive-generic.sh (kept consistent between the two
# guards). At runtime SCRIPT_DIR is .loom/hooks/ and .loom/scripts is a symlink to
# defaults/scripts, so ../scripts/lib resolves. Best-effort: a missing/unsourceable
# lib leaves loom_config_get undefined and the reader's `|| raw=false` fallback
# preserves the guard-OFF default.
if [[ -f "$SCRIPT_DIR/../scripts/lib/config-resolver.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/../scripts/lib/config-resolver.sh" 2>/dev/null || true
fi

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
# GH-PR-MERGE-REDIRECT FALSE-POSITIVE MASKING (issue #5109)
#
# The 'gh pr merge' redirect check below (guard tag loom:gh-pr-merge-redirect)
# used to grep the RAW, unmasked $COMMAND for the literal substring "gh pr
# merge" anywhere in the byte stream -- so it fired on commands that never
# actually invoked the disallowed CLI, only mentioned the phrase as inert
# text: a git commit message (passed via the CLAUDE.md-documented `-m
# "$(cat <<'EOF' ... EOF)"` heredoc idiom) that quoted the phrase as prose
# documenting the very rule this guard enforces, and a `gh issue list
# --search "..."` query whose search string happened to contain the phrase
# as text to search FOR, not a command to run.
#
# The two functions below build a MASKED copy of $COMMAND, used ONLY for the
# 'gh pr merge' substring check immediately below -- never for any other
# guard in this file, and never for decision-log redaction (that stays on
# strip_literal_text() above, unchanged). Composition matters: heredoc-body
# masking runs FIRST so any quote/backtick characters living inside a masked
# heredoc body can no longer interfere with the flag-value masking pass that
# runs second.
#
# Deliberately narrow, matching this repo's established masking conventions
# (see guard-destructive-generic.sh's mask_heredoc_bodies(), #5000/#5087):
# only two specific, well-understood-safe shapes are masked, and a command
# that merely EXECUTES the disallowed CLI through a wrapper -- `sh -c "gh pr
# merge 123"`, `bash -c 'gh pr merge 123'`, `eval "gh pr merge 123"` -- is
# UNCHANGED by either pass (neither `-c` nor `eval`'s argument is in the
# whitelist below) and still denies exactly as before. Masking only ever
# narrows what this ONE check can see; it never widens what it misses.
# =============================================================================

# Mask the BODY of a heredoc whose consuming command is `cat`, whose
# delimiter is quoted (`cat <<'EOF' ... EOF` / `cat <<"EOF" ... EOF`, and the
# `<<-` tab-stripping variant), AND whose `cat` invocation is captured by a
# command substitution ($()/backtick) that is the value of a known
# non-executing text-data flag. This is exactly the CLAUDE.md-documented
# idiom for multi-line commit/PR-body text: `git commit -m "$(cat <<'EOF'
# ... EOF)"`. A QUOTED delimiter guarantees bash performs no
# $()/backtick/$VAR expansion within the body, so the body is 100% literal
# text; the flag-capture requirement guarantees that literal text is used as
# inert data (a message/title/search value) rather than executed.
#
# Deliberately narrower than "any heredoc" on TWO axes:
#   1. A heredoc feeding an INTERPRETER (`bash <<'EOF' ... EOF`, `sh <<EOF`)
#      is genuinely live code, so masking is gated on the word immediately
#      before `<<` being `cat`.
#   2. `cat` never executes its body, but its stdout can still be routed INTO
#      a shell on the same command line -- `cat <<'EOF' | bash`, or a captured
#      `eval "$(cat <<'EOF' ...)"`. Masking those would blind this
#      catastrophic-tier guard to a real invocation, so masking is ALSO gated
#      on `cat` being captured into a text-data flag's $()/backtick value (see
#      the `capre` confinement check inside the function).
#
# Mirrors the #5087 lesson: only a heredoc block that is PROVABLY CLOSED
# inside the buffer (its bare delimiter line is found) gets its body masked.
# An unterminated/unrecognized opener masks NOTHING and the raw text flows
# through unchanged to the check below.
mask_cat_heredoc_bodies() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)
        DQ = sprintf("%c", 34)
        BT = sprintf("%c", 96)
        # A cat-heredoc body is only PROVABLY inert when the cat invocation is
        # captured by a command substitution ($()/backtick) that is itself the
        # VALUE of a known non-executing text-data flag. `capre` must match the
        # tail of the opener-line text that sits immediately before the `cat`
        # token (see the confinement check below).
        capre = "(^|[ \t])(-m|--message|--body|--notes|--title|--comment|--search)[ \t]*=?[ \t]*(" DQ "|" SQ ")?[ \t]*([$][(]|" BT ")[ \t]*$"
    }
    { lines[NR] = $0 }
    END {
        nl = NR
        for (i = 1; i <= nl; i++) {
            line = lines[i]
            off = 1
            while (1) {
                p = index(substr(line, off), "<<")
                if (p == 0) break
                p = off + p - 1
                off = p + 2
                # Require the word immediately before `<<` (ignoring
                # trailing whitespace) to be a bare "cat".
                pre = substr(line, 1, p - 1)
                if (pre !~ /(^|[^A-Za-z0-9_])cat[ \t]*$/) continue
                # HARDENING (#5109 follow-up, PR #5115 review): the word before
                # `<<` being `cat` is NOT sufficient -- `cat` never executes its
                # own body, but its stdout can still reach a shell on the SAME
                # command line, so masking a body that a shell then runs makes
                # this catastrophic-tier guard blind to a real invocation:
                #   cat <<EOF | bash        # body piped straight into bash
                #   eval "$(cat <<EOF ...)" # body captured, then eval-executed
                # Only mask when the cat-heredoc is captured by a command
                # substitution ($()/backtick) that is the VALUE of a known
                # non-executing text-data flag (-m/--message/--body/--notes/
                # --title/--comment/--search) -- the CLAUDE.md-documented
                # `-m "$(cat <<'"'"'EOF'"'"' ... EOF)"` idiom -- so the body is
                # provably confined to inert text data and can never reach a
                # shell. A bare `cat <<EOF`, a piped `cat <<EOF | bash`, or an
                # `eval "$(cat <<EOF ...)"` fails this check and is left visible
                # to the merge-redirect grep, which denies exactly as before.
                before_cat = pre
                sub(/cat[ \t]*$/, "", before_cat)
                if (before_cat !~ capre) continue
                start = p + 2
                if (substr(line, start, 1) == "-") start++
                while (substr(line, start, 1) == " " || substr(line, start, 1) == "\t") start++
                qc = substr(line, start, 1)
                if (qc != SQ && qc != DQ) continue
                start++
                wordend = start
                while (substr(line, wordend, 1) ~ /^[A-Za-z0-9_]$/) wordend++
                if (wordend <= start) continue
                if (substr(line, wordend, 1) != qc) continue
                delim = substr(line, start, wordend - start)
                # The opener line must END after the quoted delimiter. Anything
                # trailing it routes cat stdout somewhere else (a pipe into a
                # shell, a redirect into a file), which is the class that must
                # stay visible to the merge-redirect grep.
                rest = substr(line, wordend + 1)
                if (rest ~ /[^ \t]/) continue
                closeat = 0
                for (j = i + 1; j <= nl; j++) {
                    trimmed = lines[j]
                    sub(/^[ \t]+/, "", trimmed)
                    if (trimmed == delim) { closeat = j; break }
                }
                if (closeat == 0) continue
                for (j = i + 1; j < closeat; j++) {
                    gsub(/./, "X", lines[j])
                }
                break
            }
        }
        out = lines[1]
        for (i = 2; i <= nl; i++) out = out "\n" lines[i]
        printf "%s", out
    }'
}

# Mask the quoted VALUE of known non-executing, text-only flags used by
# git/gh subcommands: -m/--message, --body, --notes, --title, --comment,
# --search. A near-duplicate of strip_literal_text() above (which is used
# only for decision-log redaction) with --search added, kept as a SEPARATE
# function so this decision-time masking can never change what
# strip_literal_text() logs. Same conservative floor as strip_literal_text():
# a span that still contains an unmasked `$(`/backtick (e.g. real command
# substitution, not yet neutralized by the heredoc pass above) is left
# completely untouched.
mask_data_flag_values() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)
        DQ = sprintf("%c", 34)
        re = "(^|[ \t\n])(--message|--body|--notes|--title|--comment|--search|-m)[ \t]*=?[ \t]*(" \
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

# Mask quoted POSITIONAL arguments (no preceding flag name) to a small
# allowlist of known non-executing commands/scripts (issue #5155, extending
# the #5115 fix above). mask_data_flag_values only recognizes text following
# a named flag; it has no effect on a script whose free-text arguments are
# purely positional, e.g. `./.loom/scripts/check-duplicate.sh "TITLE"
# "DESCRIPTION"` or `grep -n "pattern" file`. Neither `grep`/`egrep`/`fgrep`/
# `rg` nor check-duplicate.sh ever EXECUTES a positional argument -- they only
# read it as search/dedup text -- so masking a quoted argument immediately
# following one of these command names (optionally after short flags, e.g.
# `grep -n "..."`) can never blind this catastrophic-tier guard to a real
# invocation. Deliberately narrow allowlist, same "deliberately narrow"
# convention documented above mask_cat_heredoc_bodies(): a command that WRAPS
# the phrase and then executes it -- `sh -c "gh pr merge 123"`, `bash -c
# '...'`, `eval "..."` -- is NOT in this allowlist and stays fully visible to
# the merge-redirect grep below, exactly as before.
#
# Masks EVERY quoted argument that directly, consecutively follows the
# command+flags (separated only by whitespace) -- not just the first -- so
# multi-positional-arg scripts like check-duplicate.sh's `TITLE DESCRIPTION`
# signature get both arguments masked. Masking stops at the first token that
# is not a quoted string (a bare filename, `&&`, `|`, etc.), leaving anything
# after that boundary -- including a real `gh pr merge` invocation chained
# onto the same line -- fully visible.
mask_command_positional_args() {
    printf '%s' "$1" | awk '
    BEGIN {
        SQ = sprintf("%c", 39)
        DQ = sprintf("%c", 34)
        # Command-name allowlist: known non-executing commands/scripts whose
        # positional string arguments are search/dedup text, never live shell
        # syntax. Extend only when another read-only positional-arg consumer
        # causes a real false positive (see #5155).
        cmdre = "(grep|egrep|fgrep|rg|\\./\\.loom/scripts/check-duplicate\\.sh)"
        # Zero or more short/long flags between the command name and the
        # first quoted positional argument (e.g. `grep -n`, `rg -i`,
        # `check-duplicate.sh --include-merged-prs --issue 5155`).
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
        local enabled=false raw
        if [[ -n "$REPO_ROOT" ]]; then
            # Migrated to the shared tier resolver (#4063), kept consistent with
            # guard-destructive-generic.sh's decision_log_enabled(). INVERSE
            # polarity: only an explicit boolean `true` enables; a missing/null
            # key, a non-boolean value, or malformed JSON stays OFF via the
            # "false" default and the `|| raw=false` fallback.
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
# Workspace-registry guard toggle — default ON (issue #4326).
#
# The `loom-daemon workspace add|remove|set-priority` ask (below) is a useful
# default backstop against accidentally mutating the operator's real
# ~/.loom/workspaces.json, but — like every other category guard in this
# file — a repo/session can opt out via the same
# `guards.<name>` config key + `LOOM_GUARD_<NAME>` env override convention
# used throughout `guard-destructive-generic.sh` (sql_guard_enabled(),
# cloud_guard_enabled(), …). This is INDEPENDENT of `LOOM_WORKSPACES_PATH`
# (the sanctioned scratch-registry seam that allows a specific mutating
# command through regardless of this toggle) — this toggle instead disables
# the ask machinery entirely, for an operator who finds it pure friction.
#
# Resolution order (highest precedence first):
#   1. LOOM_GUARD_WORKSPACE_REGISTRY env var (0/false/no disables, 1/true/yes
#      forces on). Overrides config.
#   2. .loom/config.json (or a higher config-resolver tier) ->
#      guards.workspaceRegistry (default true when absent)
#   3. Default: true (guard on)
#
# Resolved LAZILY (only once a mutating `workspace` subcommand has already
# matched) and cached, mirroring every other toggle in this file. The config
# read is best-effort: any parse failure falls through to guard-ON.
# =============================================================================
_WORKSPACE_REGISTRY_GUARD_CACHE=""
workspace_registry_guard_enabled() {
    if [[ -z "$_WORKSPACE_REGISTRY_GUARD_CACHE" ]]; then
        local enabled=true raw
        if [[ -n "$REPO_ROOT" ]]; then
            raw=$(loom_config_get "$REPO_ROOT" "guards.workspaceRegistry" "true" 2>/dev/null) || raw=true
            [[ "$raw" == "false" ]] && enabled=false
        fi
        case "${LOOM_GUARD_WORKSPACE_REGISTRY:-}" in
            0|false|no)  enabled=false ;;
            1|true|yes)  enabled=true ;;
        esac
        _WORKSPACE_REGISTRY_GUARD_CACHE="$enabled"
    fi
    [[ "$_WORKSPACE_REGISTRY_GUARD_CACHE" == "true" ]]
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

# Match against a MASKED copy of $COMMAND (issue #5109, extended by #5155) so
# a mention of the phrase inside a cat-heredoc commit-message body, a
# --search/--body/-m/etc quoted value, or a quoted POSITIONAL argument to a
# known non-executing command (grep/rg/check-duplicate.sh) doesn't
# false-positive as a real invocation. See the masking functions' doc
# comments above for exactly what is (and is NOT) neutralized.
GH_PR_MERGE_SCAN_TEXT=$(mask_data_flag_values "$(mask_command_positional_args "$(mask_cat_heredoc_bodies "$COMMAND")")")
if echo "$GH_PR_MERGE_SCAN_TEXT" | grep -qE 'gh\s+pr\s+merge'; then
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
# LOOM: Block pip install -e inside worktrees (issue #2495, hardened by #4079)
#
# Editable pip installs overwrite a global .pth file in site-packages.
# When multiple builders run in parallel worktrees, each 'pip install -e .'
# clobbers the .pth to point at its own worktree, causing all other Python
# processes to import from the wrong source tree.
#
# The second, worse failure mode (incident #4079, which motivated epic #4081):
# an editable install also drops FROZEN `<name> = <module>:main` console scripts
# into ~/.local/bin. Those outlive the package — they keep shadowing whatever is
# later installed under the same name on PATH, which is how a stale
# `pip install -e loom-tools` kept shadowing the Rust `loom-daemon` binary long
# after the Python package stopped being used. `loom-daemon-update.sh` warns
# about survivors; this guard stops new ones being created.
#
# This guard is NOT specific to Loom's own (now Python-free) tree — it protects
# any Python repo under Loom orchestration. The supported ways to run a
# worktree's own code without an editable install:
#   - `.loom/scripts/run-tests.sh`, which prepends the worktree's source root to
#     PYTHONPATH before invoking pytest; and
#   - `loom-daemon agent-spawn`, which pins PYTHONPATH into the spawned session
#     for repos whose worktree has a src/ layout it recognizes.
# =============================================================================

WORKTREE_PATH="${LOOM_WORKTREE_PATH:-}"
if [[ -n "$WORKTREE_PATH" ]]; then
    if echo "$COMMAND" | grep -qE '(pip|pip3|uv pip)\s+install\s+.*-e\s' || \
       echo "$COMMAND" | grep -qE '(pip|pip3|uv pip)\s+install\s+.*--editable\s'; then
        deny "BLOCKED: 'pip install -e' is not allowed inside worktrees. Editable installs overwrite the global .pth file, breaking parallel builders (issue #2495), and leave frozen console scripts on PATH that shadow later installs (incident #4079). Run the worktree's own code via '.loom/scripts/run-tests.sh' (it sets PYTHONPATH for you) instead of an editable install." "loom:pip-install-editable-worktree"
    fi
fi

# =============================================================================
# LOOM: Ask before registry-mutating `loom-daemon workspace` commands
# (Issue #4326)
#
# `loom-daemon workspace add|remove|set-priority` mutate the machine-level
# workspace registry (Issue #3926), normally `~/.loom/workspaces.json` — a
# SHARED file, not scoped to any one repo/worktree/session. An ad-hoc
# verification step (a builder/auditor sweep exercising registry behavior)
# that invokes the real CLI without redirecting it leaves stray/incorrect
# entries in the OPERATOR's actual registry: #4326 found a leaked
# `/private/tmp/mig-test` entry sitting at dispatch priority 3 — ahead of
# every real managed repo — for most of a day, because the directory was
# deleted after registration without a matching `workspace remove`.
#
# `LOOM_WORKSPACES_PATH` (`loom-daemon/src/workspace_registry.rs`) already
# exists as the sanctioned scratch-registry seam — every daemon unit test
# points at it instead of the real file (see
# `defaults/docs/machine-dispatcher.md`). So this guard ASKS (never a hard
# deny — an operator legitimately managing their own real registry must still
# be able to proceed) whenever a mutating `workspace` subcommand runs with
# NEITHER the env var already set in the environment NOR an inline
# `LOOM_WORKSPACES_PATH=` assignment on the same command line. `workspace
# list` is read-only and is NEVER matched by this guard.
# =============================================================================

if echo "$COMMAND" | grep -qE '(^|[/[:space:];&|])loom-daemon[[:space:]]+workspace[[:space:]]+(add|remove|set-priority)([[:space:]]|$)'; then
    if workspace_registry_guard_enabled; then
        if [[ -z "${LOOM_WORKSPACES_PATH:-}" ]] && ! echo "$COMMAND" | grep -qE 'LOOM_WORKSPACES_PATH='; then
            ask "This mutates the machine-level workspace registry ('loom-daemon workspace add/remove/set-priority') — by default that is the operator's REAL ~/.loom/workspaces.json, shared across every repo/session (Issue #4326: a leaked test entry once sat at top dispatch priority for most of a day). If this is a test/verification step, prefix the command with LOOM_WORKSPACES_PATH=<scratch-file> to isolate it from the real registry. If this IS an intentional real-registry change, confirm to proceed."
        fi
    fi
fi

# =============================================================================
# ALLOW - Everything else passes through
# =============================================================================

exit 0
