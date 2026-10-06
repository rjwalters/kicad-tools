#!/usr/bin/env bash
# test-merge-pr-ci-result.sh - Tests for merge-pr.sh's CI-run conclusion gate
# (_check_ci_result, #10444).
#
# The decision is `loom-daemon merge-pr ci-result` (Rust,
# loom-daemon/src/merge_pr/ci_result.rs, with its own unit tests). This suite
# pins the merge-pr.sh WIRING against a stub binary (LOOM_DAEMON_BIN): the
# #10403 fixture (Detect Changes cancelled while the three required contexts
# are green) is refused with the cancelled job named and the rerun hint; a
# clean run passes; an unanswered query (exit 2, missing binary, UNVERIFIED)
# warns but does not block; --dry-run reports without blocking.
#
# Usage: ./.loom/scripts/tests/test-merge-pr-ci-result.sh

# shellcheck disable=SC2034
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0
ok()   { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; [[ -z "${2:-}" ]] || echo "    $2"; }
assert_eq() { if [[ "$1" == "$2" ]]; then ok "$3"; else fail "$3" "expected '$1' got '$2'"; fi; }
assert_contains() { if grep -qF -- "$2" <<<"$1"; then ok "$3"; else fail "$3" "missing '$2' in: $1"; fi; }

warning() { echo "WARN: $*" >&2; }
error()   { echo "ERROR: $*" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-ci-result.XXXXXX")"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT

FUNCS="$WORK/funcs.sh"
awk '/^_check_ci_result\(\) \{/ { print; exit }' "$MERGE_PR_SRC" > "$FUNCS"
grep -q '_check_ci_result()' "$FUNCS" || { echo "FATAL: could not extract _check_ci_result" >&2; exit 2; }
# shellcheck disable=SC1090
source "$FUNCS"

make_stub() {
    local mode="$1" path; path="$WORK/daemon-$1"
    {
        echo '#!/usr/bin/env bash'
        echo "printf '%s\n' \"\$*\" >> '$WORK/argv-$mode'"
        case "$mode" in
            clean)      echo "echo LOOM-CI-RESULT-CLEAN" ;;
            cancelled)  echo "echo 'Merge blocked: PR #7 CI run concluded failure. Failed/cancelled jobs: Detect Changes (cancelled), Repo Hygiene Checks (cancelled). Re-run in place with gh run rerun --failed 2'; exit 1" ;;
            unverified) echo "echo 'LOOM-CI-RESULT-UNVERIFIED no CI workflow run found'" ;;
            broken)     echo "echo 'ci-result could not query the forge: rate limited'; exit 2" ;;
        esac
    } > "$path"
    chmod +x "$path"
    printf '%s' "$path"
}

PR_NUMBER=7; PR_HEAD_SHA=deadbeef; REPO_NWO=o/r; FORGE_TYPE=github; DRY_RUN=false
LAST_OUT=""; LAST_RC=0
run_guard() { set +e; LAST_OUT="$(_check_ci_result 2>&1)"; LAST_RC=$?; set -e; }

echo "Testing _check_ci_result behavior..."

LOOM_DAEMON_BIN="$(make_stub clean)" run_guard
assert_eq 0 "$LAST_RC" "successful CI run -> passes"
assert_contains "$(cat "$WORK/argv-clean")" "merge-pr ci-result --pr 7 --repo o/r --head-sha deadbeef" "daemon gets PR/repo/head operands"

# The #10403 fixture: Detect Changes cancelled, required contexts green.
LOOM_DAEMON_BIN="$(make_stub cancelled)" run_guard
assert_eq 1 "$LAST_RC" "cancelled Detect Changes -> merge refused"
assert_contains "$LAST_OUT" "Detect Changes (cancelled)" "refusal names the cancelled job"
assert_contains "$LAST_OUT" "gh run rerun --failed" "refusal suggests the in-place rerun"

LOOM_DAEMON_BIN="$(make_stub unverified)" run_guard
assert_eq 0 "$LAST_RC" "no verdict (UNVERIFIED) -> does not block"
assert_contains "$LAST_OUT" "gave no verdict" "no-verdict path warns"

LOOM_DAEMON_BIN="$(make_stub broken)" run_guard
assert_eq 0 "$LAST_RC" "forge unreachable (exit 2) -> does not block"
assert_contains "$LAST_OUT" "gave no verdict" "unreachable path warns instead of claiming all-clear"

LOOM_DAEMON_BIN="$WORK/does-not-exist" run_guard
assert_eq 0 "$LAST_RC" "missing/old binary -> warns, does not block (fail-open verb)"
assert_contains "$LAST_OUT" "gave no verdict" "missing binary warns"

DRY_RUN=true LOOM_DAEMON_BIN="$(make_stub cancelled)" run_guard
assert_eq 0 "$LAST_RC" "--dry-run -> does not block"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run reports the would-block"
DRY_RUN=false

FORGE_TYPE=gitea; rm -f "$WORK/argv-cancelled"
LOOM_DAEMON_BIN="$(make_stub cancelled)" run_guard
assert_eq 0 "$LAST_RC" "non-GitHub forge -> gate skipped"
[[ ! -e "$WORK/argv-cancelled" ]] && ok "non-GitHub forge -> daemon never invoked" || fail "non-GitHub forge -> daemon never invoked"
FORGE_TYPE=github

# End-to-end against a REAL daemon build when one exists: the Rust assessor on
# the #10403 fixture via --from-stdin (skipped, not failed, when unbuilt).
REPO_ROOT_DIR="$(cd "$HELPERS_DIR/.." 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || true)"
REAL=""
for c in "${LOOM_REAL_DAEMON_BIN:-}" "$REPO_ROOT_DIR/target/debug/loom-daemon"; do
    [[ -n "$c" && -x "$c" ]] && "$c" merge-pr ci-result --help >/dev/null 2>&1 && { REAL="$c"; break; }
done
if [[ -n "$REAL" ]]; then
    FIX='{"runs":{"workflow_runs":[{"id":9,"name":"CI","status":"completed","conclusion":"failure","created_at":"2026-10-04T21:00:00Z","html_url":"u"}]},
          "jobs":{"jobs":[{"name":"Detect Changes","conclusion":"cancelled"},{"name":"Structural Checks","conclusion":"success"},{"name":"Daemon Checks","conclusion":"success"},{"name":"Shell Syntax (macos-latest)","conclusion":"success"}]}}'
    set +e; out="$(printf '%s' "$FIX" | "$REAL" merge-pr ci-result --pr 7 --head-sha deadbeef --from-stdin 2>&1)"; rc=$?; set -e
    assert_eq 1 "$rc" "real daemon: cancelled Detect Changes + green required contexts -> refuse"
    assert_contains "$out" "Detect Changes (cancelled)" "real daemon: names the cancelled job"
    assert_contains "$out" "gh run rerun --failed 9" "real daemon: rerun hint carries the run id"
else
    echo "  SKIP: no built loom-daemon with merge-pr ci-result (Rust unit tests cover the assessor)"
fi

echo
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
