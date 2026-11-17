#!/usr/bin/env bash
# check-conflict-markers.sh - Assert no TRACKED file carries live git conflict
# markers.
#
# #6499: an abandoned `git stash pop` conflict left live conflict markers
# (`<<<<<<< Updated upstream` / `=======` / `>>>>>>> Stashed changes`) in the
# tracked, host-patched `.loom/config.json` on two fleet hosts. Nothing
# stopped them from being COMMITTED — a later `chore: resync installed Loom
# surfaces` pass swept the corrupted file into a commit on `main`, where every
# working-tree-state guard is structurally blind to it:
#
#   - `check-main-clean.sh`'s abandoned-conflict detection (#6162 AC3) and
#     `primary_checkout_reaper`'s periodic counterpart (#6499) both key on an
#     UNMERGED INDEX ENTRY (`git status --porcelain` XY in DD/AU/UD/UA/DU/AA/
#     UU). Once the conflicted content is `git add`ed and committed, the index
#     is merged again and both detectors correctly report nothing.
#   - `check-shell-syntax.sh` (#6162) catches the same corruption shape, but
#     only in `*.sh` files, via `bash -n`. `.loom/config.json` is JSON.
#
# This script is the content-level gate that covers the committed case for
# every tracked file regardless of extension. A line-start `<<<<<<< ` or
# `>>>>>>> ` in a tracked file is never legitimate content — it is always a
# conflict left unresolved.
#
# Consequence of missing it, for the file that triggered #6499: an
# unparseable `.loom/config.json` makes the daemon's `config_resolver` fall
# back to `{}` for that tier, silently running on built-in defaults for every
# block the file carried — observability, safehouse, `autonomous.roleRunner`.
# On the original incident the fleet dashboard showed a host stale for ~70
# minutes while its probe still reported UP.
#
# Opting a file out (fixtures that MUST contain markers): a file containing
# the literal string
#
#     check-conflict-markers:allow
#
# anywhere in its content is skipped. This is deliberately an in-file opt-out
# rather than a path allowlist in this script: a test that constructs a
# conflicted fixture declares its own exemption next to the fixture, so the
# exemption cannot outlive the file or drift from it.
#
# Usage:
#   ./.loom/scripts/check-conflict-markers.sh            # scan tracked files
#   ./.loom/scripts/check-conflict-markers.sh --dir <p>  # scan <p> recursively (repeatable)
#   ./.loom/scripts/check-conflict-markers.sh --quiet    # only print failures
#   ./.loom/scripts/check-conflict-markers.sh --self-test
#   ./.loom/scripts/check-conflict-markers.sh --help
#
# Exit codes:
#   0 - no tracked file carries conflict markers (including "found zero files").
#   1 - usage error, or not inside a git repository (default mode).
#   2 - one or more files carry conflict markers - every offender is named
#       with the offending line numbers.
#
# Notes:
#   - Default mode scans `git ls-files` of the repo containing $PWD, so it
#     works identically from the primary checkout and from a linked worktree
#     (each scans its own checked-out content, which is what a pre-merge CI
#     gate wants - unlike check-shell-syntax.sh, whose default mode
#     deliberately reaches for the PRIMARY checkout's INSTALLED copies).
#   - A bare `=======` line is NOT flagged on its own: it is a legitimate
#     markdown setext heading underline and a common shell/comment separator.
#     Git always emits `<<<<<<< ` and `>>>>>>> ` alongside it, so keying on
#     those two (at line start, with the trailing space git always writes
#     before the branch/stash label) is both sufficient and false-positive-free.
#   - Binary files are skipped (`grep -I`), so this never chokes on a tracked
#     image or archive.

set -uo pipefail

EXIT_OK=0
EXIT_USAGE=1
EXIT_MARKERS_FOUND=2

QUIET=0
DIRS=()
SELF_TEST=0

# The two line-start marker forms git writes. Kept as a single ERE so the
# scan is one grep pass per file.
MARKER_ERE='^(<<<<<<< |>>>>>>> )'

# In-file opt-out sentinel. Assembled at runtime so this script's own source
# does not contain the literal string (which would exempt the checker itself).
ALLOW_SENTINEL="check-conflict-markers$(printf ':')allow"

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
                echo "check-conflict-markers.sh: --dir requires a directory argument" >&2
                exit "$EXIT_USAGE"
            fi
            DIRS+=("$2")
            shift 2
            ;;
        --self-test)
            SELF_TEST=1
            shift
            ;;
        --quiet|-q)
            QUIET=1
            shift
            ;;
        *)
            echo "check-conflict-markers.sh: unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit "$EXIT_USAGE"
            ;;
    esac
done

# ---- Self-test -------------------------------------------------------------
#
# Proves the detector actually fires on the #6499 shape and stays silent on
# the legitimate near-misses, using synthetic fixtures in a temp dir. Runs the
# real scan path (--dir), not a reimplementation of it.

