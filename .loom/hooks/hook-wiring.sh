#!/usr/bin/env bash
# hook-wiring.sh — the resolve-and-run launcher every Loom hook entry in a
# repo's `.claude/settings.json` goes through (issue #7761).
#
# Usage (from a settings.json hook entry, never by hand):
#     hook-wiring.sh <HookEventName> <hook-script-name>
#   e.g.
#     hook-wiring.sh PreToolUse guard-destructive.sh
#
# stdin (the hook payload) is passed through untouched to whichever hook
# script this ends up exec'ing.
#
# ── The bug this exists to fix (#7761) ────────────────────────────────────────
# Every wired hook used to be a self-contained one-liner of the shape:
#
#     bash -c 'ROOT=$(cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." \
#       2>/dev/null && pwd) && [ -x "$ROOT/.loom/hooks/guard-destructive.sh" ] \
#       && exec "$ROOT/.loom/hooks/guard-destructive.sh" || exit 0'
#
# `exit 0` on a PreToolUse hook means ALLOW. So the command was allowed —
# silently, with no stderr, no log line, nothing distinguishing it from a guard
# that ran and decided to allow — whenever:
#
#   * `git rev-parse --git-common-dir` failed  → `$ROOT` empty → the `-x` test
#     ran against the absolute path `/.loom/hooks/…`, which can never succeed;
#   * the guard script was missing (never installed, or stripped by an
#     uninstall/migration);
#   * the guard script was present but had lost its executable bit (an archive
#     round-trip, a `cp` under a restrictive umask, a bad resync).
#
# Failing open is CORRECT for a repo that does not use Loom — a hard failure
# there would break every tool call for someone who never opted in. That is not
# in dispute. The bug was that the two cases are DISTINGUISHABLE and were not
# distinguished: a workspace carrying a `.loom/` directory is asserting that it
# EXPECTS guards, and Loom agents run with `--dangerously-skip-permissions`,
# which is exactly the configuration where the hook is the only thing between an
# agent and a destructive command. The installed-guard surface drifting from
# `defaults/hooks/` is a demonstrated failure mode, not a hypothetical one
# (#7416, #7423, and #7752 for the same class on `.loom/docs/`).
#
# ── The policy this implements ────────────────────────────────────────────────
# Resolution is a strict ladder. Each rung is tried in order; the first one that
# can actually RUN the hook wins, so a deny is only ever reached when no copy of
# the hook exists anywhere:
#
#   1. `$ROOT/.loom/hooks/<name>` is executable   → exec it. (Unchanged
#      behaviour, and the overwhelmingly common case: one `[ -x ]` test.)
#   2. The workspace is NOT a Loom workspace      → exit 0, silently. (Also
#      unchanged: a non-Loom repo never gets a diagnostic, let alone a deny.)
#   3. The hook is present but NOT executable     → run it anyway, via
#      `bash <path>` (which needs only read permission), and report the broken
#      install on stderr + in the hook error log. This is the single most likely
#      real-world breakage (#7416/#7423-class drift) and it now costs ZERO
#      coverage — the guard still makes the decision it always would have.
#   4. The repo carries no copy, but the machine-level checkout has one →
#      either the user-scope wiring already runs it (then exit 0 quietly — it is
#      covered, and firing here too would double-report every decision), or it
#      does not, in which case run the machine-level copy and report.
#   5. Nothing anywhere → the workspace expects a guard and has none:
#      **PreToolUse → DENY** (see the decision note below); any other event →
#      report loudly on stderr + log and allow, because those events have no
#      deny channel at all.
#
# ── Decision: fail CLOSED on a PreToolUse hook that cannot be found ───────────
# #7761 left "deny" vs "allow with a loud warning" as an explicit, deliberate
# choice. This file chooses DENY for `PreToolUse`, for three reasons:
#
#   * It is the established principle for this guard surface. ADR-0016
#     (`docs/adr/0016-write-target-confinement-approach.md`) states it for the
#     guard's own classification logic: "'I don't understand this command' never
#     falls through to an allow." "I have no guard to ask" is strictly weaker
#     evidence than "I asked and could not classify"; it must not be treated
#     more permissively.
#   * The wedge objection is answered by the ladder, not waved away. The
#     objection to fail-closed is that it can brick a workspace. Rungs 3 and 4
#     above remove the two recoverable breakages from the deny path entirely —
#     a lost `+x` and a migrated (copy-free) repo both keep full coverage and
#     never deny. What is left is a genuinely absent guard, i.e. an install that
#     is broken badly enough that stopping is the correct outcome for a safety
#     control.
#   * A silent allow is unfalsifiable; a deny is self-reporting. The deny reason
#     names the missing path and the exact repair command, so the failure
#     announces itself instead of being discovered after the damage.
#
# Escape hatch: `LOOM_GUARD_WIRING_FAILOPEN=1` in the environment downgrades rung
# 5's deny to the same loud warn-and-allow the non-PreToolUse events get. It is
# deliberately an ENVIRONMENT variable and deliberately NOT a `guards.*` config
# key: a config key lives in a committed file, so a single PR could restore the
# silent-allow hole for everyone, which is the exact failure mode this file
# exists to close. An env var must be set by the operator on the session's own
# process, which is an operator act, not a repository change.
#
# ── Contract ─────────────────────────────────────────────────────────────────
# Same contract as every guard in this repo: NEVER exits non-zero. A deny is
# expressed the way every other Loom guard expresses it — exit 0 with a
# `hookSpecificOutput.permissionDecision` JSON document on stdout — so this
# launcher can never wedge Claude Code in a retry loop. Emitted without `jq`, on
# purpose: a host missing `jq` must still get the decision.

