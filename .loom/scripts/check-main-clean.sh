#!/usr/bin/env bash
# check-main-clean.sh - Backstop guard: fail if the MAIN worktree is dirty.
#
# Detects the #2802 / #3513 failure mode where a Builder agent's cwd resets
# between tool calls and a repo-relative Write/Edit/Bash file operation lands
# in the MAIN repository working tree instead of the issue worktree under
# .loom/worktrees/issue-N. The builder guidance (builder.md / builder-worktree.md)
# is the primary defense (capture the absolute worktree path once, use absolute
# paths everywhere). This script is the BACKSTOP: run it after the builder phase
# and before opening a PR. If the main worktree has uncommitted changes, the
# builder has contaminated main and the sweep should abort.
#
# It resolves the MAIN worktree from anywhere — including from inside an issue
# worktree — via `git rev-parse --git-common-dir`, so it works whether invoked
# from the repo root or from a worktree.
#
# Usage:
#   ./.loom/scripts/check-main-clean.sh                  # check main worktree, exit 3 if dirty
#   ./.loom/scripts/check-main-clean.sh --snapshot FILE  # record main's porcelain state to FILE, exit 0
#   ./.loom/scripts/check-main-clean.sh --baseline FILE  # check main, ignoring dirt recorded in FILE
#   ./.loom/scripts/check-main-clean.sh --help           # show usage
#
# Baseline mechanism (#3648):
#   The plain (no-arg) check treats ANY uncommitted change on main as builder
#   contamination. That false-positives when the main worktree carried
#   pre-existing, unrelated dirt (a regenerated lockfile, a scratch edit) before
#   the sweep even started. The baseline mechanism lets a caller (e.g. /loom:sweep)
#   snapshot main's porcelain state ONCE at sweep start, then pass that snapshot
#   as a --baseline on each post-wave check. Only changes that appeared AFTER the
#   snapshot (set difference on exact porcelain lines) are treated as
#   contamination; pre-existing dirt is ignored.
#
#     ./.loom/scripts/check-main-clean.sh --snapshot .loom/main-clean-baseline.txt   # once, before wave 1
#     ./.loom/scripts/check-main-clean.sh --baseline .loom/main-clean-baseline.txt   # after each wave
#
#   Fail-safe: if the --baseline FILE is missing or unreadable, the check warns
#   to stderr and falls back to the plain whole-status behavior (never a silent
#   pass). With NO --baseline/--snapshot argument, behavior is byte-for-byte
#   identical to the pre-#3648 whole-status hard-fail — the builder.md /
#   builder-worktree.md manual call sites depend on that contract.
#
# Exit codes:
#   0 - Main worktree is clean (or, in --baseline mode, only pre-existing dirt
#       remains); or a --snapshot completed successfully.
#   2 - Usage error or could not resolve the main worktree (not a git repo).
#   3 - Main worktree is DIRTY: uncommitted changes detected (the contamination
#       this guard exists to catch). In --baseline mode, only NEW changes count.
#
# Notes:
#   - "Dirty" means `git status --porcelain` on the MAIN worktree returns any
#     output: staged, unstaged, or untracked files all count. A builder working
#     correctly in its own worktree leaves the main worktree pristine.
#   - Issue worktrees live under <main>/.loom/worktrees/ which is gitignored, so
#     their existence does NOT make the main worktree dirty.

set -euo pipefail

EXIT_OK=0
EXIT_USAGE=2
EXIT_MAIN_DIRTY=3

