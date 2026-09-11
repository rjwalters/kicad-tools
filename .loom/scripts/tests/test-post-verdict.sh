#!/usr/bin/env bash
# test-post-verdict.sh - Unit tests for post-verdict.sh (#6382).
#
# post-verdict.sh posts a Judge verdict comment with the mandatory
# `<!-- loom:verdict-sha sha=... verdict=... -->` marker appended by the
# script itself, so a call site can no longer omit it by typing it as prose.
# This suite asserts:
#   - argument validation (PR number, verdict token, SHA, body presence)
#   - the marker is ALWAYS appended, in the exact format
#     verdict-staleness-guard.sh parses (byte-for-byte regex agreement —
#     the thing AC3 in #6382 is actually guarding against: this script must
#     never become a second, silently-diverging definition of the marker)
#   - --body / --body-file (including stdin "-") both work and are mutually
#     exclusive
#   - a `gh pr comment` failure propagates as a non-zero exit
#
# This is a black-box test: post-verdict.sh is a full CLI script (no
# functions to source), so `gh` is stubbed on PATH and the real script is
# invoked as a subprocess, asserting on stdout / exit code / the stub's
# recorded writes. Mirrors the stubbing pattern in
# test-verdict-staleness-guard.sh and test-create-pr-superseded-issue.sh.
#
# Usage:
#   ./.loom/scripts/tests/test-post-verdict.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
POST_VERDICT="$SCRIPTS_DIR/post-verdict.sh"
GUARD="$SCRIPTS_DIR/verdict-staleness-guard.sh"

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
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $msg"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $msg"
    echo "    Expected substring: '$needle'"
    echo "    In: '$haystack'"
  fi
}

if [[ ! -x "$POST_VERDICT" ]]; then
  echo -e "${RED}FATAL${NC}: $POST_VERDICT not found or not executable" >&2
  exit 2
fi

if [[ ! -f "$GUARD" ]]; then
  echo -e "${RED}FATAL${NC}: $GUARD not found (needed for the regex cross-check)" >&2
  exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH ---------------------------------------------------
#   gh pr comment <N> --body <b>  -> append "<N>\t<b>" to comment-writes.log
#                                    (fails if comment-fail-<N> exists)
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
if [[ "$1" == "pr" && "$2" == "comment" ]]; then
  pr_num="$3"
  body=""
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[i]}" == "--body" ]]; then
      body="${args[i + 1]}"
    fi
  done
  if [[ -f "$STUB_DIR_FROM_ENV/comment-fail-$pr_num" ]]; then
    echo "stub gh: pr comment failed" >&2
    exit 1
  fi
  printf '%s\n' "$pr_num" > "$STUB_DIR_FROM_ENV/last-pr.txt"
  printf '%s' "$body" > "$STUB_DIR_FROM_ENV/last-body.txt"
  echo "https://github.com/owner/repo/pull/$pr_num#issuecomment-1"
  exit 0
fi
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

reset_state() {
  rm -f "$STUB_DIR"/comment-fail-* "$STUB_DIR/last-pr.txt" "$STUB_DIR/last-body.txt"
}

run_pv() {
  set +e
  OUTPUT=$("$POST_VERDICT" "$@" 2>&1)
  EXIT_CODE=$?
  set -e
  LAST_BODY="$(cat "$STUB_DIR/last-body.txt" 2>/dev/null || true)"
}

echo "Testing post-verdict.sh (#6382)..."
echo ""

# T1: --body posts a comment whose body ends with the correctly-formatted
# marker — the marker cannot be omitted because it is not part of $BODY.
reset_state
run_pv 100 approved abc1234 --body "LGTM! Everything looks good."
assert_eq "0" "$EXIT_CODE" "valid approved call -> exits 0"
assert_contains "$LAST_BODY" "LGTM! Everything looks good." "posted body carries the caller's text"
assert_contains "$LAST_BODY" "<!-- loom:verdict-sha sha=abc1234 verdict=approved -->" "posted body carries the correctly-formatted marker"

# T2: changes-requested token.
reset_state
run_pv 101 changes-requested deadbee --body "Please fix the tests."
assert_eq "0" "$EXIT_CODE" "valid changes-requested call -> exits 0"
assert_contains "$LAST_BODY" "<!-- loom:verdict-sha sha=deadbee verdict=changes-requested -->" "changes-requested marker uses the right token"

# T3: --body-file with a real file.
reset_state
BODY_FILE="$STUB_DIR/body.txt"
printf 'Approved via file.' > "$BODY_FILE"
run_pv 102 approved cafe123 --body-file "$BODY_FILE"
assert_eq "0" "$EXIT_CODE" "--body-file (real file) -> exits 0"
assert_contains "$LAST_BODY" "Approved via file." "body-file content is posted"
assert_contains "$LAST_BODY" "<!-- loom:verdict-sha sha=cafe123 verdict=approved -->" "body-file path still gets the marker"

# T4: --body-file - reads from stdin.
reset_state
run_pv_stdin() {
  set +e
  OUTPUT=$(printf 'Approved via stdin.' | "$POST_VERDICT" "$@" 2>&1)
  EXIT_CODE=$?
  set -e
  LAST_BODY="$(cat "$STUB_DIR/last-body.txt" 2>/dev/null || true)"
}
run_pv_stdin 103 approved 1234567 --body-file -
assert_eq "0" "$EXIT_CODE" "--body-file - (stdin) -> exits 0"
assert_contains "$LAST_BODY" "Approved via stdin." "stdin body content is posted"
assert_contains "$LAST_BODY" "<!-- loom:verdict-sha sha=1234567 verdict=approved -->" "stdin path still gets the marker"

