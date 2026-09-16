#!/usr/bin/env bash
# check-guards-installed.sh — assert every hook a repo's .claude/settings.json
# wires is actually present and runnable (issue #7761).
#
# Why: `.claude/settings.json` names hook scripts under `.loom/hooks/`, but
# nothing ever verified that those files EXIST and are EXECUTABLE. A missing or
# non-executable guard used to fail open silently — indistinguishable from a
# guard that ran and allowed — which matters because Loom agents run with
# `--dangerously-skip-permissions`, so the hook is the only thing between an
# agent and a destructive command. That silent-allow hole is closed at run time
# by `defaults/hooks/hook-wiring.sh`; this script is the other half: it detects
# the broken install BEFORE an agent runs, rather than at the moment a guard was
# needed and absent.
#
# The installed-guard surface drifting from `defaults/hooks/` is a demonstrated
# failure mode, not a hypothetical one: #7416 (a fix that never reached the
# installed copy), #7423 (a stale installed copy), #7752 (the same class for
# `.loom/docs/`). `resync-installed.sh` already `chmod +x`es hook files as it
# syncs, so a lost executable bit self-heals on the NEXT resync — but nothing
# reported the broken window in between, which is exactly when agents run.
#
# What it checks, for the repo at ROOT:
#   1. Every `.loom/hooks/<name>` referenced by a hook command in
#      `<ROOT>/.claude/settings.json` resolves to a file that exists and is
#      executable.
#   2. The `hook-wiring.sh` launcher those commands route through is itself
#      present and executable (it is referenced like any other hook, so this
#      falls out of check 1 — called out here because its absence degrades every
#      wiring to the inline fallback).
#   3. A hook absent from `<ROOT>/.loom/hooks/` but present in the machine-level
#      checkout (`${LOOM_HOME:-$HOME/.local/share/loom}/defaults/hooks/`) is
#      reported as DEGRADED, not BROKEN — it still runs, from the machine copy,
#      exactly as `hook-wiring.sh`'s rung 4 arranges.
#
# A repo that is NOT a Loom workspace (no `.loom/` directory and no
# `.loom-project/project.json`) is a clean no-op: reported as "not a Loom
# workspace", exit 0. Guards failing open there is correct, not a defect.
#
# Usage:
#   check-guards-installed.sh                  Check the repo containing $PWD.
#   check-guards-installed.sh --root <dir>     Check <dir> instead.
#   check-guards-installed.sh --fix            chmod +x any present-but-not-
#                                              executable hook, then re-check.
#   check-guards-installed.sh --quiet          Print only problems.
#   check-guards-installed.sh --self-test      Run this script's own tests.
#   check-guards-installed.sh --help
#
# Exit codes:
#   0 - every wired hook is runnable (including "not a Loom workspace" and
#       "no hook wiring in settings.json", both of which are legitimate).
#   1 - usage error, or ROOT is not a directory.
#   2 - one or more wired hooks are MISSING or NOT EXECUTABLE. Every offender
#       is named, with the repair command.
#
# Notes:
#   - DEGRADED (running from the machine-level checkout) does not fail the
#     check: coverage is intact. It is still printed, because a per-repo install
#     that lost its copies is worth knowing about.
#   - Deliberately makes no forge calls, reads no config, and needs no `jq`
#     (it falls back to a grep-based extraction) so it can run in the middle of
#     an install, before anything else is in place.

set -uo pipefail

ROOT=""
QUIET=0
FIX=0
SELF_TEST=0

RED='\033[0;31m'
YELLOW='\033[0;33m'
GREEN='\033[0;32m'
NC='\033[0m'
if [[ ! -t 1 ]]; then
    RED=''; YELLOW=''; GREEN=''; NC=''
fi

usage() {
    sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root) ROOT="${2:-}"; shift 2 ;;
        --fix) FIX=1; shift ;;
        --quiet) QUIET=1; shift ;;
        --self-test) SELF_TEST=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "check-guards-installed: unknown argument '$1'" >&2; exit 1 ;;
    esac
done

say() { [[ "$QUIET" -eq 1 ]] || printf '%b\n' "$*"; }
problem() { printf '%b\n' "$*" >&2; }

# ---------- hook-name extraction ----------
#
# Pull every `.loom/hooks/<name>` reference out of a settings.json. jq is
# preferred (it reads only hook COMMAND strings); the grep fallback scans the
# whole file, which over-reports at worst — and over-reporting a hook that ought
# to be installed is the safe direction for this check.
extract_hook_names() {
    local settings="$1"
    [[ -f "$settings" ]] || return 0
    local raw=""
    if command -v jq >/dev/null 2>&1; then
        raw="$(jq -r '[.. | objects | .command? // empty] | .[]' "$settings" 2>/dev/null)"
    fi
    if [[ -z "$raw" ]]; then
        raw="$(cat "$settings" 2>/dev/null)"
    fi
    printf '%s\n' "$raw" \
        | grep -o '\.loom/hooks/[A-Za-z0-9._-]*\.sh' 2>/dev/null \
        | sed 's|.*/||' \
        | sort -u
}

is_loom_workspace() {
    [[ -d "$1/.loom" ]] || [[ -f "$1/.loom-project/project.json" ]]
}

