#!/usr/bin/env bash
# test-merge-pr-head-sync-retry.sh — the named regression suite for #8164:
# merge-pr.sh's own main-sync moved the head, then its merge call was refused
# 409 "Head branch was modified" because it re-used the pre-sync head SHA.
#
# THE INCIDENT: `merge-pr.sh <n>` on a Judge-approved, mergeable PR failed with
#   Details: PR #<n>: {"message":"Head branch was modified. ...","status":"409"}
# The PR's timeline named the culprit: merge-pr.sh itself. Its merge-retry
# loops answer "Base branch was modified" by calling forge_update_branch(),
# which lands a `Merge branch 'main' into <branch>` commit ON THE HEAD BRANCH,
# and then retried the merge with $MERGE_PRECONDITION_SHA as it stood BEFORE
# that push. GitHub refused; the #5579 classifier read the refusal as "the head
# moved under us" and exited 3. Re-running a minute later succeeded, because
# the fresh read observed the sync commit. The script lost a merge to a race it
# created itself.
#
# The fix has two halves, and this suite covers both:
#
#   1. _refresh_precondition_sha() — after ANY push to the head branch, record
#      the fact attribution depends on (this run pushed to the head branch at
#      all). Deliberately does NOT re-read or adopt the new head SHA itself —
#      an earlier version did, blindly, which meant a session pushing a commit
#      on top of ours mid-sync got squashed into the merge with no refusal.
#      Leaving the precondition alone routes every adoption through part 2.
#   2. _head_moved_or_resync() — reached on EVERY head-SHA adoption on this
#      path now, not just the asynchronous-push residual window. A mismatch is
#      retried ONCE, and only when `loom-daemon merge-pr head-sync-retry`
#      attributes the new head to our own sync: a two-parent merge whose FIRST
#      parent is the head we were about to merge and whose SECOND parent is
#      already in the base branch. Every other shape stays the #5579 hard stop,
#      because merging an unreviewed diff is worse than a spurious re-queue.
#
# The decision itself is Rust (loom-daemon/src/merge_pr/head_sync.rs, slice 4
# of the merge-pr port #8191) with its own unit tests; this suite exercises
#   1. the merge-pr.sh-side WIRING against stub binaries whose stdout/exit
#      encode the daemon's contract — including every way the binary can fail,
#      each of which must land on the pre-#8164 behaviour (exit 3, re-queue);
#   2. the REAL binary's offline contract via --from-stdin, driven with the
#      incident's own commit shape.
#
# It therefore needs a built loom-daemon; it is wired in the "Native Port
# Suites" CI job, which builds one first, and FAILS (never skips) without one.
# Strategy mirrors test-merge-pr-stale-required-checks.sh (#8248).
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-head-sync-retry.sh

# SC2034: PR_NUMBER/REPO_NWO/GH/YELLOW/NC/MERGE_ATTEMPT/MAX_MERGE_RETRIES are
# read only by the extracted+sourced functions, which shellcheck cannot see.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
YELLOW=''

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

# --- Running a function IN THIS SHELL while capturing its output ---
#
# A command substitution is a subshell, so `out="$(fn)"` discards exactly the
# global mutations these two functions exist to make ($MERGE_PRECONDITION_SHA,
# $_HEAD_SELF_SYNCED, $_HEAD_RESYNC_USED). Redirect to a file instead, and read
# it back afterwards — the function itself stays in the current shell.
CAPTURED=""
capture() {
    local log="$STUB_DIR/capture.log" rc=0
    set +e
    "$@" >"$log" 2>&1
    rc=$?
    set -e
    CAPTURED="$(cat "$log")"
    return "$rc"
}

# --- Minimal logging shims the extracted functions call ---
info()    { echo "INFO: $*"; }
warning() { echo "WARN: $*" >&2; }

# --- Pin the REAL binary for the --from-stdin contract tests below ---
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr"
REAL_DAEMON_BIN="$LOOM_DAEMON_SELF_BIN"

# --- Extract the functions under test from merge-pr.sh and source them ---
# Extracting from source keeps the suite in lockstep with the script rather
# than re-implementing it (the strategy test-merge-pr-head-mismatch.sh uses
# for _is_head_mismatch_response/error_head_moved, which this suite reuses).
FUNCS_FILE="$(mktemp)"
STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-head-sync.XXXXXX")"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT

