#!/usr/bin/env bash
# test-merge-pr-pr-state.sh - Wiring tests for merge-pr.sh's terminal-state
# gate (already merged / closed unmerged, #8191 slice).
#
# The decision is `loom-daemon merge-pr pr-state` (Rust,
# loom-daemon/src/merge_pr/pr_state.rs, with its own unit + CLI tests). This
# suite pins the merge-pr.sh-side WIRING against stub binaries driven through
# LOOM_DAEMON_BIN, in particular the rollout-window path: a daemon that lacks
# `pr-state` (exit 2, no output) or a missing binary must fall back to the
# retired PR_MERGED/PR_STATE predicate, so a merged PR still exits 0 with
# "already merged" and a closed PR still exits 1 -- never proceeding into the
# later gates (which can write to a terminal PR's branch).
#
# Usage: ./.loom/scripts/tests/test-merge-pr-pr-state.sh

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
assert_not_contains() { if grep -qF -- "$2" <<<"$1"; then fail "$3" "unexpected '$2' in: $1"; else ok "$3"; fi; }

warning() { echo "WARN: $*"; }
error()   { echo "ERROR: $*" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-pr-state.XXXXXX")"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT

# Extract the gate: from its `case` line through the line closing it with esac.
GATE="$WORK/gate.sh"
awk '/^case .*merge-pr pr-state / { on=1 } on { print } on && /esac$/ { exit }' "$MERGE_PR_SRC" > "$GATE"
if ! grep -q 'merge-pr pr-state' "$GATE" || ! grep -q 'esac$' "$GATE"; then echo "FATAL: could not extract the pr-state gate" >&2; exit 2; fi

make_stub() {
    local mode="$1" path; path="$WORK/daemon-$1"
    {
        echo '#!/usr/bin/env bash'
        case "$mode" in
            merged) echo "echo 'LOOM-PR-STATE MERGED'" ;;
            closed) echo "echo 'LOOM-PR-STATE CLOSED'" ;;
            open)   echo "echo 'LOOM-PR-STATE OPEN'" ;;
            # An older daemon lacking the verb: clap's usage error, exit 2, nothing on stdout.
            old)    echo "echo \"error: unrecognized subcommand 'pr-state'\" >&2; exit 2" ;;
        esac
    } > "$path"
    chmod +x "$path"
    printf '%s' "$path"
}

PR_NUMBER=7
LAST_OUT=""; LAST_ERR=""; LAST_RC=0
# run_gate <state> <merged>
run_gate() {
    PR_STATE="$1"; PR_MERGED="$2"
    set +e
    # shellcheck disable=SC1090
    LAST_OUT="$( (source "$GATE"; echo "REACHED-AFTER-GATE") 2>"$WORK/stderr")"; LAST_RC=$?
    LAST_ERR="$(cat "$WORK/stderr")"
    set -e
}

echo "Testing merge-pr.sh terminal-state gate..."

# T1: a positive verdict is acted on.
LOOM_DAEMON_BIN="$(make_stub merged)" run_gate closed true
assert_eq 0 "$LAST_RC" "MERGED -> exit 0"
assert_contains "$LAST_OUT" "already merged" "MERGED -> 'already merged'"
assert_not_contains "$LAST_OUT" "REACHED-AFTER-GATE" "MERGED -> later gates never run"
LOOM_DAEMON_BIN="$(make_stub closed)" run_gate closed false
assert_eq 1 "$LAST_RC" "CLOSED -> exit 1"
assert_contains "$LAST_ERR" "is closed (not merged)" "CLOSED -> refusal message"
LOOM_DAEMON_BIN="$(make_stub open)" run_gate open false
assert_eq 0 "$LAST_RC" "OPEN -> proceeds"
assert_contains "$LAST_OUT" "REACHED-AFTER-GATE" "OPEN -> merge flow continues"

# T2: older daemon without `pr-state` and a missing binary fall back to the
# retired predicate -- identical exit codes and messages.
for bin in "$(make_stub old)" "$WORK/does-not-exist"; do
    label="$(basename "$bin")"
    LOOM_DAEMON_BIN="$bin" run_gate closed true
    assert_eq 0 "$LAST_RC" "$label: merged PR -> exit 0"
    assert_contains "$LAST_OUT" "already merged" "$label: merged PR -> 'already merged'"
    assert_not_contains "$LAST_OUT" "REACHED-AFTER-GATE" "$label: merged PR -> later gates never run"
    LOOM_DAEMON_BIN="$bin" run_gate closed false
    assert_eq 1 "$LAST_RC" "$label: closed PR -> exit 1"
    assert_contains "$LAST_ERR" "is closed (not merged)" "$label: closed PR -> refusal message"
    assert_not_contains "$LAST_OUT" "REACHED-AFTER-GATE" "$label: closed PR -> later gates never run"
    LOOM_DAEMON_BIN="$bin" run_gate open false
    assert_eq 0 "$LAST_RC" "$label: open PR -> proceeds"
    assert_contains "$LAST_OUT" "REACHED-AFTER-GATE" "$label: open PR -> merge flow continues"
done

echo
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
