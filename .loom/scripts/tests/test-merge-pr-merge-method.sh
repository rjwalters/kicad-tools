#!/usr/bin/env bash
# test-merge-pr-merge-method.sh - Tests for the --merge-method flag (#8845)
#
# Verifies:
#   1. --merge-method appears in --help output.
#   2. CLI rejects bad input early: missing value, unrecognized vocabulary.
#   3. Source contains the resolution logic surface (MERGE_METHOD_REQUESTED,
#      the `loom-daemon forge merge-method` call, the hard-block/warning
#      branches) -- companion to the inline simulation in test 4.
#   4. Inline simulation of the three-way resolution branch (allowed / not
#      allowed / daemon unavailable-or-declined), mirroring the exact
#      decision shape merge-pr.sh's post-parse override block uses, the same
#      "replicate the decision shape" technique
#      test-merge-pr-worktree-path.sh's Test 4 already establishes for this
#      file -- avoids depending on a live forge round-trip for every branch.
#   5. One live end-to-end check: a stubbed `loom-daemon` on PATH that
#      declines the requested method (exit 1) makes merge-pr.sh refuse fast,
#      before ever touching a specific PR, with the daemon's own message
#      surfaced -- never a silent fallback to squash.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-merge-method.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qF "$pattern" "$file"; then pass "$msg"; else fail "$msg (pattern: $pattern)"; fi
}

[[ -x "$MERGE_PR" ]] || { echo "ERROR: $MERGE_PR not executable" >&2; exit 1; }

# --- Test 1: --help mentions --merge-method ---
echo "Test 1: --help documents --merge-method"

set +e
HELP_OUTPUT=$("$MERGE_PR" --help 2>&1)
HELP_RC=$?
set -e
if [[ $HELP_RC -eq 0 ]] && [[ "$HELP_OUTPUT" == *"--merge-method"* ]]; then
    pass "--help output mentions --merge-method"
else
    fail "--help should mention --merge-method (rc=$HELP_RC)"
fi
if [[ "$HELP_OUTPUT" == *"squash|merge|rebase"* || "$HELP_OUTPUT" == *"squash"*"merge"*"rebase"* ]]; then
    pass "--help output names the three merge methods"
else
    fail "--help should name squash/merge/rebase; got: $HELP_OUTPUT"
fi

# --- Test 2: CLI rejects bad --merge-method input early (no network needed:
# vocabulary validation happens inside the arg-parsing loop itself, before
# REPO_NWO is ever used for a daemon call). ---
echo ""
echo "Test 2: CLI rejects bad --merge-method input"

set +e
out=$("$MERGE_PR" --merge-method 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"--merge-method requires a value"* ]]; then
    pass "missing value for --merge-method errors with rc!=0 and clear message"
else
    fail "missing value: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

set +e
out=$("$MERGE_PR" --merge-method fast-forward 1 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"--merge-method must be one of: squash, merge, rebase"* ]]; then
    pass "unrecognized --merge-method vocabulary errors and names the valid set"
else
    fail "bad vocabulary: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

# A recognized value passing the vocabulary check is exercised end-to-end in
# Test 5 below (via a stubbed loom-daemon) rather than here.

# --- Test 3: source contains the resolution logic surface ---
echo ""
echo "Test 3: merge-pr.sh source contains the #8845 resolution logic"

assert_grep 'MERGE_METHOD_REQUESTED=' "$MERGE_PR" \
    "merge-pr.sh declares MERGE_METHOD_REQUESTED state"
assert_grep 'loom-daemon forge merge-method --repo' "$MERGE_PR" \
    "merge-pr.sh calls loom-daemon forge merge-method"
assert_grep 'Merge blocked: $_MPM_OUT' "$MERGE_PR" \
    "a validated-disallowed request (exit 1) hard-blocks with the daemon's message"
assert_grep 'using it unvalidated' "$MERGE_PR" \
    "an unverifiable request (daemon declines/errors) warns rather than blocking"
