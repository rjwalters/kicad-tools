#!/usr/bin/env bash
# verify-proposal-refs.sh - Verify every path / path:line / "tracked N files"
# claim in a drafted Hermit or Architect proposal body against origin/main of
# the CURRENT workspace, before the proposal is filed (issue #7658).
#
# Hermit and Architect proposals go straight to Champion — unlike Curator,
# nothing mechanically checks that a cited path exists, that a cited line
# range is real, or that a "tracked" claim (e.g. "six tracked `.pyc` files")
# matches `git ls-files`. On 2026-09-14 this produced proposals citing paths
# from a sibling repo the proposer had read, a nonexistent test file, and a
# false tracked-file count — each costing two Champion evaluations plus an
# operator escalation.
#
# What this script checks, extracted from the body file:
#   1. `path`, `path:L`, `path:L1-L2` references — backticked or bare — whose
#      first path segment matches a real top-level entry of `origin/main`
#      (a "recognized top-level dir/file"). Existence is checked with
#      `git ls-tree -r origin/main --name-only`; line ranges are checked by
#      `loom-daemon git-blob-lines --range`, which follows a `120000` (symlink)
#      tree entry to the document it points at before counting (#8656) — a
#      dangling link is reported as BROKEN SYMLINK and is NOT a miss, because
#      an unreadable file is an inconclusive check rather than a disproof.
#   2. "<N> tracked `<pattern>` files" claims (N as a digit or one..ten
#      spelled out) — checked against `git ls-files -- <pattern>`.
#
# Every miss is listed; the script exits non-zero if there is at least one.
#
# Workspace rooting (#7658, Ask item 4): this script NEVER targets a sibling
# checkout. It resolves the repo to check against from $LOOM_WORKSPACE if
# set, else $PWD — always the dispatched workspace, never a hardcoded or
# discovered sibling clone.
#
# Usage:
#   ./verify-proposal-refs.sh <body-file>
#
# Exit codes:
#   0 - every reference and tracked-claim checks out (including "none found")
#   1 - one or more references/claims are misses (listed on stdout)
#   2 - usage error or prerequisites missing (no body file, not a git repo,
#       origin/main unreachable from the workspace)

set -uo pipefail

usage() {
    echo "Usage: $0 <body-file>" >&2
    exit 2
}