# _refresh_precondition_sha is a single dense line (the file is ratchet-frozen
# and `shell-budget --check` refuses any growth of the portable pool), so it is
# captured by `print; next` rather than by the brace-matching the other two
# need — the same split test-merge-pr-stale-required-checks.sh makes.
awk '
  /^_refresh_precondition_sha\(\) \{/ { print; next }
  /^error_head_moved\(\) \{/          { capture=1 }
  /^_head_moved_or_resync\(\) \{/     { capture=1 }
  capture                             { print }
  /^}$/ && capture                    { capture=0 }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

for fn in error_head_moved _refresh_precondition_sha _head_moved_or_resync; do
    if ! grep -q "^$fn() {" "$FUNCS_FILE"; then
        echo -e "${RED}FATAL${NC}: could not extract $fn from $MERGE_PR_SRC" >&2
        exit 2
    fi
done
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Shared globals the extracted functions read ---
PR_NUMBER="8164"
REPO_NWO="rjwalters/loom"
GH="gh"
MERGE_ATTEMPT=1
MAX_MERGE_RETRIES=3

# The three SHAs of the incident.
APPROVED="aaaaaaaa1111111111111111111111111111aaaa"   # what Judge approved
SYNCED="cccccccc3333333333333333333333333333cccc"     # what update-branch left
FOREIGN="dddddddd4444444444444444444444444444dddd"    # someone else's push

# --- A stub forge_get_pr_nocache whose answer the test scripts ---
STUB_HEAD_SHA=""
STUB_PR_READ_FAILS=false
forge_get_pr_nocache() {
    [[ "$STUB_PR_READ_FAILS" == "true" ]] && return 1
    printf '{"head":{"sha":"%s"}}' "$STUB_HEAD_SHA"
}

# --- Stub loom-daemon binaries, one per contract outcome ---
#
# Contract being stubbed (cli/merge_pr_head_sync.rs):
#   exit 0 + "LOOM-HEAD-SELF-SYNC-RETRY <sha>"  = retry once against <sha>
#   exit 1 + the reason                          = foreign head move (re-queue)
#   exit 2 + the reason                          = undeterminable (re-queue)
make_stub() {
    local mode="$1" path
    path="$STUB_DIR/loom-daemon-$mode"
    {
        echo '#!/usr/bin/env bash'
        echo "printf '%s\n' \"\$*\" > '$STUB_DIR/argv-$mode'"
        echo "cat > '$STUB_DIR/stdin-$mode'"
        case "$mode" in
            retry)   echo "echo 'LOOM-HEAD-SELF-SYNC-RETRY $SYNCED'; exit 0" ;;
            foreign) echo "echo 'PR #8164: not retrying the head-SHA mismatch — the current head ${FOREIGN:0:8}'\\''s first parent is not the approved head.'; exit 1" ;;
            unknown) echo "echo 'PR #8164: not retrying — gh api failed'; exit 2" ;;
            silent)  echo "exit 0" ;;
            wrongsentinel) echo "echo 'RETRY $SYNCED'; exit 0" ;;
        esac
    } > "$path"
    chmod +x "$path"
    printf '%s' "$path"
}
STUB_RETRY="$(make_stub retry)"
STUB_FOREIGN="$(make_stub foreign)"
STUB_UNKNOWN="$(make_stub unknown)"
STUB_SILENT="$(make_stub silent)"
STUB_WRONG="$(make_stub wrongsentinel)"

# _head_moved_or_resync() either returns 0 (retry) or never returns (exit 3),
# so the refusal paths are run in a subshell to capture the code.
LAST_OUT=""
LAST_RC=0
run_resync_isolated() {
    set +e
    LAST_OUT="$( _head_moved_or_resync "$@" 2>&1 )"
    LAST_RC=$?
    set -e
}

# ============================================================================
# Part 1: _refresh_precondition_sha — records the push, adopts nothing
# ============================================================================
echo ""
echo "Testing _refresh_precondition_sha (records the push; leaves the SHA alone)..."

