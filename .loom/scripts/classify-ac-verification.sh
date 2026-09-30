#!/usr/bin/env bash
# classify-ac-verification.sh - Champion's out-of-band acceptance-criteria gate
# (#6883).
#
# WHY THIS EXISTS
#
# Champion's "Step 4: Verify Issue Auto-Close" converts "PR merged" into "issue
# done". That inference is sound when every acceptance criterion is something CI
# can check. It is UNSOUND when a criterion names a verification the suite
# structurally cannot perform -- a live external source, a real scheduled run,
# or an observation over time. In the incident behind #6883 a filter silently
# dropped items, the regression test FABRICATED the external payload the fix
# assumed, the suite went green, Champion closed the issue, and the item kept
# being dropped on the very next real run. The AC had said, close to verbatim,
# "confirm the dropped item is processed on the next real run" -- and nothing in
# the pipeline noticed that step had never been performed.
#
# This script is the mechanical half of the fix: it reads an issue body, finds
# its acceptance-criteria checklist, and classifies each item as CI-checkable or
# as requiring out-of-band verification, using a fixed, documented phrase
# vocabulary (below) rather than unstructured judgment. It then looks for the
# `<!-- loom:ac-verified sha=<head> -->` evidence marker that a Builder, author,
# or Judge posts to assert the out-of-band step was actually performed.
#
# The prose contract this enforces lives in
# `defaults/.claude/commands/loom/champion-pr-merge.md`, Step 4 -> "Out-of-Band
# Acceptance-Criteria Gate". Keep the OUT_OF_BAND_PHRASES vocabulary below in
# sync with the phrase list documented there -- this is the enforcement side,
# that doc is the human-readable side.
#
# ASYMMETRY OF THE TWO ERROR DIRECTIONS (why the vocabulary is tuned the way it
# is). A false NEGATIVE reproduces the exact incident: an unmet criterion is
# closed out silently and nobody ever learns. A false POSITIVE leaves an issue
# open with a comment naming the criterion, which any human -- or a one-line
# evidence marker -- clears in seconds. The costs are not symmetric, so where a
# phrase is genuinely ambiguous this vocabulary keeps it. What it does NOT do is
# reach for bare words (`live`, `real`, `run`, `verify`, `production`), which
# appear in ordinary engineering prose constantly and would flag nearly every
# issue -- same instruction-shaped-fragment discipline warn-operator-gated.sh
# applies to its own PHRASES vocabulary.
#
# Usage:
#   # Forge mode (what Champion runs):
#   classify-ac-verification.sh --issue <N> [--pr <N>] [--repo <nwo>] [--head-sha <sha>]
#
#   # Hermetic mode (what the tests drive; no forge calls):
#   classify-ac-verification.sh --body-file <path> [--evidence-file <path>] [--head-sha <sha>]
#
# Options:
#   --issue <N>          Issue whose acceptance criteria are classified (forge mode).
#   --pr <N>             PR being merged. Supplies the head SHA the marker must
#                        carry, and its body+comments are searched for the marker
#                        alongside the issue's.
#   --repo <nwo>         Repo owner/name (optional; defaults to the current repo).
#   --head-sha <sha>     Override the head SHA the marker is checked against.
#                        Required in hermetic mode when an evidence file is given.
#   --body-file <path>   Read the issue body from a file instead of the forge.
#   --evidence-file <path>  Read the marker-search text (comments, PR body) from
#                        a file instead of the forge.
#   --help,-h            Show this help.
#
# Output (stdout, one tab-separated line per OUT-OF-BAND criterion; silent when
# there are none):
#   <matched phrase>\t<criterion text, whitespace-normalized across wrapped lines>
#
# Exit codes:
#   0  CLEAR         - an AC checklist was found; no item requires out-of-band
#                      verification. Champion closes as it always has.
#   10 NO-AC         - no acceptance-criteria checklist found at all. Champion's
#                      existing purely-mechanical Step 4 behavior is unchanged --
#                      this is the common case and must stay a no-op.
#   11 SATISFIED     - out-of-band criteria found AND a current-SHA
#                      `loom:ac-verified` marker is present. Champion closes.
#   12 UNVERIFIED    - out-of-band criteria found and NO marker. Champion HOLDS
#                      the close (reopening if GitHub already auto-closed) and
#                      comments, quoting the criteria printed on stdout.
#   13 STALE-MARKER  - out-of-band criteria found; a marker exists but its SHA is
#                      not the PR's head. Same HOLD action as 12: the evidence
#                      describes a tree that is not the one being merged.
#   1  ERROR         - usage / precondition failure. FAILS CLOSED: the caller must
#                      treat this like 12 (hold and report), never like 0 --
#                      "the classifier could not run" is not "the criteria are met".

