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
# shellcheck source=lib/write-scope-fixture.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/write-scope-fixture.sh"

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

# assert_contains/assert_not_contains use a pure-bash substring match (no
# forked printf|grep pipeline) so a transient fork/exec failure under
# run-ci-suites.sh's parallel suite pool can never masquerade as a genuine
# content mismatch (#7819, #7874).
assert_not_contains() {
  local haystack="$1" needle="$2" msg="$3"
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$haystack" != *"$needle"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $msg"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $msg"
    echo "    Unexpected substring: '$needle'"
    echo "    In: '$haystack'"
  fi
}

assert_contains() {
  local haystack="$1" needle="$2" msg="$3"
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$haystack" == *"$needle"* ]]; then
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

# --- #7647 formal-review reconciliation gate reads -------------------------
# post-verdict.sh runs check-review-feedback.sh on every `approved` verdict.
# This suite is about the verdict-sha marker, not about review reconciliation
# (that lives in test-review-feedback-reconciliation.sh), so answer those reads
# with a clean, complete, EMPTY review state -> the gate reports CLEAR and the
# marker assertions below exercise exactly what they did before.
if [[ "$1" == "api" ]]; then
  # #10581 round 2: scenario files fail a label DELETE / a label read.
  [[ " $* " == *" -X DELETE "* && -f "$LOOM_TEST_STUB_DIR/delete-fail" ]] && { echo "HTTP 502" >&2; exit 1; }
  [[ "$*" == *"/labels?"* && -f "$LOOM_TEST_STUB_DIR/labels-read-fail" ]] && { echo "HTTP 502" >&2; exit 1; }
  # #10485: the final head compare reads `repos/O/R/pulls/N --jq .head.sha`.
  if [[ "$2" =~ ^repos/[^/]+/[^/]+/pulls/[0-9]+$ ]]; then
    [[ -f "$LOOM_TEST_STUB_DIR/final-head-fail" ]] && exit 1
    if [[ -f "$LOOM_TEST_STUB_DIR/final-head" ]]; then cat "$LOOM_TEST_STUB_DIR/final-head"; else cat "$LOOM_TEST_STUB_DIR/cur-sha"; fi
    exit 0
  fi
  if [[ "$2" == "graphql" ]]; then
    printf '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}}\n'
    exit 0
  fi
  # A paginated list endpoint with a server-side --jq filter over an empty
  # array produces no output at all.
  printf ''
  exit 0
fi
if [[ "$1" == "repo" && "$2" == "view" ]]; then
  echo "owner/repo"
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "edit" ]]; then
  printf '%s\n' "$*" >> "$LOOM_TEST_STUB_DIR/pr-edit.log"
  [[ -f "$LOOM_TEST_STUB_DIR/pr-edit-fail" ]] && { echo "stub gh: pr edit failed" >&2; exit 1; }
  exit 0
fi
if [[ "$1" == "issue" && "$2" == "comment" ]]; then
  # #9774: post-verdict.sh posts through forge_gh_comment_rl_safe, which is
  # `gh issue comment` shaped (a PR IS an issue for comments) — same capture
  # contract as the `pr comment` case below.
  pr_num="$3"
  body=""
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[i]}" == "--body" ]]; then
      body="${args[i + 1]}"
    fi
  done
  if [[ -f "$STUB_DIR_FROM_ENV/comment-fail-$pr_num" ]]; then
    echo "stub gh: issue comment failed" >&2
    exit 1
  fi
  printf '%s\n' "$pr_num" > "$STUB_DIR_FROM_ENV/last-pr.txt"
  printf '%s' "$body" > "$STUB_DIR_FROM_ENV/last-body.txt"
  echo "https://github.com/owner/repo/issues/$pr_num#issuecomment-1"
  exit 0
