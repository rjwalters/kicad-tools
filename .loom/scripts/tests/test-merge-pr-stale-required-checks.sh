#!/usr/bin/env bash
# test-merge-pr-stale-required-checks.sh - Unit tests for the PRE-merge
# required-check freshness guard in merge-pr.sh (#8248).
#
# THE INCIDENT this guard exists for: PR #8078's File Size Ratchet ran green
# at 2026-09-17T22:54:20Z; PR #8204 tightened that baseline entry 1845->1815
# at 2026-09-18T11:45:21Z; PR #8078 hand-merged at 20:31Z with the 22h-old
# green result never re-run, landing 1816 onto a 1815 baseline. main was red.
# A stale green ratchet result is not "we don't know" — it is "we are
# asserting slack that has since been spent", so the merge must be refused
# until the check re-runs against the current head.
#
# The decision is `loom-daemon merge-pr stale-checks` (Rust,
# loom-daemon/src/merge_pr/stale_checks.rs, slice 3 of the merge-pr port
# #8191), covered by its own unit tests including the incident timeline.
# This suite exercises BOTH halves:
#
#   1. the merge-pr.sh-side WIRING, against a stub binary driven through
#      LOOM_DAEMON_BIN (a fake whose stdout/exit encode the daemon's
#      contract): pass-through on CLEAN, hard-block on the stale refusal,
#      fail-closed on exit 2 / missing binary / silent-but-nonzero output,
#      the --dry-run report-without-block contract, and that the guard is a
#      no-op on non-GitHub forges;
#   2. the REAL binary's offline contract via --from-stdin — the incident
#      payload must produce exit 1 with a refusal naming the check and both
#      timestamps, and a fresh payload must produce the CLEAN sentinel.
#
# It therefore needs a built loom-daemon; it is wired in the "Native Port
# Suites" CI job, which builds one first, and FAILS (never skips) without
# one. Strategy mirrors test-merge-pr-verdict-label-guard.sh (#8112).
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-stale-required-checks.sh

# SC2034: PR_NUMBER/PR_JSON/PR_HEAD_SHA/REPO_NWO/DRY_RUN/FORGE_TYPE are read
# only by the extracted+sourced function, which shellcheck cannot see.
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

# --- Minimal logging/error shims the extracted function calls ---
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }
error()   { echo "ERROR: $*" >&2; exit 1; }

# --- Pin the REAL binary for the --from-stdin contract tests below ---
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr"
REAL_DAEMON_BIN="$LOOM_DAEMON_SELF_BIN"

# --- Extract the function under test from merge-pr.sh and source it ---
# From the `# Pre-merge required-check freshness guard (#8248).` banner to the
# invocation line after the dense one-line function — extracting from source
# keeps the suite in lockstep with the script instead of re-implementing it.
FUNCS_FILE="$(mktemp)"
STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-stale-checks.XXXXXX")"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT

awk '/^_check_required_check_freshness\(\) \{/ { print; exit }' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_check_required_check_freshness()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _check_required_check_freshness from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- A stub loom-daemon whose behavior the test scripts ---
#
# Contract being stubbed (cli/merge_pr_stale_checks.rs):
#   exit 0 + "LOOM-STALE-CHECKS-CLEAN"          = every required check fresh
#   exit 1 + refusal text                        = stale (blocks)
#   exit 2 + reason                              = could not determine
# The stub records its argv so the suite can assert the guard passes the
# right operands (--pr/--repo/--head-sha/--base-ref), then emits the canned
# outcome named by $STUB_MODE via a marker file for stdout capture.
make_stub() {
    local mode="$1" path
    path="$STUB_DIR/loom-daemon-$mode"
    {
        echo '#!/usr/bin/env bash'
        echo "printf '%s\n' \"\$*\" > '$STUB_DIR/argv-$mode'"
        case "$mode" in
            clean)  echo "echo 'LOOM-STALE-CHECKS-CLEAN'; exit 0" ;;
            stale)  echo "cat <<'REFUSAL'