set -uo pipefail

# Colors (skip when not a TTY).
if [[ -t 2 ]]; then
    RED='\033[0;31m'; NC='\033[0m'
else
    RED=''; NC=''
fi
err() { echo -e "${RED}ERROR: $1${NC}" >&2; }

show_help() {
    sed -n '2,80p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---- out-of-band phrase vocabulary (extracted by tests) ----
#
# Instruction-shaped fragments, matched case-insensitively as substrings of ONE
# acceptance-criterion item (the bullet plus its wrapped continuation lines,
# joined and whitespace-normalized). Grouped by the three failure shapes #6883
# names; the groups are documentation only -- matching iterates the array in
# declared order so the reported phrase is deterministic for a criterion that
# matches more than one.
#
# DELIBERATELY EXCLUDED bare words, and why: `live` ("live reload"), `real`
# ("real-world example"), `run` ("run the test suite"), `verify` ("verify the
# output"), `production` ("production build"), `manual` ("manual page"),
# `observe` ("observe the invariant"), `end-to-end` (an end-to-end *test* is
# ordinarily CI-checkable). Each of those appears in ordinary acceptance-criteria
# prose constantly; a bare-substring vocabulary built from them would flag
# essentially every issue and train Champion's readers to ignore the gate.
OUT_OF_BAND_PHRASES=(
    # (1) requires a live external source
    'live verification'
    'live run'
    'live source'
    'live site'
    'against the live'
    'against production'
    'production run'
    'real response'
    'real page'
    'real api'
    # (2) requires a real scheduled run
    'real run'
    'next run'
    'scheduled run'
    'cron run'
    # (3) requires an observation over time
    'over the next'
    'observed over'
    'observe over'
    'over a period'
    'in the wild'
    # (4) explicitly declared out-of-band / manual by the author
    'out-of-band'
    'out of band'
    'manual verification'
    'manually verify'
    'verify manually'
    'manually confirm'
)

# Print the FIRST matching phrase for a criterion, or nothing. Case-insensitive;
# iterates OUT_OF_BAND_PHRASES in declared order for determinism.
_ac_out_of_band_phrase_match() {
    local item="$1" phrase lower_item
    lower_item="$(printf '%s' "$item" | tr '[:upper:]' '[:lower:]')"
    for phrase in "${OUT_OF_BAND_PHRASES[@]}"; do
        if printf '%s' "$lower_item" | grep -qF -- "$phrase"; then
            printf '%s' "$phrase"
            return 0
        fi
    done
    return 1
}

# ---- acceptance-criteria checklist extraction (extracted by tests) ----
#
# An "acceptance-criteria checklist" is: every markdown task-list item
# (`- [ ]` / `- [x]` / `* [ ]`, any indentation) that appears under a heading
# whose text contains "acceptance criteria" (case-insensitive), up to the next
# heading of ANY level. This deliberately covers the several headings this fleet
# actually emits -- "## Acceptance Criteria", "## Suggested acceptance criteria",
# "### Sharpened Acceptance Criteria" -- and deliberately does NOT sweep every
# checkbox in the body: `## Test Plan` and `## Dependencies` checklists are other
# roles' machinery (Judge's test-plan posture is explicitly non-blocking) and
# folding them in here would hold issues on steps this gate has no standing to
# gate. A body with no such heading yields no items at all -> exit 10.
#
# A criterion's text is its bullet line plus every following line that is not
# blank, not a new task-list item, and not a heading -- markdown's own wrapped-
# continuation shape, which the Curator's own sharpened ACs use heavily. Lines
# are joined with single spaces and whitespace-collapsed so the reported text is
# one greppable line.
#
# CHECKED BOXES ARE NOT EVIDENCE. A `- [x]` item is classified exactly like
# `- [ ]`. The incident this gate closes is precisely a case where the author who
# checked the box was the person whose assumption was untested; a checkbox
# carries no author, no timestamp, and no tree binding. The `loom:ac-verified`
# marker does carry all three, and it is the only thing that satisfies this gate.
_extract_ac_items() {
    local body="$1"
    printf '%s\n' "$body" | awk '
        function flush() {
            if (buf != "") {
                gsub(/[[:space:]]+/, " ", buf)
                sub(/^ /, "", buf); sub(/ $/, "", buf)
                if (buf != "") print buf
            }
            buf = ""
        }
        # Heading: enter/leave the AC section, and always terminate a pending
        # item. `#+` rather than `#{1,6}` -- interval expressions are not
        # portable across every awk this fleet runs on, and no markdown line
        # starting with seven or more `#` is anything but a heading anyway.
        /^[[:space:]]*#+[[:space:]]/ {
            flush()
            heading = tolower($0)
            in_ac = (heading ~ /acceptance criteria/) ? 1 : 0
            next
        }
        in_ac == 0 { next }
        # New task-list item.
        /^[[:space:]]*[-*][[:space:]]+\[[ xX]\]/ {
            flush()
            line = $0
            sub(/^[[:space:]]*[-*][[:space:]]+\[[ xX]\][[:space:]]*/, "", line)
            buf = line
            next
        }
        # Blank line ends the current item.
        /^[[:space:]]*$/ { flush(); next }
        # Any other non-blank line continues the current item (wrapped bullet).
        { if (buf != "") buf = buf " " $0 }
        END { flush() }
    '
}