fi

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
# #9774: the transport tries the daemon chokepoint first. This suite's subject
# is the verdict semantics over the GH ladder, so pin the SELF daemon to a
# mock that refuses (the pre-#9818 shape) — the gh stub above stays the path
# under test, deterministically, whatever binary the host happens to have.
# #10581: the verdict gate / label verbs are the daemon's (their logic is
# tested in loom_daemon::verdict_gate and test-post-verdict-gate.sh against
# the real binary); here they answer from scenario files so this suite pins
# post-verdict.sh's WIRING: gate-answer ("<rc> <line>"), labels-fail.
cat > "$STUB_DIR/loom-daemon" <<'MOCK'
#!/usr/bin/env bash
# #10485: `forge wait-checks` is the exact-head CI reader the approval gate
# calls. Scenario files drive it: ci-stdout (sentinel line), ci-stderr (detail
# lines), ci-garbage / ci-absent (no sentinel / "unknown subcommand" like an
# older binary). Default: GREEN on the SHA under test.
if [[ "${1:-} ${2:-}" == "forge wait-checks" ]]; then
  printf '%s\n' "$*" >> "$LOOM_TEST_STUB_DIR/wait-checks-calls.log"
  [[ -f "$LOOM_TEST_STUB_DIR/ci-absent" ]] && { echo "error: unrecognized subcommand 'wait-checks'" >&2; exit 2; }
  [[ -f "$LOOM_TEST_STUB_DIR/ci-garbage" ]] && { echo "<html>502 Bad Gateway</html>"; exit 0; }
  # models the real reader: an empty rollup settles to NONE only given time
  # for ~3 polls (--timeout >= 10); below that it reports TIMEOUT
  if [[ -f "$LOOM_TEST_STUB_DIR/ci-empty" ]]; then
    t=0; prev=""
    for a in "$@"; do [[ "$prev" == "--timeout" ]] && t="$a"; prev="$a"; done
    if [[ -f "$LOOM_TEST_STUB_DIR/ci-required" || "$t" -lt 10 ]]; then
      echo "LOOM-CHECKS-TIMEOUT $(cat "$LOOM_TEST_STUB_DIR/cur-sha")"; exit 1
    fi
    echo "LOOM-CHECKS-NONE $(cat "$LOOM_TEST_STUB_DIR/cur-sha")"; exit 1
  fi
  if [[ -f "$LOOM_TEST_STUB_DIR/ci-stdout" ]]; then
    cat "$LOOM_TEST_STUB_DIR/ci-stdout"
    [[ -f "$LOOM_TEST_STUB_DIR/ci-stderr" ]] && cat "$LOOM_TEST_STUB_DIR/ci-stderr" >&2
    exit 1
  fi
  echo "LOOM-CHECKS-GREEN $(cat "$LOOM_TEST_STUB_DIR/cur-sha")"
  exit 0
fi
D="$LOOM_TEST_STUB_DIR"
# The #10581 capability probe asks `forge <verb> --help`. Answered here (and
# not logged) so the scenario files below drive only the real calls; with
# old-daemon present it answers like a binary that predates the verbs.
if [[ "${1:-}" == forge && "${2:-}" == verdict-* && "${3:-}" == --help ]]; then
  [[ -f "$D/old-daemon" ]] && { echo "error: unrecognized subcommand '$2'" >&2; exit 2; }
  exit 0
fi
if [[ "${1:-}" == --version ]]; then echo "loom-daemon 0.19.870 (mock)"; exit 0; fi
if [[ "${1:-} ${2:-}" == "forge verdict-gate" ]]; then
  printf '%s\n' "$*" >> "$D/daemon-calls.log"
  [[ -f "$D/gate-answer" ]] || { echo "LOOM-VERDICT-GATE PROCEED ok"; exit 0; }
  read -r rc line < "$D/gate-answer"; echo "$line"; exit "$rc"
fi
if [[ "${1:-} ${2:-}" == "forge verdict-lock" ]]; then
  printf '%s\n' "$*" >> "$D/daemon-calls.log"
  [[ "${3:-}" == acquire && -f "$D/lock-fail" ]] && { echo "forge verdict-lock: could not take the lock" >&2; exit 9; }
  exit 0
fi
if [[ "${1:-} ${2:-}" == "forge verdict-reconcile" ]]; then
  printf '%s\n' "$*" >> "$D/daemon-calls.log"
  # reconcile-answer.N answers the Nth call (an interleaving); else reconcile-answer
  N="$(grep -c '^forge verdict-reconcile' "$D/daemon-calls.log")"
  F="$D/reconcile-answer"; [[ -f "$F.$N" ]] && F="$F.$N"
  [[ -f "$F" ]] || { echo "LOOM-VERDICT-RECONCILE STABLE"; exit 0; }
  read -r rc line < "$F"; echo "$line"; exit "$rc"
fi
if [[ "${1:-} ${2:-}" == "forge verdict-labels" ]]; then
  printf '%s\n' "$*" >> "$D/daemon-calls.log"
  [[ -f "$D/labels-fail" ]] && { echo "forge verdict-labels: did not hold: missing loom:pr.  Repair: gh pr edit $3 --add-label loom:pr" >&2; exit 1; }
  echo "LOOM-VERDICT-LABELS OK"; exit 0
