#!/usr/bin/env bash
# detect-unlabeled-epics.sh - Report-only backstop: find open issues that
# declare an epic shape but do not carry the `loom:epic` label (#6715).
#
# WHY (#6715): an epic that loses `loom:epic` — by a bug in a code path that
# mutates it (the originally-reported, but unreproduced, hypothesis), a
# manual edit, or a stale/forked consumer install running old prompt text —
# becomes permanently invisible to Champion's epic-discovery query, since
# that query filters ON the label. #6715's primary fix is a doc caveat plus a
# regression test on the one escalation path that was suspected
# (test-epic-label-preserved-on-escalation.sh); this script is the
# defense-in-depth backstop that catches the symptom directly, no matter
# which code path (or human) caused it, and works against ANY repo's live
# issues, not just this one.
#
# DETECTION: an open issue is "epic-shaped" if it matches ANY ONE of:
#   1. TITLE PREFIX — the title, after stripping leading markdown decoration
#      (heading `#`, emphasis `*`/`_`, blockquote `>`, list marker `-`),
#      starts with "Epic:" case-insensitively. Mirrors the real convention in
#      epic.md's "Create the Epic" step (`--title "Epic: [Title]"`) and the
#      title-prefix discipline already used by warn-operator-gated.sh (#6391).
#   2. PHASES HEADING — the body contains a `## Phases` heading, the real
#      structural marker epic.md's template puts in every epic body.
#   3. CHILD REFERENCE — the issue's own number is referenced as
#      "[Epic #<N>]" in the TITLE of an OPEN `loom:epic-phase` issue — the
#      real convention epic.md's Phase 5 step uses when filing phase issues
#      (`--title "[Epic #$EPIC_NUMBER] [Issue Title]"`).
#
# REPORT-ONLY — never mutates a label, comment, or issue state. Follows the
# same read-only, report-don't-mutate convention as check-phantom-labels.sh
# and warn-operator-gated.sh. Always exits 0 on a successful scan (advisory,
# non-blocking — including when candidates were found), matching
# warn-operator-gated.sh's convention; a real precondition/forge failure
# exits non-zero (see Exit codes below).
#
# DISMISSAL (edge case, #6715 test plan): an epic that legitimately lost
# `loom:epic` via a deliberate human reclassification should not be reported
# forever.
#   - `--dismiss "N1 N2 ..."` excludes issue numbers for this run only.
#   - `--dismiss-file <path>` (default: .loom/state/detect-unlabeled-epics-dismissed
#     if present, otherwise none) holds one issue number per line — blank
#     lines and `#`-comments ignored — for a persistent exclusion a human
#     maintains by hand. No forge write, no relabeling: purely a local filter
#     on this script's own report.
#
# Usage:
#   detect-unlabeled-epics.sh [--repo OWNER/NAME] [--dismiss "N1 N2 ..."]
#                              [--dismiss-file <path>] [--limit N] [--json]
#
# Options:
#   --repo OWNER/NAME    Target a specific repo (default: current repo's git remote).
#   --dismiss "N1 N2"    Space-separated issue numbers to exclude from this run.
#   --dismiss-file PATH  File of issue numbers (one per line) to exclude.
#                        Defaults to .loom/state/detect-unlabeled-epics-dismissed
#                        if it exists; pass an empty string to disable.
#   --limit N            Max open issues to scan (default: 500).
#   --json               Emit a JSON summary instead of human-readable lines.
#   --help, -h           Show this help.
#
# Output (human mode, stdout, one line per candidate, silent when none found):
#   <N>\t⚠ <reason>
# where <reason> is one of:
#   title declares an epic: "<matched prefix>"
#   body has a "## Phases" heading
#   referenced as [Epic #<N>] by open phase issue #<M>
# A candidate matching more than one signal prints one line per signal.
#
# Exit codes:
#   0 - scan completed (report may be empty or non-empty — advisory only)
#   1 - usage / precondition failure (e.g. unresolvable repo)
#   2 - forge read failure (e.g. `gh issue list` errored)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Two levels up from either layout this ships in: defaults/scripts/<this> or
# the installed .loom/scripts/<this> — both are exactly repo-root/X/scripts/.
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ -t 2 ]]; then
    RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; BLUE=''; NC=''
fi
err()  { echo -e "${RED}ERROR: $1${NC}" >&2; }
info() { echo -e "${BLUE}ℹ $1${NC}" >&2; }

