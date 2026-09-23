#!/usr/bin/env bash
# test-merge-pr-stale-check-redate.sh - Unit tests for the --redate-stale-checks
# remedy wired into merge-pr.sh's #8248 required-check freshness guard (#8508).
#
# THE GAP this closes: #8248's guard is correct policy, but nothing in the
# fleet produces the fresh evidence it waits for. PR #8493 failed THREE
# consecutive Champion merge ticks (2026-09-21) with the identical refusal —
# its branch had no new commits so no CI run ever re-dated its checks, `main`
# kept advancing so every retry compared against a later base tip, and both
# merge-pr.sh's internal re-run and `gh run rerun` failed with `Resource not
# accessible by integration` (no actions:write). A Judge-approved,
# safety-criteria-clean PR could therefore sit blocked indefinitely, and after
# Champion's idempotency guard suppressed the repeat failure comments there
# was no durable record of it anywhere on the PR.
#
# The remedy itself is `loom-daemon merge-pr redate-checks` (Rust,
# loom-daemon/src/merge_pr/redate.rs), covered by its own unit tests including
# the bound (one push per head) and the escalation to a loom:operator hold.
# This suite exercises the merge-pr.sh-side WIRING against a stub binary
# driven through LOOM_DAEMON_BIN:
#
#   1. the remedy is OFF by default — an unflagged run still hard-blocks and
#      never invokes the subcommand (the flag is opt-in, so a human merging by
#      hand never has a commit pushed onto their branch by surprise);
#   2. --redate-stale-checks + a stale verdict -> the subcommand is invoked
#      with the documented operands and, on its exit 0, merge-pr.sh exits 4
#      (the distinct "re-dated, not merged" outcome) rather than 1;
#   3. every non-zero subcommand outcome (escalated/head-moved/failed) leaves
#      the ORIGINAL #8248 refusal standing — the guard is never bypassed;
#   4. the remedy never runs when the guard passed, when the guard could not
#      run (fail-closed, exit 2), or under --dry-run.
#
# It therefore needs a built loom-daemon only for the #8508 subcommand's
# existence check at the end; the wiring tests use a stub. Wired in the
# "Native Port Suites" CI job alongside test-merge-pr-stale-required-checks.sh.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-stale-check-redate.sh

# SC2034: PR_NUMBER/PR_JSON/PR_HEAD_SHA/PR_BRANCH/REPO_NWO/DRY_RUN/FORGE_TYPE
# are read only by the extracted+sourced function, which shellcheck cannot see.
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

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

# --- Minimal logging/error shims the extracted function calls ---
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }
error()   { echo "ERROR: $*" >&2; exit 1; }

# --- The real binary, for the subcommand-exists assertion at the end ---
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr"
REAL_DAEMON_BIN="$LOOM_DAEMON_SELF_BIN"

# --- Extract the guard from merge-pr.sh and source it -----------------------
# Extracting from source keeps the suite in lockstep with the script instead
# of re-implementing its wiring. Same extraction shape as
# test-merge-pr-stale-required-checks.sh.
FUNCS_FILE="$(mktemp)"
STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-redate.XXXXXX")"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT

awk '/^_check_required_check_freshness\(\) \{/ { print; exit }' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_check_required_check_freshness()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _check_required_check_freshness from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- A stub loom-daemon covering BOTH subcommands the guard may call --------
#
# Contract being stubbed:
#   merge-pr stale-checks : 0 + CLEAN sentinel / 1 + refusal / 2 + reason
#   merge-pr redate-checks: 0 + LOOM-REDATE-PUSHED / 1 failed / 3 head moved
#                           / 4 escalated to loom:operator
# $1 selects the stale-checks answer, $2 the redate-checks answer. Each
# invocation's argv is appended to $STUB_DIR/argv-<subcommand> so the suite
# can assert which calls happened and with which operands.
make_stub() {
    local checks_mode="$1" redate_mode="$2" path
    path="$STUB_DIR/loom-daemon-$checks_mode-$redate_mode"
    {
        echo '#!/usr/bin/env bash'
        echo 'SUB="$2"'
        echo "printf '%s\n' \"\$*\" >> \"$STUB_DIR/argv-\$SUB\""
        echo 'if [ "$SUB" = "stale-checks" ]; then'
        case "$checks_mode" in
            clean)  echo "  echo 'LOOM-STALE-CHECKS-CLEAN'; exit 0" ;;
            stale)  echo "  echo \"Merge blocked: PR #8493's required check \\\`.gitignore Convergence Check\\\` last ran at 2026-09-21 10:47:20 UTC, before the current base-branch tip — its green result is evidence about a tree that no longer exists (#8248).\"; exit 1" ;;
            nodate) echo "  echo 'could not determine'; exit 2" ;;
        esac
        echo 'fi'
        echo 'if [ "$SUB" = "redate-checks" ]; then'
        case "$redate_mode" in
            pushed)    echo "  echo 'LOOM-REDATE-PUSHED sha=cafe1234'; exit 0" ;;
            escalated) echo "  echo 'LOOM-REDATE-ESCALATED pr=8493 head=deadbeef label=loom:operator notice=posted'; exit 4" ;;
            moved)     echo "  echo 'branch already moved to feed0000'; exit 3" ;;
            failed)    echo "  echo 'could not create the re-date commit'; exit 1" ;;
        esac
        echo 'fi'
        echo 'echo "stub: unexpected subcommand $SUB" >&2; exit 64'
    } > "$path"
    chmod +x "$path"
    printf '%s' "$path"
}