# T1: THE INCIDENT, corrected. An earlier version of this fix blindly adopted
# whatever head.sha the forge reported here — no parent inspection, no
# containment check. That is unsafe: a session pushing a commit on top of ours
# mid-sync (the #5579 scenario) would get squashed into the merge with no
# refusal, because nothing downstream re-checked the adopted SHA. The
# precondition must stay exactly what it was; only _head_moved_or_resync()
# below is allowed to move it, and only under the structural attribution check.
MERGE_PRECONDITION_SHA="$APPROVED"
_HEAD_SELF_SYNCED=""
STUB_HEAD_SHA="$SYNCED"
capture _refresh_precondition_sha || true
T1_OUT="$CAPTURED"
assert_eq "$APPROVED" "$MERGE_PRECONDITION_SHA" "the precondition is left untouched — no blind adoption of the new head"
assert_eq "true" "$_HEAD_SELF_SYNCED" "it still records that THIS run pushed to the head branch"
assert_eq "" "$T1_OUT" "it performs no forge read and logs nothing (there is no SHA to report yet)"

# T2: it does not consult the forge at all — no STUB_HEAD_SHA/STUB_PR_READ_FAILS
# dependency, so a forge read failure at this point cannot affect it, unlike
# the old re-read which had to special-case that failure.
MERGE_PRECONDITION_SHA="$APPROVED"
_HEAD_SELF_SYNCED=""
STUB_PR_READ_FAILS=true
_refresh_precondition_sha >/dev/null 2>&1
assert_eq "$APPROVED" "$MERGE_PRECONDITION_SHA" "the precondition is untouched even when the forge read would have failed"
assert_eq "true" "$_HEAD_SELF_SYNCED" "the push is still recorded regardless of forge state"
STUB_PR_READ_FAILS=false

# T3: it returns 0 unconditionally — it runs mid-loop under `set -e`, so a
# nonzero return would abort the merge run outright.
MERGE_PRECONDITION_SHA="$APPROVED"
set +e
_refresh_precondition_sha >/dev/null 2>&1
T3_RC=$?
set -e
assert_eq "0" "$T3_RC" "_refresh_precondition_sha always returns 0 (it runs under set -e)"

# T4: two base-syncs in the same run (e.g. the first retry's attributed
# resync itself needed a further base-sync) do not un-set an already-spent
# retry budget — _refresh_precondition_sha only ever sets _HEAD_SELF_SYNCED,
# it never touches _HEAD_RESYNC_USED. The second sync's eventual head-mismatch
# therefore reaches _head_moved_or_resync() with the budget still spent and is
# re-queued rather than silently granted a second retry (Judge's #8429 review:
# "if every base-sync now spends the single retry, two syncs in one run will
# re-queue where today's code would proceed — safe, but ... covered by a test").
MERGE_PRECONDITION_SHA="$APPROVED"
_HEAD_SELF_SYNCED=""
_HEAD_RESYNC_USED=true
_refresh_precondition_sha >/dev/null 2>&1
assert_eq "true" "$_HEAD_RESYNC_USED" "a second base-sync does not reset an already-spent retry budget"
_HEAD_RESYNC_USED=""

# ============================================================================
# Part 2: _head_moved_or_resync — the residual-window fix, and its fail-safety
# ============================================================================
echo ""
echo "Testing _head_moved_or_resync (one attributed retry, else exit 3)..."

RESPONSE_409='PR #8164: {"message":"Head branch was modified. Review and try the merge again.","status":"409"}'

# T5: attributed to our own sync -> return 0 (the caller retries) against the
# SHA the daemon reported, and the one-shot budget is now spent.
MERGE_PRECONDITION_SHA="$APPROVED"
_HEAD_SELF_SYNCED=true
_HEAD_RESYNC_USED=""
STUB_HEAD_SHA="$SYNCED"
T5_RC=0
LOOM_DAEMON_BIN="$STUB_RETRY" capture _head_moved_or_resync "$RESPONSE_409" || T5_RC=$?
T5_OUT="$CAPTURED"
assert_eq "0" "$T5_RC" "an attributed head move returns 0 so the caller retries (not exit 3)"
assert_eq "$SYNCED" "$MERGE_PRECONDITION_SHA" "the retry is re-gated on the freshly-read head"
assert_eq "true" "$_HEAD_RESYNC_USED" "the single retry is marked spent"
assert_contains "$T5_OUT" "#8164" "the retry announces itself"