show_help() {
    sed -n '2,66p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---- title-prefix vocabulary (extracted by tests) ----
# Mirrors the real epic.md title convention (`--title "Epic: [Title]"`).
TITLE_PREFIXES=(
    'Epic:'
)

# Return 0 (and print the matched prefix) when `title`, after stripping
# leading whitespace and markdown decoration (heading `#`, emphasis `*`/`_`,
# blockquote `>`, list marker `-`), starts with one of TITLE_PREFIXES
# case-insensitively. Otherwise return 1 and print nothing. Same discipline
# as warn-operator-gated.sh's _operator_gate_title_prefix_match (#6391).
_epic_title_prefix_match() {
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

# Return 0 when `body` contains a "## Phases" heading line (whitespace after
# the `##` is tolerant; the heading text itself must match exactly, matching
# epic.md's literal template).
_epic_body_has_phases_heading() {
    local body="$1"
    grep -qE '^##[[:space:]]+Phases[[:space:]]*$' <<<"$body"
}

# Extract every N referenced as "[Epic #N]" in `text` (title or body),
# deduplicated. Prints one number per line; nothing if no match.
_extract_epic_child_refs() {
    local text="$1"
    grep -oE '\[Epic #[0-9]+\]' <<<"$text" | grep -oE '[0-9]+' | sort -un
}

# Return 0 when `issue_num` (bare, no '#') appears as a whole token in the
# space-separated `dismiss_list`.
_is_dismissed() {
    local issue_num="$1" dismiss_list="$2" tok
    for tok in $dismiss_list; do
        [[ "$tok" == "$issue_num" ]] && return 0
    done
    return 1
}

# Read a dismiss file (blank lines / #-comments ignored) into a
# space-separated list on stdout. Missing/empty path -> prints nothing.
_read_dismiss_file() {
    local path="$1"
    [[ -n "$path" && -f "$path" ]] || return 0
    grep -vE '^[[:space:]]*(#|$)' "$path" | tr -s '[:space:]' ' '
}

# ---- core scan (extracted by tests) ----
# Given the raw `gh issue list --json number,title,body,labels` array on
# stdin, print one "<N>\t⚠ <reason>" line per (candidate, signal), honoring
# DISMISS_LIST (space-separated issue numbers, global). Sets MATCH_COUNT.
_scan_unlabeled_epics() {
    local issues_json="$1"

    # Pass 1: collect every child reference "[Epic #N]" from OPEN
    # loom:epic-phase issues' titles (the real convention) -- reference set,
    # keyed by referencing child issue number so the report can cite it.
    local referenced_by refs child_num child_title child_body child_labels ref
    referenced_by=""
    while IFS= read -r issue; do
        child_num="$(jq -r '.number' <<<"$issue")"
        child_labels="$(jq -r '[.labels[].name] | join(",")' <<<"$issue")"
        [[ ",$child_labels," == *",loom:epic-phase,"* ]] || continue
        child_title="$(jq -r '.title' <<<"$issue")"
        child_body="$(jq -r '.body // ""' <<<"$issue")"
        refs="$(_extract_epic_child_refs "$child_title")"$'\n'"$(_extract_epic_child_refs "$child_body")"
        for ref in $refs; do
            [[ -n "$ref" ]] || continue
            referenced_by+="$ref:$child_num"$'\n'
        done
    done < <(jq -c '.[]' <<<"$issues_json")

    # Pass 2: classify every open issue.
    local num title body labels title_match
    while IFS= read -r issue; do
        num="$(jq -r '.number' <<<"$issue")"
        title="$(jq -r '.title' <<<"$issue")"
        body="$(jq -r '.body // ""' <<<"$issue")"
        labels="$(jq -r '[.labels[].name] | join(",")' <<<"$issue")"

        # Already labeled -- not a candidate.
        [[ ",$labels," == *",loom:epic,"* ]] && continue
        # Dismissed -- filtered by design, not a hard-fail.
        _is_dismissed "$num" "$DISMISS_LIST" && continue

        if title_match="$(_epic_title_prefix_match "$title")"; then
            printf '%s\t⚠ title declares an epic: "%s"\n' "$num" "$title_match"
            MATCH_COUNT=$((MATCH_COUNT + 1))
        fi

        if _epic_body_has_phases_heading "$body"; then
            printf '%s\t⚠ body has a "## Phases" heading\n' "$num"
            MATCH_COUNT=$((MATCH_COUNT + 1))
        fi

        while IFS=: read -r ref_epic ref_child; do
            [[ "$ref_epic" == "$num" ]] || continue
            printf '%s\t⚠ referenced as [Epic #%s] by open phase issue #%s\n' "$num" "$num" "$ref_child"
            MATCH_COUNT=$((MATCH_COUNT + 1))
        done <<<"$referenced_by"
    done < <(jq -c '.[]' <<<"$issues_json")
    return 0
}

# ---- arg parsing + main (skipped when sourced by tests) ----
REPO_NWO_ARG=""
DISMISS_CLI=""
DISMISS_FILE_ARG=""
DISMISS_FILE_SET=false
LIMIT=500
JSON_OUTPUT=false

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --repo)         REPO_NWO_ARG="${2:-}"; shift 2 ;;
            --dismiss)      DISMISS_CLI="${2:-}"; shift 2 ;;
            --dismiss-file) DISMISS_FILE_ARG="${2:-}"; DISMISS_FILE_SET=true; shift 2 ;;
            --limit)        LIMIT="${2:-500}"; shift 2 ;;
            --json)         JSON_OUTPUT=true; shift ;;
            --help|-h)      show_help; exit 0 ;;
            --*)            err "Unknown flag: $1"; exit 1 ;;
            *)              err "Unexpected argument: $1"; exit 1 ;;
        esac
    done

    # shellcheck source=lib/forge-helpers.sh
    source "$SCRIPT_DIR/lib/forge-helpers.sh"
    # forge-helpers.sh sets `-e` on the current shell. This script is
    # deliberately advisory (like warn-operator-gated.sh): a single issue
    # whose title/body doesn't match a signal is expected to make an
    # internal grep return 1, and that must NOT abort the whole scan when
    # captured via a plain `var=$(...)` assignment (an assignment-only
    # simple command's failing status DOES trigger errexit in bash). Restore
    # this script's own top-of-file `set -uo pipefail` (no `-e`).
    set +e -u -o pipefail

    forge_detect
    if [[ -n "$REPO_NWO_ARG" ]]; then
        REPO_NWO="$REPO_NWO_ARG"
    else
        REPO_NWO="$(forge_get_repo_nwo gh)"
    fi
    if [[ -z "$REPO_NWO" ]]; then
        err "could not resolve a repo (pass --repo OWNER/NAME)"
        exit 1
    fi
    if [[ "$FORGE_TYPE" != "github" ]]; then
        err "detect-unlabeled-epics.sh only supports GitHub today (FORGE_TYPE=$FORGE_TYPE)"
        exit 1
    fi

    if [[ "$DISMISS_FILE_SET" == "true" ]]; then
        DISMISS_FILE="$DISMISS_FILE_ARG"
    else
        DISMISS_FILE="$REPO_ROOT/.loom/state/detect-unlabeled-epics-dismissed"
    fi
    DISMISS_LIST="$DISMISS_CLI $(_read_dismiss_file "$DISMISS_FILE")"

    if [[ "$JSON_OUTPUT" != "true" ]]; then
        info "Scanning $REPO_NWO for open issues declaring an epic shape without loom:epic..."
    fi

    ISSUES_JSON="$(gh issue list --repo "$REPO_NWO" --state open \
        --json number,title,body,labels --limit "$LIMIT" 2>/dev/null || true)"
    if [[ -z "$ISSUES_JSON" || "$ISSUES_JSON" == "null" ]]; then
        err "could not read open issues from $REPO_NWO"
        exit 2
    fi

    MATCH_COUNT=0
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        RESULT_TSV="$(_scan_unlabeled_epics "$ISSUES_JSON")"
        if [[ -n "$RESULT_TSV" ]]; then
            jq -n --arg repo "$REPO_NWO" \
                --argjson candidates "$(printf '%s\n' "$RESULT_TSV" | jq -R -s '
                    split("\n") | map(select(length > 0)) |
                    map(split("\t")) | map({issue: (.[0]|tonumber), reason: .[1]})')" \
                '{repo: $repo, candidates: $candidates}'
        else
            jq -n --arg repo "$REPO_NWO" '{repo: $repo, candidates: []}'
        fi
    else
        _scan_unlabeled_epics "$ISSUES_JSON"
        if [[ "$MATCH_COUNT" -eq 0 ]]; then
            info "No unlabeled epics found."
        else
            info "$MATCH_COUNT candidate line(s) found — report-only, no labels changed. Dismiss a false positive with --dismiss \"<N>\" or add it to $DISMISS_FILE."
        fi
    fi
    exit 0
fi