# --- Shared globals the function reads ---
PR_NUMBER="8493"
PR_JSON='{"base":{"ref":"main"}}'
PR_HEAD_SHA="deadbeef"
PR_BRANCH="feature/issue-8478"
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

echo "Testing --redate-stale-checks wiring (#8508)..."

# T1: OFF by default. An unflagged run keeps today's behavior exactly: hard
# block (exit 1) and the redate subcommand is never invoked. This is what
# protects a human's `merge-pr.sh 123` from having a commit pushed onto their
# branch without asking.
STUB="$(make_stub stale pushed)"
unset REDATE_STALE_CHECKS
LOOM_DAEMON_BIN="$STUB" run_guard
assert_eq "1" "$LAST_RC" "no flag + stale -> unchanged hard block (exit 1)"
assert_eq "no" "$([[ -f "$STUB_DIR/argv-redate-checks" ]] && echo yes || echo no)" \
    "no flag -> the redate remedy is never invoked"

# T2: THE REMEDY. Flagged + stale -> the subcommand runs with the documented
# operands and its exit 0 becomes merge-pr.sh's exit 4 ("re-dated, not merged").
rm -f "$STUB_DIR/argv-redate-checks"
REDATE_STALE_CHECKS=true
LOOM_DAEMON_BIN="$STUB" run_guard
assert_eq "4" "$LAST_RC" "flagged + stale + push succeeded -> exit 4, NOT 1"
REDATE_ARGV="$(cat "$STUB_DIR/argv-redate-checks" 2>/dev/null || echo "")"
assert_contains "$REDATE_ARGV" "merge-pr redate-checks" "the remedy subcommand is invoked"
assert_contains "$REDATE_ARGV" "--pr 8493" "remedy is passed the PR number"
assert_contains "$REDATE_ARGV" "--repo rjwalters/loom" "remedy is passed the repository"
assert_contains "$REDATE_ARGV" "--branch feature/issue-8478" "remedy is passed the HEAD BRANCH, not the PR number"
assert_contains "$REDATE_ARGV" "--expected-head-sha deadbeef" "remedy gates on the SHA this merge attempt used"
assert_contains "$LAST_OUT" "LOOM-REDATE-PUSHED" "the remedy's own output is surfaced"
assert_contains "$LAST_OUT" "not merged" "exit 4 is explained as 're-dated, not merged'"

# T3: the bound was reached and the PR was escalated (subcommand exit 4) ->
# the ORIGINAL #8248 refusal still blocks the merge. The guard is not weakened
# by a remedy that could not produce fresh evidence.
STUB="$(make_stub stale escalated)"
LOOM_DAEMON_BIN="$STUB" run_guard
assert_eq "1" "$LAST_RC" "escalated -> the #8248 refusal still blocks (exit 1)"
assert_contains "$LAST_OUT" "no longer exists (#8248)" "the original refusal text is preserved"
assert_contains "$LAST_OUT" "LOOM-REDATE-ESCALATED" "the escalation is reported alongside it"

# T4: head moved out from under the remedy (subcommand exit 3) -> same: the
# refusal stands, annotated with why no fresh evidence was produced.
STUB="$(make_stub stale moved)"
LOOM_DAEMON_BIN="$STUB" run_guard
assert_eq "1" "$LAST_RC" "head moved -> the #8248 refusal still blocks (exit 1)"
assert_contains "$LAST_OUT" "no longer exists (#8248)" "refusal preserved on the head-moved path"

# T5: the remedy itself failed (subcommand exit 1) -> refusal stands, and the
# reason is attached so the operator does not have to guess.
STUB="$(make_stub stale failed)"
LOOM_DAEMON_BIN="$STUB" run_guard
assert_eq "1" "$LAST_RC" "remedy failure -> the #8248 refusal still blocks (exit 1)"
assert_contains "$LAST_OUT" "did not produce fresh evidence" "the failed remedy is explained"