# T6: the operands. The daemon cannot attribute anything without them, and a
# silently-missing --self-synced would turn every retry into a refusal.
assert_contains "$(cat "$STUB_DIR/argv-retry")" "--pr 8164" "the PR number is passed"
assert_contains "$(cat "$STUB_DIR/argv-retry")" "--repo rjwalters/loom" "the repository is passed"
assert_contains "$(cat "$STUB_DIR/argv-retry")" "--precondition-sha $APPROVED" "the pre-retry precondition SHA is passed"
assert_contains "$(cat "$STUB_DIR/argv-retry")" "--self-synced" "--self-synced is passed when this run pushed to the head"
assert_not_contains "$(cat "$STUB_DIR/argv-retry")" "--retry-used" "--retry-used is absent while the budget is unspent"
assert_not_contains "$(cat "$STUB_DIR/argv-retry")" "--mismatch-confirmed" "--mismatch-confirmed is absent for a text-classified mismatch"
assert_contains "$(cat "$STUB_DIR/stdin-retry")" "Head branch was modified" "the forge response is passed on stdin, not in argv"

# T7: a run that never synced must not claim the head move. The flag is the
# only thing separating "our own sync" from "a session pushed mid-merge".
MERGE_PRECONDITION_SHA="$APPROVED"
_HEAD_SELF_SYNCED=""
_HEAD_RESYNC_USED=""
rm -f "$STUB_DIR/argv-retry"
LOOM_DAEMON_BIN="$STUB_RETRY" run_resync_isolated "$RESPONSE_409"
assert_not_contains "$(cat "$STUB_DIR/argv-retry")" "--self-synced" "--self-synced is NOT passed when this run never pushed to the head"

# T8: the budget is one. Once spent, --retry-used says so and the daemon
# refuses — no unbounded chase of a moving head.
MERGE_PRECONDITION_SHA="$APPROVED"
_HEAD_SELF_SYNCED=true
_HEAD_RESYNC_USED=true
rm -f "$STUB_DIR/argv-retry"
LOOM_DAEMON_BIN="$STUB_RETRY" run_resync_isolated "$RESPONSE_409"
assert_contains "$(cat "$STUB_DIR/argv-retry")" "--retry-used" "--retry-used is passed once the single retry is spent"

# T9: the loop's own attempt budget also spends it — the retry `continue`s
# into an iteration that may not exist, and a retry nobody can make must not
# be authorized.
MERGE_PRECONDITION_SHA="$APPROVED"
_HEAD_SELF_SYNCED=true
_HEAD_RESYNC_USED=""
MERGE_ATTEMPT=3
rm -f "$STUB_DIR/argv-retry"
LOOM_DAEMON_BIN="$STUB_RETRY" run_resync_isolated "$RESPONSE_409"
assert_contains "$(cat "$STUB_DIR/argv-retry")" "--retry-used" "the last loop attempt is treated as a spent budget"
MERGE_ATTEMPT=1

# T10: the native caller's exit-code confirmation is threaded through.
MERGE_PRECONDITION_SHA="$APPROVED"
_HEAD_SELF_SYNCED=true
_HEAD_RESYNC_USED=""
rm -f "$STUB_DIR/argv-retry"
set +e
LOOM_DAEMON_BIN="$STUB_RETRY" _head_moved_or_resync "loom-daemon: head SHA precondition rejected" exit-code >/dev/null 2>&1
set -e
assert_contains "$(cat "$STUB_DIR/argv-retry")" "--mismatch-confirmed" "the native exit-4 caller passes --mismatch-confirmed"