# ---------- the check ----------
#
# Prints a per-hook status line and returns 0 (all runnable) or 2 (at least one
# MISSING / NOT EXECUTABLE).
run_check() {
    local root="$1"
    local settings="$root/.claude/settings.json"
    local machine_hooks="${LOOM_HOME:-$HOME/.local/share/loom}/defaults/hooks"

    if [[ ! -d "$root" ]]; then
        problem "check-guards-installed: '$root' is not a directory."
        return 1
    fi

    if ! is_loom_workspace "$root"; then
        say "check-guards-installed: ${GREEN}OK${NC} — $root is not a Loom workspace (no .loom/); hooks correctly fail open here."
        return 0
    fi

    if [[ ! -f "$settings" ]]; then
        say "check-guards-installed: ${GREEN}OK${NC} — $root has no .claude/settings.json; no project-level hook wiring to verify."
        return 0
    fi

    local names
    names="$(extract_hook_names "$settings")"
    if [[ -z "$names" ]]; then
        say "check-guards-installed: ${GREEN}OK${NC} — no .loom/hooks/ entries wired in $settings (machine-level wiring, or none)."
        return 0
    fi

    local rc=0 name path fixed
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        path="$root/.loom/hooks/$name"
        if [[ -x "$path" ]]; then
            say "  ${GREEN}ok${NC}        $name"
            continue
        fi
        if [[ -f "$path" ]]; then
            if [[ "$FIX" -eq 1 ]] && chmod +x "$path" 2>/dev/null && [[ -x "$path" ]]; then
                say "  ${GREEN}fixed${NC}     $name (restored the executable bit)"
                continue
            fi
            fixed="chmod +x '$path'"
            problem "  ${RED}NOT EXEC${NC}  $name — present but not executable. Repair: $fixed"
            rc=2
            continue
        fi
        if [[ -f "$machine_hooks/$name" ]]; then
            say "  ${YELLOW}degraded${NC}  $name — no per-repo copy; runs from $machine_hooks/$name"
            continue
        fi
        problem "  ${RED}MISSING${NC}   $name — not at $path and not in the machine checkout ($machine_hooks). Repair: re-run scripts/install-loom.sh"
        rc=2
    done <<< "$names"

    if [[ "$rc" -eq 0 ]]; then
        say "check-guards-installed: ${GREEN}OK${NC} — every hook wired in $settings is runnable."
    else
        problem "check-guards-installed: ${RED}BROKEN GUARD INSTALL${NC} in $root — see the offenders above. Until repaired, hook-wiring.sh DENIES PreToolUse tool calls in this workspace rather than allowing them unguarded (#7761)."
    fi
    return "$rc"
}

# ---------- self-test ----------
self_test() {
    local tmp pass=0 fail=0
    tmp="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" EXIT

    _expect() { # <label> <expected-rc> <actual-rc>
        if [[ "$2" == "$3" ]]; then
            printf '  ok   %s\n' "$1"; pass=$((pass + 1))
        else
            printf '  FAIL %s (expected rc %s, got %s)\n' "$1" "$2" "$3"; fail=$((fail + 1))
        fi
    }

    _fixture() { # <dir> -> a Loom workspace wiring guard-destructive.sh
        mkdir -p "$1/.loom/hooks" "$1/.claude"
        cat > "$1/.claude/settings.json" <<'JSON'
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "bash -c 'exec \"$W/.loom/hooks/guard-destructive.sh\"'" }
        ]
      }
    ]
  }
}
JSON
    }

    # 1. Not a Loom workspace -> clean no-op.
    mkdir -p "$tmp/plain"
    ( QUIET=1; run_check "$tmp/plain" >/dev/null 2>&1 ); _expect "non-Loom workspace passes" 0 $?

    # 2. Wired hook present and executable -> pass.
    _fixture "$tmp/good"
    printf '#!/bin/sh\nexit 0\n' > "$tmp/good/.loom/hooks/guard-destructive.sh"
    chmod +x "$tmp/good/.loom/hooks/guard-destructive.sh"
    ( QUIET=1; run_check "$tmp/good" >/dev/null 2>&1 ); _expect "installed executable hook passes" 0 $?

    # 3. Wired hook present but NOT executable -> fail.
    _fixture "$tmp/noexec"
    printf '#!/bin/sh\nexit 0\n' > "$tmp/noexec/.loom/hooks/guard-destructive.sh"
    chmod -x "$tmp/noexec/.loom/hooks/guard-destructive.sh"
    ( QUIET=1; run_check "$tmp/noexec" >/dev/null 2>&1 ); _expect "non-executable hook fails" 2 $?

    # 4. ...and --fix repairs it.
    ( QUIET=1; FIX=1; run_check "$tmp/noexec" >/dev/null 2>&1 ); _expect "--fix restores the executable bit" 0 $?
    if [[ -x "$tmp/noexec/.loom/hooks/guard-destructive.sh" ]]; then
        printf '  ok   --fix left the file executable\n'; pass=$((pass + 1))
    else
        printf '  FAIL --fix left the file executable\n'; fail=$((fail + 1))
    fi

    # 5. Wired hook missing entirely (and no machine copy) -> fail.
    _fixture "$tmp/missing"
    ( QUIET=1; LOOM_HOME="$tmp/no-such-checkout"; run_check "$tmp/missing" >/dev/null 2>&1 )
    _expect "missing hook fails" 2 $?

    # 6. Missing per-repo copy but present machine-level -> degraded, passes.
    mkdir -p "$tmp/machine/defaults/hooks"
    printf '#!/bin/sh\nexit 0\n' > "$tmp/machine/defaults/hooks/guard-destructive.sh"
    ( QUIET=1; LOOM_HOME="$tmp/machine"; run_check "$tmp/missing" >/dev/null 2>&1 )
    _expect "machine-level fallback is degraded, not broken" 0 $?

    printf 'check-guards-installed --self-test: %d passed, %d failed\n' "$pass" "$fail"
    [[ "$fail" -eq 0 ]]
}

if [[ "$SELF_TEST" -eq 1 ]]; then
    self_test
    exit $?
fi

if [[ -z "$ROOT" ]]; then
    ROOT="$(cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd)" || ROOT=""
    [[ -n "$ROOT" ]] || ROOT="$PWD"
fi

run_check "$ROOT"
exit $?