set -u

EVENT="${1:-}"
NAME="${2:-}"

# Machine-level checkout that `provision-hooks.sh` wires user-scope entries
# against (same default as `_phook_cmd`'s `${LOOM_HOME:-$HOME/.local/share/loom}`).
LOOM_HOME_DIR="${LOOM_HOME:-$HOME/.local/share/loom}"

# A missing/garbled invocation is a defect in settings.json, not in the
# workspace — report it and allow, rather than denying every tool call over a
# wiring typo we cannot act on anyway.
if [[ -z "$EVENT" || -z "$NAME" ]]; then
    echo "[loom] hook-wiring.sh: called without <HookEventName> <hook-script-name>; allowing." >&2
    exit 0
fi

# ---------- resolve the workspace root ----------
#
# LOOM_PROJECT_ROOT wins when set: the settings.json wrapper (and
# provision-hooks.sh's user-scope wrapper) already resolved the worktree-aware
# root, and passing it through means this launcher agrees with the caller rather
# than re-deriving a possibly different answer.
#
# Otherwise: `git rev-parse --git-common-dir`/.. resolves the MAIN checkout even
# from inside a linked worktree, which is what every other Loom hook uses. When
# git is unavailable or the cwd is not a repository, that yields the empty string
# — the #7761 degenerate path — so fall back to CLAUDE_PROJECT_DIR (Claude Code
# exports the project directory) and finally to the cwd. Resolving SOMETHING here
# is what keeps the empty-ROOT case on the same ladder as every other failure
# instead of on a third, silent path of its own.
#
# The `git rev-parse` result is captured and emptiness-tested BEFORE the `cd`,
# which the pre-#7761 wiring did not do — and that detail is the whole reason the
# degenerate path was invisible. `cd "$(git rev-parse …)/.."` with an empty
# substitution is `cd "/.."`, which SUCCEEDS and resolves to `/`: ROOT came back
# as the filesystem root rather than empty, the `-x` test then ran against
# `/.loom/hooks/<name>` (which can never exist), the `.loom/` check found no
# `/.loom`, and the hook fell through to a silent allow that looked exactly like
# "not a Loom workspace". Testing the substitution first is what turns that into
# a real fallback chain.
ROOT="${LOOM_PROJECT_ROOT:-}"
if [[ -z "$ROOT" ]]; then
    GIT_COMMON_DIR="$(git rev-parse --git-common-dir 2>/dev/null)" || GIT_COMMON_DIR=""
    if [[ -n "$GIT_COMMON_DIR" ]]; then
        ROOT="$(cd "$GIT_COMMON_DIR/.." 2>/dev/null && pwd)" || ROOT=""
    fi
fi
[[ -n "$ROOT" ]] || ROOT="${CLAUDE_PROJECT_DIR:-}"
[[ -n "$ROOT" ]] || ROOT="$PWD"

HOOK="$ROOT/.loom/hooks/$NAME"
MACHINE_HOOK="$LOOM_HOME_DIR/defaults/hooks/$NAME"

# ---------- rung 1: the normal path ----------
if [[ -x "$HOOK" ]]; then
    exec "$HOOK"
fi

# ---------- rung 2: not a Loom workspace -> silent allow (unchanged) ----------
#
# Detection mirrors the workspace gate `_phook_cmd` (scripts/install/
# provision-hooks.sh) already uses — a `.loom/` directory (legacy/current
# layout) or a `.loom-project/project.json` (migrated layout) — rather than
# inventing a third convention. `.loom/` as a DIRECTORY, not
# `.loom/config.json`, because a half-installed repo that has `.loom/hooks/` but
# no config is exactly the broken state this file must still speak up about.
is_loom_workspace() {
    [[ -d "$ROOT/.loom" ]] || [[ -f "$ROOT/.loom-project/project.json" ]]
}

if ! is_loom_workspace; then
    exit 0