# --- Every non-authorization lands on the pre-#8164 behaviour: exit 3 -------
#
# This is the fail-safety argument for the whole slice: the guard can only ADD
# merges that would otherwise have been re-queued. It can never remove a
# refusal, whatever goes wrong with the binary.
for case_name in foreign unknown silent wrongsentinel missing; do
    MERGE_PRECONDITION_SHA="$APPROVED"
    _HEAD_SELF_SYNCED=true
    _HEAD_RESYNC_USED=""
    STUB_HEAD_SHA="$FOREIGN"
    case "$case_name" in
        foreign)       stub="$STUB_FOREIGN";  label="a foreign head move (exit 1)" ;;
        unknown)       stub="$STUB_UNKNOWN";  label="an undeterminable attribution (exit 2)" ;;
        silent)        stub="$STUB_SILENT";   label="exit 0 with no output at all" ;;
        wrongsentinel) stub="$STUB_WRONG";    label="exit 0 with the wrong sentinel" ;;
        missing)       stub="$STUB_DIR/does-not-exist"; label="no loom-daemon binary at all" ;;
    esac
    LOOM_DAEMON_BIN="$stub" run_resync_isolated "$RESPONSE_409"
    assert_eq "3" "$LAST_RC" "$label -> exit 3 (re-queue, the pre-#8164 behaviour)"
done

# T11: the exit-3 diagnostic still names both SHAs (#5714's contract, which
# the wrapper must not have swallowed).
MERGE_PRECONDITION_SHA="$APPROVED"
_HEAD_SELF_SYNCED=true
_HEAD_RESYNC_USED=""
STUB_HEAD_SHA="$FOREIGN"
LOOM_DAEMON_BIN="$STUB_FOREIGN" run_resync_isolated "$RESPONSE_409"
assert_contains "$LAST_OUT" "$APPROVED" "the exit-3 diagnostic still names the stale (gated-on) SHA"
assert_contains "$LAST_OUT" "$FOREIGN" "the exit-3 diagnostic still names the current head SHA"
assert_contains "$LAST_OUT" "PR head moved" "the exit-3 diagnostic is still error_head_moved()'s"
assert_contains "$LAST_OUT" "first parent" "the daemon's refusal reason is surfaced to the operator"

# ============================================================================
# Part 3: source wiring — the fix must be reachable from both retry loops
# ============================================================================
echo ""
echo "Testing merge-pr.sh source wiring..."