# ---- Loom-owned transient state paths (#3778) ----------------------------
# Runtime bookkeeping the sweep orchestrator itself writes under .loom/ during a
# run (checkpoints, the token pool, stats, locks, exit-codes, …). These are
# gitignored in Loom's *source* .gitignore, but a consumer repo's installed
# loom-managed .gitignore block can drift out of sync and omit newer entries —
# so the same transient surfaces as an untracked path and false-positives this
# backstop as "builder contamination" (observed in rjwalters/anvil, #3778).
# Rather than depend on the consumer's .gitignore being current, exclude the
# known Loom-owned transient paths internally, unconditionally. None of these is
# source a builder should ever be writing to main, so filtering them can never
# mask real contamination. Prefixes ending in "/" match a directory subtree; the
# others match an exact path. Keep roughly in sync with the loom-managed block in
# Loom's source .gitignore.
LOOM_OWNED_PREFIXES=(
    ".loom/sweep-checkpoint/"
    ".loom/sweep-run/"
    ".loom/tokens/"
    ".loom/accounts.env"
    ".loom/exit-codes/"
    ".loom/stats/"
    ".loom/CANARY"
    ".loom/spawn-loop.pid"
    ".loom/spawn-loop-state.json"
    ".loom/stop-spawn-loop"
    ".loom/locks/"
    ".loom/logs/"
    ".loom/worktrees/"
    ".loom-managed"
)

# is_loom_owned <repo-relative-path> -> 0 if the path is a Loom-owned transient.
is_loom_owned() {
    local path="$1" prefix
    for prefix in "${LOOM_OWNED_PREFIXES[@]}"; do
        if [[ "$prefix" == */ ]]; then
            [[ "$path" == "$prefix"* ]] && return 0
        else
            [[ "$path" == "$prefix" ]] && return 0
        fi
    done
    return 1
}

# filter_loom_owned <porcelain-text> -> the same text with Loom-owned transient
# lines removed. Parses each `git status --porcelain` v1 line (2 status chars +
# space + path; rename lines carry "old -> new" and are keyed on the new path).
filter_loom_owned() {
    local in="$1" line path out=""
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        path="${line:3}"
        if [[ "$path" == *" -> "* ]]; then
            path="${path##* -> }"
        fi
        path="${path%\"}"
        path="${path#\"}"
        if is_loom_owned "$path"; then
            continue
        fi
        out+="$line"$'\n'
    done <<< "$in"
    printf '%s' "${out%$'\n'}"
}

usage() {
    sed -n '2,54p' "$0" | sed 's/^# \{0,1\}//'
}

# ---- Argument parsing ----------------------------------------------------
# Modes: plain (no args, legacy back-compat), snapshot, baseline.
MODE="plain"
MODE_FILE=""

case "${1:-}" in
    -h|--help)
        usage
        exit "$EXIT_OK"
        ;;
    "")
        ;;
    --snapshot)
        if [[ -z "${2:-}" ]]; then
            echo "check-main-clean.sh: --snapshot requires a file argument" >&2
            echo "Run with --help for usage." >&2
            exit "$EXIT_USAGE"
        fi
        MODE="snapshot"
        MODE_FILE="$2"
        ;;
    --baseline)
        if [[ -z "${2:-}" ]]; then
            echo "check-main-clean.sh: --baseline requires a file argument" >&2
            echo "Run with --help for usage." >&2
            exit "$EXIT_USAGE"
        fi
        MODE="baseline"
        MODE_FILE="$2"
        ;;
    *)
        echo "check-main-clean.sh: unknown argument: $1" >&2
        echo "Run with --help for usage." >&2
        exit "$EXIT_USAGE"
        ;;
esac

# ---- Resolve the main worktree root --------------------------------------
# Resolve the canonical git common dir, then the main worktree root (its parent).
# This works from the repo root AND from inside any worktree.
common_dir=$(git rev-parse --git-common-dir 2>/dev/null || true)
if [[ -z "$common_dir" ]]; then
    echo "check-main-clean.sh: not inside a git repository" >&2
    exit "$EXIT_USAGE"
fi

# git-common-dir may be relative; resolve to an absolute path.
abs_common=$(cd "$common_dir" 2>/dev/null && pwd) || abs_common="$common_dir"
main_root=$(dirname "$abs_common")