Merge blocked: PR #8078's required check \`File Size Ratchet\` last ran at 2026-09-17 22:54:20 UTC, before the current base-branch tip (2026-09-18 11:45:21 UTC, abc123) — its green result is evidence about a tree that no longer exists (#8248).
REFUSAL
exit 1" ;;
            nodate) echo "echo 'could not determine'; exit 2" ;;
            silent) echo "exit 0" ;;
            hang)   echo "exit 127" ;;
        esac
    } > "$path"
    chmod +x "$path"
    printf '%s' "$path"
}
STUB_CLEAN="$(make_stub clean)"
STUB_STALE="$(make_stub stale)"
STUB_NODATE="$(make_stub nodate)"
STUB_SILENT="$(make_stub silent)"

# --- Shared globals the function reads ---
PR_NUMBER="8078"
PR_JSON='{"base":{"ref":"main"}}'
PR_HEAD_SHA="deadbeef"
REPO_NWO="rjwalters/loom"
DRY_RUN=false
FORGE_TYPE="github"

LAST_OUT=""
LAST_RC=0
run_guard() {
    set +e
    LAST_OUT="$( _check_required_check_freshness 2>&1 )"
    LAST_RC=$?
    set -e
}

echo "Testing _check_required_check_freshness behavior..."

# T1: CLEAN sentinel -> guard passes through, and the operands are the
# documented ones (PR, repo, head SHA, base ref resolved from $PR_JSON).
LOOM_DAEMON_BIN="$STUB_CLEAN" run_guard
assert_eq "0" "$LAST_RC" "clean sentinel -> guard passes (exit 0)"
assert_contains "$(cat "$STUB_DIR/argv-clean")" "--pr 8078" "guard passes the PR number"
assert_contains "$(cat "$STUB_DIR/argv-clean")" "--repo rjwalters/loom" "guard passes the repository"
assert_contains "$(cat "$STUB_DIR/argv-clean")" "--head-sha deadbeef" "guard passes the head SHA"
assert_contains "$(cat "$STUB_DIR/argv-clean")" "--base-ref main" "guard resolves the base ref from PR_JSON"

# T2: THE INCIDENT — stale refusal -> hard block naming the check and BOTH
# timestamps. Baseline tightened at 11:45Z under a check that ran at 22:54Z
# the night before: the merge must NOT stay green.
LOOM_DAEMON_BIN="$STUB_STALE" run_guard
assert_eq "1" "$LAST_RC" "stale green required check -> merge hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "Merge blocked" "stale block message is emitted"
assert_contains "$LAST_OUT" "File Size Ratchet" "block names the stale check"
assert_contains "$LAST_OUT" "2026-09-17 22:54:20 UTC" "block names the run's start timestamp"
assert_contains "$LAST_OUT" "2026-09-18 11:45:21 UTC" "block names the base-tip timestamp"

# T3: could-not-determine (exit 2) -> fails CLOSED. A freshness guard that
# cannot run must not read as one that passed (ci-principles rule 6).
LOOM_DAEMON_BIN="$STUB_NODATE" run_guard
assert_eq "1" "$LAST_RC" "exit 2 (undeterminable) -> merge refused (fail closed)"
assert_contains "$LAST_OUT" "could not run" "fail-closed path explains itself"

# T4: exit 0 WITHOUT the sentinel (old/silent binary) -> fails closed too;
# only a positive clean signal passes.
LOOM_DAEMON_BIN="$STUB_SILENT" run_guard
assert_eq "1" "$LAST_RC" "exit 0 without the sentinel -> merge refused (fail closed)"

# T5: missing binary entirely -> fails closed with the build/install remedy.
LOOM_DAEMON_BIN="$STUB_DIR/does-not-exist" run_guard
assert_eq "1" "$LAST_RC" "missing loom-daemon -> merge refused (fail closed)"
assert_contains "$LAST_OUT" "cargo build" "fail-closed message names the remedy"

# T6: --dry-run + stale -> reports the would-be block, exits 0 (dry-run
# contract shared with every other guard in this script).
DRY_RUN=true
LOOM_DAEMON_BIN="$STUB_STALE" run_guard
assert_eq "0" "$LAST_RC" "--dry-run + stale -> guard does NOT exit 1 (dry-run contract)"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run reports the would-be block"
DRY_RUN=false

