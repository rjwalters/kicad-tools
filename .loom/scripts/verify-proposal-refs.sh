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
#      `git ls-tree -r origin/main --name-only`; line ranges are checked
#      against `git show origin/main:<path> | wc -l`.
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
declare -A TOP_LEVEL
while IFS= read -r entry; do
    [[ -n "$entry" ]] && TOP_LEVEL["$entry"]=1
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
    TOP_LEVEL["$generic"]=1
done

is_recognized_top() {
    local first="${1%%/*}"
    [[ -n "${TOP_LEVEL[$first]:-}" ]]
}

# --- Extract path / path:L / path:L1-L2 references (backticked or bare).
# Requires at least one '/' so bare prose (version numbers, "e.g.", "and/or")
# never matches; a directory component is exactly what "under a recognized
# top-level dir" means.
PATH_RE='[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+(:[0-9]+(-[0-9]+)?)?'

FULL_TREE_CACHE=""
full_tree() {
    if [[ -z "$FULL_TREE_CACHE" ]]; then
        FULL_TREE_CACHE="$(git -C "$WORKSPACE" ls-tree -r origin/main --name-only)"
    fi
    printf '%s' "$FULL_TREE_CACHE"
}

mapfile -t CANDIDATES < <(grep -oE "$PATH_RE" "$BODY_FILE" | sort -u)

for raw_candidate in "${CANDIDATES[@]}"; do
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

    if ! full_tree | grep -qFx "$path"; then
        MISSES+=("MISSING FILE: \`$path\` does not exist on origin/main")
        continue
    fi

    if [[ -n "$lines" ]]; then
        start="${lines%%-*}"
        end="${lines##*-}"
        total=$(git -C "$WORKSPACE" show "origin/main:$path" | wc -l | tr -d ' ')
        if (( start > total )) || (( end > total )); then
            MISSES+=("BAD LINE RANGE: \`$candidate\` — origin/main:$path has only $total lines")
        fi
    fi
done

# --- "<N> tracked `<pattern>` files" claims, e.g. "six tracked `.pyc` files".
declare -A NUM_WORDS=(
    [one]=1 [two]=2 [three]=3 [four]=4 [five]=5
    [six]=6 [seven]=7 [eight]=8 [nine]=9 [ten]=10
)

TRACKED_RE='([0-9]+|one|two|three|four|five|six|seven|eight|nine|ten)[[:space:]]+tracked[[:space:]]+`([^`]+)`[[:space:]]+files?'

while IFS= read -r line; do
    if [[ "$line" =~ $TRACKED_RE ]]; then
        count_raw="${BASH_REMATCH[1]}"
        pattern="${BASH_REMATCH[2]}"

        if [[ "$count_raw" =~ ^[0-9]+$ ]]; then
            claimed="$count_raw"
        else
            claimed="${NUM_WORDS[$count_raw]:-}"
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
