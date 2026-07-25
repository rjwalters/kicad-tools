#!/usr/bin/env bash
# Loom Stacked-PR Out-of-Set Dependency Detect-and-Warn (issue #3747, v2 item 4)
#
# The DETECTION half of broad dependency-awareness for /loom:sweep, WITHOUT the
# unsafe auto-expansion of the candidate set. During candidate-set resolution
# (Modes A and B), each resolved candidate issue's body is scanned for dependency
# references (`Depends on #A` / `Requires #A` / `Part of #A`). When a referenced
# `#A` is OPEN and NOT in this sweep's resolved candidate set and NOT already
# covered by an operator `--depends-on`, this tool emits a clear ADVISORY warning
# so the operator can decide to `--depends-on <A>` or include `#A` — but it never
# silently reaches out to external issues. The candidate set is NEVER modified.
#
# This preserves the "load-bearing" same-candidate-set safety property that
# --auto-stack (#3759) relies on: detection + advisory only, no auto-expansion,
# no probing/expanding to external issues, no diamonds/multi-parent, no auto-detach.
#
# It REUSES the exact parser vocabulary established in defaults/roles/guide.md
# (`parse_dependencies`) and generalized by --auto-stack (#3759) — it does NOT
# introduce a second divergent parser. The only difference from --auto-stack's
# detection is which phrases and which side of the set it acts on: auto-stack
# acts on IN-set `Depends on`/`Requires` edges (to stack them); this tool warns
# on OUT-of-set `Depends on`/`Requires`/`Part of` references (to surface them).
#
# Usage:
#   warn-out-of-set-deps.sh --candidates "<N ...>" [--depends-on "<N ...>"] [--repo <nwo>]
#
# Example:
#   warn-out-of-set-deps.sh --candidates "124 125 126" --depends-on "130"
#
# Behavior, per candidate issue in --candidates:
#   1. Read the candidate's body (`gh issue view <N> --json body`).
#   2. Parse dependency references (Depends on / Requires / Part of).
#   3. For each referenced #A:
#        - IN the candidate set          -> NO warning (auto-stack's domain).
#        - covered by an operator         -> NO warning (already declared).
#          --depends-on value
#        - CLOSED                         -> NO warning (nothing stale to build on).
#        - OPEN and out-of-set and        -> ADVISORY warning to stderr.
#          not --depends-on-covered
#   4. Dedup: warn at most once per (candidate, dependency) pair.
#
# The warning is NON-BLOCKING — this tool always exits 0 (advisory only). The
# sweep proceeds regardless. In Mode A's no-prompt fast path the warnings go to
# stderr/log (never a prompt); in interactive contexts they may be surfaced in
# the candidate-set preview.
#
# Options:
#   --candidates "<N ...>"  Space-separated resolved candidate issue numbers (required).
#   --depends-on "<N ...>"  Space-separated operator-declared parent issue numbers
#                           already covered by --depends-on (optional; default none).
#   --repo <nwo>            Repo owner/name (optional; defaults to the current repo).
#   --help,-h               Show this help.
#
# Exit codes:
#   0 = always (advisory, non-blocking) — including when warnings were emitted.
#   1 = usage / precondition failure (e.g. missing --candidates).

set -uo pipefail

# Colors (skip when not a TTY).
if [[ -t 2 ]]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; YELLOW=''; BLUE=''; NC=''
fi
err()  { echo -e "${RED}ERROR: $1${NC}" >&2; }
info() { echo -e "${BLUE}ℹ $1${NC}" >&2; }

