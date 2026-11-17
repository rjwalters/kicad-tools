#!/usr/bin/env bash
# test-merge-pr-release-hold.sh - `merge-pr.sh <N> --release-hold` (#13517).
#
# The eligibility decision is `loom-daemon merge-pr release-hold` (Rust,
# loom-daemon/src/merge_pr/hold_release.rs); this suite drives the REAL verb
# and checks the merge-pr.sh wiring. Trusted-author filtering of the comments
# is merge-pr.sh's `_trusted_pr_comments`: an untrusted marker reaches the verb
# as ABSENT, which the "no marker" case below covers.
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"
# shellcheck source=lib/assert.sh
source "$TEST_DIR/lib/assert.sh"
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr"

H=abcdef1234567890abcdef1234567890abcdef12
OLD=1111111222222233333334444444555555566666
LBL=$'loom:pr\nloom:blocked'
good="<!-- champion:hold-state head=$H -->
<!-- loom:verdict-sha sha=$H verdict=approved -->"

run() { # <labels> <comments> -> sets OUT, RC
  RC=0
  OUT="$(printf '%s\n' "$2" | "$LOOM_DAEMON_BIN" merge-pr release-hold --pr 9 --head-sha "$H" --operator alice --labels "$1" 2>/dev/null)" || RC=$?
}

run "$LBL" "$good"
assert_eq 0 "$RC" "happy path releases"
assert_contains "$OUT" "LOOM-HOLD-RELEASE-OK" "happy path prints the sentinel"
assert_contains "$OUT" "@alice" "audit comment names the operator"
assert_contains "$OUT" "champion:hold-released-by-operator head=$H" "audit comment carries the marker and head"

run "$LBL" "<!-- champion:hold-state head=$OLD -->
<!-- loom:verdict-sha sha=$H verdict=approved -->"
assert_eq 1 "$RC" "head moved since the hold refuses"
assert_contains "$OUT" "Nothing was written" "refusal says nothing was written"

run "$LBL" "<!-- champion:hold-state head=$H -->
<!-- loom:verdict-sha sha=$OLD verdict=approved -->"
assert_eq 1 "$RC" "head moved since the Judge approval refuses"

run "$LBL"$'\nloom:changes-requested' "$good"
assert_eq 1 "$RC" "an extra contradicting label refuses"

run "$LBL" "<!-- loom:verdict-sha sha=$H verdict=approved -->"
assert_eq 1 "$RC" "no hold-state marker refuses"

# merge-pr.sh wiring: flag parsed, verb invoked, bare refusal names the flag.
assert_contains "$(cat "$MERGE_PR_SRC")" "--release-hold) RELEASE_HOLD=true" "script parses --release-hold"
assert_contains "$(cat "$MERGE_PR_SRC")" "merge-pr release-hold" "script calls the release-hold verb"
bare="$("$LOOM_DAEMON_BIN" merge-pr verdict-contradiction --pr 9 --head-sha "$H" --labels "$LBL" || true)"
assert_contains "$bare" "merge-pr.sh 9 --release-hold" "bare refusal on the hold shape names --release-hold"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
