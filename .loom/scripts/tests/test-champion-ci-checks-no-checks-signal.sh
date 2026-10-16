#!/usr/bin/env bash
# test-champion-ci-checks-no-checks-signal.sh - Regression tests for #6211:
# Champion's "no CI checks" merge-gate read couldn't distinguish genuine
# no-checks from a transient `gh pr checks` failure.
#
# Champion's criterion #6 ("CI Status Check") in `champion-pr-merge.md` is
# prose an LLM instance reads and executes, not a standalone script (same
# situation as test-champion-critical-file-check.sh) — so this file mirrors
# the documented `read_ci_checks()` helper in a local function and pins the
# shipped markdown's exact commands with `assert_doc_contains`, catching
# drift between the two.
#
# Bug recap (#6211, follow-up from #6169/PR #6212): both criterion #6 and
# the pre-merge-comment CI-status gather step treated ANY empty stdout from
# `gh pr checks --json bucket,name` as proof "no CI checks are configured
# for this PR" and therefore safe to auto-merge (`CHECKS=$(gh pr checks
# <number> --json bucket,name 2>/dev/null)`, stderr discarded, exit code
# never inspected). But `gh pr checks` can ALSO return empty stdout during a
# transient forge failure (e.g. the intermittent TLS handshake error #6169
# hit, observed ~1 call in 3 on one host) — indistinguishable from genuine
# "no checks configured" once stderr is thrown away. Since this is
# Champion's auto-merge gate, that ambiguity is a real false-positive path.
#
# Fix: `read_ci_checks()` only trusts "no checks" (NO_CHECKS="true") when it
# observes the DOCUMENTED genuine-no-checks signature -- empty stdout,
# nonzero exit, and stderr containing "no checks reported". Any other empty
# read is ambiguous, retried once, and if still ambiguous fails CLOSED
# (NO_CHECKS="unknown") rather than being trusted as "no checks". The
# genuine no-checks case is resolved on the FIRST read (no artificial wait
# for the common checkless-repo case); only an ambiguous read pays the one
# retry.
#
# This file asserts:
#   1. A confirmed genuine no-checks read (empty stdout, nonzero exit,
#      matching stderr) resolves to NO_CHECKS="true" on the first attempt --
#      no retry, no artificial wait.
#   2. A one-off ambiguous empty read (transient failure shape) that clears
#      on retry resolves to NO_CHECKS="false" with the real checks from the
#      retry -- and gh was called exactly twice.
#   3. An ambiguous empty read that persists through the retry resolves to
#      NO_CHECKS="unknown" (fail closed), not "true" -- the exact false
#      positive #6211 reports.
#   4. Real checks on the first read resolve to NO_CHECKS="false" with zero
#      retries (regression guard: the common healthy case is unaffected).
#   5. The shipped markdown in champion-pr-merge.md and
#      champion-reference.md carries the fixed `read_ci_checks()` logic and
#      no longer contains the old blanket-empty-stdout detection.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-ci-checks-no-checks-signal.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (SCRIPTS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (SCRIPTS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_MD="$PROMPT_DIR/champion-pr-merge.md"
CHAMPION_REF_MD="$PROMPT_DIR/champion-reference.md"

# shellcheck source=lib/assert.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# Pin a literal snippet as present verbatim in a doc file — catches drift
# between this test's mirrored function and the shipped markdown.
assert_doc_contains() {
    local file="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" "$file"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (missing literal in $file: $needle)"
    fi
}

