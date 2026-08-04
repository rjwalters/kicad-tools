#!/usr/bin/env bash
# Loom Operator-Gate Advisory Scan for /loom:sweep all (#5137)
#
# The `all` sentinel's aggressive candidate taxonomy hard-skips exactly one
# label class (`loom:operator-only`) and treats every other open issue as a
# buildable candidate. Some issues declare operator-gating in their BODY TEXT
# ("the index is login-walled, so acquisition is operator-gated", "Operator
# decision: send this paired with the paper") without carrying the label —
# so the sentinel would plan to dispatch them as ordinary build candidates.
#
# This tool scans each candidate's already-fetched `body` (the same field the
# survey fetches for ## Affected Files overlap estimation, #4161, and
# Depends on / Requires edge detection, #3759 — no new API call) for two
# independent, ADVISORY-ONLY signals:
#
#   1. Direct phrase match — the body itself contains an instruction-shaped
#      operator-gate fragment (see PHRASES below). Case-insensitive.
#   2. Dependency match — the body declares `Depends on #A` / `Requires #A`
#      (the same restricted vocabulary #3759's --auto-stack and #3747's
#      warn-out-of-set-deps.sh both reuse) and #A currently carries the
#      `loom:operator-only` label — the #87 -> #4 shape from the issue: the
#      sweep would skip the prerequisite as operator-only, then dispatch the
#      dependent that needs it.
#
# ADVISORY ONLY: this tool never hard-skips a candidate, never mutates a
# label, and never blocks the sweep. It only ANNOTATES the confirmation-gate
# listing so the operator can decide. It always exits 0.
#
# Usage:
#   warn-operator-gated.sh --candidates "<N ...>" [--repo <nwo>]
#
# Example:
#   warn-operator-gated.sh --candidates "87 65 64 73"
#
# Output (stdout, one line per MATCHING candidate — silent for the rest):
#   <N>\t<annotation>
# where <annotation> is one of:
#   ⚠ body declares operator-gating: "<matched phrase>"
#   ⚠ depends on #<A>, which is loom:operator-only
# A candidate matching both prints two lines (one per signal).
#
# Zero matches across the whole candidate set -> zero lines of output, and the
# caller's plan/listing is byte-for-byte unchanged from before this tool
# existed (per #5137's own acceptance criterion).
#
# Options:
#   --candidates "<N ...>"  Space-separated candidate issue numbers (required).
#   --repo <nwo>            Repo owner/name (optional; defaults to the current repo).
#   --help,-h               Show this help.
#
# Exit codes:
#   0 = always (advisory, non-blocking) — including when matches were emitted.
#   1 = usage / precondition failure (e.g. missing --candidates).

set -uo pipefail

# Colors (skip when not a TTY).
if [[ -t 2 ]]; then
    RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; BLUE=''; NC=''
fi
err()  { echo -e "${RED}ERROR: $1${NC}" >&2; }
info() { echo -e "${BLUE}ℹ $1${NC}" >&2; }