assert_grep 'loom-daemon not found; using --merge-method' "$MERGE_PR" \
    "a missing loom-daemon warns and falls back to the unvalidated request"

# --- Test 4: inline simulation of the three-way resolution branch ---
echo ""
echo "Test 4: resolution branch (inline simulation)"

# Mirrors the exact decision shape of merge-pr.sh's post-parse override block:
# a stub `loom-daemon` binary's (exit code, stdout) pair resolves to either
# the daemon's answer, a hard block, or an unvalidated pass-through with a
# warning -- never a silent fallback to squash.
simulate_resolve() {
    # Args: $1 daemon_available ("0"/"1")  $2 stub_rc  $3 stub_out
    local daemon_available="$1" stub_rc="$2" stub_out="$3"
    local repo_merge_method="squash" # auto-detected value before any override
    if [[ "$daemon_available" == "1" ]]; then
        if [[ "$stub_rc" -eq 0 ]]; then
            repo_merge_method="$stub_out"
        elif [[ "$stub_rc" -eq 1 ]]; then
            echo "BLOCKED: $stub_out"
            return 1
        else
            echo "WARN: could not validate (exit $stub_rc: $stub_out); using it unvalidated"
            repo_merge_method="requested-value"
        fi
    else
        echo "WARN: loom-daemon not found; using unvalidated"
        repo_merge_method="requested-value"
    fi
    echo "$repo_merge_method"
}

out=$(simulate_resolve "1" "0" "rebase")
if [[ "$out" == "rebase" ]]; then
    pass "allowed request (daemon exit 0) resolves to the daemon's answer"
else
    fail "expected 'rebase', got: $out"
fi

set +e
out=$(simulate_resolve "1" "1" "requested merge method 'merge' is not allowed; allowed method(s): squash")
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == "BLOCKED: requested merge method 'merge' is not allowed; allowed method(s): squash" ]]; then
    pass "disallowed request (daemon exit 1) hard-blocks and names the allowed set"
else
    fail "expected a BLOCKED result naming the allowed set; got rc=$rc, out='$out'"
fi

out=$(simulate_resolve "1" "3" "gitea declined")
if [[ "$out" == *"WARN: could not validate"* ]] && [[ "$out" == *"requested-value"* ]]; then
    pass "an undecided daemon response (e.g. Gitea's decline) warns and uses the request unvalidated -- never a silent squash"
else
    fail "expected a WARN + unvalidated pass-through; got: $out"
fi

out=$(simulate_resolve "0" "0" "")
if [[ "$out" == *"WARN: loom-daemon not found"* ]] && [[ "$out" == *"requested-value"* ]]; then
    pass "a missing daemon warns and uses the request unvalidated (no-daemon fallback, matches pre-#8845 auto-detect posture)"
else
    fail "expected a WARN + unvalidated pass-through; got: $out"
fi

# --- Test 5: live end-to-end -- a stubbed loom-daemon that declines the
# requested method makes merge-pr.sh refuse fast, before ever fetching a
# specific PR (the override block runs right after arg parsing, well before
# any PR-number-specific network call) ---
echo ""
echo "Test 5: live stubbed-daemon end-to-end (disallowed request hard-blocks)"

STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT
cat > "$STUB_DIR/loom-daemon" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == "forge" && "$2" == "merge-method" ]]; then
  echo "requested merge method 'merge' is not allowed by this repository; allowed method(s): squash" >&2
  exit 1
fi
exit 1
STUB
chmod +x "$STUB_DIR/loom-daemon"

set +e
out=$(PATH="$STUB_DIR:$PATH" "$MERGE_PR" --merge-method merge 999999999 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"Merge blocked:"* ]] && [[ "$out" == *"allowed method(s): squash"* ]]; then
    pass "a stubbed daemon declining the request hard-blocks the merge with its own message (never a silent squash fallback)"
else
    fail "expected a 'Merge blocked' refusal naming the allowed set; got rc=$rc, out='$out'"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