if [[ ! -d "$main_root" ]]; then
    echo "check-main-clean.sh: could not resolve main worktree root from '$common_dir'" >&2
    exit "$EXIT_USAGE"
fi

status=$(git -C "$main_root" status --porcelain 2>/dev/null || true)

# Exclude Loom-owned transient state paths regardless of the consumer's
# .gitignore currency (#3778) — applied before snapshot/baseline/plain logic so
# every mode sees a consistently-filtered view.
status=$(filter_loom_owned "$status")

# ---- Snapshot mode: record and exit --------------------------------------
if [[ "$MODE" == "snapshot" ]]; then
    # Ensure the parent directory exists so callers can point at .loom/ transients.
    snap_dir=$(dirname "$MODE_FILE")
    if [[ -n "$snap_dir" && ! -d "$snap_dir" ]]; then
        mkdir -p "$snap_dir" 2>/dev/null || true
    fi
    if ! printf '%s\n' "$status" > "$MODE_FILE" 2>/dev/null; then
        echo "check-main-clean.sh: could not write snapshot to '$MODE_FILE'" >&2
        exit "$EXIT_USAGE"
    fi
    echo "check-main-clean.sh: snapshot of main worktree written to $MODE_FILE ($main_root)"
    exit "$EXIT_OK"
fi

# ---- Baseline mode: subtract pre-existing dirt ---------------------------
# Only lines that appear in the current status but NOT in the baseline are
# treated as new (builder) contamination. A missing/unreadable baseline falls
# back to the plain whole-status behavior (fail-safe, never a silent pass).
effective_status="$status"
ignored=0
if [[ "$MODE" == "baseline" ]]; then
    if [[ -r "$MODE_FILE" ]]; then
        # Set difference: current porcelain lines minus baseline lines.
        # comm -13 prints lines unique to the second (current) input.
        effective_status=$(comm -13 \
            <(sort "$MODE_FILE") \
            <(printf '%s\n' "$status" | sort) \
            | sed '/^$/d')
        pre_existing=$(printf '%s\n' "$status" | sed '/^$/d' | grep -c '' || true)
        new_count=$(printf '%s\n' "$effective_status" | sed '/^$/d' | grep -c '' || true)
        ignored=$(( pre_existing - new_count ))
        if [[ "$ignored" -lt 0 ]]; then ignored=0; fi
    else
        echo "WARNING: check-main-clean.sh: baseline file '$MODE_FILE' is missing or unreadable." >&2
        echo "         Falling back to whole-status check (fail-safe)." >&2
        effective_status="$status"
    fi
fi

if [[ -n "$effective_status" ]]; then
    echo "ERROR: MAIN worktree is dirty (uncommitted changes detected)." >&2
    echo "       Main worktree: $main_root" >&2
    echo "" >&2
    echo "       This usually means a builder wrote to the main repo instead of" >&2
    echo "       its issue worktree (cwd reset between tool calls — see #3513/#2802)." >&2
    echo "       Builders MUST capture the absolute worktree path once and use" >&2
    echo "       absolute paths for every Write/Edit/Bash file operation." >&2
    echo "" >&2
    if [[ "$ignored" -gt 0 ]]; then
        echo "       ($ignored pre-existing change(s) ignored via baseline $MODE_FILE)" >&2
        echo "" >&2
    fi
    echo "       Offending changes:" >&2
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        echo "         $line" >&2
    done <<< "$effective_status"
    exit "$EXIT_MAIN_DIRTY"
fi

if [[ "$ignored" -gt 0 ]]; then
    echo "check-main-clean.sh: main worktree carries only pre-existing dirt, no new contamination ($main_root)"
    echo "check-main-clean.sh: $ignored pre-existing change(s) ignored via baseline $MODE_FILE"
else
    echo "check-main-clean.sh: main worktree is clean ($main_root)"
fi
exit "$EXIT_OK"