if [[ "$SELF_TEST" -eq 1 ]]; then
    st_dir=$(mktemp -d)
    trap 'rm -rf "$st_dir"' EXIT
    st_fail=0

    st_assert() {
        local label="$1" expected="$2" actual="$3"
        if [[ "$expected" == "$actual" ]]; then
            echo "  ok: $label"
        else
            echo "  FAIL: $label (expected exit $expected, got $actual)" >&2
            st_fail=1
        fi
    }

    # 1. The #6499 shape: a stash-pop conflict inside a JSON object.
    mkdir -p "$st_dir/positive"
    {
        printf '{\n  "safehouse": {\n'
        printf '<<<<<<< Updated upstream\n'
        printf '    "room": "!abc:example.com"\n'
        printf '=======\n'
        printf '    "socket": "/home/ubuntu/.loom/safehoused/state/safehoused.sock",\n'
        printf '    "room": "loom-fleet"\n'
        printf '>>>>>>> Stashed changes\n'
        printf '  }\n}\n'
    } > "$st_dir/positive/config.json"
    "$0" --dir "$st_dir/positive" --quiet >/dev/null 2>&1
    st_assert "conflicted json is flagged" "$EXIT_MARKERS_FOUND" "$?"

    # 2. Clean tree.
    mkdir -p "$st_dir/clean"
    printf '{\n  "safehouse": { "room": "loom-fleet" }\n}\n' > "$st_dir/clean/config.json"
    "$0" --dir "$st_dir/clean" --quiet >/dev/null 2>&1
    st_assert "clean json passes" "$EXIT_OK" "$?"

    # 3. Legitimate near-misses that MUST NOT be flagged: a markdown setext
    #    heading underline, a `=======` separator comment, and inline
    #    backticked marker text mid-line (how the troubleshooting docs
    #    describe this very failure).
    mkdir -p "$st_dir/nearmiss"
    {
        printf 'Recovery\n'
        printf '========\n'
        printf '\n'
        printf 'Look for literal `<<<<<<<` / `=======` / `>>>>>>>` markers.\n'
        printf '=======\n'
    } > "$st_dir/nearmiss/doc.md"
    "$0" --dir "$st_dir/nearmiss" --quiet >/dev/null 2>&1
    st_assert "setext/separator/inline markers are not flagged" "$EXIT_OK" "$?"

    # 4. In-file opt-out honored (a deliberate conflicted fixture).
    mkdir -p "$st_dir/optout"
    {
        printf '# fixture: %s\n' "$ALLOW_SENTINEL"
        printf '<<<<<<< Updated upstream\n=======\n>>>>>>> Stashed changes\n'
    } > "$st_dir/optout/fixture.sh"
    "$0" --dir "$st_dir/optout" --quiet >/dev/null 2>&1
    st_assert "in-file opt-out sentinel is honored" "$EXIT_OK" "$?"

    # 5. An empty scan is success, not a vacuous failure.
    mkdir -p "$st_dir/empty"
    "$0" --dir "$st_dir/empty" --quiet >/dev/null 2>&1
    st_assert "empty scan passes" "$EXIT_OK" "$?"

    if [[ "$st_fail" -ne 0 ]]; then
        echo "check-conflict-markers.sh: --self-test FAILED" >&2
        exit "$EXIT_MARKERS_FOUND"
    fi
    echo "check-conflict-markers.sh: --self-test passed"
    exit "$EXIT_OK"
fi

# ---- Collect files to scan -------------------------------------------------

FILES=()

if [[ "${#DIRS[@]}" -gt 0 ]]; then
    for d in "${DIRS[@]}"; do
        if [[ ! -d "$d" ]]; then
            echo "check-conflict-markers.sh: --dir path does not exist or is not a directory: $d" >&2
            exit "$EXIT_USAGE"
        fi
        while IFS= read -r -d '' f; do
            FILES+=("$f")
        done < <(find -L "$d" -type f -print0 2>/dev/null | sort -z)
    done
else
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "check-conflict-markers.sh: not inside a git repository" >&2
        exit "$EXIT_USAGE"
    fi
    repo_root=$(git rev-parse --show-toplevel 2>/dev/null || true)
    if [[ -z "$repo_root" ]]; then
        echo "check-conflict-markers.sh: could not resolve repository root" >&2
        exit "$EXIT_USAGE"
    fi
    while IFS= read -r -d '' f; do
        # `git ls-files` can name a path deleted from the working tree.
        [[ -f "$repo_root/$f" ]] || continue
        FILES+=("$repo_root/$f")
    done < <(git -C "$repo_root" ls-files -z)
fi

# ---- Scan ------------------------------------------------------------------

FAIL_COUNT=0
FAIL_NAMES=()
CHECKED=0

for f in ${FILES[@]+"${FILES[@]}"}; do
    # -I: skip binary files. Also skips anything unreadable, which is not this
    # check's business to adjudicate.
    hits=$(grep -InE "$MARKER_ERE" "$f" 2>/dev/null) || hits=""
    CHECKED=$((CHECKED + 1))
    [[ -n "$hits" ]] || continue

    if grep -qIF "$ALLOW_SENTINEL" "$f" 2>/dev/null; then
        continue
    fi

    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAIL_NAMES+=("$f")
    echo "ERROR: check-conflict-markers.sh: $f contains live git conflict markers:" >&2
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        echo "  $line" >&2
    done <<< "$hits"
done

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo "" >&2
    echo "check-conflict-markers.sh: $FAIL_COUNT of $CHECKED file(s) carry conflict markers:" >&2
    for f in "${FAIL_NAMES[@]}"; do
        echo "  - $f" >&2
    done
    echo "" >&2
    echo "  These are an unresolved merge/rebase/stash-pop left in the tree (#6499)." >&2
    echo "  Resolve each hunk by hand - keep this host's own side, delete the markers" >&2
    echo "  and the losing side - then re-verify the file. For JSON:" >&2
    echo "      jq . <path>" >&2
    echo "  See .loom/docs/troubleshooting.md -> 'Conflict markers left in" >&2
    echo "  .loom/config.json after a git stash pop (#6499)'." >&2
    echo "  A fixture that MUST contain markers can opt out by embedding the" >&2
    echo "  literal string '$ALLOW_SENTINEL' in its own content." >&2
    exit "$EXIT_MARKERS_FOUND"
fi

if [[ "$QUIET" -eq 0 ]]; then
    echo "check-conflict-markers.sh: $CHECKED file(s) scanned, no conflict markers found."
fi
exit "$EXIT_OK"
