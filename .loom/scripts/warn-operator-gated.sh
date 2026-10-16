#!/usr/bin/env bash
# Loom Operator-Gate Advisory Scan for /loom:sweep all (#5137)
#
# The `all` sentinel's aggressive candidate taxonomy hard-skips the hold
# labels (`loom:operator`, `loom:blocked`, `loom:frozen`; #11795 folded the
# older operator sub-kinds and `loom:needs-capability` into them) and treats
# every other open issue as a buildable candidate. Some issues
# declare operator-gating in their BODY TEXT ("the index is login-walled, so
# acquisition is operator-gated", "Operator decision: send this paired with
# the paper") without carrying a hold label — so the sentinel would plan to
# dispatch them as ordinary build candidates. Some declare it in their TITLE
# instead ("Operator: visit the county archive and photograph the 1850
# census page") with a body that contains none of the phrase vocabulary
# below — a real 2026-08-17 sweep run missed exactly this shape (#6391).
#
# This tool scans each candidate's already-fetched `body` (the same field the
# survey fetches for ## Affected Files overlap estimation, #4161, and
# Depends on / Requires edge detection, #3759 — no new API call) plus the
# candidate's `title` (one extra `gh issue view --json title` read per
# candidate, #6391) for three independent, ADVISORY-ONLY signals:
#
#   1. Direct phrase match — the body itself contains an instruction-shaped
#      operator-gate fragment (see PHRASES below). Case-insensitive.
#   2. Dependency match — the body declares `Depends on #A` / `Requires #A`
#      (the same restricted vocabulary #3759's --auto-stack and #3747's
#      warn-out-of-set-deps.sh both reuse) and #A currently carries an
#      operator-gate label — the registry's `operator_gate` property
#      (`loom-daemon labels list --property operator_gate --with-retired`,
#      #10013/#11795), with a fallback for a host without a daemon that
#      answers.
#      The #87 -> #4 shape from the issue: the sweep would skip the
#      prerequisite as held, then dispatch the dependent that needs it.
#   3. Title-prefix match (#6391) — the title itself, after stripping leading
#      whitespace/markdown decoration, STARTS WITH `Operator:` / `Operator —`
#      / `Operator-only` (see TITLE_PREFIXES below). Case-insensitive. A
#      PREFIX match, not a substring-anywhere match — a title that merely
#      contains "operator" mid-sentence ("Fix operator dashboard rendering")
#      does not match. Checked independently of the body, so it still fires
#      when the body is empty or contains none of the PHRASES vocabulary.
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
#   ⚠ depends on #<A>, which is <operator-gate label, e.g. loom:operator>
#   ⚠ title declares operator-gating: "<matched prefix>"
# A candidate matching more than one signal prints one line per signal; a
# dependency carrying more than one gate label prints one dependency line per
# label (deduplicated by label, in registry order).
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
    sed -n '2,70p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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