# ---- evidence marker (extracted by tests) ----
#
# `<!-- loom:ac-verified sha=<head> -->`, modeled on judge.md's
# `<!-- loom:verdict-sha sha=... verdict=... -->` (#5686) and anchored the same
# way require-complexity-marker.sh / extract-capability-markers.sh anchor theirs
# (#4840): the full `<!-- ... -->` comment form with a HEX sha, so prose that
# merely quotes the syntax with a `<head>` placeholder can never be mistaken for
# a live marker. Prints every marker SHA found, one per line.
_extract_ac_verified_shas() {
    printf '%s' "$1" \
        | grep -oiE '<!--[[:space:]]*loom:ac-verified[[:space:]]+sha=[0-9a-fA-F]{7,40}[[:space:]]*-->' \
        | sed -E 's/.*[sS][hH][aA]=([0-9a-fA-F]{7,40}).*/\1/' \
        | tr '[:upper:]' '[:lower:]'
}

# True when marker_sha and head_sha name the same commit. Abbreviation-tolerant
# in either direction (a marker may carry a short SHA; `gh` returns the full 40),
# matching how every other short-SHA comparison in this fleet is done.
_ac_sha_matches() {
    local marker_sha head_sha
    marker_sha="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    head_sha="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
    [[ -n "$marker_sha" && -n "$head_sha" ]] || return 1
    [[ "$head_sha" == "$marker_sha"* || "$marker_sha" == "$head_sha"* ]]
}