fi

# ---------- reporting helpers (only reachable in a Loom workspace) ----------

HOOK_ERROR_LOG="$ROOT/.loom/logs/hook-errors.log"

log_line() {
    mkdir -p "$(dirname "$HOOK_ERROR_LOG")" 2>/dev/null || true
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [hook-wiring] $1" >> "$HOOK_ERROR_LOG" 2>/dev/null || true
}

# Both channels, always together: stderr so a human watching the session sees it
# immediately, the log so a headless sweep leaves evidence behind. "No signal at
# all" was the whole bug — one channel is not enough to call it fixed.
report() {
    echo "[loom] BROKEN GUARD INSTALL: $1" >&2
    log_line "$1"
}

# Emit a PreToolUse deny decision without jq (a host missing jq must still get
# the decision). $REASON is composed entirely from fixed text plus filesystem
# paths, so the only escaping a path can need is for `\` and `"`.
json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

emit_deny() {
    local reason
    reason="$(json_escape "$1")"
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$reason"
    exit 0
}

# ---------- rung 3: present but not executable -> run it anyway ----------
#
# `bash <path>` needs only read permission, so a lost `+x` costs ZERO guard
# coverage. Reported every time (not once) on purpose: the repair is one
# `chmod +x` and the state should not be allowed to become background noise.
#
# Known, accepted narrow window: the user-scope wrapper's transition-dedup step
# (`_phook_cmd`, scripts/install/provision-hooks.sh) defers to the project entry
# only when the per-repo copy is EXECUTABLE, so in this one state it does not
# defer and the machine-level copy runs too — the same guard decides twice, and
# the decision log carries two lines. That is deliberately preferred over
# standing down here: the per-repo copy is the authoritative one for this repo,
# running it costs nothing but a duplicate log line, and the state is loudly
# reported and one `chmod +x` from gone.
if [[ -r "$HOOK" ]]; then
    report "$HOOK is not executable (lost its +x bit). Running it via 'bash' so the hook still applies. Repair: chmod +x '$HOOK' (or run .loom/scripts/check-guards-installed.sh --fix)."
    exec bash "$HOOK"
fi

# ---------- rung 4: fall back to the machine-level checkout ----------
#
# A repo migrated to machine-level hooks (Epic #3835 Phase 6) carries NO
# `.loom/hooks/` copies by design; if a stale project-level entry survives the
# migration, rung 5 would deny a workspace whose guards are in fact running
# perfectly from the user-scope wiring. So when the machine-level copy is
# runnable, this rung covers both shapes:
#
#   * the user-scope entry for this hook is already wired → stand down quietly,
#     because that entry will run the same script and firing here too would
#     double-report every decision. Detected with the same fork-free
#     `case "$(<file)"` substring test `_phook_cmd` uses, including the `[ -f ]`
#     guard that keeps `$(<missing)` from writing to stderr;
#   * it is not wired → run the machine copy ourselves and report the
#     incomplete per-repo install.
#
# Deliberately nested under `[ -r "$MACHINE_HOOK" ]`: standing down for a
# user-scope entry whose target does not exist would just relocate the silent
# allow, which is the bug, not the fix.
if [[ -r "$MACHINE_HOOK" ]]; then
    USER_SETTINGS="$HOME/.claude/settings.json"
    if [[ -f "$USER_SETTINGS" ]]; then
        case "$(<"$USER_SETTINGS")" in
            *"/defaults/hooks/$NAME"*) exit 0 ;;
        esac
    fi
    report "$HOOK is missing; falling back to the machine-level copy at $MACHINE_HOOK. The hook still applies, but this repo's install is incomplete. Repair: re-run scripts/install-loom.sh."
    LOOM_PROJECT_ROOT="$ROOT" exec bash "$MACHINE_HOOK"
fi

# ---------- rung 5: nothing anywhere ----------

MSG="$NAME is not installed in this Loom workspace (looked for '$HOOK' and '$MACHINE_HOOK'). This workspace has a .loom/ directory, so it expects Loom's hooks to be present — a missing hook here is a broken install, not an opt-out. Repair: re-run scripts/install-loom.sh (or .loom/scripts/check-guards-installed.sh to see the full picture), then restart Claude Code."

report "$MSG"

if [[ "$EVENT" == "PreToolUse" && "${LOOM_GUARD_WIRING_FAILOPEN:-0}" != "1" ]]; then
    emit_deny "Loom guard hook $MSG  (Set LOOM_GUARD_WIRING_FAILOPEN=1 in the session's environment to downgrade this deny to a warning.)"
fi

# Non-PreToolUse events have no deny channel, and an explicit fail-open was
# requested for PreToolUse: allow, but never silently — report() above already
# wrote to stderr and the hook error log.
exit 0
