#!/usr/bin/env bash
# check-shell-syntax.sh - Assert installed shell surfaces PARSE (`bash -n`).
#
# #6162: an abandoned `git stash pop` conflict left live conflict markers
# (`<<<<<<< Updated upstream` / `=======` / `>>>>>>> Stashed changes`) in
# `defaults/scripts/spawn-claude.sh` in the primary checkout. Nothing asserted
# that installed shell surfaces actually parse, so the corruption sat
# undetected — and since `resync-installed.sh` copies `defaults/` into every
# consumer's `.loom/`, running a resync while that corruption was live would
# have shipped a non-parsing spawn script fleet-wide. This script is the
# narrow, cheap, deterministic guard the incident asked for: a shell script
# that fails `bash -n` is unambiguously broken, with no false positives.
#
# Two modes:
#   - Default (no --dir): scans the INSTALLED surfaces of the CURRENT repo's
#     primary worktree — `.loom/hooks/*.sh` (top-level only, matching the
#     resync/installer's own non-recursive hooks walk) and
#     `.loom/scripts/**/*.sh` (recursive, matching resync's recursive scripts
#     walk). Resolves the main worktree the same worktree-safe way
#     check-main-clean.sh does (`git rev-parse --git-common-dir`), so it can be
#     run from inside an issue worktree and still check the PRIMARY checkout's
#     installed copies, never the worktree's own (gitignored) .loom/.
#   - `--dir <path>` (repeatable): scans <path> recursively for `*.sh` files
#     instead of the installed-surface default. Used by resync-installed.sh to
#     validate the SOURCE tree (defaults/hooks, defaults/scripts) before
#     copying anything out of it (#6162 AC2) — checking the source is a
#     superset of "before any resync" (AC1): a source file that cannot parse
#     is refused before it ever reaches an installed copy.
#
# Usage:
#   ./.loom/scripts/check-shell-syntax.sh                 # scan installed .loom/hooks + .loom/scripts
#   ./.loom/scripts/check-shell-syntax.sh --dir <path>     # scan <path> recursively for *.sh (repeatable)
#   ./.loom/scripts/check-shell-syntax.sh --quiet          # only print failures (and the final summary)
#   ./.loom/scripts/check-shell-syntax.sh --help
#
# Exit codes:
#   0 - every scanned *.sh file parses cleanly (including "found zero files").
#   1 - usage error, or (default mode only) could not resolve the main worktree.
#   2 - one or more *.sh files failed `bash -n` — every offender is named with
#       its `bash -n` error.
#
# Notes:
#   - Only files literally named `*.sh` are scanned — this mirrors exactly
#     which files resync-installed.sh's hooks/scripts walks treat as shell
#     payload; a shebang-based scan would also have to define behavior for
#     extensionless executables that are not part of that walk (e.g.
#     `.loom/bin/loom`), which is a different surface than the one #6162
#     names as suggested acceptance criteria.
#   - `bash -n` only proves the file PARSES — it does not run it, so this is
#     safe to run against untrusted or half-written content and has no side
#     effects.

set -uo pipefail

EXIT_OK=0
EXIT_USAGE=1
EXIT_SYNTAX_FAIL=2

QUIET=0
DIRS=()

usage() {
    awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit "$EXIT_OK"
            ;;
        --dir)
            if [[ $# -lt 2 || -z "$2" ]]; then
                echo "check-shell-syntax.sh: --dir requires a directory argument" >&2
                exit "$EXIT_USAGE"
            fi
            DIRS+=("$2")
            shift 2
            ;;
        --quiet|-q)
            QUIET=1
            shift
            ;;
        *)
            echo "check-shell-syntax.sh: unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit "$EXIT_USAGE"
            ;;
    esac
done

# ---- Resolve which directories to scan ------------------------------------

SCAN_MODE="installed"
if [[ "${#DIRS[@]}" -gt 0 ]]; then
    SCAN_MODE="explicit"
    for d in "${DIRS[@]}"; do
        if [[ ! -d "$d" ]]; then
            echo "check-shell-syntax.sh: --dir path does not exist or is not a directory: $d" >&2
            exit "$EXIT_USAGE"
        fi
    done
fi

FILES=()

if [[ "$SCAN_MODE" == "explicit" ]]; then
    for d in "${DIRS[@]}"; do
        while IFS= read -r -d '' f; do
            FILES+=("$f")
        done < <(find -L "$d" -type f -name '*.sh' -print0 | sort -z)
    done
else
    # Resolve the main worktree root the same worktree-safe way
    # check-main-clean.sh does, so this can be invoked from inside an issue
    # worktree and still check the PRIMARY checkout's installed copies.
    common_dir=$(git rev-parse --git-common-dir 2>/dev/null || true)
    if [[ -z "$common_dir" ]]; then
        echo "check-shell-syntax.sh: not inside a git repository" >&2
        exit "$EXIT_USAGE"
    fi
    abs_common=$(cd "$common_dir" 2>/dev/null && pwd) || abs_common="$common_dir"
    main_root=$(dirname "$abs_common")
    if [[ ! -d "$main_root" ]]; then
        echo "check-shell-syntax.sh: could not resolve main worktree root from '$common_dir'" >&2
        exit "$EXIT_USAGE"
    fi

    HOOKS_DIR="$main_root/.loom/hooks"
    SCRIPTS_DIR="$main_root/.loom/scripts"

    if [[ -d "$HOOKS_DIR" ]]; then
        # Top-level only — matches resync-installed.sh's own non-recursive
        # hooks walk ("for src in \"$DEFAULTS_DIR/hooks/\"*.sh").
        shopt -s nullglob
        for f in "$HOOKS_DIR"/*.sh; do
            FILES+=("$f")
        done
        shopt -u nullglob
    fi

    if [[ -d "$SCRIPTS_DIR" ]]; then
        # -L: this repo's own .loom/scripts is a symlink to defaults/scripts
        # (the dogfood layout); a plain `find` does not descend into a
        # top-level symlinked directory argument on every platform (observed
        # on BSD/macOS find — GNU find's behavior differs), so without -L this
        # would silently scan zero files here. Mirrors resync-installed.sh's
        # own `find -L` precedent (#5222) for the same symlink shape.
        while IFS= read -r -d '' f; do
            FILES+=("$f")
        done < <(find -L "$SCRIPTS_DIR" -type f -name '*.sh' -print0 | sort -z)
    fi
fi

# ---- Run bash -n over every discovered file --------------------------------

FAIL_COUNT=0
FAIL_NAMES=()
CHECKED=0

for f in ${FILES[@]+"${FILES[@]}"}; do
    CHECKED=$((CHECKED + 1))
    err_out=$(bash -n "$f" 2>&1)
    rc=$?
    if [[ $rc -ne 0 ]]; then
        FAIL_COUNT=$((FAIL_COUNT + 1))
        FAIL_NAMES+=("$f")
        echo "ERROR: check-shell-syntax.sh: $f does not parse (bash -n):" >&2
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            echo "  $line" >&2
        done <<< "$err_out"
    fi
done

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo "" >&2
    echo "check-shell-syntax.sh: $FAIL_COUNT of $CHECKED script(s) FAILED to parse:" >&2
    for f in "${FAIL_NAMES[@]}"; do
        echo "  - $f" >&2
    done
    exit "$EXIT_SYNTAX_FAIL"
fi

if [[ "$QUIET" -eq 0 ]]; then
    echo "check-shell-syntax.sh: $CHECKED script(s) parse cleanly."
fi
exit "$EXIT_OK"