fi
echo "mock loom-daemon: forge comment not under test here" >&2
exit 127
MOCK
chmod +x "$STUB_DIR/loom-daemon"
export LOOM_DAEMON_SELF_BIN="$STUB_DIR/loom-daemon"
# #10485: the CI gate invokes the reader through LOOM_DAEMON_BIN (or PATH).
export LOOM_DAEMON_BIN="$STUB_DIR/loom-daemon"
# #9548: post-verdict.sh vets its write target through the write scope before it
# writes. It runs from a checkout registered as owner/repo (origin, .loom/, push
# reported to the permission probe), so the real decision admits it.
write_scope_register "$STUB_DIR/checkout" owner/repo
cd "$STUB_DIR/checkout"

reset_state() {
  rm -f "$STUB_DIR"/comment-fail-* "$STUB_DIR/last-pr.txt" "$STUB_DIR/last-body.txt" \
    "$STUB_DIR"/ci-stdout "$STUB_DIR"/ci-stderr "$STUB_DIR"/ci-garbage "$STUB_DIR"/ci-absent "$STUB_DIR"/ci-empty "$STUB_DIR"/ci-required \
    "$STUB_DIR"/final-head "$STUB_DIR"/final-head-fail "$STUB_DIR/wait-checks-calls.log" \
    "$STUB_DIR/gate-answer" "$STUB_DIR/reconcile-answer" "$STUB_DIR"/reconcile-answer.* "$STUB_DIR/labels-fail" "$STUB_DIR/lock-fail" "$STUB_DIR/daemon-calls.log" \
    "$STUB_DIR/old-daemon" "$STUB_DIR/delete-fail" "$STUB_DIR/labels-read-fail" "$STUB_DIR/pr-edit.log" "$STUB_DIR/pr-edit-fail"
}