# T6: a missing binary cannot turn the remedy into a bypass.
LOOM_DAEMON_BIN="$STUB_DIR/does-not-exist" run_guard
assert_eq "1" "$LAST_RC" "missing loom-daemon + flag -> still refused (fail closed)"

# T7: the guard PASSED -> the remedy must not run at all (nothing to re-date).
rm -f "$STUB_DIR/argv-redate-checks"
STUB="$(make_stub clean pushed)"
LOOM_DAEMON_BIN="$STUB" run_guard
assert_eq "0" "$LAST_RC" "clean -> guard passes through"
assert_eq "no" "$([[ -f "$STUB_DIR/argv-redate-checks" ]] && echo yes || echo no)" \
    "clean -> the remedy is never invoked"

# T8: could-not-determine (exit 2, fail closed) -> the remedy must NOT run.
# An undeterminable freshness is not "stale"; pushing a commit would be acting
# on an unknown, which is exactly what the fail-closed contract forbids.
rm -f "$STUB_DIR/argv-redate-checks"
STUB="$(make_stub nodate pushed)"
LOOM_DAEMON_BIN="$STUB" run_guard
assert_eq "1" "$LAST_RC" "exit 2 (undeterminable) -> still refused"
assert_eq "no" "$([[ -f "$STUB_DIR/argv-redate-checks" ]] && echo yes || echo no)" \
    "undeterminable freshness -> the remedy is never invoked (no push on an unknown)"

# T9: --dry-run never writes. A dry run reports the would-be block and must
# not push a commit to anyone's branch.
rm -f "$STUB_DIR/argv-redate-checks"
DRY_RUN=true
STUB="$(make_stub stale pushed)"
LOOM_DAEMON_BIN="$STUB" run_guard
assert_eq "0" "$LAST_RC" "--dry-run + stale -> dry-run contract preserved (exit 0)"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run still reports the would-be block"
assert_eq "no" "$([[ -f "$STUB_DIR/argv-redate-checks" ]] && echo yes || echo no)" \
    "--dry-run -> the remedy is never invoked (no writes)"
DRY_RUN=false

# T10: non-GitHub forge -> the whole guard, remedy included, is a no-op.
rm -f "$STUB_DIR/argv-redate-checks"
FORGE_TYPE="gitea"
LOOM_DAEMON_BIN="$STUB" run_guard
assert_eq "0" "$LAST_RC" "gitea -> guard (and remedy) are a no-op"
assert_eq "no" "$([[ -f "$STUB_DIR/argv-redate-checks" ]] && echo yes || echo no)" \
    "gitea -> the remedy is never invoked"
FORGE_TYPE="github"
unset REDATE_STALE_CHECKS

# --- Script-level contract --------------------------------------------------

# T11: the flag is accepted by the argument parser and advertised in --help.
HELP_OUT="$(bash "$MERGE_PR_SRC" --help 2>&1)"
assert_contains "$HELP_OUT" "--redate-stale-checks" "--help advertises the flag"
assert_contains "$HELP_OUT" "4 = stale required checks were re-dated" "--help documents exit 4"
assert_contains "$(grep -c -- '--redate-stale-checks) REDATE_STALE_CHECKS=true' "$MERGE_PR_SRC")" "1" \
    "the argument parser has exactly one arm for the flag"

# T12: the remedy must NEVER be reachable without the guard having first
# reported STALE (rc 1). Guards against a refactor that hoists the remedy out
# of the refusal path and turns it into an unconditional push.
GUARD_LINE="$(grep -n '^_check_required_check_freshness() {' "$MERGE_PR_SRC" | cut -d: -f1)"
GUARD_BODY="$(sed -n "${GUARD_LINE}p" "$MERGE_PR_SRC")"
assert_contains "$GUARD_BODY" 'REDATE_STALE_CHECKS:-false' "the remedy is gated on the opt-in flag"
assert_contains "$GUARD_BODY" 'rc -eq 1' "the remedy is gated on a STALE verdict specifically"
assert_not_contains "$GUARD_BODY" '--allow-stale' "no bypass flag was introduced for #8248"

# T13: the real binary knows the subcommand (a stub cannot prove this, and an
# old binary that silently lacks it must fail loudly rather than look like a
# remedy that ran).
set +e
REDATE_HELP="$("$REAL_DAEMON_BIN" merge-pr redate-checks --help 2>&1)"
REDATE_HELP_RC=$?
set -e
assert_eq "0" "$REDATE_HELP_RC" "real binary: 'merge-pr redate-checks --help' exits 0"
for opt in --pr --repo --branch --expected-head-sha; do
    assert_contains "$REDATE_HELP" "$opt" "real binary: subcommand accepts $opt"
done

echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