# T7: non-GitHub forge -> guard is a no-op and never invokes the binary.
FORGE_TYPE="gitea"
rm -f "$STUB_DIR/argv-clean"
LOOM_DAEMON_BIN="$STUB_CLEAN" run_guard
assert_eq "0" "$LAST_RC" "gitea -> guard is a no-op (exit 0)"
assert_eq "" "$([[ -f "$STUB_DIR/argv-clean" ]] && cat "$STUB_DIR/argv-clean" || echo "")" "gitea -> the binary was never invoked"
FORGE_TYPE="github"

# T8: PR_JSON without .base.ref -> falls back to DEFAULT_BRANCH_NAME.
PR_JSON='{}'
DEFAULT_BRANCH_NAME="trunk"
LOOM_DAEMON_BIN="$STUB_CLEAN" run_guard
assert_contains "$(cat "$STUB_DIR/argv-clean")" "--base-ref trunk" "missing .base.ref -> DEFAULT_BRANCH_NAME used"
PR_JSON='{"base":{"ref":"main"}}'
unset DEFAULT_BRANCH_NAME

# --- The REAL binary's offline contract (--from-stdin) ----------------------
#
# The wiring above proves the shell side; these prove the binary side of the
# same incident: the exact #8248 timeline goes in, exit 1 + a refusal naming
# the check and both timestamps comes out; a fresh run yields the sentinel.
INCIDENT_PAYLOAD='{"tip_sha":"abc123","base_tip":"2026-09-18T11:45:21Z","required":["File Size Ratchet"],"check_runs":[{"name":"File Size Ratchet","status":"completed","conclusion":"success","started_at":"2026-09-17T22:54:20Z"}]}'
FRESH_PAYLOAD='{"tip_sha":"abc123","base_tip":"2026-09-18T11:45:21Z","required":["File Size Ratchet"],"check_runs":[{"name":"File Size Ratchet","status":"completed","conclusion":"success","started_at":"2026-09-18T12:00:00Z"}]}'

set +e
BIN_OUT="$(printf '%s' "$INCIDENT_PAYLOAD" | "$REAL_DAEMON_BIN" merge-pr stale-checks --pr 8078 --repo rjwalters/loom --head-sha deadbeef --base-ref main --from-stdin 2>&1)"
BIN_RC=$?
set -e
assert_eq "1" "$BIN_RC" "real binary: incident timeline -> exit 1 (the merge is refused)"
assert_contains "$BIN_OUT" "File Size Ratchet" "real binary: refusal names the stale check"
assert_contains "$BIN_OUT" "2026-09-17 22:54:20 UTC" "real binary: refusal names the run timestamp"
assert_contains "$BIN_OUT" "2026-09-18 11:45:21 UTC" "real binary: refusal names the base-tip timestamp"

set +e
BIN_OUT="$(printf '%s' "$FRESH_PAYLOAD" | "$REAL_DAEMON_BIN" merge-pr stale-checks --pr 8078 --repo rjwalters/loom --head-sha deadbeef --base-ref main --from-stdin 2>&1)"
BIN_RC=$?
set -e
assert_eq "0" "$BIN_RC" "real binary: fresh run -> exit 0"
assert_contains "$BIN_OUT" "LOOM-STALE-CHECKS-CLEAN" "real binary: fresh run prints the CLEAN sentinel"

# --- Placement: the guard must run before either merge path -----------------
IFS= read -r first_match < <(grep -n '^_check_required_check_freshness$' "$MERGE_PR_SRC")
guard_line="${first_match%%:*}"
IFS= read -r first_match < <(grep -n '^# Handle auto-merge mode' "$MERGE_PR_SRC")
automerge_line="${first_match%%:*}"
assert_eq yes "$( [[ -n "$guard_line" && -n "$automerge_line" && "$guard_line" -lt "$automerge_line" ]] && echo yes || echo no )" "Guard precedes both merge paths"

echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