#
# The "human action" pair (#6198) is narrowed the same way: agents commonly
# open an operator-task body with "**Operator task — requires human action, not
# automation.**", which `requires operator` misses by one word. `requires human
# action` / `needs human action` are instruction-shaped; a bare `human`
# substring would false-positive on ordinary prose like "the human-in-the-loop
# design" or "a human reviews the diff".
PHRASES=(
    'operator-gated'
    'operator-only in substance'
    'operator authorization required'
    'operator decision:'
    'operator task'
    'requires operator'
    'requires human action'
    'needs human action'
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

# ---- title-prefix vocabulary (extracted by tests, #6391) ----
# A title that opens by declaring operator-gating up front — "Operator:
# visit the archive", "Operator — renew the domain", "Operator-only: rotate
# this credential" — is a distinct discipline from the PHRASES vocabulary
# above: a PREFIX match (must appear at the true start of the title, after
# stripping leading whitespace/markdown decoration), not a substring-anywhere
# match. A title that merely mentions "operator" mid-sentence ("Fix operator
# dashboard rendering") must NOT match — same false-positive discipline the
# PHRASES vocabulary already applies to bodies.
TITLE_PREFIXES=(
    'Operator:'
    'Operator —'
    'Operator-only'
)

# Return 0 (and print the matched prefix, display-cased as declared in
# TITLE_PREFIXES) when `title`, after stripping leading whitespace and
# markdown decoration (heading `#`, emphasis `*`/`_`, blockquote `>`, list
# marker `-`), starts with one of TITLE_PREFIXES case-insensitively.
# Otherwise return 1 and print nothing.
_operator_gate_title_prefix_match() {
    local title="$1" prefix stripped lower_stripped lower_prefix
    stripped="$(printf '%s' "$title" | sed -E 's/^[[:space:]#*_>-]+//')"
    lower_stripped="$(printf '%s' "$stripped" | tr '[:upper:]' '[:lower:]')"
    for prefix in "${TITLE_PREFIXES[@]}"; do
        lower_prefix="$(printf '%s' "$prefix" | tr '[:upper:]' '[:lower:]')"
        if [[ "$lower_stripped" == "$lower_prefix"* ]]; then
            printf '%s' "$prefix"
            return 0
        fi
    done
    return 1
}

# ---- operator-gate label set (registry-derived, #11795) ----
# The source of truth is the label registry's `operator_gate` property
# (`defaults/labels.json`, #10013), read once per run in
# _warn_operator_gated. `--with-retired` adds the retired names that fold into
# a gate label (loom:operator-only and its sub-kinds), so a dependency `labels
# migrate` has not relabelled yet is still flagged (#11843 drops the flag). The
# fallback (no daemon, or one too old for the flag) names the canonical label
# and its retired names; label_literal_guard.rs pins it to the registry's
# operator_gate set.

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

    # Signal 3: title-prefix match (#6391). Checked independently of the
    # body read below, so it still fires when the body is empty or its
    # early-return below would otherwise skip the rest of this function.
    local title
    if [[ -n "$REPO_NWO" ]]; then
        title="$(gh issue view "$candidate" --repo "$REPO_NWO" --json title -q '.title' 2>/dev/null || echo '')"
    else
        title="$(gh issue view "$candidate" --json title -q '.title' 2>/dev/null || echo '')"
    fi
    if [[ -n "$title" ]]; then
        local title_matched
        if title_matched="$(_operator_gate_title_prefix_match "$title")"; then
            printf '%s\t⚠ title declares operator-gating: "%s"\n' "$candidate" "$title_matched"
            MATCH_COUNT=$((MATCH_COUNT + 1))
        fi
    fi

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

    # Signal 2: a declared dependency (#A) that itself carries an
    # operator-gate label (a held prerequisite means the child cannot itself
    # be completed either).
    # requires-daemon: labels optional   a missing or older binary takes the fallback
    local dep gate_labels; gate_labels="$("${LOOM_DAEMON_BIN:-loom-daemon}" labels list --property operator_gate --with-retired 2>/dev/null)" || gate_labels="loom:operator loom:operator-only loom:operator-decision loom:operator-mechanical loom:operator-blocked"
    for dep in $(parse_operator_gate_deps "$body"); do
        [[ "$dep" == "$candidate" ]] && continue
        local labels gate_label
        if [[ -n "$REPO_NWO" ]]; then
            labels="$(gh issue view "$dep" --repo "$REPO_NWO" --json labels -q '[.labels[].name] | join(",")' 2>/dev/null || echo '')"
        else
            labels="$(gh issue view "$dep" --json labels -q '[.labels[].name] | join(",")' 2>/dev/null || echo '')"
        fi
        for gate_label in $gate_labels; do
            if printf '%s' ",$labels," | grep -qF ",$gate_label,"; then
                printf '%s\t⚠ depends on #%s, which is %s\n' "$candidate" "$dep" "$gate_label"
                MATCH_COUNT=$((MATCH_COUNT + 1))
            fi
        done
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