show_help() {
    sed -n '2,60p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---- parser (reused vocabulary, extracted by tests) ----
# Parse out-of-set dependency references from an issue body. REUSES guide.md's
# parse_dependencies vocabulary (the `(Blocked by|Depends on|Requires|- [.]) #N`
# convention #3759's --auto-stack also derives from), restricted to the three
# DECLARATION phrases this item covers: Depends on / Requires / Part of.
#
# Deliberately EXCLUDES `Blocked by` (that phrase belongs to the loom:blocked
# machinery, exactly as --auto-stack excludes it from stacking detection).
# Emits the deduplicated referenced issue numbers, one per line.
parse_out_of_set_deps() {
    local body="$1"
    echo "$body" | grep -oE '(Depends on|Requires|Part of) #[0-9]+' \
        | grep -oE '#[0-9]+' | tr -d '#' | sort -un
}

# ---- membership helpers ----
# True (0) if $1 appears as a whole word in the space-separated list $2.
_in_set() {
    local needle="$1" haystack=" $2 "
    [[ "$haystack" == *" $needle "* ]]
}

# ---- core (extracted by tests) ----
# Emit advisory warnings for one candidate's out-of-set open dependency refs.
# Uses globals: CANDIDATES, DEPENDS_ON_SET, REPO_NWO. Increments WARN_COUNT for
# every (candidate, dependency) pair warned. Always returns 0.
_warn_candidate_out_of_set_deps() {
    local candidate="$1"

    local body
    if [[ -n "$REPO_NWO" ]]; then
        body="$(gh issue view "$candidate" --repo "$REPO_NWO" --json body -q '.body' 2>/dev/null || echo '')"
    else
        body="$(gh issue view "$candidate" --json body -q '.body' 2>/dev/null || echo '')"
    fi
    [[ -n "$body" ]] || return 0

    local dep
    for dep in $(parse_out_of_set_deps "$body"); do
        # Self-reference (a body that names its own number) is never a dependency.
        [[ "$dep" == "$candidate" ]] && continue
        # In-set reference -> auto-stack's domain, not ours. No warning.
        _in_set "$dep" "$CANDIDATES" && continue
        # Already covered by an operator --depends-on -> declared. No warning.
        _in_set "$dep" "$DEPENDS_ON_SET" && continue

        # Out-of-set + not operator-covered: warn only if the dependency is OPEN
        # (a closed dependency has nothing stale to build against).
        local state
        if [[ -n "$REPO_NWO" ]]; then
            state="$(gh issue view "$dep" --repo "$REPO_NWO" --json state -q '.state' 2>/dev/null || echo 'UNKNOWN')"
        else
            state="$(gh issue view "$dep" --json state -q '.state' 2>/dev/null || echo 'UNKNOWN')"
        fi
        # gh reports OPEN/CLOSED (uppercase). Anything not clearly OPEN is skipped
        # (a closed dep, or an unreadable one — never warn on ambiguity).
        [[ "$state" == "OPEN" ]] || continue

        echo -e "${YELLOW}warning: issue #${candidate} declares \"Depends on #${dep}\", but #${dep} is not in this sweep's candidate set — pass --depends-on ${dep} or include #${dep} to stack them; otherwise #${candidate} may build against a stale base.${NC}" >&2
        WARN_COUNT=$((WARN_COUNT + 1))
    done
    return 0
}

# Scan every candidate for out-of-set open dependency references. Always 0.
_warn_out_of_set_deps() {
    local candidate
    for candidate in $CANDIDATES; do
        _warn_candidate_out_of_set_deps "$candidate"
    done
    return 0
}

# ---- arg parsing ----
CANDIDATES=""
DEPENDS_ON_SET=""
REPO_NWO=""

# Only parse args + run main when executed directly (not when sourced by tests).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --candidates) CANDIDATES="${2:-}"; shift 2 ;;
            --depends-on) DEPENDS_ON_SET="${2:-}"; shift 2 ;;
            --repo)       REPO_NWO="${2:-}"; shift 2 ;;
            --help|-h)    show_help; exit 0 ;;
            --*)          err "Unknown flag: $1"; exit 1 ;;
            *)            err "Unexpected argument: $1"; exit 1 ;;
        esac
    done

    if [[ -z "$CANDIDATES" ]]; then
        err "Usage: warn-out-of-set-deps.sh --candidates \"<N ...>\" [--depends-on \"<N ...>\"] [--repo <nwo>]"
        exit 1
    fi

    WARN_COUNT=0
    _warn_out_of_set_deps
    # Advisory + non-blocking: always exit 0, even when warnings were emitted.
    [[ "$WARN_COUNT" -eq 0 ]] || info "$WARN_COUNT out-of-set dependency warning(s) emitted (advisory — sweep proceeds)."
    exit 0
fi