# Both "Base branch was modified" branches must re-read the head AFTER the
# sync and BEFORE looping back to the merge call. Anchored on the loop headers
# so a branch that moved to another part of the script cannot pass by accident.
auto_loop_refresh=$(awk '
  /^  for MERGE_ATTEMPT in \$\(seq 1 \$MAX_MERGE_RETRIES\); do/ { infor=1 }
  infor && /forge_update_branch/                                { synced=1 }
  infor && synced && /_refresh_precondition_sha/                { print "ok"; exit }
  infor && synced && /^        continue$/                       { print "missing"; exit }
' "$MERGE_PR_SRC")
assert_eq "ok" "$auto_loop_refresh" "auto-merge retry loop re-reads the head after forge_update_branch, before continuing"

sync_loop_refresh=$(awk '
  /^for MERGE_ATTEMPT in \$\(seq 1 \$MAX_MERGE_RETRIES\); do/ { infor=1 }
  infor && /forge_update_branch/                              { synced=1 }
  infor && synced && /_refresh_precondition_sha/              { print "ok"; exit }
  infor && synced && /^      continue$/                       { print "missing"; exit }
' "$MERGE_PR_SRC")
assert_eq "ok" "$sync_loop_refresh" "synchronous retry loop re-reads the head after forge_update_branch, before continuing"

# All THREE head-mismatch sites route through the wrapper: the two text
# classified ones and the native `loom-daemon forge auto-merge` exit-4 one. A
# site left calling error_head_moved() directly would keep the #8164 bug on
# whichever path it is.
assert_eq "3" "$(grep -c '_head_moved_or_resync "\$' "$MERGE_PR_SRC")" "all three head-mismatch sites route through _head_moved_or_resync"

# The retry sites must `continue` — a retry that falls through would proceed
# past the merge call it was supposed to re-make.
assert_eq "3" "$(grep -c '_head_moved_or_resync "\$[A-Z_]*"\( exit-code\)\? && continue' "$MERGE_PR_SRC")" "each retry authorization continues the retry loop"

# ============================================================================
# Part 4: the REAL binary's offline contract (--from-stdin)
# ============================================================================
echo ""
echo "Testing the real loom-daemon's head-sync-retry contract..."

BASE_TIP="bbbbbbbb2222222222222222222222222222bbbb"

run_real() {
    local payload="$1"; shift
    set +e
    BIN_OUT="$(printf '%s' "$payload" | "$REAL_DAEMON_BIN" merge-pr head-sync-retry \
        --pr 8164 --repo rjwalters/loom --precondition-sha "$APPROVED" --from-stdin "$@" 2>&1)"
    BIN_RC=$?
    set -e
}

# The incident's own shape: head = merge(approved head, base tip).
INCIDENT="{\"response\":\"Head branch was modified. Review and try the merge again.\",\"current_head_sha\":\"$SYNCED\",\"head_parents\":[\"$APPROVED\",\"$BASE_TIP\"],\"second_parent_in_base\":true}"
run_real "$INCIDENT" --self-synced
assert_eq "0" "$BIN_RC" "real binary: the #8164 incident shape authorizes the retry (exit 0)"
assert_contains "$BIN_OUT" "LOOM-HEAD-SELF-SYNC-RETRY $SYNCED" "real binary: the authorization carries the head to merge next"

# Same evidence, but we never synced: nothing to attribute it to.
run_real "$INCIDENT"
assert_eq "1" "$BIN_RC" "real binary: identical evidence without --self-synced is refused"

# Same evidence, budget spent.
run_real "$INCIDENT" --self-synced --retry-used
assert_eq "1" "$BIN_RC" "real binary: the retry budget is one"

# A session pushed a commit on top of the approved head: the #5579 case, which
# must stay a hard stop — this is the assertion that keeps the fix from
# becoming "retry on any 409".
FOREIGN_PUSH="{\"response\":\"Head branch was modified.\",\"current_head_sha\":\"$FOREIGN\",\"head_parents\":[\"$APPROVED\"]}"
run_real "$FOREIGN_PUSH" --self-synced
assert_eq "1" "$BIN_RC" "real binary: a commit pushed on top of the approved head is refused"

# A two-parent merge whose second parent is NOT in the base: the shape an
# attacker would need, carrying unreviewed content behind a sync-looking
# commit. Attribution is structural, so it is refused.
NOT_BASE="{\"response\":\"Head branch was modified.\",\"current_head_sha\":\"$SYNCED\",\"head_parents\":[\"$APPROVED\",\"$FOREIGN\"],\"second_parent_in_base\":false}"
run_real "$NOT_BASE" --self-synced
assert_eq "1" "$BIN_RC" "real binary: a merge of something other than the base is refused"

# Containment undeterminable (a compare lookup failed): refused, not assumed.
UNKNOWN_BASE="{\"response\":\"Head branch was modified.\",\"current_head_sha\":\"$SYNCED\",\"head_parents\":[\"$APPROVED\",\"$BASE_TIP\"]}"
run_real "$UNKNOWN_BASE" --self-synced
assert_eq "1" "$BIN_RC" "real binary: an undetermined second parent is refused, never assumed"

# A refusal that is not a head mismatch at all.
BASE_MODIFIED="{\"response\":\"Base branch was modified. Review and try the merge again.\",\"current_head_sha\":\"$SYNCED\",\"head_parents\":[\"$APPROVED\",\"$BASE_TIP\"],\"second_parent_in_base\":true}"
run_real "$BASE_MODIFIED" --self-synced
assert_eq "1" "$BIN_RC" "real binary: 'Base branch was modified' is not a head mismatch and is not retried"

# ...unless the caller establishes it by exit code, as the native path does.
run_real "$BASE_MODIFIED" --self-synced --mismatch-confirmed
assert_eq "0" "$BIN_RC" "real binary: --mismatch-confirmed substitutes for the response text"

# Malformed evidence: an answer that cannot be computed is exit 2, and the
# caller treats 2 exactly like 1 (re-queue).
run_real "not json at all" --self-synced
assert_eq "2" "$BIN_RC" "real binary: unparseable evidence is exit 2 (could not determine)"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
