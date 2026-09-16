#!/usr/bin/env bash
# test-label-preflight.sh - Unit tests for defaults/scripts/lib/label-preflight.sh
# (#6716).
#
# Why this lib exists: a role decides on a label (e.g.
# loom:operator-mechanical) and applies it with a bare `gh issue edit
# --add-label`, which can fail SILENTLY if that label simply doesn't exist in
# the target repo's live label set -- the kicad-tools#4507 incident was only
# recoverable because the agent happened to write a prose note about it.
# label-preflight.sh promotes that failure mode to a structural, greppable
# stderr marker (LOOM-LABEL-MISSING / LOOM-LABEL-APPLY-FAILED) instead.
#
# This is an in-process test: the lib is meant to be SOURCED (not exec'd), so
# rather than spawning subprocesses with a stubbed `gh` on PATH, this sources
# the real lib directly and overrides `gh` with a bash FUNCTION in the same
# shell (which bash resolves before a PATH lookup) — the simplest faithful
# stub for library code that calls `gh` unqualified (never `command gh`).
#
# Coverage:
#   1. loom_label_exists: true when the label is in `gh label list`'s output,
#      false when it is absent, false (fail-closed) when gh itself errors.
#   2. loom_apply_issue_label: silent success when `gh issue edit --add-label`
#      succeeds (no stderr, exit 0).
#   3. loom_apply_issue_label: `gh issue edit` fails AND the label does not
#      exist -> exit 1, a single LOOM-LABEL-MISSING stderr line naming the
#      label, the target, and the remediation.
#   4. loom_apply_issue_label: `gh issue edit` fails but the label DOES exist
#      -> exit 1, a LOOM-LABEL-APPLY-FAILED stderr line (a different failure
#      class than #3 — never conflated).
#   5. loom_apply_pr_label mirrors 2-4 via `gh pr edit`.
#   6. The optional `-R OWNER/NAME` repo flag is forwarded to every `gh` call
#      and echoed back in both marker lines.
#   7. Re-sourcing the file is a harmless no-op (source guard).
#
# Usage:
#   ./defaults/scripts/tests/test-label-preflight.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
LIB="$SCRIPTS_DIR/lib/label-preflight.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg (expected: '$expected', actual: '$actual')"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
        pass "$msg"
    else
        fail "$msg (expected substring '$needle' in: '$haystack')"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if ! printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
        pass "$msg"
    else
        fail "$msg (unexpected substring '$needle' in: '$haystack')"
    fi
}

if [[ ! -f "$LIB" ]]; then
    echo -e "${RED}FATAL${NC}: $LIB not found" >&2
    exit 2
fi

GH_LOG_FILE="$(mktemp)"
trap 'rm -f "$GH_LOG_FILE" 2>/dev/null || true' EXIT

# --- gh stub (bash function, resolved before PATH since the lib calls it
#     unqualified) ------------------------------------------------------------
#
#   gh label list ...        -> echoes $GH_STUB_LABELS (one name per line)
#   gh issue edit ... / gh pr edit ...
#                             -> exit $GH_STUB_EDIT_RC (0 succeeds; nonzero
#                                also prints $GH_STUB_EDIT_ERR to mimic a real
#                                gh error message)
gh() {
    printf '%s\n' "$*" >> "$GH_LOG_FILE"
    case "$1" in
        label)
            [[ "$2" == "list" ]] && { printf '%s\n' "${GH_STUB_LABELS:-}"; return 0; }
            ;;
        issue | pr)
            if [[ "$2" == "edit" ]]; then
                if [[ "${GH_STUB_EDIT_RC:-0}" -eq 0 ]]; then
                    return 0
                fi
                echo "${GH_STUB_EDIT_ERR:-stub gh: edit failed}" >&2
                return "${GH_STUB_EDIT_RC}"
            fi
            ;;
    esac
    echo "stub gh: unhandled args: $*" >&2
    return 3
}

# shellcheck source=/dev/null
source "$LIB"

reset_stub() {
    : > "$GH_LOG_FILE"
    GH_STUB_LABELS=""
    GH_STUB_EDIT_RC=0
    GH_STUB_EDIT_ERR=""
}

echo ""
echo "=== loom_label_exists ==="

reset_stub
GH_STUB_LABELS=$'loom:issue\nloom:pr\nloom:operator-only'
if loom_label_exists "loom:pr"; then pass "loom_label_exists true for a present label"; else fail "loom_label_exists true for a present label"; fi
if ! loom_label_exists "loom:operator-mechanical"; then pass "loom_label_exists false for an absent label"; else fail "loom_label_exists false for an absent label"; fi

reset_stub
gh() { printf '%s\n' "$*" >> "$GH_LOG_FILE"; return 1; }
if ! loom_label_exists "loom:pr"; then pass "loom_label_exists fails closed on a gh error"; else fail "loom_label_exists fails closed on a gh error"; fi

echo ""
echo "=== loom_apply_issue_label: success is silent ==="

reset_stub
gh() {
    printf '%s\n' "$*" >> "$GH_LOG_FILE"
    case "$1" in
        label) [[ "$2" == "list" ]] && { printf '%s\n' "${GH_STUB_LABELS:-}"; return 0; } ;;
        issue | pr) [[ "$2" == "edit" ]] && return "${GH_STUB_EDIT_RC:-0}" ;;
    esac
    return 3
}
ERR_OUT="$(loom_apply_issue_label 42 "loom:pr" 2>&1 1>/dev/null)"
RC=$?
assert_eq "0" "$RC" "a successful gh issue edit returns 0"
assert_eq "" "$ERR_OUT" "a successful apply prints nothing on stderr"