[[ $# -eq 1 ]] || usage
BODY_FILE="$1"

if [[ ! -f "$BODY_FILE" ]]; then
    echo "ERROR: body file not found: $BODY_FILE" >&2
    exit 2
fi

# Workspace-rooted, never a sibling checkout (#7658 Ask item 4).
WORKSPACE="${LOOM_WORKSPACE:-$PWD}"

if ! git -C "$WORKSPACE" rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "ERROR: workspace is not a git repository: $WORKSPACE" >&2
    exit 2
fi

if ! git -C "$WORKSPACE" rev-parse --verify origin/main >/dev/null 2>&1; then
    echo "ERROR: origin/main not reachable from workspace: $WORKSPACE" >&2
    echo "  (run 'git fetch origin' in the workspace first)" >&2
    exit 2
fi

# --- Line-range checking is `loom-daemon git-blob-lines --range` (#8656).
#
# This used to be `git show "origin/main:$path" | wc -l` inline. That reads the
# path THROUGH GIT, and since #7842 every `.loom/docs/*.md` with a
# `defaults/docs/` counterpart is a SYMLINK on origin/main (tree mode 120000) —
# so the read returned the link-target STRING, `wc -l` measured the link, and
# every genuinely in-range citation under `.loom/docs/` was reported as a miss.
# Since this script BLOCKS FILING, that pushed proposers away from citing the
# installed doc paths at all.
#
# The subcommand follows a 120000 entry to its real blob (bounded hops; the
# target is relative to the LINK'S OWN directory; normalization is lexical,
# because the path lives in a commit rather than the working tree), then
# answers the range question against THAT blob. It also distinguishes the two
# failures the old one-liner conflated: an out-of-range span is a disproof (a
# miss), while an unreadable target is an INCONCLUSIVE check and must never
# block filing — the same distinction #8593 established for Champion.
#
# shellcheck source=lib/locate-daemon-bin.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/locate-daemon-bin.sh"
# requires-daemon: git-blob-lines >= 0.19.307   #8656 — the symlink-aware line-range check; a resolved binary predating this subcommand fails clap's own "unrecognized subcommand" per citation (visible on stderr) and every line range is SKIPPED, never silently passed as in-range, because the alternative is the symlink-blind `git show | wc -l` this replaced
DAEMON_BIN="$(loom_resolve_self_daemon_bin)"
[[ -n "$DAEMON_BIN" ]] || echo "WARNING: no loom-daemon binary resolved — line ranges will NOT be checked (inconclusive, never a miss); path existence and tracked-file claims are unaffected" >&2

MISSES=()
CHECKED_PATHS=0
CHECKED_CLAIMS=0

# --- Recognized top-level dirs: the UNION of (a) origin/main's own top-level
# tree entries in THIS workspace, and (b) a generic allowlist of directory
# names conventional across many kinds of repos (software and otherwise).
#
# (a) alone would MISS the exact failure this script exists to catch: a
# citation like `verification/_repo_utils.py` copied from a sibling repo,
# whose top segment ("verification") by definition does not exist in the
# target workspace — treating "not a top-level entry here" as "not a path
# reference" would silently skip precisely the false citation we need to
# flag. (b) alone would fail to recognize this repo's own less-common
# top-level dirs. The union recognizes both.
#
# This still filters out ordinary prose that merely contains a slash
# ("and/or", "his/her", "3/4", date-like "2026/09/14") because those first
# segments match neither list.
# A newline-delimited string, not `declare -A` (bash 4+; macOS ships 3.2 --
# #7751). This is a pure SET whose only consumer is the whole-word membership
# test in is_recognized_top() below, so a leading/trailing-newline-delimited
# string plus a bash `case` glob is an exact substitute -- and it spawns no
# subprocess per lookup, unlike a `grep -qxF` over the same data.
TOP_LEVEL_NL=$'\n'
while IFS= read -r entry; do
    [[ -n "$entry" ]] && TOP_LEVEL_NL="${TOP_LEVEL_NL}${entry}"$'\n'
done < <(git -C "$WORKSPACE" ls-tree --name-only origin/main)

for generic in \
    src lib libs source sources vendor third_party \
    test tests spec specs fixtures \
    docs doc documentation \
    scripts script bin tools cmd cmds \
    internal pkg pkgs packages crates crate \
    api app apps web frontend backend server client \
    config configs conf \
    examples example samples demo demos \
    assets static public \
    include includes \
    migrations models controllers views \
    deploy deployment infra ci k8s terraform \
    data datasets \
    build dist target node_modules \
    hardware layout verification sim rtl tb firmware fw benches hdl \
    .github .loom .claude .gitea
do
    TOP_LEVEL_NL="${TOP_LEVEL_NL}${generic}"$'\n'
done

is_recognized_top() {
    local first="${1%%/*}"
    # Anchored on both delimiters so "doc" never matches inside "docs".
    case "$TOP_LEVEL_NL" in
        *$'\n'"$first"$'\n'*) return 0 ;;
    esac
    return 1
}

# --- Extract path / path:L / path:L1-L2 references (backticked or bare).
# Requires at least one '/' so bare prose (version numbers, "e.g.", "and/or")
# never matches; a directory component is exactly what "under a recognized
# top-level dir" means.
PATH_RE='[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+(:[0-9]+(-[0-9]+)?)?'

# Whole-tree membership, as a newline-delimited SET tested with a bash `case`
# glob -- deliberately NOT `full_tree | grep -qFx "$path"` (#7736, #7771).
#
# Under the `set -o pipefail` above, `grep -qFx` exits the moment it matches,
# closing the pipe while the producer is still writing a tree listing far
# larger than the 64K pipe buffer. The producer takes SIGPIPE (141), pipefail
# reports the PIPELINE as failed, and `if ! ...` then records a `MISSING FILE`
# miss for a path that is demonstrably present -- a silent wrong answer, not a
# flake (#7771). The same pipe also surfaced as "printf: write error: Broken
# pipe" noise in CI (#7736). A `case` test has no pipe, no subprocess, and no
# exit status to misreport; it mirrors is_recognized_top() above, and the
# candidate paths are drawn from [A-Za-z0-9_.-/:] only, so no glob
# metacharacter can reach the pattern side.
FULL_TREE_NL=""
full_tree_contains() {
    if [[ -z "$FULL_TREE_NL" ]]; then
        FULL_TREE_NL=$'\n'"$(git -C "$WORKSPACE" ls-tree -r origin/main --name-only)"$'\n'
    fi
    case "$FULL_TREE_NL" in
        *$'\n'"$1"$'\n'*) return 0 ;;
    esac
    return 1
}