# Pin a literal snippet's ABSENCE from a doc file — catches a regression
# back to the unsafe blanket-empty-stdout detection.
assert_doc_lacks() {
    local file="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" "$file"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (found stale/unsafe literal in $file: $needle)"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

# =====================================================================
# champion-pr-merge.md criterion #6's read_ci_checks(), mirrored verbatim
# from defaults/.claude/commands/loom/champion-pr-merge.md (#6211).
# =====================================================================
read_ci_checks() {
  local number="$1" attempt out err_file err rc
  for attempt in 1 2; do
    err_file=$(mktemp)
    out=$(gh pr checks "$number" --json bucket,name 2>"$err_file")
    rc=$?
    err=$(cat "$err_file"); rm -f "$err_file"

    if [ -n "$out" ] && [ "$(printf '%s\n' "$out" | jq 'length')" != "0" ]; then
      CHECKS="$out"; NO_CHECKS="false"
      return 0
    fi

    if [ "$rc" -ne 0 ] && printf '%s' "$err" | grep -qi "no checks reported"; then
      CHECKS=""; NO_CHECKS="true"
      return 0
    fi

    [ "$attempt" -eq 1 ] && sleep 0   # test stub: real doc sleeps 3s here
  done

  CHECKS=""; NO_CHECKS="unknown"
  return 1
}

# --- Stub `gh` so `gh pr checks <number> --json bucket,name` returns canned
# (rc, stdout, stderr) triples in sequence, one per call. State lives in temp
# files so it survives the `$(...)` subshell forks read_ci_checks() makes on
# every call. ---
GH_CALLS_FILE="$(mktemp)"
GH_QUEUE_FILE="$(mktemp)"   # one entry per line: "rc<TAB>stdout<TAB>stderr"
trap 'rm -f "$GH_CALLS_FILE" "$GH_QUEUE_FILE"' EXIT

gh() {
    if [[ "${1:-}" == "pr" && "${2:-}" == "checks" ]]; then
        local n total idx line rc out err
        n=$(($(cat "$GH_CALLS_FILE") + 1))
        echo "$n" > "$GH_CALLS_FILE"
        total=$(wc -l < "$GH_QUEUE_FILE" | tr -d ' ')
        idx=$n
        [[ "$idx" -gt "$total" ]] && idx="$total"
        line=$(sed -n "${idx}p" "$GH_QUEUE_FILE")
        rc=$(printf '%s' "$line" | cut -f1)
        out=$(printf '%s' "$line" | cut -f2)
        err=$(printf '%s' "$line" | cut -f3)
        [[ -n "$out" ]] && printf '%s' "$out"
        [[ -n "$err" ]] && printf '%s' "$err" >&2
        return "$rc"
    fi
    command gh "$@"
}

reset_gh_stub() {
    echo 0 > "$GH_CALLS_FILE"
    : > "$GH_QUEUE_FILE"
}

# Appends one canned (rc, stdout, stderr) response to the gh-stub queue.
queue_gh_response() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$GH_QUEUE_FILE"; }

gh_call_count() { cat "$GH_CALLS_FILE"; }

REAL_CHECKS_JSON='[{"bucket":"pass","name":"build"}]'

echo "--- read_ci_checks: confirmed genuine no-checks signature resolves immediately (#6211) ---"

# (a) The documented genuine-no-checks signature: empty stdout, nonzero
# exit, stderr matching "no checks reported". Must resolve on the FIRST
# read -- no retry, no artificial wait, since it matches immediately.
reset_gh_stub
queue_gh_response "1" "" "no checks reported on the 'feature/foo' branch"
CHECKS=""; NO_CHECKS=""
read_ci_checks "123"
calls="$(gh_call_count)"
assert_eq "true" "$NO_CHECKS" "(a) confirmed 'no checks reported' stderr resolves NO_CHECKS=true"
assert_eq "" "$CHECKS" "(a) CHECKS is empty on the genuine no-checks path"
assert_eq "1" "$calls" "(a) resolved on the FIRST read -- no retry for the common checkless-repo case"

echo
echo "--- read_ci_checks: ambiguous empty read (transient failure shape) that clears on retry (#6211) ---"

# (b) THE bug, reproduced: the first read is empty stdout with a nonzero
# exit but stderr that does NOT match the genuine no-checks signature (a
# transient forge failure -- e.g. an intermittent TLS error, possibly with
# swallowed/blank stderr). Before the fix, empty stdout alone was trusted as
# "no checks" here -- a false positive. After the fix it must retry, and
# once the retry returns real check data, NO_CHECKS must be "false" (checks
# exist), never "true".
reset_gh_stub
queue_gh_response "1" "" ""
queue_gh_response "0" "$REAL_CHECKS_JSON" ""
CHECKS=""; NO_CHECKS=""
read_ci_checks "123"
calls="$(gh_call_count)"
assert_eq "false" "$NO_CHECKS" "(b) an ambiguous empty first read that clears on retry resolves NO_CHECKS=false (checks exist), NOT true"
assert_eq "$REAL_CHECKS_JSON" "$CHECKS" "(b) CHECKS holds the real data recovered on retry"
assert_eq "2" "$calls" "(b) gh was called exactly twice (one retry, not trusting the first empty read)"

echo
echo "--- read_ci_checks: ambiguous empty read that persists fails CLOSED, not as 'no checks' (#6211) ---"

# (c) The literal false-positive #6211 reports: EVERY read comes back
# ambiguous-empty (ordinary empty stdout, nonzero exit, no matching
# stderr -- indistinguishable from a persistent transient failure). This
# must NOT resolve as NO_CHECKS="true" (which would auto-merge over
# possibly-real pending CI) -- it must fail closed as "unknown".
reset_gh_stub
queue_gh_response "1" "" "unexpected EOF"
queue_gh_response "1" "" "unexpected EOF"
CHECKS=""; NO_CHECKS=""
read_ci_checks "123"
rc=$?
calls="$(gh_call_count)"
assert_eq "unknown" "$NO_CHECKS" "(c) a persistently ambiguous empty read resolves NO_CHECKS=unknown (fail closed), NEVER true"
assert_eq "1" "$rc" "(c) read_ci_checks returns nonzero (not settled) on the ambiguous-after-retry outcome"
assert_eq "2" "$calls" "(c) gh was called exactly twice (one retry) before giving up"

echo
echo "--- read_ci_checks: real checks on the first read need no retry (regression guard) ---"

# (d) The ordinary healthy case: real check data on the very first read.
# Must resolve immediately with no unnecessary retry.
reset_gh_stub
queue_gh_response "0" "$REAL_CHECKS_JSON" ""
CHECKS=""; NO_CHECKS=""
read_ci_checks "123"
calls="$(gh_call_count)"
assert_eq "false" "$NO_CHECKS" "(d) real checks present on the first read resolve NO_CHECKS=false"
assert_eq "$REAL_CHECKS_JSON" "$CHECKS" "(d) CHECKS holds the first-read data"
assert_eq "1" "$calls" "(d) only ONE gh call needed for the common healthy case (no unnecessary retry)"

echo
echo "--- Doc pins: champion-pr-merge.md ships the fixed read_ci_checks() (#6211) ---"

assert_doc_contains "$CHAMPION_MD" \
    'read_ci_checks() {' \
    "criterion #6 defines the read_ci_checks() helper"

assert_doc_contains "$CHAMPION_MD" \
    'if [ "$rc" -ne 0 ] && printf '"'"'%s'"'"' "$err" | grep -qi "no checks reported"; then' \
    "read_ci_checks() only trusts 'no checks' on the confirmed rc!=0 + stderr-text signature"

assert_doc_contains "$CHAMPION_MD" \
    'CHECKS=""; NO_CHECKS="unknown"' \
    "read_ci_checks() fails closed to NO_CHECKS=unknown on a persistently ambiguous empty read"

assert_doc_contains "$CHAMPION_MD" \
    'read_ci_checks <number>' \
    "criterion #6's check-loop calls read_ci_checks before evaluating buckets"

assert_doc_contains "$CHAMPION_MD" \
    'echo "SKIP: gh pr checks returned an ambiguous empty read twice in a row' \
    "criterion #6 SKIPs (does not merge) on the ambiguous-after-retry NO_CHECKS=unknown outcome"

assert_doc_lacks "$CHAMPION_MD" \
    'if [ -z "$CHECKS" ] || [ "$(printf '"'"'%s\n'"'"' "$CHECKS" | jq '"'"'length'"'"')" = "0" ]; then' \
    "criterion #6 no longer trusts bare empty stdout alone as 'no checks'"

assert_doc_contains "$CHAMPION_MD" \
    "#6211" \
    "champion-pr-merge.md documents the #6211 fix"

echo
echo "--- Doc pins: pre-merge-comment CI-status gather re-uses read_ci_checks() too (#6211) ---"

assert_doc_contains "$CHAMPION_MD" \
    'read_ci_checks "$PR_NUMBER"' \
    "pre-merge-comment CI-status gather step calls read_ci_checks() (both call sites fixed)"

assert_doc_lacks "$CHAMPION_MD" \
    'CHECKS=$(gh pr checks "$PR_NUMBER" --json bucket,name 2>/dev/null)' \
    "pre-merge-comment CI-status gather step no longer reads gh pr checks with stderr discarded and no retry"

echo
echo "--- Doc pins: champion-reference.md Edge Case 1 matches the fixed behavior (#6211) ---"

assert_doc_contains "$CHAMPION_REF_MD" \
    'read_ci_checks "$PR_NUMBER"' \
    "champion-reference.md Edge Case 1 documents the read_ci_checks() call"

assert_doc_contains "$CHAMPION_REF_MD" \
    'NO_CHECKS="unknown"' \
    "champion-reference.md Edge Case 1 documents the fail-closed 'unknown' outcome"

assert_doc_lacks "$CHAMPION_REF_MD" \
    'CHECKS=$(gh pr checks "$PR_NUMBER" --json bucket,name 2>/dev/null)' \
    "champion-reference.md Edge Case 1 no longer reads gh pr checks with stderr discarded and no retry"

assert_doc_lacks "$CHAMPION_REF_MD" \
    'if [ -z "$CHECKS" ] || [ "$(printf '"'"'%s\n'"'"' "$CHECKS" | jq '"'"'length'"'"')" = "0" ]; then' \
    "champion-reference.md Edge Case 1 no longer trusts bare empty stdout alone as 'no checks'"

assert_doc_contains "$CHAMPION_REF_MD" \
    "#6211" \
    "champion-reference.md documents the #6211 fix"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