# ---- core (extracted by tests) ----
# Classify $BODY against $EVIDENCE/$HEAD_SHA. Prints out-of-band criteria to
# stdout; returns the exit code documented in the header.
_classify_ac() {
    local items out_of_band=0 item phrase

    items="$(_extract_ac_items "$BODY")"
    if [[ -z "$items" ]]; then
        return 10
    fi

    while IFS= read -r item; do
        [[ -n "$item" ]] || continue
        if phrase="$(_ac_out_of_band_phrase_match "$item")"; then
            printf '%s\t%s\n' "$phrase" "$item"
            out_of_band=$((out_of_band + 1))
        fi
    done <<<"$items"

    if [[ "$out_of_band" -eq 0 ]]; then
        return 0
    fi

    # At least one out-of-band criterion. Is there evidence it was performed?
    local marker_shas found_marker=0 sha
    marker_shas="$(_extract_ac_verified_shas "$EVIDENCE")"
    if [[ -z "$marker_shas" ]]; then
        return 12
    fi
    while IFS= read -r sha; do
        [[ -n "$sha" ]] || continue
        found_marker=1
        if _ac_sha_matches "$sha" "$HEAD_SHA"; then
            return 11
        fi
    done <<<"$marker_shas"

    # A marker exists but none of them describe the tree being merged. This is
    # the same fail-safe shape as a stale verdict: evidence about another tree is
    # not evidence about this one.
    [[ "$found_marker" -eq 1 ]] && return 13
    return 12
}

# ---- arg parsing ----
ISSUE=""
PR=""
REPO_NWO=""
HEAD_SHA=""
BODY_FILE=""
EVIDENCE_FILE=""
BODY=""
EVIDENCE=""

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --issue)         ISSUE="${2:-}"; shift 2 ;;
            --pr)            PR="${2:-}"; shift 2 ;;
            --repo)          REPO_NWO="${2:-}"; shift 2 ;;
            --head-sha)      HEAD_SHA="${2:-}"; shift 2 ;;
            --body-file)     BODY_FILE="${2:-}"; shift 2 ;;
            --evidence-file) EVIDENCE_FILE="${2:-}"; shift 2 ;;
            --help|-h)       show_help; exit 0 ;;
            --*)             err "Unknown flag: $1"; exit 1 ;;
            *)               err "Unexpected argument: $1"; exit 1 ;;
        esac
    done

    if [[ -n "$BODY_FILE" ]]; then
        if [[ ! -f "$BODY_FILE" ]]; then
            err "--body-file not found: $BODY_FILE"
            exit 1
        fi
        BODY="$(cat "$BODY_FILE")"
        if [[ -n "$EVIDENCE_FILE" ]]; then
            if [[ ! -f "$EVIDENCE_FILE" ]]; then
                err "--evidence-file not found: $EVIDENCE_FILE"
                exit 1
            fi
            EVIDENCE="$(cat "$EVIDENCE_FILE")"
        fi
    elif [[ -n "$ISSUE" ]]; then
        REPO_ARGS=()
        [[ -n "$REPO_NWO" ]] && REPO_ARGS=(--repo "$REPO_NWO")

        BODY="$(gh issue view "$ISSUE" "${REPO_ARGS[@]}" --json body -q '.body' 2>/dev/null)" || {
            err "could not read issue #$ISSUE"
            exit 1
        }
        # Evidence may live on the issue's comments OR on the merged PR (body or
        # comments), and counts only from a TRUSTED author (#9548: an outsider's
        # marker is prose; the PR body only when the PR's author is trusted).
        # requires-daemon: forge optional   Without `trusted-comments --fetch` (absent or older binary) there is no evidence: exit 12, the issue stays held open (the fail-safe answer).
        EVIDENCE="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge trusted-comments --fetch "$ISSUE" "${REPO_ARGS[@]}" 2>/dev/null | jq -r '.[].body' || echo '')"
        if [[ -n "$PR" ]]; then
            EVIDENCE="$EVIDENCE
$("${LOOM_DAEMON_BIN:-loom-daemon}" forge trusted-comments --fetch "$PR" --with-body "${REPO_ARGS[@]}" 2>/dev/null | jq -r '.[].body' || echo '')"
            if [[ -z "$HEAD_SHA" ]]; then
                HEAD_SHA="$(gh pr view "$PR" "${REPO_ARGS[@]}" --json headRefOid -q '.headRefOid' 2>/dev/null || echo '')"
            fi
        fi
    else
        err "Usage: classify-ac-verification.sh --issue <N> [--pr <N>] [--repo <nwo>] | --body-file <path> [--evidence-file <path>] [--head-sha <sha>]"
        exit 1
    fi

    _classify_ac
    exit $?
fi