CANDIDATES=()
while IFS= read -r _cand; do
    [[ -n "$_cand" ]] || continue
    CANDIDATES+=("$_cand")
done < <(grep -oE "$PATH_RE" "$BODY_FILE" | sort -u)

for raw_candidate in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
    # Strip trailing sentence punctuation the character class can't exclude
    # (e.g. "...pdk.py:185." at the end of a sentence).
    candidate="$raw_candidate"
    while [[ "$candidate" == *. ]]; do
        candidate="${candidate%.}"
    done
    [[ -z "$candidate" ]] && continue

    path="$candidate"
    lines=""
    if [[ "$candidate" =~ ^(.+):([0-9]+)(-([0-9]+))?$ ]]; then
        path="${BASH_REMATCH[1]}"
        lines="${BASH_REMATCH[2]}${BASH_REMATCH[4]:+-${BASH_REMATCH[4]}}"
    fi

    is_recognized_top "$path" || continue
    CHECKED_PATHS=$((CHECKED_PATHS + 1))

    if ! full_tree_contains "$path"; then
        MISSES+=("MISSING FILE: \`$path\` does not exist on origin/main")
        continue
    fi

    # Exit 1 => out of range (a miss, text already rendered, naming the
    # RESOLVED path). Exit 10 => unreadable, already reported on stderr and
    # deliberately NOT a miss. Anything else (0, or no binary) => nothing.
    if [[ -n "$lines" && -n "$DAEMON_BIN" ]]; then
        miss=$("$DAEMON_BIN" git-blob-lines "$path" --rev origin/main --workspace "$WORKSPACE" --range "$lines" --cite "$candidate")
        (( $? == 1 )) && MISSES+=("$miss")
    fi
done

# --- "<N> tracked `<pattern>` files" claims, e.g. "six tracked `.pyc` files".
# A `case`, not `declare -A` (bash 4+; #7751). For a fixed literal word->number
# table this is arguably clearer than either array form, and it keeps the
# "unknown word yields empty" contract the caller already relies on.
num_word() {
    case "$1" in
        one) printf '1' ;;   two) printf '2' ;;   three) printf '3' ;;
        four) printf '4' ;;  five) printf '5' ;;  six) printf '6' ;;
        seven) printf '7' ;; eight) printf '8' ;; nine) printf '9' ;;
        ten) printf '10' ;;
        *) : ;;
    esac
}

TRACKED_RE='([0-9]+|one|two|three|four|five|six|seven|eight|nine|ten)[[:space:]]+tracked[[:space:]]+`([^`]+)`[[:space:]]+files?'

while IFS= read -r line; do
    if [[ "$line" =~ $TRACKED_RE ]]; then
        count_raw="${BASH_REMATCH[1]}"
        pattern="${BASH_REMATCH[2]}"

        if [[ "$count_raw" =~ ^[0-9]+$ ]]; then
            claimed="$count_raw"
        else
            claimed="$(num_word "$count_raw")"
        fi
        [[ -z "$claimed" ]] && continue

        CHECKED_CLAIMS=$((CHECKED_CLAIMS + 1))

        # A bare extension like ".pyc" is shorthand for "*.pyc" as a
        # git ls-files pathspec; anything else is used as-is.
        glob="$pattern"
        [[ "$glob" == .* ]] && glob="*$glob"

        actual=$(git -C "$WORKSPACE" ls-files -- "$glob" | wc -l | tr -d ' ')
        if [[ "$actual" != "$claimed" ]]; then
            MISSES+=("FALSE TRACKED CLAIM: \"$count_raw tracked \`$pattern\` files\" — git ls-files shows $actual")
        fi
    fi
done < "$BODY_FILE"

if (( ${#MISSES[@]} > 0 )); then
    echo "verify-proposal-refs.sh: ${#MISSES[@]} miss(es) in $BODY_FILE" >&2
    for m in "${MISSES[@]}"; do
        echo "  - $m" >&2
    done
    exit 1
fi

echo "verify-proposal-refs.sh: all references check out (checked $CHECKED_PATHS path ref(s), $CHECKED_CLAIMS tracked-claim(s))"
exit 0
