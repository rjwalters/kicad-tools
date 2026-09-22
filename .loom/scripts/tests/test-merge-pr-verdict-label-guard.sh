#!/usr/bin/env bash
# test-merge-pr-verdict-label-guard.sh - Unit tests for the PRE-merge
# verdict-label contradiction guard in merge-pr.sh (#8112).
#
# Two concurrent Judge passes on the same PR head can reach different
# verdicts roughly a minute apart (observed on PR #8076), leaving a PR
# carrying BOTH `loom:pr` (approved) and a blocking/contradicting label
# (`loom:changes-requested`, `loom:blocked`, `loom:operator`, or
# `loom:review-requested`) simultaneously. `_check_loom_pr_label` only
# checks `loom:pr`'s ABSENCE (#7419); it has no way to see a contradicting
# label standing beside a PRESENT `loom:pr`. `_check_verdict_label_
# contradiction` closes that gap: it hard-blocks the merge (error, exit 1)
# naming BOTH offending labels, `--dry-run` reports the would-be block
# without exiting 1, and there is deliberately no bypass flag (not even
# `--allow-unapproved`, which covers a different act — see merge-pr.sh's own
# comment above the guard).
#
# The decision logic itself (which label pairs count as a contradiction) is
# `loom-daemon merge-pr verdict-contradiction` (Rust,
# loom-daemon/src/merge_pr/labels.rs) as of epic #7810 slice 2 of #8191, and
# is covered independently by its own unit tests — including that the verdict
# is invariant under every permutation of the label set. This suite exercises
# the merge-pr.sh-side wiring: dry-run behaviour, hard-block behaviour, that
# no override flag exists, and that the guard FAILS CLOSED when the binary
# behind it cannot run.
#
# Strategy (mirrors test-merge-pr-loom-pr-label-guard.sh): extract
# _check_verdict_label_contradiction from the real merge-pr.sh source and
# source it, driving the REAL `loom-daemon merge-pr verdict-contradiction`
# behind it (no stub — its logic is pure string matching with no forge calls),
# then assert on exit code + emitted message. The guard calls `error` (which `exit 1`s), so it is always
# invoked inside a command-substitution subshell.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-verdict-label-guard.sh

# SC2034: PR_NUMBER/PR_LABELS/PR_HEAD_SHA/DRY_RUN are read only by the
# extracted+sourced function, which shellcheck cannot see.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    # Here-string, not a pipe (see test-merge-pr-loom-pr-label-guard.sh's
    # identical rationale, #3820).
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# --- Minimal logging/error shims the extracted function calls ---
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }
error()   { echo "ERROR: $*" >&2; exit 1; }

# The guard's decision is now `loom-daemon merge-pr verdict-contradiction`.
# Pin the binary this suite tests against and verify it knows the subcommand —
# FATAL, never a skip: a suite that skipped itself when no binary resolved
# would report green while testing nothing, which is the exact failure this
# epic keeps running into.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr"