# T5: --body and --body-file together -> rejected.
reset_state
run_pv 104 approved abc1234 --body "x" --body-file "$BODY_FILE"
assert_eq "2" "$EXIT_CODE" "--body and --body-file together -> exit 2"
assert_contains "$OUTPUT" "mutually exclusive" "error names the conflict"

# T6: missing body entirely -> rejected, no comment posted.
reset_state
run_pv 105 approved abc1234
assert_eq "2" "$EXIT_CODE" "no --body/--body-file -> exit 2"
assert_eq "" "$(cat "$STUB_DIR/last-pr.txt" 2>/dev/null || true)" "no comment attempted without a body"

# T7: non-numeric PR number -> rejected.
reset_state
run_pv abc approved abc1234 --body "x"
assert_eq "2" "$EXIT_CODE" "non-numeric PR number -> exit 2"

# T8: invalid verdict token -> rejected (only approved/changes-requested are
# valid, matching verdict-staleness-guard.sh's verdict_token_for_label()).
reset_state
run_pv 106 rejected abc1234 --body "x"
assert_eq "2" "$EXIT_CODE" "invalid verdict token -> exit 2"
assert_contains "$OUTPUT" "approved" "error message names the valid tokens"

# T9: invalid SHA (too short / non-hex) -> rejected.
reset_state
run_pv 107 approved xyz --body "x"
assert_eq "2" "$EXIT_CODE" "SHA too short / non-hex -> exit 2"

reset_state
run_pv 108 approved "not-a-real-sha-value" --body "x"
assert_eq "2" "$EXIT_CODE" "SHA with non-hex characters -> exit 2"

# T10: empty body string -> rejected (an omitted marker AND an empty body
# would otherwise post a comment that is just the marker).
reset_state
run_pv 109 approved abc1234 --body ""
assert_eq "2" "$EXIT_CODE" "empty --body -> exit 2"

# T10b: --body starting with '@' is refused — the same
# --body-@path-does-not-expand anti-pattern the Bash guard hard-denies for a
# literal `gh pr comment` call, reproduced here because that guard
# pattern-matches literal command text and cannot see a call routed through
# this script (#6382).
reset_state
run_pv 109 approved abc1234 --body "@/tmp/review-109.md"
assert_eq "2" "$EXIT_CODE" "--body starting with @ -> exit 2"
assert_contains "$OUTPUT" "does NOT read the file" "error explains the @path anti-pattern"
assert_eq "" "$(cat "$STUB_DIR/last-pr.txt" 2>/dev/null || true)" "no comment posted with a literal @path body"

# T11: a `gh pr comment` failure propagates as a non-zero exit — the caller's
# `&&`-chained label edit must not run on a failed comment.
reset_state
touch "$STUB_DIR/comment-fail-110"
run_pv 110 approved abc1234 --body "x"
assert_eq "1" "$EXIT_CODE" "gh pr comment failure -> exit 1"

# T12: --help / -h prints usage and exits 0 without touching gh.
reset_state
run_pv --help
assert_eq "0" "$EXIT_CODE" "--help -> exit 0"
assert_contains "$OUTPUT" "Usage:" "help output includes a Usage section"

# --- T13: format cross-check against verdict-staleness-guard.sh ------------
# The whole point of AC3 in #6382 is that this script must not become a
# SECOND place the marker format is defined. Extract the guard's own
# MARKER_TEST regex template (parameterized on $VERDICT_TOKEN) and assert
# post-verdict.sh's actual output for both verdict tokens matches it.
GUARD_MARKER_TEMPLATE="$(grep -m1 '^MARKER_TEST=' "$GUARD" | sed -E 's/^MARKER_TEST="(.*)"$/\1/')"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -z "$GUARD_MARKER_TEMPLATE" ]]; then
  TESTS_FAILED=$((TESTS_FAILED + 1))
  echo -e "  ${RED}FAIL${NC}: could not extract MARKER_TEST from verdict-staleness-guard.sh — did its format change?"
else
  TESTS_PASSED=$((TESTS_PASSED + 1))
  echo -e "  ${GREEN}PASS${NC}: extracted verdict-staleness-guard.sh's MARKER_TEST template"

  for token in approved changes-requested; do
    reset_state
    run_pv 200 "$token" 0123456789abcdef0123456789abcdef01234567 --body "cross-check"
    GUARD_REGEX="${GUARD_MARKER_TEMPLATE//\$VERDICT_TOKEN/$token}"
    # -E (POSIX extended), not -P: the guard's own regex uses only portable
    # ERE syntax ([0-9a-f]{7,40}), and BSD grep (macOS) has no -P at all.
    if printf '%s' "$LAST_BODY" | grep -Eq -- "$GUARD_REGEX"; then
      TESTS_PASSED=$((TESTS_PASSED + 1))
      echo -e "  ${GREEN}PASS${NC}: post-verdict.sh's $token marker matches verdict-staleness-guard.sh's own regex"
    else
      TESTS_FAILED=$((TESTS_FAILED + 1))
      echo -e "  ${RED}FAIL${NC}: post-verdict.sh's $token marker does NOT match verdict-staleness-guard.sh's regex"
      echo "    Guard regex: $GUARD_REGEX"
      echo "    Posted body: $LAST_BODY"
    fi
    TESTS_RUN=$((TESTS_RUN + 1))
  done
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
  exit 1
fi
exit 0