echo ""
echo "=== loom_apply_issue_label: label missing -> LOOM-LABEL-MISSING ==="

reset_stub
GH_STUB_LABELS=$'loom:issue\nloom:pr'
GH_STUB_EDIT_RC=1
GH_STUB_EDIT_ERR="could not add label: 'loom:operator-mechanical' not found"
gh() {
    printf '%s\n' "$*" >> "$GH_LOG_FILE"
    case "$1" in
        label) [[ "$2" == "list" ]] && { printf '%s\n' "${GH_STUB_LABELS:-}"; return 0; } ;;
        issue | pr) [[ "$2" == "edit" ]] && { echo "$GH_STUB_EDIT_ERR" >&2; return "$GH_STUB_EDIT_RC"; } ;;
    esac
    return 3
}
ERR_OUT="$(loom_apply_issue_label 4507 "loom:operator-mechanical" 2>&1 1>/dev/null)"
RC=$?
assert_eq "1" "$RC" "a missing-label apply returns 1"
assert_contains "$ERR_OUT" "LOOM-LABEL-MISSING:" "the failure is marked LOOM-LABEL-MISSING"
assert_contains "$ERR_OUT" 'label="loom:operator-mechanical"' "the marker names the label"
assert_contains "$ERR_OUT" 'target="issue #4507"' "the marker names the target issue"
assert_contains "$ERR_OUT" "sync-labels.sh" "the marker names the remediation"
assert_not_contains "$ERR_OUT" "LOOM-LABEL-APPLY-FAILED" "a missing label is never conflated with a generic apply failure"

echo ""
echo "=== loom_apply_issue_label: label exists but edit still fails -> LOOM-LABEL-APPLY-FAILED ==="

reset_stub
GH_STUB_LABELS=$'loom:issue\nloom:pr'
GH_STUB_EDIT_RC=1
GH_STUB_EDIT_ERR="HTTP 403: Resource not accessible by integration"
gh() {
    printf '%s\n' "$*" >> "$GH_LOG_FILE"
    case "$1" in
        label) [[ "$2" == "list" ]] && { printf '%s\n' "${GH_STUB_LABELS:-}"; return 0; } ;;
        issue | pr) [[ "$2" == "edit" ]] && { echo "$GH_STUB_EDIT_ERR" >&2; return "$GH_STUB_EDIT_RC"; } ;;
    esac
    return 3
}
ERR_OUT="$(loom_apply_issue_label 99 "loom:pr" 2>&1 1>/dev/null)"
RC=$?
assert_eq "1" "$RC" "an existing-label apply failure returns 1"
assert_contains "$ERR_OUT" "LOOM-LABEL-APPLY-FAILED:" "the failure is marked LOOM-LABEL-APPLY-FAILED"
assert_contains "$ERR_OUT" "403" "the marker includes gh's underlying error text"
assert_not_contains "$ERR_OUT" "LOOM-LABEL-MISSING" "an existing label is never reported as missing"

echo ""
echo "=== loom_apply_pr_label mirrors the issue path via 'gh pr edit' ==="

reset_stub
GH_STUB_LABELS=""
GH_STUB_EDIT_RC=1
GH_STUB_EDIT_ERR="not found"
gh() {
    printf '%s\n' "$*" >> "$GH_LOG_FILE"
    case "$1" in
        label) [[ "$2" == "list" ]] && { printf '%s\n' "${GH_STUB_LABELS:-}"; return 0; } ;;
        issue | pr) [[ "$2" == "edit" ]] && { echo "$GH_STUB_EDIT_ERR" >&2; return "$GH_STUB_EDIT_RC"; } ;;
    esac
    return 3
}
ERR_OUT="$(loom_apply_pr_label 777 "loom:pr" 2>&1 1>/dev/null)"
assert_contains "$ERR_OUT" 'target="pr #777"' "the PR path names a pr target, not an issue"
assert_contains "$(cat "$GH_LOG_FILE")" "pr edit 777" "loom_apply_pr_label calls 'gh pr edit', not 'gh issue edit'"

echo ""
echo "=== -R OWNER/NAME is forwarded to every gh call and echoed in the marker ==="

reset_stub
GH_STUB_LABELS=""
GH_STUB_EDIT_RC=1
gh() {
    printf '%s\n' "$*" >> "$GH_LOG_FILE"
    case "$1" in
        label) [[ "$2" == "list" ]] && { printf '%s\n' "${GH_STUB_LABELS:-}"; return 0; } ;;
        issue | pr) [[ "$2" == "edit" ]] && return "${GH_STUB_EDIT_RC:-0}" ;;
    esac
    return 3
}
ERR_OUT="$(loom_apply_issue_label 10 "loom:pr" -R "octocat/hello-world" 2>&1 1>/dev/null)"
assert_contains "$ERR_OUT" 'repo="octocat/hello-world"' "the marker names the override repo"
assert_contains "$(cat "$GH_LOG_FILE")" "issue edit 10 -R octocat/hello-world --add-label loom:pr" \
    "the -R flag is forwarded to the gh issue edit call"
assert_contains "$(cat "$GH_LOG_FILE")" "label list -R octocat/hello-world" \
    "the -R flag is forwarded to the gh label list fallback lookup too"

echo ""
echo "=== source guard: re-sourcing is a harmless no-op ==="

BEFORE_TYPE="$(type -t loom_label_exists)"
# shellcheck source=/dev/null
source "$LIB"
AFTER_TYPE="$(type -t loom_label_exists)"
assert_eq "$BEFORE_TYPE" "$AFTER_TYPE" "re-sourcing the lib does not break the function definitions"
assert_eq "function" "$AFTER_TYPE" "loom_label_exists remains a function after a second source"

echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