run_pv() {
  set +e
  printf '%s' "${3:-}" > "$STUB_DIR/cur-sha"
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
  printf '%s' "${3:-}" > "$STUB_DIR/cur-sha"
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


# --- T14: exact-head all-CI gate (#10485) ------------------------------------
# Every denial must exit non-zero AND post nothing (no comment => the caller's
# `&&`-chained loom:pr edit cannot run). Fake reader sentinels drive each case.
CI_SHA_FULL="0123456789abcdef0123456789abcdef01234567"
no_comment() {
  assert_eq "" "$(cat "$STUB_DIR/last-pr.txt" 2>/dev/null || true)" "$1: no comment posted"
}

# green on the exact head: approval posted, reader asked for one snapshot of this PR
reset_state
run_pv 300 approved "$CI_SHA_FULL" --body "ok"
assert_eq "0" "$EXIT_CODE" "CI green on exact head -> approval posted"
assert_contains "$LAST_BODY" "<!-- loom:verdict-sha sha=$CI_SHA_FULL verdict=approved -->" "green: marker preserved"
assert_contains "$(cat "$STUB_DIR/wait-checks-calls.log")" "forge wait-checks 300 --repo owner/repo --timeout 20" "green: bounded snapshot read of the PR"

# NONE (legitimately no CI, per the reader's zero-row settle) is accepted
reset_state
echo "LOOM-CHECKS-NONE $CI_SHA_FULL" > "$STUB_DIR/ci-stdout"
run_pv 301 approved "$CI_SHA_FULL" --body "ok"
assert_eq "0" "$EXIT_CODE" "reader NONE (settled: no required contexts) -> approval posted"

# checkless repo against the real reader's settle behaviour (regression: a
# --timeout 0 read of an empty rollup is TIMEOUT, which refused every approval)
reset_state
touch "$STUB_DIR/ci-empty"
run_pv 310 approved "$CI_SHA_FULL" --body "ok"
assert_eq "0" "$EXIT_CODE" "checkless repo settles to NONE within the default timeout -> approval posted"
reset_state
touch "$STUB_DIR/ci-empty" "$STUB_DIR/ci-required"
run_pv 311 approved "$CI_SHA_FULL" --body "ok"
assert_eq "5" "$EXIT_CODE" "empty rollup with required contexts stays TIMEOUT -> exit 5"
no_comment "empty rollup + required"

# pending
reset_state
echo "LOOM-CHECKS-TIMEOUT $CI_SHA_FULL build,test" > "$STUB_DIR/ci-stdout"
run_pv 302 approved "$CI_SHA_FULL" --body "ok"
assert_eq "5" "$EXIT_CODE" "pending checks -> exit 5"
assert_contains "$OUTPUT" "LOOM-CHECKS-TIMEOUT" "pending: reader evidence shown"
assert_contains "$OUTPUT" "leave loom:review-requested" "pending: next action named"
no_comment "pending"

# non-required failure is RED too (the reader folds every observed check)
reset_state
echo "LOOM-CHECKS-RED $CI_SHA_FULL optional-lint" > "$STUB_DIR/ci-stdout"
printf 'optional-lint\thttps://example.test/run/9\t9\n' > "$STUB_DIR/ci-stderr"
run_pv 303 approved "$CI_SHA_FULL" --body "ok"
assert_eq "6" "$EXIT_CODE" "failed (non-required) check -> exit 6"
assert_contains "$OUTPUT" "optional-lint" "red: failing check named"
assert_contains "$OUTPUT" "https://example.test/run/9" "red: failing check url shown"
no_comment "red"

# cancelled / timed_out / action_required are classified failing by the reader -> RED
reset_state
echo "LOOM-CHECKS-RED $CI_SHA_FULL deploy-preview" > "$STUB_DIR/ci-stdout"
run_pv 304 approved "$CI_SHA_FULL" --body "ok"
assert_eq "6" "$EXIT_CODE" "cancelled check (reader RED) -> exit 6"
no_comment "cancelled"

# reader ERROR
reset_state
echo "LOOM-CHECKS-ERROR read-failed: HTTP 502" > "$STUB_DIR/ci-stdout"
run_pv 305 approved "$CI_SHA_FULL" --body "ok"
assert_eq "5" "$EXIT_CODE" "reader ERROR -> exit 5"
no_comment "reader error"

# empty rollup with required contexts / approval-required fork workflow: the
# reader holds it at TIMEOUT (never NONE) with the required names pending
reset_state
echo "LOOM-CHECKS-TIMEOUT $CI_SHA_FULL ci/required" > "$STUB_DIR/ci-stdout"
run_pv 306 approved "$CI_SHA_FULL" --body "ok"
assert_eq "5" "$EXIT_CODE" "empty rollup, required contexts / approval-required -> exit 5"
no_comment "empty/approval-required"

# HEAD-MOVED reported by the reader
reset_state
echo "LOOM-CHECKS-HEAD-MOVED $CI_SHA_FULL fedcba9876543210fedcba9876543210fedcba98" > "$STUB_DIR/ci-stdout"
run_pv 307 approved "$CI_SHA_FULL" --body "ok"
assert_eq "5" "$EXIT_CODE" "reader HEAD-MOVED -> exit 5"
no_comment "reader head-moved"

# green was read for a DIFFERENT head than the one reviewed
reset_state
echo "LOOM-CHECKS-GREEN fedcba9876543210fedcba9876543210fedcba98" > "$STUB_DIR/ci-stdout"
run_pv 308 approved "$CI_SHA_FULL" --body "ok"
assert_eq "5" "$EXIT_CODE" "green for a different head than reviewed -> exit 5"
no_comment "wrong-head green"

# SHA moves between the CI read and the post (final compare)
reset_state
echo "fedcba9876543210fedcba9876543210fedcba98" > "$STUB_DIR/final-head"
run_pv 309 approved "$CI_SHA_FULL" --body "ok"
assert_eq "5" "$EXIT_CODE" "head moved between CI read and post -> exit 5"
assert_contains "$OUTPUT" "moved or unreadable" "final compare: reason shown"
no_comment "final compare"

# head unreadable at the final compare fails closed
reset_state
touch "$STUB_DIR/final-head-fail"
run_pv 310 approved "$CI_SHA_FULL" --body "ok"
assert_eq "5" "$EXIT_CODE" "final head read failure -> exit 5"
no_comment "final head read failure"

# status read failure: older daemon (no wait-checks), garbage output, no binary
reset_state
touch "$STUB_DIR/ci-absent"
run_pv 311 approved "$CI_SHA_FULL" --body "ok"
assert_eq "5" "$EXIT_CODE" "daemon without wait-checks -> exit 5 (fail closed)"
assert_contains "$OUTPUT" "resync-installed.sh" "older daemon: resync hint shown"
no_comment "older daemon"

reset_state
touch "$STUB_DIR/ci-garbage"
run_pv 312 approved "$CI_SHA_FULL" --body "ok"
assert_eq "5" "$EXIT_CODE" "garbage reader output (no sentinel) -> exit 5"
no_comment "garbage output"

reset_state
OLD_DAEMON_BIN="$LOOM_DAEMON_BIN"
export LOOM_DAEMON_BIN="$STUB_DIR/does-not-exist"
run_pv 313 approved "$CI_SHA_FULL" --body "ok"
export LOOM_DAEMON_BIN="$OLD_DAEMON_BIN"
assert_eq "5" "$EXIT_CODE" "missing daemon binary -> exit 5"
no_comment "missing daemon"

# fast path (docs-only style body) goes through the same single gate
reset_state
echo "LOOM-CHECKS-TIMEOUT $CI_SHA_FULL docs-lint" > "$STUB_DIR/ci-stdout"
run_pv 314 approved "$CI_SHA_FULL" --body "Docs-only fast path: approved."
assert_eq "5" "$EXIT_CODE" "fast-path approval is gated by the same CI check"
no_comment "fast path"

# changes-requested is never gated on CI (it cannot merge anything)
reset_state
echo "LOOM-CHECKS-RED $CI_SHA_FULL build" > "$STUB_DIR/ci-stdout"
run_pv 315 changes-requested "$CI_SHA_FULL" --body "CI failing: build"
assert_eq "0" "$EXIT_CODE" "changes-requested posts even when CI is red"
assert_eq "" "$(cat "$STUB_DIR/wait-checks-calls.log" 2>/dev/null || true)" "changes-requested does not read CI"

# --- T15: verdict gate + label transition wiring (#10581) -------------------

# Success: gate consulted with the verdict, SHA, repo and overrule text; then
# the label verb runs for the same verdict.
reset_state
run_pv 300 approved abc1234 --body "ok" --overrules-prior "each prior point was fixed in the follow-up commit"
assert_eq "0" "$EXIT_CODE" "gate PROCEED + labels OK -> exit 0"
CALLS="$(cat "$STUB_DIR/daemon-calls.log" 2>/dev/null || true)"
assert_contains "$CALLS" "forge verdict-gate 300 --repo owner/repo --verdict approved --sha abc1234 --overrules-prior each prior point" "gate gets PR, repo, verdict, sha, overrule"
assert_contains "$CALLS" "forge verdict-labels 300 --repo owner/repo --verdict approved" "label transition runs after the post"
assert_contains "$CALLS" "forge verdict-lock acquire 300 --repo owner/repo" "the per-PR lock is taken before the gate"
assert_contains "$CALLS" "forge verdict-lock release 300 --repo owner/repo" "the per-PR lock is released at exit"

# Lock timeout: exit 9 (fail closed), the gate never runs, nothing is posted.
reset_state
touch "$STUB_DIR/lock-fail"
run_pv 309 approved abc1234 --body "ok"
assert_eq "9" "$EXIT_CODE" "lock unavailable -> exit 9"
assert_eq "" "$(grep verdict-gate "$STUB_DIR/daemon-calls.log" 2>/dev/null || true)" "lock unavailable: gate never ran"
no_comment "lock unavailable"
rm -f "$STUB_DIR/lock-fail"

# REFUSE: exit 7, nothing posted, labels untouched (same-head contradiction,
# loom:ci-failure, unread state all arrive here as REFUSE).
reset_state
echo "3 LOOM-VERDICT-GATE REFUSE the PR carries loom:ci-failure" > "$STUB_DIR/gate-answer"
run_pv 301 approved abc1234 --body "ok"
assert_eq "7" "$EXIT_CODE" "gate REFUSE -> exit 7"
assert_contains "$OUTPUT" "loom:ci-failure" "refusal reason is shown"
no_comment "refuse"
assert_eq "" "$(grep verdict-labels "$STUB_DIR/daemon-calls.log" 2>/dev/null || true)" "refuse: no label write"

# DEDUPE: no second comment, labels still applied, exit 0.
reset_state
echo "10 LOOM-VERDICT-GATE DEDUPE a changes-requested verdict for abc1234 was posted 30s ago" > "$STUB_DIR/gate-answer"
run_pv 302 changes-requested abc1234 --body "dup"
assert_eq "0" "$EXIT_CODE" "gate DEDUPE -> exit 0"
no_comment "dedupe"
assert_contains "$(cat "$STUB_DIR/daemon-calls.log")" "forge verdict-labels 302" "dedupe: labels still applied"

# A gate answer without the sentinel (old daemon, crash) never passes an approval...
reset_state
echo "2 error: unrecognized subcommand 'verdict-gate'" > "$STUB_DIR/gate-answer"
run_pv 303 approved abc1234 --body "ok"
assert_eq "7" "$EXIT_CODE" "no gate sentinel on an approval -> exit 7"
no_comment "gate unavailable (approve)"
# ... a sentinel line with the wrong exit code does not either ...
reset_state
echo "1 LOOM-VERDICT-GATE PROCEED ok" > "$STUB_DIR/gate-answer"
run_pv 304 approved abc1234 --body "ok"
assert_eq "7" "$EXIT_CODE" "PROCEED with a non-zero exit -> exit 7"
no_comment "rc/sentinel mismatch"
# ... but a changes-requested still posts (it cannot merge anything).
reset_state
echo "2 error: unrecognized subcommand 'verdict-gate'" > "$STUB_DIR/gate-answer"
run_pv 305 changes-requested abc1234 --body "please fix"
assert_eq "0" "$EXIT_CODE" "no gate sentinel on changes-requested -> still posts"
assert_contains "$OUTPUT" "WARNING" "degraded gate is loud"
assert_eq "305" "$(cat "$STUB_DIR/last-pr.txt" 2>/dev/null || true)" "changes-requested comment posted"

# Label write failure after the post (#10605): non-zero, with the repair hint.
reset_state
touch "$STUB_DIR/labels-fail"
run_pv 306 approved abc1234 --body "ok"
assert_eq "8" "$EXIT_CODE" "label transition failure -> exit 8"
assert_contains "$OUTPUT" "Repair: gh pr edit 306" "repair command is printed"
assert_contains "$OUTPUT" "forge verdict-labels 306 --repo owner/repo --verdict approved" "re-run command is printed"
assert_eq "306" "$(cat "$STUB_DIR/last-pr.txt" 2>/dev/null || true)" "the comment itself was posted"

# --- T16: cross-host reconcile wiring (#10581) ------------------------------

# The gate's seen-opposite count is handed to the reconcile verb.
reset_state
echo "0 LOOM-VERDICT-GATE PROCEED overruling the changes-requested verdict seen-opposite=2 seen-same-max-id=0" > "$STUB_DIR/gate-answer"
run_pv 320 approved abc1234 --body "ok" --overrules-prior "each prior point was fixed in the follow-up commit"
assert_eq "0" "$EXIT_CODE" "reconcile STABLE -> exit 0"
assert_contains "$(cat "$STUB_DIR/daemon-calls.log")" "forge verdict-reconcile 320 --repo owner/repo --verdict approved --sha abc1234 --seen-opposite 2 --seen-same-max-id 0 --nonce" "reconcile runs with the gate's counts"

# An approval that lost the race: superseding changes-requested marker, flipped
# labels, exit 7.
reset_state
echo "11 LOOM-VERDICT-RECONCILE SUPERSEDED 1 concurrent changes-requested verdict(s) landed" > "$STUB_DIR/reconcile-answer"
run_pv 321 approved abc1234 --body "ok"
assert_eq "7" "$EXIT_CODE" "superseded approval -> exit 7"
assert_contains "$OUTPUT" "SUPERSEDED" "the loss is announced"
assert_contains "$(cat "$STUB_DIR/daemon-calls.log")" "forge verdict-labels 321 --repo owner/repo --verdict changes-requested" "labels flipped to changes-requested"
assert_contains "$(cat "$STUB_DIR/last-body.txt" 2>/dev/null || true)" "verdict=changes-requested" "a superseding changes-requested marker is posted"

# The reconcile runs BEFORE the labels: an approval is never live while arbitration is pending.
CALLS="$(cat "$STUB_DIR/daemon-calls.log")"
assert_eq "yes" "$([[ "${CALLS%%forge verdict-labels*}" == *"forge verdict-reconcile"* ]] && echo yes || echo no)" "reconcile precedes verdict-labels"

# A changes-requested that prevails re-asserts its own labels.
reset_state
echo "0 LOOM-VERDICT-RECONCILE PREVAILS 1 concurrent approved verdict(s) landed" > "$STUB_DIR/reconcile-answer"
run_pv 322 changes-requested abc1234 --body "fix"
assert_eq "0" "$EXIT_CODE" "changes-requested prevails -> exit 0"
assert_eq "1" "$(grep -c 'forge verdict-labels 322 --repo owner/repo --verdict changes-requested' "$STUB_DIR/daemon-calls.log")" "prevailing changes-requested applies its labels once, after the reconcile"

# An unreadable / unavailable reconcile is never silent.
reset_state
echo "1 LOOM-VERDICT-RECONCILE UNREAD the PR's comments could not be re-read" > "$STUB_DIR/reconcile-answer"
run_pv 323 approved abc1234 --body "ok"
assert_eq "8" "$EXIT_CODE" "unread reconcile -> exit 8"
assert_contains "$OUTPUT" "NOT live" "unread approval is announced as not live"
assert_eq "0" "$(grep -c 'forge verdict-labels 323' "$STUB_DIR/daemon-calls.log")" "unread reconcile: NO label call, so loom:pr is never applied (final state, not just exit code)"

# An unread changes-requested still applies its (safe) labels.
reset_state
echo "1 LOOM-VERDICT-RECONCILE UNREAD x" > "$STUB_DIR/reconcile-answer"
run_pv 324 changes-requested abc1234 --body "fix"
assert_eq "1" "$(grep -c 'forge verdict-labels 324 --repo owner/repo --verdict changes-requested' "$STUB_DIR/daemon-calls.log")" "unread reconcile: changes-requested labels still applied"

# An identical verdict that lost the lowest-id race: comment withdrawn by the verb, NO labels touched.
reset_state
echo "12 LOOM-VERDICT-RECONCILE DUPLICATE comment=7 an identical verdict landed first" > "$STUB_DIR/reconcile-answer"
run_pv 325 approved abc1234 --body "ok"
assert_eq "0" "$EXIT_CODE" "duplicate loser -> exit 0"
assert_eq "0" "$(grep -c 'forge verdict-labels 325' "$STUB_DIR/daemon-calls.log")" "duplicate loser applies no labels (the winner owns them)"

# Cross-host interleaving (#10581): the first reconcile is STABLE, then a rival
# changes-requested lands before the label write; the post-label re-read must
# supersede the approval and flip the labels, so loom:pr is not left standing.
reset_state
echo "0 LOOM-VERDICT-RECONCILE STABLE" > "$STUB_DIR/reconcile-answer.1"
echo "11 LOOM-VERDICT-RECONCILE SUPERSEDED 1 concurrent changes-requested verdict(s) landed" > "$STUB_DIR/reconcile-answer.2"
run_pv 326 approved abc1234 --body "ok"
assert_eq "7" "$EXIT_CODE" "rival landing between reconcile and labels -> exit 7"
assert_eq "2" "$(grep -c 'forge verdict-reconcile 326' "$STUB_DIR/daemon-calls.log")" "arbitration is re-run after the label write"
CALLS="$(cat "$STUB_DIR/daemon-calls.log")"
assert_eq "yes" "$([[ "$CALLS" == *"forge verdict-labels 326 --repo owner/repo --verdict approved"*"forge verdict-labels 326 --repo owner/repo --verdict changes-requested"* ]] && echo yes || echo no)" "the approval labels are followed by the changes-requested flip (final state converges)"

# An unreadable post-label re-read is never left live.
reset_state
echo "0 LOOM-VERDICT-RECONCILE STABLE" > "$STUB_DIR/reconcile-answer.1"
echo "1 LOOM-VERDICT-RECONCILE UNREAD x" > "$STUB_DIR/reconcile-answer.2"
run_pv 327 approved abc1234 --body "ok"
assert_eq "8" "$EXIT_CODE" "unread post-label arbitration -> exit 8"
assert_contains "$OUTPUT" "loom:pr was withdrawn" "the approval is announced as withdrawn"

# ... but only when the withdrawal actually happened (#10581 round 2): a failed
# DELETE with an unreadable label state says so and prints the manual removal.
reset_state
echo "0 LOOM-VERDICT-RECONCILE STABLE" > "$STUB_DIR/reconcile-answer.1"
echo "1 LOOM-VERDICT-RECONCILE UNREAD x" > "$STUB_DIR/reconcile-answer.2"
touch "$STUB_DIR/delete-fail" "$STUB_DIR/labels-read-fail"
run_pv 328 approved abc1234 --body "ok"
assert_eq "8" "$EXIT_CODE" "unread re-arbitration + failed DELETE -> exit 8"
assert_not_contains "$OUTPUT" "loom:pr was withdrawn" "a failed DELETE is never reported as a withdrawal"
assert_contains "$OUTPUT" "could NOT be withdrawn" "the failed withdrawal is announced"
assert_contains "$OUTPUT" "gh pr edit 328 --repo owner/repo --remove-label loom:pr" "the manual removal command is printed"

# --- T17: a daemon without the #10581 verbs (capability probe, round 2) -----

# An approval posts on the legacy path (round 4, so a script roll ahead of the
# daemon release never stalls approvals) with main's Judge-prompt label write,
# naming the binary and its --version; no verdict verb is called.
reset_state
touch "$STUB_DIR/old-daemon"
run_pv 330 approved abc1234 --body "ok"
assert_eq "0" "$EXIT_CODE" "verb-less daemon + approval -> posted on the legacy path"
assert_contains "$OUTPUT" "legacy path" "the degraded path is loud"
assert_contains "$OUTPUT" "$STUB_DIR/loom-daemon" "names the resolved binary"
assert_contains "$OUTPUT" "loom-daemon 0.19.870 (mock)" "names the binary's own --version"
assert_contains "$OUTPUT" "forge verdict-lock" "names the missing verbs"
assert_contains "$OUTPUT" "roll loom-daemon" "says to roll the daemon"
assert_not_contains "$OUTPUT" "older than" "no guessed version floor"
assert_eq "330" "$(cat "$STUB_DIR/last-pr.txt" 2>/dev/null || true)" "approval comment posted"
assert_contains "$(cat "$STUB_DIR/last-body.txt" 2>/dev/null || true)" "verdict=approved" "the verdict-sha marker is still appended"
assert_eq "" "$(cat "$STUB_DIR/daemon-calls.log" 2>/dev/null || true)" "verb-less daemon: no lock, gate or label call"
assert_contains "$(cat "$STUB_DIR/pr-edit.log" 2>/dev/null || true)" "pr edit 330 --repo owner/repo --add-label loom:pr --remove-label loom:review-requested --remove-label loom:reviewing" "legacy path: main's approval label write"
assert_not_contains "$(cat "$STUB_DIR/pr-edit.log" 2>/dev/null || true)" "--remove-label loom:changes-requested" "legacy approval never removes loom:changes-requested (#4560/#8112)"

# The legacy approval still runs the #10485 final head compare.
reset_state
touch "$STUB_DIR/old-daemon"
echo "fffffff0000000000000000000000000000000ff" > "$STUB_DIR/final-head"
run_pv 333 approved abc1234 --body "ok"
assert_eq "5" "$EXIT_CODE" "verb-less daemon + moved head -> exit 5"
no_comment "verb-less daemon (moved head)"
assert_eq "" "$(cat "$STUB_DIR/pr-edit.log" 2>/dev/null || true)" "moved head: no label flip"

# A changes-requested still posts, on the legacy path, loudly.
reset_state
touch "$STUB_DIR/old-daemon"
run_pv 331 changes-requested abc1234 --body "please fix"
assert_eq "0" "$EXIT_CODE" "verb-less daemon + changes-requested -> posted on the legacy path"
assert_contains "$OUTPUT" "legacy path" "the degraded path is loud"
assert_eq "331" "$(cat "$STUB_DIR/last-pr.txt" 2>/dev/null || true)" "changes-requested comment posted"
assert_contains "$(cat "$STUB_DIR/last-body.txt" 2>/dev/null || true)" "verdict=changes-requested" "the verdict-sha marker is still appended"
assert_eq "" "$(cat "$STUB_DIR/daemon-calls.log" 2>/dev/null || true)" "legacy path: no verdict verb called"
assert_contains "$(cat "$STUB_DIR/pr-edit.log" 2>/dev/null || true)" "pr edit 331 --repo owner/repo --add-label loom:changes-requested --remove-label loom:pr --remove-label loom:review-requested --remove-label loom:reviewing" "legacy path: the exclusive changes-requested transition"

# ... and a failed label step there is loud: exit 8 with the repair command.
reset_state
touch "$STUB_DIR/old-daemon" "$STUB_DIR/pr-edit-fail"
run_pv 332 changes-requested abc1234 --body "please fix"
assert_eq "8" "$EXIT_CODE" "legacy path label failure -> exit 8"
assert_eq "332" "$(cat "$STUB_DIR/last-pr.txt" 2>/dev/null || true)" "the comment itself was posted"
assert_contains "$OUTPUT" "Repair: gh pr edit 332 --repo owner/repo --add-label loom:changes-requested" "repair command is printed"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
  exit 1
fi
exit 0