# --- Extract the function under test from merge-pr.sh and source it ---
# From `_check_verdict_label_contradiction() {` up to (not including) the
# following `_check_verdict_label_contradiction` invocation line — the
# function is written as a single dense line (file-size-ratchet offset, see
# merge-pr.sh's own comment above it), so the extraction captures exactly
# that one line. Extracting from source keeps the test in lockstep with the
# script instead of re-implementing it.
#
# Two more lines come with it (#8285): the one-line `_mp_daemon_roll_hint`,
# which the refusal path calls to name the daemon version floor and the roll
# command, and every `# requires-daemon:` marker comment — that function reads
# the floor back out of ${BASH_SOURCE[0]}, i.e. out of THIS extracted file, so
# the markers have to travel with it or the refusal degrades to
# `<undeclared>` here while production says 0.19.172.
FUNCS_FILE="$(mktemp)"
trap 'rm -f "$FUNCS_FILE" 2>/dev/null || true' EXIT
awk '
  /^# requires-daemon:/          { print; next }
  /^_mp_daemon_roll_hint\(\) \{/ { print; next }
  /^_check_verdict_label_contradiction\(\) \{/ { print; exit }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_check_verdict_label_contradiction()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _check_verdict_label_contradiction from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Shared globals the function reads ---
PR_NUMBER="8076"
PR_LABELS=""
PR_HEAD_SHA="abc1234"
DRY_RUN=false

LAST_OUT=""
LAST_RC=0
run_guard() {
    set +e
    LAST_OUT="$( _check_verdict_label_contradiction 2>&1 )"
    LAST_RC=$?
    set -e
}

echo "Testing _check_verdict_label_contradiction behavior..."

# T1: loom:pr + loom:changes-requested -> hard block (exit 1) naming BOTH labels.
DRY_RUN=false
PR_LABELS=$'loom:pr\nloom:changes-requested'
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "1" "$LAST_RC" "loom:pr + loom:changes-requested -> merge hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "Merge blocked" "Block message is emitted"
assert_contains "$LAST_OUT" "loom:pr" "Block message names loom:pr"
assert_contains "$LAST_OUT" "loom:changes-requested" "Block message names the contradicting label"
assert_contains "$LAST_OUT" "deadbeef" "Block message prints the current head SHA"

# T2: same contradiction, labels in the OPPOSITE order -> still detected and
# blocked (order independence: the guard walks a fixed list of blocking
# labels, never "whichever label the forge happens to return first").
DRY_RUN=false
PR_LABELS=$'loom:changes-requested\nloom:pr'
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "1" "$LAST_RC" "Reversed label order -> still hard-blocked (order-independent)"
assert_contains "$LAST_OUT" "loom:changes-requested" "Reversed order -> message still names the contradicting label"

# T3: loom:pr + loom:blocked -> hard block.
DRY_RUN=false
PR_LABELS=$'loom:pr\nloom:blocked'
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "1" "$LAST_RC" "loom:pr + loom:blocked -> merge hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "loom:blocked" "Block message names loom:blocked"

# T4: loom:pr + loom:operator -> hard block.
DRY_RUN=false
PR_LABELS=$'loom:pr\nloom:operator'
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "1" "$LAST_RC" "loom:pr + loom:operator -> merge hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "loom:operator" "Block message names loom:operator"

# T5: loom:pr alone (the overwhelmingly common case) -> guard is a no-op.
DRY_RUN=false
PR_LABELS=$'loom:pr'
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "0" "$LAST_RC" "loom:pr alone -> guard passes (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "loom:pr alone -> no block message"

# T6: loom:changes-requested WITHOUT loom:pr -> guard is a no-op (the
# existing _check_loom_pr_label guard, not this one, handles a missing
# loom:pr — this guard only fires on an actual CONTRADICTION).
DRY_RUN=false
PR_LABELS=$'loom:changes-requested'
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "0" "$LAST_RC" "loom:changes-requested without loom:pr -> this guard is a no-op"
assert_not_contains "$LAST_OUT" "Merge blocked" "No loom:pr present -> no block message from this guard"

# T7: no labels at all -> guard is a no-op.
DRY_RUN=false
PR_LABELS=""
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "0" "$LAST_RC" "Empty label set -> guard passes (exit 0)"

# T8: contradiction + --dry-run -> warning printed, exits 0, no hard block.
DRY_RUN=true
PR_LABELS=$'loom:pr\nloom:changes-requested'
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "0" "$LAST_RC" "--dry-run + contradiction -> guard does NOT exit 1 (dry-run contract)"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run -> reports the would-be block"
assert_contains "$LAST_OUT" "loom:changes-requested" "--dry-run message still names the contradicting label"
DRY_RUN=false

# T9: loom:pr + loom:review-requested -> also treated as a contradiction
# (generalizing #4570 the same way Champion's Verdict-State Janitor Part 1
# does, #7018 — a stray loom:pr beside an active/pending re-review).
DRY_RUN=false
PR_LABELS=$'loom:pr\nloom:review-requested'
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "1" "$LAST_RC" "loom:pr + loom:review-requested -> merge hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "loom:review-requested" "Block message names loom:review-requested"

# --- FAILS CLOSED when the implementation behind the guard cannot run ---
#
# Moving this decision from a sourced shell function into a SUBPROCESS
# (epic #7810 slice 2 of #8191) introduces a failure mode the original could
# not have: the binary can be missing, or be an older install that does not
# know the subcommand. A sourced function is either defined or the script does
# not start.
#
# That new mode must fail CLOSED. To a caller that only checks for a zero exit,
# "the guard found nothing" and "the guard never ran" are the same observation,
# and this guard is the last thing standing between a racing approval and an
# irreversible merge. So every outcome that is not a clean exit 1 refuses.
echo ""
echo "Testing fail-closed behavior when loom-daemon cannot answer..."

# T-FC1: no binary at all. Driven with a genuinely absent path rather than a
# stub returning 127, because what is under test is how the wrapper handles an
# exit code it did not expect — inventing the code would assume the answer.
DRY_RUN=false
PR_LABELS=$'loom:pr\nloom:changes-requested'
PR_HEAD_SHA="deadbeef"
_SAVED_BIN="${LOOM_DAEMON_BIN:-}"
LOOM_DAEMON_BIN="/nonexistent/loom-daemon"
run_guard
assert_eq "1" "$LAST_RC" "an absent loom-daemon BLOCKS the merge (never silently proceeds)"
assert_contains "$LAST_OUT" "Merge blocked" "the refusal is stated as a block"
assert_contains "$LAST_OUT" "could not run" "the message distinguishes 'never ran' from 'found a contradiction'"
assert_contains "$LAST_OUT" "loom-daemon" "the message names what is missing, so it is actionable"
# #8285: "actionable" now means the operator can act WITHOUT a second lookup —
# the version floor this guard needs, and the command that rolls this host to
# it. Before, the message said only "build or install loom-daemon", which on a
# host whose binary was merely OLD (not absent) read as already-done advice.
assert_contains "$LAST_OUT" "requires loom-daemon >= " \
  "the refusal names the MINIMUM daemon version, read from merge-pr.sh's requires-daemon marker"
assert_contains "$LAST_OUT" "cli/loom-daemon-update.sh --fetch" \
  "the refusal names the artifact-first roll command for this host"
assert_not_contains "$LAST_OUT" "<undeclared>" \
  "the floor resolved from a real marker (a '<undeclared>' here means the markers did not travel with the extraction)"

# T-FC2: a binary that EXISTS and exits ZERO but never emits the clean
# sentinel — e.g. an older loom-daemon, or anything substituted onto the path.
# /bin/echo exits 0 and prints its arguments, so a contract that inferred
# "clean" from a zero exit would wave this straight through.
LOOM_DAEMON_BIN="/bin/echo"
run_guard
assert_eq "1" "$LAST_RC" "a zero-exit binary without the clean sentinel still BLOCKS"
assert_contains "$LAST_OUT" "could not run" "its stdout is not mistaken for a verdict"

# T-FC3: silent success. /bin/true exits 0 and prints nothing — the exact
# shape that any "absence of a complaint means clean" contract accepts.
LOOM_DAEMON_BIN="/usr/bin/true"
run_guard
assert_eq "1" "$LAST_RC" "a silently-succeeding binary BLOCKS (silence is not consent)"

# T-FC4: silent failure with the code that used to mean "clean". This is the
# case that killed the first contract: exit 1 is the most common generic
# failure code there is, so mapping it to "reviewed and clean" was fail-open.
LOOM_DAEMON_BIN="/usr/bin/false"
run_guard
assert_eq "1" "$LAST_RC" "a binary exiting 1 BLOCKS — exit 1 alone never means clean"

# T-FC5: the fail-closed path still honours --dry-run's no-side-effects
# contract — it reports the would-be block without exiting 1.
DRY_RUN=true
run_guard
assert_eq "0" "$LAST_RC" "--dry-run reports the fail-closed block without exiting 1"
assert_contains "$LAST_OUT" "dry-run" "the dry-run marker is present"
DRY_RUN=false
LOOM_DAEMON_BIN="$_SAVED_BIN"

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing merge-pr.sh source guards..."
src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_check_verdict_label_contradiction" \
  "merge-pr.sh defines and invokes _check_verdict_label_contradiction"
assert_contains "$src" "merge-pr verdict-contradiction" \
  "merge-pr.sh delegates the verdict decision to loom-daemon"
assert_not_contains "$src" '"$1" == "--allow-verdict-contradiction"' \
  "no bypass flag exists for this guard (deliberate — see merge-pr.sh's comment above the guard)"

# Assert the guard is invoked BEFORE the auto-merge path (line ordering),
# same convention as test-merge-pr-loom-pr-label-guard.sh.
guard_match="$(grep -n '^_check_verdict_label_contradiction$' "$MERGE_PR_SRC" || true)"
guard_line="${guard_match%%$'\n'*}"
guard_line="${guard_line%%:*}"
automerge_match="$(grep -n '^# Handle auto-merge mode' "$MERGE_PR_SRC" || true)"
automerge_line="${automerge_match%%$'\n'*}"
automerge_line="${automerge_line%%:*}"
if [[ -n "$guard_line" && -n "$automerge_line" && "$guard_line" -lt "$automerge_line" ]]; then
    ordered="yes"
else
    ordered="no (guard=$guard_line automerge=$automerge_line)"
fi
assert_eq "yes" "$ordered" \
  "guard is invoked before both merge paths (before '# Handle auto-merge mode')"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