show_help() {
    sed -n '2,58p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---- phrase vocabulary (extracted by tests) ----
# Instruction-shaped operator-gate fragments (case-insensitive), matched on
# word/phrase boundaries rather than a bare substring — the same discipline
# the loom:blocked taxonomy row already applies to `hold until` / `wait
# until` / `defer` (avoiding false positives like "waiting on CI"). The
# issue's own "credentials" candidate phrasing is narrowed here to "requires
# credentials" / "needs credentials" for the same reason: a bare "credentials"
# substring would false-positive on unrelated text like "store credentials in
# .env" or "credentials are already configured".
PHRASES=(
    'operator-gated'
    'operator-only in substance'
    'operator authorization required'
    'operator decision:'
    'requires operator'
    'login-walled'
    'paid gpu'
    'requires credentials'
    'needs credentials'
)

# Print the FIRST matching phrase's containing fragment (the phrase itself,
# not the whole line) for a body, or nothing if no phrase matches. Matching is
# case-insensitive; iterates PHRASES in declared order so the result is
# deterministic for a body matching more than one phrase.
_operator_gate_phrase_match() {
    local body="$1" phrase lower_body
    lower_body="$(printf '%s' "$body" | tr '[:upper:]' '[:lower:]')"
    for phrase in "${PHRASES[@]}"; do
        if printf '%s' "$lower_body" | grep -qF -- "$phrase"; then
            printf '%s' "$phrase"
            return 0
        fi
    done
    return 1
}

# ---- dependency parser (reused vocabulary, restricted to Depends on / Requires) ----
# Modeled on guide.md's parse_dependencies / #3759's --auto-stack detection —
# tolerant of markdown emphasis/colon between the phrase and #N (#4508).
# Deliberately EXCLUDES `Blocked by` (that phrase drives the distinct
# loom:blocked machinery) and the `- [ ]` task-list form, exactly as
# --auto-stack does. Emits the deduplicated referenced issue numbers.
parse_operator_gate_deps() {
    local body="$1"
    echo "$body" | grep -E '(Depends on|Requires)[*_:[:space:]]*#[0-9]+' \
        | grep -oE '#[0-9]+' | tr -d '#' | sort -un
}

# ---- core (extracted by tests) ----
# Emit advisory annotation line(s) for one candidate. Uses REPO_NWO. Always
# returns 0.
_warn_candidate_operator_gated() {
    local candidate="$1"

    local body
    if [[ -n "$REPO_NWO" ]]; then
        body="$(gh issue view "$candidate" --repo "$REPO_NWO" --json body -q '.body' 2>/dev/null || echo '')"
    else
        body="$(gh issue view "$candidate" --json body -q '.body' 2>/dev/null || echo '')"
    fi
    [[ -n "$body" ]] || return 0

    # Signal 1: direct phrase match in the candidate's own body.
    local matched
    if matched="$(_operator_gate_phrase_match "$body")"; then
        printf '%s\t⚠ body declares operator-gating: "%s"\n' "$candidate" "$matched"
        MATCH_COUNT=$((MATCH_COUNT + 1))
    fi

    # Signal 2: a declared dependency (#A) that itself carries loom:operator-only.
    local dep
    for dep in $(parse_operator_gate_deps "$body"); do
        [[ "$dep" == "$candidate" ]] && continue
        local labels
        if [[ -n "$REPO_NWO" ]]; then
            labels="$(gh issue view "$dep" --repo "$REPO_NWO" --json labels -q '[.labels[].name] | join(",")' 2>/dev/null || echo '')"
        else
            labels="$(gh issue view "$dep" --json labels -q '[.labels[].name] | join(",")' 2>/dev/null || echo '')"
        fi
        if printf '%s' ",$labels," | grep -qF ',loom:operator-only,'; then
            printf '%s\t⚠ depends on #%s, which is loom:operator-only\n' "$candidate" "$dep"
            MATCH_COUNT=$((MATCH_COUNT + 1))
        fi
    done
    return 0
}

# Scan every candidate. Always 0.
_warn_operator_gated() {
    local candidate
    for candidate in $CANDIDATES; do
        _warn_candidate_operator_gated "$candidate"
    done
    return 0
}

# ---- arg parsing ----
CANDIDATES=""
REPO_NWO=""

# Only parse args + run main when executed directly (not when sourced by tests).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --candidates) CANDIDATES="${2:-}"; shift 2 ;;
            --repo)       REPO_NWO="${2:-}"; shift 2 ;;
            --help|-h)    show_help; exit 0 ;;
            --*)          err "Unknown flag: $1"; exit 1 ;;
            *)            err "Unexpected argument: $1"; exit 1 ;;
        esac
    done

    if [[ -z "$CANDIDATES" ]]; then
        err "Usage: warn-operator-gated.sh --candidates \"<N ...>\" [--repo <nwo>]"
        exit 1
    fi

    MATCH_COUNT=0
    _warn_operator_gated
    # Advisory + non-blocking: always exit 0, even when matches were emitted.
    [[ "$MATCH_COUNT" -eq 0 ]] || info "$MATCH_COUNT operator-gate advisory annotation(s) emitted (advisory — sweep proceeds)."
    exit 0
fi
